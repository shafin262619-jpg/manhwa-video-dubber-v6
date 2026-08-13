"""Permanent end-to-end regression tests for the full-auto paths (FA-E2).

Two HTTP-only permanent tests prove the PRD's core claim: uploading a video
requires *zero* further clicks on the ``auto_tts`` path and exactly *one*
pause (the audio upload) on the ``user_upload`` path, before the final video
is downloadable.

They complement (never replace) the old manual-flow regression test
``test_app_orchestration.py`` (G1), which stays untouched and keeps passing
in parallel as the backward-compat proof.

Both tests drive the app purely through HTTP endpoints (``TestClient``) with
Gemini mocked and ffmpeg mocked; the D2 TTS clips are real ffmpeg silence
placeholders (same deterministic pattern as the G1 orchestration test) so
there is no real network anywhere. A recording wrapper tracks every request
so the "single-pause" claim can be asserted from the actual endpoint calls:
the old manual route pages (``/voiceover/{job_id}/choose``,
``/voiceover/{job_id}/align_uploaded``, ``/final/{job_id}``) must NOT be
hit on either path.
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
    key_store,
    render_final,
    subtitle_extract,
    translator,
    video_ingest,
    voiceover_auto,
    voiceover_upload,
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


class RecordingClient:
    """Thin wrapper that records every (method, path) request made."""

    def __init__(self, client):
        self._client = client
        self.requests = []

    def get(self, url, **kwargs):
        self.requests.append(("GET", url.split("?")[0]))
        return self._client.get(url, **kwargs)

    def post(self, url, **kwargs):
        self.requests.append(("POST", url.split("?")[0]))
        return self._client.post(url, **kwargs)


class FullAutoOrchestrationTest(unittest.TestCase):
    """FA-E2: the two permanent end-to-end regression tests."""

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

        self.client = RecordingClient(TestClient(app))
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
            mock.patch.object(
                voiceover_upload,
                "_gemini_align",
                return_value=[
                    {
                        "serial": 1, "start_sec": 0.0, "end_sec": 1.0,
                        "alignment_fallback": False, "alignment_source": "gemini",
                    },
                    {
                        "serial": 2, "start_sec": 1.0, "end_sec": 2.0,
                        "alignment_fallback": False, "alignment_source": "gemini",
                    },
                ],
            ),
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
                "format": {"duration": "2.0"},
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
        """Poll GET /api/jobs/{job_id}/status until the given stage is done.

        Records the first-seen order of stages so the test can assert the
        stage-through (e.g. upload_pipeline -> auto_full_render).
        """
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.client.get(f"/api/jobs/{job_id}/status").json()
            for name in last.get("stages", {}):
                if name not in self.stage_order:
                    self.stage_order.append(name)
            entry = last.get("stages", {}).get(stage, {})
            if entry.get("state") == "done":
                return last
            if entry.get("state") == "error":
                self.fail(f"stage {stage} errored: {last}")
            time.sleep(interval)
        self.fail(f"stage {stage} not done within {timeout}s; last={last}")

    def _final_path(self, job_id):
        return self.output_root / job_id / "final_video.mp4"

    def test_auto_tts_zero_click_end_to_end(self):
        # POST /upload (auto_tts) -> poll the status endpoint ONLY -> the final
        # video becomes downloadable. No /choose, /align_uploaded or /final
        # page is ever requested.
        self.stage_order = []
        res = self.client.post(
            "/upload",
            data={"voice_source": "auto_tts"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        status = self._wait_stage(job_id, "auto_full_render")
        self.assertIn("upload_pipeline", status["stages"])
        self.assertIn("auto_full_render", status["stages"])
        self.assertEqual(
            self.stage_order,
            ["upload_pipeline", "auto_full_render"],
            "stage-through must be upload_pipeline then auto_full_render",
        )
        self.assertTrue(self._final_path(job_id).exists())

        dl = self.client.get(f"/download/{job_id}")
        self.assertEqual(dl.status_code, 200)
        self.assertGreater(len(dl.content), 0)

        forbidden = (
            f"/voiceover/{job_id}/choose",
            f"/voiceover/{job_id}/align_uploaded",
            f"/final/{job_id}",
        )
        for path in forbidden:
            self.assertNotIn(
                ("GET", path), self.client.requests,
                f"{path} must not be called on the zero-click auto path",
            )

    def test_user_upload_single_pause_end_to_end(self):
        # POST /upload (user_upload) -> stops at upload_pipeline done (does
        # NOT auto-continue) -> POST the audio -> user_audio_pipeline done ->
        # final video downloadable. The three old manual pages must never be
        # requested — the only pause is the audio upload itself.
        self.stage_order = []
        res = self.client.post(
            "/upload",
            data={"voice_source": "user_upload"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        status = self._wait_stage(job_id, "upload_pipeline")
        self.assertNotIn(
            "auto_full_render", status.get("stages", {}),
            "user_upload must stop at upload_pipeline done",
        )
        self.assertFalse(
            self._final_path(job_id).exists(),
            "no final video before the audio is uploaded",
        )

        res = self.client.post(
            f"/voiceover/{job_id}/upload",
            files={"audio": ("voice.wav", self.silence_bytes, "audio/wav")},
        )
        self.assertEqual(res.status_code, 200, res.text)

        status = self._wait_stage(job_id, "user_audio_pipeline")
        self.assertIn("user_audio_pipeline", status["stages"])
        self.assertTrue(self._final_path(job_id).exists())

        dl = self.client.get(f"/download/{job_id}")
        self.assertEqual(dl.status_code, 200)
        self.assertGreater(len(dl.content), 0)

        forbidden = (
            f"/voiceover/{job_id}/choose",
            f"/voiceover/{job_id}/align_uploaded",
            f"/final/{job_id}",
        )
        for path in forbidden:
            self.assertNotIn(
                ("GET", path), self.client.requests,
                f"{path} must not be called on the single-pause upload path",
            )


if __name__ == "__main__":
    unittest.main()
