"""Tests for pipeline.full_auto_chain (FA-B1/B2 orchestration wrappers).

The chain functions are pure Python (no HTTP): they run D2/D3 -> D4 -> E1 ->
E2 -> F3 directly. Gemini TTS is mocked with real ffmpeg silence placeholders
(the D2 real-ffmpeg-silence pattern) and the E2/F3 ffmpeg/ffprobe steps are
mocked via ``auto_cut._run``, matching the existing D2/E2/F3 unit tests.
"""

import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pipeline import (
    auto_cut,
    full_auto_chain,
    job_status,
    key_store,
    render_final,
    video_ingest,
    voiceover_auto,
    voiceover_upload,
)


def _ok_result(stdout=""):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _wav_bytes(duration_sec):
    """Build a real mono wav of the given duration and return its bytes."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.wav"
        voiceover_auto._make_silence(duration_sec, path)
        return path.read_bytes()


class FullAutoChainBase(unittest.TestCase):
    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-fa-chain"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore_paths)

        self._keys_patch = mock.patch.object(
            key_store, "get_active_keys", return_value=["k1"]
        )
        self._keys_patch.start()
        self.addCleanup(self._keys_patch.stop)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _write_subtitles(self):
        (self.job_dir / "subtitles_hi.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "text_zh": "A", "text_hi": "पहला"},
                    {"serial": 2, "text_zh": "B", "text_hi": "दूसरा"},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "text_zh": "A", "start_sec": 0.0, "end_sec": 1.5},
                    {"serial": 2, "text_zh": "B", "start_sec": 2.0, "end_sec": 3.5},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def _write_choice(self, mode="auto_tts"):
        (self.job_dir / "voice_source_choice.json").write_text(
            json.dumps({"job_id": self.job_id, "mode": mode}, ensure_ascii=False),
            encoding="utf-8",
        )

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
                "format": {"duration": "10.0"},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        return {"format": {}, "streams": []}

    def _mock_auto_run(self, calls):
        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                return _ok_result(json.dumps(self._probe_for(cmd[-1])))
            if cmd and cmd[0] == "ffmpeg":
                Path(cmd[-1]).write_bytes(b"out")
            return _ok_result()

        return fake_run


class AutoTtsChainTest(FullAutoChainBase):
    def test_run_auto_tts_chain_reaches_final_video(self):
        self._write_subtitles()
        self._write_choice("auto_tts")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")

        calls = []
        with mock.patch.object(
            voiceover_auto, "_call_tts", return_value=_wav_bytes(1.0)
        ), mock.patch.object(auto_cut, "_run", side_effect=self._mock_auto_run(calls)):
            result = full_auto_chain.run_auto_tts_chain(self.job_id)

        self.assertIn("voiceover", result)
        self.assertIn("final", result)
        self.assertEqual(result["voiceover"]["status"], "ok")
        self.assertEqual(result["final"]["status"], "ok")

        final = self.output_root / self.job_id / "final_video.mp4"
        self.assertTrue(final.exists(), "outputs/<job_id>/final_video.mp4 created")

        for name in (
            "voiceover_hi.wav",
            "timestamps_hi_auto.json",
            "timestamps_hi_final.json",
            "edit_guideline.json",
            "draft_final_video.mp4",
        ):
            self.assertTrue((self.job_dir / name).exists(), f"missing {name}")

        ffmpeg_cmds = [c for c in calls if c[0] == "ffmpeg"]
        self.assertTrue(
            any(arg.endswith("final_video.mp4") for cmd in ffmpeg_cmds for arg in cmd),
            "final render ffmpeg step ran",
        )


class UserUploadChainTest(FullAutoChainBase):
    def test_run_user_upload_chain_reaches_final_video(self):
        # Precondition: voiceover_hi.wav is already saved (D3 alignment step
        # reads it). We save a real silence wav, mock the Gemini align call,
        # then run the whole D3 -> F3 chain directly.
        self._write_subtitles()
        self._write_choice("user_upload")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(_wav_bytes(2.0))

        align_timestamps = [
            {
                "serial": 1, "start_sec": 0.0, "end_sec": 1.0,
                "alignment_fallback": False, "alignment_source": "gemini",
            },
            {
                "serial": 2, "start_sec": 1.0, "end_sec": 2.0,
                "alignment_fallback": False, "alignment_source": "gemini",
            },
        ]
        calls = []
        with mock.patch.object(
            voiceover_upload, "_gemini_align", return_value=align_timestamps
        ), mock.patch.object(auto_cut, "_run", side_effect=self._mock_auto_run(calls)):
            result = full_auto_chain.run_user_upload_chain(self.job_id)

        self.assertIn("alignment", result)
        self.assertIn("final", result)
        self.assertEqual(result["alignment"]["status"], "ok")
        self.assertEqual(result["final"]["status"], "ok")

        final = self.output_root / self.job_id / "final_video.mp4"
        self.assertTrue(final.exists(), "outputs/<job_id>/final_video.mp4 created")

        for name in (
            "timestamps_hi_upload.json",
            "timestamps_hi_final.json",
            "edit_guideline.json",
            "draft_final_video.mp4",
        ):
            self.assertTrue((self.job_dir / name).exists(), f"missing {name}")


class StageStatusTest(FullAutoChainBase):
    """F9: the chain records a per-stage status entry, in order, done."""

    def test_auto_chain_writes_per_stage_status_in_order(self):
        self._write_subtitles()
        self._write_choice("auto_tts")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")

        calls = []
        with mock.patch.object(
            voiceover_auto, "_call_tts", return_value=_wav_bytes(1.0)
        ), mock.patch.object(auto_cut, "_run", side_effect=self._mock_auto_run(calls)):
            full_auto_chain.run_auto_tts_chain(self.job_id)

        status = job_status.read_status(self.job_id)
        self.assertEqual(
            list(status.get("stages", {}).keys()),
            [
                "D2_voiceover",
                "D4_unify",
                "E1_guideline",
                "E2_draft",
                "F3_final",
            ],
            "each chain stage records its own status entry, in order",
        )
        for stage in ("D2_voiceover", "D4_unify", "E1_guideline", "E2_draft", "F3_final"):
            self.assertEqual(status["stages"][stage]["state"], "done", stage)

    def test_user_upload_chain_writes_per_stage_status_in_order(self):
        self._write_subtitles()
        self._write_choice("user_upload")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(_wav_bytes(2.0))

        align_timestamps = [
            {
                "serial": 1, "start_sec": 0.0, "end_sec": 1.0,
                "alignment_fallback": False, "alignment_source": "gemini",
            },
            {
                "serial": 2, "start_sec": 1.0, "end_sec": 2.0,
                "alignment_fallback": False, "alignment_source": "gemini",
            },
        ]
        calls = []
        with mock.patch.object(
            voiceover_upload, "_gemini_align", return_value=align_timestamps
        ), mock.patch.object(auto_cut, "_run", side_effect=self._mock_auto_run(calls)):
            full_auto_chain.run_user_upload_chain(self.job_id)

        status = job_status.read_status(self.job_id)
        self.assertEqual(
            list(status.get("stages", {}).keys()),
            [
                "D3_align",
                "D4_unify",
                "E1_guideline",
                "E2_draft",
                "F3_final",
            ],
            "each chain stage records its own status entry, in order",
        )
        for stage in ("D3_align", "D4_unify", "E1_guideline", "E2_draft", "F3_final"):
            self.assertEqual(status["stages"][stage]["state"], "done", stage)


class FailureCaseTest(FullAutoChainBase):
    """FA-B3: every stage's exceptions propagate out of the chain uncaught.

    A mid-chain failure must stop the following steps (no partial/silent
    state), and nothing may be swallowed with a bare ``except: pass`` — the
    caller (group C/D) is responsible for catching and reporting.
    """

    def _write_auto_inputs(self):
        self._write_subtitles()
        self._write_choice("auto_tts")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")

    def _write_user_inputs(self):
        self._write_subtitles()
        self._write_choice("user_upload")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(_wav_bytes(2.0))

    def _assert_no_final_video(self):
        self.assertFalse(
            (self.output_root / self.job_id / "final_video.mp4").exists(),
            "final_video.mp4 must NOT be produced when the chain fails",
        )

    def test_auto_tts_total_failure_propagates_and_stops_chain(self):
        # D2 (TTS) completely fails -> the exception propagates out of the
        # chain, no later stage runs and no final_video.mp4 is created.
        self._write_auto_inputs()
        with mock.patch.object(
            voiceover_auto,
            "generate_auto_voiceover",
            side_effect=RuntimeError("all TTS keys exhausted"),
        ), mock.patch.object(
            auto_cut, "_run", side_effect=self._mock_auto_run([])
        ) as run_mock:
            with self.assertRaises(RuntimeError):
                full_auto_chain.run_auto_tts_chain(self.job_id)
        self._assert_no_final_video()
        run_mock.assert_not_called()  # no ffmpeg work ran -> chain stopped

    def test_draft_validation_failure_propagates_and_stops_chain(self):
        # E2 (draft render) fails validation -> DraftValidationError
        # propagates, F3 (finalize) is never called, no final video.
        self._write_auto_inputs()
        with mock.patch.object(
            voiceover_auto, "_call_tts", return_value=_wav_bytes(1.0)
        ), mock.patch.object(
            auto_cut,
            "build_draft_video",
            side_effect=auto_cut.DraftValidationError("draft validation failed"),
        ), mock.patch.object(render_final, "finalize_video") as finalize_mock:
            with self.assertRaises(auto_cut.DraftValidationError):
                full_auto_chain.run_auto_tts_chain(self.job_id)
        self._assert_no_final_video()
        finalize_mock.assert_not_called()

    def test_final_render_failure_propagates_and_stops_chain(self):
        # F3 (final render) fails (e.g. ffprobe duration mismatch) -> the
        # RuntimeError propagates, no final_video.mp4 is produced.
        self._write_auto_inputs()
        with mock.patch.object(
            voiceover_auto, "_call_tts", return_value=_wav_bytes(1.0)
        ), mock.patch.object(
            auto_cut, "_run", side_effect=self._mock_auto_run([])
        ), mock.patch.object(
            render_final,
            "finalize_video",
            side_effect=RuntimeError("ffprobe duration mismatch"),
        ):
            with self.assertRaises(RuntimeError):
                full_auto_chain.run_auto_tts_chain(self.job_id)
        self._assert_no_final_video()

    def test_user_upload_align_failure_propagates_and_stops_chain(self):
        # D3 (align) fails -> the exception propagates, F3 never runs.
        self._write_user_inputs()
        with mock.patch.object(
            voiceover_upload,
            "align_uploaded_voiceover",
            side_effect=FileNotFoundError("no voiceover_hi.wav"),
        ), mock.patch.object(render_final, "finalize_video") as finalize_mock:
            with self.assertRaises(FileNotFoundError):
                full_auto_chain.run_user_upload_chain(self.job_id)
        self._assert_no_final_video()
        finalize_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
