"""HTTP-level tests for FA-A1: upfront voice-source input on /upload.

The voice-source question (auto TTS vs own audio) is now taken on the upload
form itself, so the choice is persisted the moment the upload succeeds — the
full-auto chain never depends on a later /voiceover/{job_id}/choose click.
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


class AutoFullRenderWireTest(unittest.TestCase):
    """FA-C1: /upload with voice_source=auto_tts keeps running, on the SAME
    thread, straight through the full-auto chain down to final_video.mp4 —
    reached by polling the status endpoint only. The user_upload path stops at
    upload_pipeline done (group D wires it later)."""

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

    def _wait_for_stage(self, job_id, stage, timeout=20.0, interval=0.1):
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

    def test_auto_tts_reaches_final_video_via_status_polling_only(self):
        res = self.client.post(
            "/upload",
            data={"voice_source": "auto_tts"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        self._wait_for_stage(job_id, "upload_pipeline")
        status = self._wait_for_stage(job_id, "auto_full_render")

        self.assertTrue(
            (self.output_root / job_id / "final_video.mp4").exists(),
            "auto_tts path must produce final_video.mp4",
        )
        result = status["stages"]["auto_full_render"]["result"]
        self.assertIn("voiceover", result)
        self.assertIn("final", result)
    def test_user_upload_stays_stopped_at_upload_done(self):
        res = self.client.post(
            "/upload",
            data={"voice_source": "user_upload"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]

        self._wait_for_stage(job_id, "upload_pipeline")
        time.sleep(0.3)
        status = self.client.get(f"/api/jobs/{job_id}/status").json()
        self.assertNotIn(
            "auto_full_render", status.get("stages", {}),
            "user_upload must not trigger the auto chain",
        )
        self.assertFalse(
            (self.output_root / job_id / "final_video.mp4").exists(),
            "user_upload path stops at upload_pipeline done",
        )

    def test_auto_tts_status_page_shows_final_video_no_choose_link(self):
        # FA-C2: GET /upload/{job_id} must eventually show the final video
        # player + download link directly (no "choose voiceover source" link).
        res = self.client.post(
            "/upload",
            data={"voice_source": "auto_tts"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        self._wait_for_stage(job_id, "auto_full_render")

        page = self.client.get(f"/upload/{job_id}").text
        self.assertIn("<video", page, "final video player present")
        self.assertIn(f'href="/download/{job_id}"', page, "download link present")
        self.assertNotIn(
            "choose voiceover source", page,
            "no manual voiceover-source click needed on auto_tts path",
        )

    def test_user_upload_status_page_keeps_choose_link(self):
        # FA-C2: the user_upload page must keep the old "choose voiceover
        # source" continue link (proof nothing broke on that path).
        res = self.client.post(
            "/upload",
            data={"voice_source": "user_upload"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        self._wait_for_stage(job_id, "upload_pipeline")

        page = self.client.get(f"/upload/{job_id}").text
        self.assertIn("choose voiceover source", page)
        self.assertNotIn("<video", page)


if __name__ == "__main__":
    unittest.main()
