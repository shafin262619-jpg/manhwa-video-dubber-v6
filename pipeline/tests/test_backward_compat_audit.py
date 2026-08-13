"""HTTP-level regression tests for the FA-E1 backward-compat audit.

Group E's first chunk verifies that the OLD manual routes
(``/voiceover/{job_id}/choose``, ``/voiceover/{job_id}/upload``,
``/voiceover/{job_id}/align_uploaded``, ``/final/{job_id}``,
``/review/{job_id}``) still coexist with the new full-auto pipeline — a user
can drive the old flow directly by URL at any time.

The audit found one real bug: after a manual voice-source override the
``GET /upload/{job_id}`` page could fall into an infinite redirect loop — the
polling page redirects whenever the *overall* status state is ``done``, but
the flat ``stage``/``state`` fields may describe a *different* stage than the
one the page is polling (e.g. ``auto_full_render``/``done`` while the page is
waiting on ``upload_pipeline``), or the polled stage may never have started
(a manual override to ``auto_tts`` after upload). Both cases loop forever.

Fixes (in ``app.py``):
- ``upload_status_page()`` now gates each branch on the per-stage history
  (``stages.<stage>.state``) instead of the flat ``stage``/``state`` fields,
  so a page never polls a stage that is already done or was superseded.
- When ``voice_source == "auto_tts"`` and ``auto_full_render`` never started
  but ``upload_pipeline`` finished, the page resumes the chain itself (via
  ``_start_stage(job_id, "auto_full_render", _run_auto_full_render)``) so it
  converges to the final video instead of polling a stage that will never run.
- The FA-C1 same-thread chain logic is factored into ``_run_auto_full_render``
  (still runs on the upload thread, no new spawn) so the resume path and the
  upload path share one implementation.

These tests lock in that behavior so a future refactor cannot reintroduce the
loop. The original manual-flow regression test ``test_app_orchestration.py``
is untouched and still passes alongside.
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


class BackwardCompatAuditTest(unittest.TestCase):
    """FA-E1: manual voice-source overrides must not crash, loop or duplicate."""

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
        self.auto_run_calls = []
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
        self.auto_run_calls.append(cmd)
        if cmd and cmd[0] == "ffprobe":
            return _ok_result(json.dumps(self._probe_for(cmd[-1])))
        if cmd and cmd[0] == "ffmpeg":
            Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
            Path(cmd[-1]).write_bytes(b"faked-video-bytes")
        return _ok_result()

    def _upload(self, voice_source=None):
        data = {}
        if voice_source is not None:
            data["voice_source"] = voice_source
        res = self.client.post(
            "/upload",
            data=data,
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        return res.json()["job_id"]

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

    def _final_path(self, job_id):
        return self.output_root / job_id / "final_video.mp4"

    def test_override_to_user_upload_after_auto_render_shows_audio_form(self):
        # auto_tts upload runs the whole chain to the final video; a manual
        # override to user_upload must not crash or loop — /upload/{job_id}
        # must show the audio-upload form directly (upload_pipeline done lives
        # in the stage history even though the flat stage is auto_full_render).
        job_id = self._upload(voice_source="auto_tts")
        self._wait_stage(job_id, "auto_full_render")
        self.assertTrue(self._final_path(job_id).exists())

        res = self.client.post(
            f"/voiceover/{job_id}/choose", data={"mode": "user_upload"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        choice = json.loads(
            (self.upload_root / job_id / "voice_source_choice.json").read_text()
        )
        self.assertEqual(choice["mode"], "user_upload")

        page = self.client.get(f"/upload/{job_id}").text
        self.assertIn(f'action="/voiceover/{job_id}/upload"', page)
        self.assertNotIn("choose voiceover source", page)
        self.assertNotIn("window.location.href", page)

        # The override is honored end-to-end: uploading audio re-renders the
        # final video from the user's audio (download works again).
        res = self.client.post(
            f"/voiceover/{job_id}/upload",
            files={"audio": ("voice.wav", self.silence_bytes, "audio/wav")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self._wait_stage(job_id, "user_audio_pipeline")
        dl = self.client.get(f"/download/{job_id}")
        self.assertEqual(dl.status_code, 200)
        self.assertGreater(len(dl.content), 0)

    def test_re_override_to_auto_tts_is_idempotent_no_extra_final_render(self):
        # Re-choosing auto_tts after the auto chain already ran must not crash
        # and must not re-run the final (F3) render — /upload still shows the
        # already-rendered final video.
        job_id = self._upload(voice_source="auto_tts")
        self._wait_stage(job_id, "auto_full_render")
        final_path = self._final_path(job_id)
        self.assertTrue(final_path.exists())
        mtime_before = final_path.stat().st_mtime_ns

        res = self.client.post(
            f"/voiceover/{job_id}/choose", data={"mode": "auto_tts"}
        )
        self.assertEqual(res.status_code, 200, res.text)

        self.assertEqual(final_path.stat().st_mtime_ns, mtime_before)
        page = self.client.get(f"/upload/{job_id}").text
        self.assertIn("<video", page)
        self.assertIn(f'href="/download/{job_id}"', page)

    def test_override_to_auto_tts_from_user_upload_resumes_to_final(self):
        # A user_upload job stops at upload_pipeline done; switching it to
        # auto_tts via the old /choose route runs D2->E2. GET /upload/{job_id}
        # must NOT poll a never-started auto_full_render stage forever — it
        # resumes the chain and the final video becomes downloadable.
        job_id = self._upload(voice_source="user_upload")
        self._wait_stage(job_id, "upload_pipeline")
        status = self.client.get(f"/api/jobs/{job_id}/status").json()
        self.assertNotIn("auto_full_render", status.get("stages", {}))

        res = self.client.post(
            f"/voiceover/{job_id}/choose", data={"mode": "auto_tts"}
        )
        self.assertEqual(res.status_code, 200, res.text)

        page = self.client.get(f"/upload/{job_id}").text
        self.assertIn("Processing", page)
        self._wait_stage(job_id, "auto_full_render")
        self.assertTrue(self._final_path(job_id).exists())
        dl = self.client.get(f"/download/{job_id}")
        self.assertEqual(dl.status_code, 200)
        self.assertGreater(len(dl.content), 0)

    def test_old_manual_routes_still_work_by_direct_url(self):
        # A job that stopped at upload (user_upload) can still be driven
        # entirely through the old manual routes by direct URL, as pre-FA
        # users expect. user_upload is used so no auto_full_render background
        # chain leaks past this test into the next one.
        job_id = self._upload(voice_source="user_upload")
        self._wait_stage(job_id, "upload_pipeline")

        res = self.client.get(f"/voiceover/{job_id}/choose")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(f'action="/voiceover/{job_id}/choose"', res.text)

        res = self.client.post(
            f"/voiceover/{job_id}/choose", data={"mode": "user_upload"}
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(f'action="/voiceover/{job_id}/upload"', res.text)

        res = self.client.post(
            f"/voiceover/{job_id}/upload",
            files={"audio": ("voice.wav", self.silence_bytes, "audio/wav")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        self._wait_stage(job_id, "user_audio_pipeline")

        res = self.client.get(f"/voiceover/{job_id}/align_uploaded")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("timestamps_hi_upload.json", res.text)

        res = self.client.get(f"/review/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        res = self.client.get(f"/final/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self._wait_stage(job_id, "final_render")
        dl = self.client.get(f"/download/{job_id}")
        self.assertEqual(dl.status_code, 200)
        self.assertGreater(len(dl.content), 0)


if __name__ == "__main__":
    unittest.main()
