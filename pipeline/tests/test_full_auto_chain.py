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
    key_store,
    render_final,
    video_ingest,
    voiceover_auto,
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
                "format": {"duration": "2.0"},
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


if __name__ == "__main__":
    unittest.main()
