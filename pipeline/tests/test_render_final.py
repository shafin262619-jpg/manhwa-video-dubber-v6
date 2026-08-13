"""Tests for pipeline.render_final (F3 final render + download).

The ffmpeg normalize step is mocked: we assert the exact command (codecs,
+faststart, in/out paths) and that finalize produces the output file, then
that the /final and /download endpoints serve it with the expected links.
"""

import json
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import auto_cut, render_final, video_ingest


def _ok_result(stdout=""):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


class RenderFinalBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-f3"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _write_draft(self, content=b"fake-draft"):
        (self.job_dir / "draft_final_video.mp4").write_bytes(content)

    def _run_finalize(self):
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                return _ok_result(
                    json.dumps({"format": {"duration": "14.2"}, "streams": []})
                )
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"final")
            return _ok_result()

        with mock.patch.object(auto_cut, "_run", side_effect=fake_run):
            result = render_final.finalize_video(
                self.job_id,
                upload_root=self.upload_root,
                output_root=self.output_root,
            )
        return result, calls


class FinalizeTest(RenderFinalBase):
    def test_finalize_writes_normalized_output(self):
        self._write_draft()
        result, calls = self._run_finalize()

        expected = self.output_root / self.job_id / "final_video.mp4"
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["final_path"], str(expected))
        self.assertEqual(result["draft_path"], str(self.job_dir / "draft_final_video.mp4"))
        self.assertAlmostEqual(result["duration_sec"], 14.2, places=3)
        self.assertTrue(expected.exists())

        ffmpeg_cmds = [c for c in calls if c[0] == "ffmpeg"]
        self.assertEqual(len(ffmpeg_cmds), 1)
        cmd = ffmpeg_cmds[0]
        self.assertIn("-i", cmd)
        self.assertTrue(any(arg.endswith("draft_final_video.mp4") for arg in cmd))
        self.assertTrue(any(arg.endswith("final_video.mp4") for arg in cmd))
        self.assertTrue(any(arg == auto_cut.config.RENDER_VIDEO_CODEC for arg in cmd))
        self.assertTrue(any(arg == auto_cut.config.RENDER_AUDIO_CODEC for arg in cmd))
        self.assertIn("+faststart", cmd)

    def test_missing_draft_raises(self):
        with self.assertRaises(FileNotFoundError):
            render_final.finalize_video(
                self.job_id,
                upload_root=self.upload_root,
                output_root=self.output_root,
            )

    def test_missing_job_raises(self):
        with self.assertRaises(FileNotFoundError):
            render_final.finalize_video(
                "nope",
                upload_root=self.upload_root,
                output_root=self.output_root,
            )

    def test_ffmpeg_failure_raises(self):
        self._write_draft()
        with mock.patch.object(
            auto_cut, "_run", side_effect=RuntimeError("ffmpeg/ffprobe error: boom")
        ):
            with self.assertRaises(RuntimeError):
                render_final.finalize_video(
                    self.job_id,
                    upload_root=self.upload_root,
                    output_root=self.output_root,
                )

    def test_empty_output_raises(self):
        self._write_draft()

        def fake_run(cmd, timeout=None):
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"")
            return _ok_result()

        with mock.patch.object(auto_cut, "_run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                render_final.finalize_video(
                    self.job_id,
                    upload_root=self.upload_root,
                    output_root=self.output_root,
                )


class RenderFinalEndpointTest(RenderFinalBase):
    def setUp(self):
        super().setUp()
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _mock_run(self, calls):
        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                return _ok_result(
                    json.dumps({"format": {"duration": "14.2"}, "streams": []})
                )
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"final")
            return _ok_result()

        return fake_run

    def test_final_page_renders_video_download_and_back_to_review(self):
        self._write_draft()
        calls = []
        # U1c: the first GET returns the intermediate processing page and
        # starts a background thread; the done page (identical markup to
        # pre-U1c) is served on a later GET once the render finishes.
        with mock.patch.object(auto_cut, "_run", side_effect=self._mock_run(calls)):
            first = self.client.get(f"/final/{self.job_id}")
            self.assertEqual(first.status_code, 200)
            self.assertIn("Processing", first.text)
            self._wait_for_final_done()
            res = self.client.get(f"/final/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'<video controls src="/download/{self.job_id}">', res.text)
        self.assertIn(f'href="/download/{self.job_id}"', res.text)
        self.assertIn(f'href="/review/{self.job_id}"', res.text)
        self.assertTrue((self.output_root / self.job_id / "final_video.mp4").exists())

    def _wait_for_final_done(self, timeout=10.0, interval=0.1):
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get(f"/api/jobs/{self.job_id}/status")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            stage_info = (body.get("stages") or {}).get("final_render")
            if stage_info and stage_info.get("state") == "done":
                return body
            if stage_info and stage_info.get("state") == "error":
                self.fail(f"final_render errored: {stage_info}")
            time.sleep(interval)
        self.fail(f"final_render for {self.job_id} did not finish in {timeout}s")

    def test_final_page_unknown_job_404(self):
        res = self.client.get("/final/missing-job")
        self.assertEqual(res.status_code, 404)

    def test_download_serves_final_file(self):
        final = self.output_root / self.job_id / "final_video.mp4"
        final.parent.mkdir(parents=True)
        final.write_bytes(b"final-content")
        res = self.client.get(f"/download/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, b"final-content")
        self.assertEqual(res.headers["content-type"], "video/mp4")

    def test_download_missing_final_404(self):
        res = self.client.get(f"/download/{self.job_id}")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
