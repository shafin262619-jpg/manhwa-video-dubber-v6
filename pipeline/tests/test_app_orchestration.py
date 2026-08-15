"""HTTP-level end-to-end wiring regression test (G1).

Every pipeline module (S1-F3) is unit-tested in isolation, but that alone never
catches the bug where ``app.py`` endpoints fail to call them in the right
order — a user driving the real app could never get past the first stage. This
test therefore drives the app **purely through the HTTP endpoints**
(``fastapi.testclient.TestClient``), never by calling pipeline modules
directly, and asserts that each step produces the file the next step consumes.

Chain under test:

1. ``POST /settings/keys``                     -> add a Gemini key
2. ``POST /upload``                            -> B1+B2+C1 (subtitles_zh/hi)
3. ``POST /voiceover/{job_id}/choose``         -> D2+D4+E1+E2 (draft video)
4. ``GET  /review/{job_id}``                   -> review page has per-clip data
5. ``POST /review/{job_id}/edit``              -> F2 partial re-render
6. ``GET  /final/{job_id}``                    -> F3 final render
7. ``GET  /download/{job_id}``                 -> final file served

Gemini calls are mocked; ``auto_cut``/``render_final`` ffmpeg is mocked; the
D2 TTS clips are real ffmpeg silence placeholders so the voiceover is
deterministic (no real network anywhere).
"""

import json
import shutil
import subprocess
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import (
    auto_cut,
    job_status as job_status_store,
    key_store,
    render_final,
    subtitle_extract,
    translator,
    video_ingest,
    voiceover_auto,
)


def _ok_result(stdout=""):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_sample_video(path):
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", "color=c=black:s=320x240:d=5",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )


