"""HTTP-level tests for FA-A1: upfront voice-source input on /upload.

The voice-source question (auto TTS vs own audio) is now taken on the upload
form itself, so the choice is persisted the moment the upload succeeds — the
full-auto chain never depends on a later /voiceover/{job_id}/choose click.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import key_store, render_final, subtitle_extract, translator, video_ingest, voiceover_auto


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


class UpfrontVoiceSourceTest(unittest.TestCase):
    """FA-A1: /upload persists the voice-source choice synchronously."""

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
                ],
            ),
            mock.patch.object(
                translator, "_call_gemini_text", return_value="नमस्ते"
            ),
            mock.patch.object(
                voiceover_auto, "_call_tts", return_value=self.silence_bytes
            ),
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

    def _upload(self, voice_source=None):
        data = {}
        if voice_source is not None:
            data["voice_source"] = voice_source
        return self.client.post(
            "/upload",
            data=data,
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )

    def test_voice_source_user_upload_persisted_immediately(self):
        # Uploading with voice_source="user_upload" must persist
        # voice_source_choice.json synchronously — before the upload_pipeline
        # background thread finishes (later groups read this at once).
        self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        res = self._upload(voice_source="user_upload")
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        choice = self.upload_root / job_id / "voice_source_choice.json"
        self.assertTrue(choice.exists(), "voice_source_choice.json written upfront")
        data = json.loads(choice.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "user_upload")

    def test_default_voice_source_is_auto_tts(self):
        # No voice_source field -> the "auto_tts" default is persisted.
        self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        res = self._upload()
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        choice = self.upload_root / job_id / "voice_source_choice.json"
        self.assertTrue(choice.exists())
        data = json.loads(choice.read_text(encoding="utf-8"))
        self.assertEqual(data["mode"], "auto_tts")

    def test_invalid_voice_source_rejected_400(self):
        self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        res = self._upload(voice_source="garbage")
        self.assertEqual(res.status_code, 400)
        self.assertIn("invalid voice source", res.json()["detail"])


if __name__ == "__main__":
    unittest.main()
