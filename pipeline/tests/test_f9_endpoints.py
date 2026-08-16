"""App-level tests for the F9 endpoints (409 confirm flow, history, resume).

Drives the app through HTTP (TestClient) with Gemini/TTS/ffmpeg mocked exactly
like ``test_full_auto_orchestration``. Covers:

- POST /upload returns 409 + needs_confirm when the history cap (3) is full,
  and never starts the new job's pipeline until confirmed (two-step flow).
- POST /jobs/{job_id}/confirm-start evicts the oldest job (deleting its files
  when asked), registers the pending job and starts its pipeline.
- GET /history lists the recent jobs with live metadata.
- POST /jobs/{job_id}/resume returns 409 for complete jobs and starts the
  resume thread for interrupted ones.
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
    history_store,
    job_config,
    key_store,
    render_final,
    subtitle_extract,
    translator,
    video_ingest,
    voiceover_auto,
)


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _ok_result(stdout=""):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


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


class F9EndpointsTest(unittest.TestCase):
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
        self.client.post("/settings/keys", data={"key": "test-gemini-key"})

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
                "format": {"duration": "5.0"},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        return {"format": {}, "streams": []}

    def _fake_auto_run(self, cmd, timeout=None):
        if cmd and cmd[0] == "ffprobe":
            return _ok_result(json.dumps(self._probe_for(cmd[-1])))
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"faked-video-bytes")
        return _ok_result()

    def _wait_stage(self, job_id, stage, timeout=20.0, interval=0.1):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.client.get(f"/api/jobs/{job_id}/status").json()
            entry = last.get("stages", {}).get(stage, {})
            if entry.get("state") == "done":
                return last
            if entry.get("state") == "error":
                self.fail(f"stage {stage} errored: {last}")
            time.sleep(interval)
        self.fail(f"stage {stage} not done within {timeout}s; last={last}")

    def _fill_history(self, n):
        """Create + register n jobs directly (oldest first)."""
        for i in range(n):
            job_id = f"job-{i}"
            job_dir = self.upload_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job_meta.json").write_text(
                json.dumps({"source_filename": f"video-{i}.mp4"}), encoding="utf-8"
            )
            history_store.register_job(
                job_id, meta={"target_video_name": f"video-{i}.mp4"}
            )

    def _upload(self, filename="sample.mp4"):
        return self.client.post(
            "/upload",
            data={"voice_source": "auto_tts"},
            files={"file": (filename, self.video_bytes, "video/mp4")},
        )


class UploadConfirmFlowTest(F9EndpointsTest):
    def test_fourth_upload_returns_409_and_does_not_start(self):
        self._fill_history(3)
        res = self._upload()
        self.assertEqual(res.status_code, 409, res.text)
        body = res.json()["detail"]
        self.assertTrue(body["needs_confirm"])
        self.assertEqual(body["would_evict"], "job-0")
        self.assertTrue(body["delete_files"])
        new_id = body["job_id"]

        # Files were saved (source + per-job config) but the pipeline never
        # started: no upload_pipeline status exists for the pending job.
        self.assertTrue((self.upload_root / new_id / "source.mp4").exists())
        cfg = job_config.read_config(new_id)
        self.assertIsNotNone(cfg)
        self.assertEqual(cfg["engine"], job_config.default_engine())
        status = self.client.get(f"/api/jobs/{new_id}/status").json()
        self.assertEqual(status["state"], "not_started")

        # No silent eviction: history still holds exactly the 3 old jobs.
        hist = self.client.get("/api/history").json()["history"]
        self.assertEqual([e["job_id"] for e in hist], ["job-2", "job-1", "job-0"])
        self.assertTrue((self.upload_root / "job-0").is_dir())

    def test_confirm_start_evicts_oldest_then_runs(self):
        self._fill_history(3)
        res = self._upload()
        body = res.json()["detail"]
        new_id = body["job_id"]

        confirm = self.client.post(
            f"/jobs/{new_id}/confirm-start",
            params={"evict_job_id": "job-0", "delete_files": True},
        )
        self.assertEqual(confirm.status_code, 200, confirm.text)
        self.assertEqual(confirm.json()["status"], "processing")

        # Oldest job's files were deleted and the new job is in history.
        self.assertFalse((self.upload_root / "job-0").exists())
        hist = self.client.get("/api/history").json()["history"]
        self.assertEqual([e["job_id"] for e in hist], [new_id, "job-2", "job-1"])

        # The pipeline actually started: it completes down to the final video.
        self._wait_stage(new_id, "auto_full_render")
        self.assertTrue(
            (self.output_root / new_id / "final_video.mp4").exists()
        )

    def test_confirm_start_keep_files_option(self):
        self._fill_history(3)
        new_id = self._upload().json()["detail"]["job_id"]
        confirm = self.client.post(
            f"/jobs/{new_id}/confirm-start",
            params={"evict_job_id": "job-0", "delete_files": False},
        )
        self.assertEqual(confirm.status_code, 200, confirm.text)
        # Oldest job dropped from history but its files are kept on disk.
        self.assertTrue((self.upload_root / "job-0").is_dir())
        hist = self.client.get("/api/history").json()["history"]
        self.assertNotIn("job-0", [e["job_id"] for e in hist])
        # Drain the background pipeline within this test so no thread leaks
        # into the next test after the mocks are torn down.
        self._wait_stage(new_id, "auto_full_render")

    def test_confirm_start_unknown_job_404(self):
        res = self.client.post(
            "/jobs/no-such/confirm-start", params={"evict_job_id": "job-0"}
        )
        self.assertEqual(res.status_code, 404)

    def test_upload_rejects_invalid_engine(self):
        self._fill_history(0)
        res = self.client.post(
            "/upload",
            data={"voice_source": "auto_tts", "engine": "bogus_engine"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("invalid engine", res.text)


class HistoryEndpointTest(F9EndpointsTest):
    def test_history_lists_newest_first_with_metadata(self):
        for i, job_id in enumerate(("job-a", "job-b")):
            job_dir = self.upload_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            job_config.write_config(
                job_id, engine="gemini_only", target_lang="hi",
                voice_source="auto_tts",
            )
            history_store.register_job(
                job_id, meta={"target_video_name": f"{job_id}.mp4"}
            )
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["limit"], 3)
        entries = body["history"]
        self.assertEqual([e["job_id"] for e in entries], ["job-b", "job-a"])
        self.assertEqual(entries[0]["target_video_name"], "job-b.mp4")
        self.assertEqual(entries[0]["engine"], "gemini_only")
        self.assertEqual(entries[0]["voice_source"], "auto_tts")

    def test_history_empty(self):
        body = self.client.get("/api/history").json()
        self.assertEqual(body["history"], [])
        self.assertEqual(body["limit"], 3)


class ResumeEndpointTest(F9EndpointsTest):
    def _make_interrupted_job(self):
        """A job past upload (subtitles done) whose chain never ran."""
        job_dir = self.upload_root / "job-resume"
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": "job-resume", "duration_sec": 5.0}),
            encoding="utf-8",
        )
        (job_dir / "source.mp4").write_bytes(b"fake-source")
        (job_dir / "voiceover_hi.wav").write_bytes(self.silence_bytes)
        (job_dir / "subtitles_hi.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते"},
                    {"serial": 2, "text_zh": "B", "text_hi": "अलविदा"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (job_dir / "subtitles_zh.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "text_zh": "A", "start_sec": 0.0, "end_sec": 1.5},
                    {"serial": 2, "text_zh": "B", "start_sec": 2.0, "end_sec": 3.5},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (job_dir / "voice_source_choice.json").write_text(
            json.dumps({"job_id": "job-resume", "mode": "auto_tts"}), encoding="utf-8"
        )
        return "job-resume"

    def _make_complete_job(self):
        job_id = "job-done"
        job_dir = self.upload_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "source.mp4").write_bytes(b"fake-source")
        (job_dir / "voiceover_hi.wav").write_bytes(self.silence_bytes)
        (job_dir / "subtitles_hi.json").write_text("[]", encoding="utf-8")
        (job_dir / "voice_source_choice.json").write_text(
            json.dumps({"job_id": job_id, "mode": "auto_tts"}), encoding="utf-8"
        )
        for name in (
            "timestamps_hi_auto.json",
            "timestamps_hi_final.json",
            "edit_guideline.json",
        ):
            (job_dir / name).write_text("[]", encoding="utf-8")
        (job_dir / "draft_final_video.mp4").write_bytes(b"draft")
        out = self.output_root / job_id
        out.mkdir(parents=True, exist_ok=True)
        (out / "final_video.mp4").write_bytes(b"final")
        return job_id

    def test_resume_complete_job_returns_409(self):
        job_id = self._make_complete_job()
        res = self.client.post(f"/jobs/{job_id}/resume")
        self.assertEqual(res.status_code, 409)
        self.assertIn("nothing to resume", res.text)

    def test_resume_before_upload_returns_409(self):
        job_dir = self.upload_root / "job-early"
        job_dir.mkdir(parents=True, exist_ok=True)
        res = self.client.post("/jobs/job-early/resume")
        self.assertEqual(res.status_code, 409)
        self.assertIn("not finished", res.text)

    def test_resume_interrupted_job_runs_to_completion(self):
        job_id = self._make_interrupted_job()
        res = self.client.post(f"/jobs/{job_id}/resume")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["resume_point"], "D2_voiceover")
        self.assertEqual(body["status"], "processing")

        self._wait_stage(job_id, "resume")
        self.assertTrue(
            (self.output_root / job_id / "final_video.mp4").exists()
        )
        status = self.client.get(f"/api/jobs/{job_id}/status").json()
        self.assertEqual(status["stages"]["resume"]["state"], "done")


if __name__ == "__main__":
    unittest.main()