class HttpWiringChainTest(unittest.TestCase):
    """One test per wiring break-point + the full happy-path chain."""

    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.key_store_path = Path(self._tmp) / "gemini_keys_store.json"
        self.output_root = Path(self._tmp) / "outputs"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_key_store = key_store.KEY_STORE_PATH
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        key_store.KEY_STORE_PATH = self.key_store_path
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore_paths)

        video_path = Path(self._tmp) / "sample.mp4"
        _make_sample_video(video_path)
        self.video_bytes = video_path.read_bytes()

        silence_path = Path(self._tmp) / "silence.wav"
        voiceover_auto._make_silence(1.0, silence_path)
        self.silence_bytes = silence_path.read_bytes()

        self.client = TestClient(app)
        self.calls = []
        self._mocks = [
            mock.patch.object(
                subtitle_extract,
                "_call_gemini",
                return_value=[
                    {"text": "你好", "start_sec": 0.0, "end_sec": 1.5},
                    {"text": "再见", "start_sec": 2.0, "end_sec": 3.5},
                ],
            ),
            mock.patch.object(
                translator, "_call_gemini_text", return_value="नमस्ते\nअलविदा"
            ),
            mock.patch.object(
                voiceover_auto, "_call_tts", return_value=self.silence_bytes
            ),
            mock.patch.object(auto_cut, "_run", side_effect=self._fake_auto_run),
        ]
        for patch in self._mocks:
            patch.start()
        self.addCleanup(self._stop_mocks)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        key_store.KEY_STORE_PATH = self._orig_key_store
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _stop_mocks(self):
        for patch in self._mocks:
            patch.stop()

    def _probe_for(self, path):
        name = Path(path).name
        if name == "voiceover_hi.wav":
            return {"format": {"duration": "2.0"}, "streams": []}
        if name == "source.mp4":
            return {
                "format": {"duration": "10.0"},
                "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
            }
        if name in ("draft_final_video.mp4", "final_video.mp4"):
            return {
                "format": {"duration": "2.0"},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        return {"format": {}, "streams": []}

    def _fake_auto_run(self, cmd, timeout=None):
        self.calls.append(cmd)
        if cmd and cmd[0] == "ffprobe":
            return _ok_result(json.dumps(self._probe_for(cmd[-1])))
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"out")
        return _ok_result()

    def _wait_for_upload_done(self, job_id, timeout=10.0, interval=0.1):
        """Poll /api/jobs/{job_id}/status until the upload pipeline finishes.

        The pipeline runs in a background thread since U1b, so the /upload
        response no longer carries the "pipeline" key; the summary is read
        from the status file instead. Gemini calls are mocked, so this
        converges quickly.
        """
        return self._wait_for_stage_done(
            job_id, "upload_pipeline", timeout=timeout, interval=interval
        )

    def _wait_for_stage_done(self, job_id, stage, timeout=10.0, interval=0.1):
        """Poll /api/jobs/{job_id}/status until the given stage is done."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            res = self.client.get(f"/api/jobs/{job_id}/status")
            self.assertEqual(res.status_code, 200)
            body = res.json()
            stage_info = (body.get("stages") or {}).get(stage)
            if stage_info and stage_info.get("state") == "done":
                return body
            if stage_info and stage_info.get("state") == "error":
                self.fail(f"stage {stage} errored: {body}")
            time.sleep(interval)
        self.fail(f"stage {stage} for {job_id} did not finish in {timeout}s")

    def test_full_http_chain(self):
        # 1. Add a Gemini key.
        res = self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(key_store.get_active_keys(), ["test-gemini-key"])

        # 2. Upload -> responds immediately with "processing"; the B1 (extract)
        #    -> B2 (serialize) -> C1 (translate) chain now runs in a
        #    background thread. Wait for it, then verify the summary from the
        #    status file instead of the response body.
        res = self.client.post(
            "/upload",
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        job_id = body["job_id"]
        self.assertTrue(job_id)
        self.assertEqual(body["status"], "processing")
        self.assertNotIn("pipeline", body)

        status = self._wait_for_upload_done(job_id)
        upload_stage = status["stages"]["upload_pipeline"]
        self.assertEqual(upload_stage["extraction_status"], "ok")
        self.assertEqual(upload_stage["serials"], 2)
        job_dir = self.upload_root / job_id
        for name in ("subtitles_zh_raw.json", "subtitles_zh.json", "subtitles_hi.json"):
            self.assertTrue((job_dir / name).exists(), f"missing {name} after upload")

        # 3. Choose auto-TTS -> must trigger D2 -> D4 -> E1 -> E2 down to the
        #    draft video, so the review phase has data to load.
        res = self.client.post(f"/voiceover/{job_id}/choose", data={"mode": "auto_tts"})
        self.assertEqual(res.status_code, 200, res.text)
        for name in (
            "voice_source_choice.json",
            "voiceover_hi.wav",
            "timestamps_hi_auto.json",
            "timestamps_hi_final.json",
            "edit_guideline.json",
            "draft_final_video.mp4",
        ):
            self.assertTrue((job_dir / name).exists(), f"missing {name} after choose")

        # 4. Review page loads the per-clip data (E1+E2 already done).
        res = self.client.get(f"/review/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(f'src="/review/{job_id}/clip/1"', res.text)
        self.assertIn(f'src="/review/{job_id}/clip/2"', res.text)
        self.assertIn("नमस्ते", res.text)
        self.assertIn("Target duration: 1.0s", res.text)

        # 5. Review edit (F2) applies and the guideline updates that serial only.
        res = self.client.post(
            f"/review/{job_id}/edit",
            data={"serial": "1", "new_source_start": "0.0", "new_source_end": "0.75"},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(f'href="/review/{job_id}"', res.text)
        guideline = json.loads(
            (job_dir / "edit_guideline.json").read_text(encoding="utf-8")
        )
        edited = next(e for e in guideline if e["serial"] == 1)
        other = next(e for e in guideline if e["serial"] == 2)
        self.assertAlmostEqual(edited["source_start_sec"], 0.0, places=3)
        self.assertAlmostEqual(edited["source_end_sec"], 0.75, places=3)
        self.assertAlmostEqual(edited["pts_multiplier"], round(1.0 / 0.75, 4), places=4)
        self.assertFalse(edited["flagged"])
        self.assertAlmostEqual(other["source_start_sec"], 2.0, places=3)
        self.assertAlmostEqual(other["source_end_sec"], 3.5, places=3)

        # 6. Final render (F3) normalizes the draft into outputs/<job_id>.
        #    U1c: the first GET shows the intermediate processing page and
        #    starts a background thread; the done page is served on a later GET
        #    once the render finishes.
        res = self.client.get(f"/final/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("Processing", res.text)
        self._wait_for_stage_done(job_id, "final_render")
        self.assertTrue((self.output_root / job_id / "final_video.mp4").exists())
        res = self.client.get(f"/final/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(f'src="/download/{job_id}"', res.text)
        self.assertIn(f'href="/download/{job_id}"', res.text)
        self.assertIn(f'href="/review/{job_id}"', res.text)
        self.assertTrue((self.output_root / job_id / "final_video.mp4").exists())

        # 7. Download serves the final file.
        res = self.client.get(f"/download/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(res.headers["content-type"], "video/mp4")
        self.assertEqual(res.content, b"out")

    def test_upload_without_key_still_blocked(self):
        res = self.client.post(
            "/upload",
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("Gemini API key", res.json()["detail"])

    def test_review_before_voiceover_404(self):
        # A job that stopped before the voiceover phase has no edit_guideline,
        # so the review page must say so (and not crash).
        self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        res = self.client.post(
            "/upload",
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        job_id = res.json()["job_id"]
        self._wait_for_upload_done(job_id)
        res = self.client.get(f"/review/{job_id}")
        self.assertEqual(res.status_code, 404)

    def test_upload_pipeline_resume_skips_build_subtitle_list(self):
        # B4: when subtitles_hi.json already exists, _run_upload_pipeline()
        # takes the idempotent resume path and must NOT call
        # build_subtitle_list() (B3 wiring stays off that path).
        job_dir = self.upload_root / "resume-job"
        job_dir.mkdir(parents=True)
        (job_dir / "subtitles_zh_raw.json").write_text(
            json.dumps(
                {"status": "ok", "segments_count": 1, "failed_segments": [],
                 "subtitles": []}
            ),
            encoding="utf-8",
        )
        (job_dir / "subtitles_hi.json").write_text(
            json.dumps([{"serial": 1, "text_hi": "नमस्ते"}]), encoding="utf-8"
        )
        with mock.patch(
            "pipeline.subtitle_builder.build_subtitle_list"
        ) as build, mock.patch(
            "pipeline.subtitle_extract.extract_subtitles"
        ) as extract, mock.patch(
            "pipeline.translator.translate_subtitles"
        ) as translate:
            import app as app_module

            app_module._run_upload_pipeline("resume-job")
        build.assert_not_called()
        extract.assert_not_called()
        translate.assert_not_called()
        status = job_status_store.read_status("resume-job")
        stage = status["stages"]["upload_pipeline"]
        self.assertEqual(stage["state"], "done")
        self.assertEqual(stage["serials"], 1)


if __name__ == "__main__":
    unittest.main()
