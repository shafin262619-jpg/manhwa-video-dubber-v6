"""Tests for pipeline.resume (F9 resume-from-interruption).

The F9 acceptance test is ``test_resume_after_crash_skips_completed_stage``:
a stage that wrote its artifact and then raised (simulated kill mid-chain) is
NOT re-run — ``resume_job`` continues from the next stage onward.
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
    resume,
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
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.wav"
        voiceover_auto._make_silence(duration_sec, path)
        return path.read_bytes()


class ResumeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-resume"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore_paths)

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
                Path(cmd[-1]).parent.mkdir(parents=True, exist_ok=True)
                Path(cmd[-1]).write_bytes(b"out")
            return _ok_result()

        return fake_run


class FindResumePointTest(ResumeBase):
    def test_no_subtitles_yet_reports_upload_pipeline(self):
        # No subtitles_hi.json -> the D2+ chains have nothing to resume.
        self.assertEqual(resume.find_resume_point(self.job_id), "upload_pipeline")

    def test_auto_path_steps(self):
        _require_tools()
        self._write_subtitles()
        self._write_choice("auto_tts")
        self.assertEqual(
            resume.find_resume_point(self.job_id), "D2_voiceover"
        )
        (self.job_dir / "timestamps_hi_auto.json").write_text("[]", encoding="utf-8")
        self.assertEqual(resume.find_resume_point(self.job_id), "D4_unify")
        (self.job_dir / "timestamps_hi_final.json").write_text("[]", encoding="utf-8")
        self.assertEqual(resume.find_resume_point(self.job_id), "E1_guideline")
        (self.job_dir / "edit_guideline.json").write_text("[]", encoding="utf-8")
        self.assertEqual(resume.find_resume_point(self.job_id), "E2_draft")
        (self.job_dir / "draft_final_video.mp4").write_bytes(b"draft")
        self.assertEqual(resume.find_resume_point(self.job_id), "F3_final")
        (self.output_root / self.job_id).mkdir(parents=True, exist_ok=True)
        (self.output_root / self.job_id / "final_video.mp4").write_bytes(b"final")
        self.assertIsNone(resume.find_resume_point(self.job_id))

    def test_user_path_uses_upload_timestamps(self):
        _require_tools()
        self._write_subtitles()
        self._write_choice("user_upload")
        self.assertEqual(resume.find_resume_point(self.job_id), "D3_align")
        (self.job_dir / "timestamps_hi_upload.json").write_text("[]", encoding="utf-8")
        self.assertEqual(resume.find_resume_point(self.job_id), "D4_unify")


class ResumeJobTest(ResumeBase):
    def test_resume_after_crash_skips_completed_stage(self):
        # F9 acceptance: simulate a kill mid-chain — D3 (align) wrote its
        # artifact then raised. resume_job must continue from D4 onward and
        # must NOT re-run D3.
        _require_tools()
        self._write_subtitles()
        self._write_choice("user_upload")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(_wav_bytes(2.0))

        calls = []

        def crashing_align(job_id, upload_root=None):
            # D3 finishes writing its artifact, then the process "dies" here.
            (self.job_dir / "timestamps_hi_upload.json").write_text(
                json.dumps(
                    [
                        {
                            "serial": 1, "start_sec": 0.0, "end_sec": 1.0,
                            "alignment_fallback": False, "alignment_source": "gemini",
                        },
                        {
                            "serial": 2, "start_sec": 1.0, "end_sec": 2.0,
                            "alignment_fallback": False, "alignment_source": "gemini",
                        },
                    ]
                ),
                encoding="utf-8",
            )
            raise RuntimeError("killed mid-chain")

        align_mock = mock.patch.object(
            voiceover_upload, "align_uploaded_voiceover", side_effect=crashing_align
        )
        auto_run_mock = mock.patch.object(
            auto_cut, "_run", side_effect=self._mock_auto_run(calls)
        )
        align_mock.start()
        auto_run_mock.start()
        try:
            with self.assertRaises(RuntimeError):
                full_auto_chain.run_user_upload_chain(self.job_id)

            self.assertEqual(
                voiceover_upload.align_uploaded_voiceover.call_count, 1,
                "D3 ran once during the original (crashed) run",
            )
            self.assertFalse(
                (self.output_root / self.job_id / "final_video.mp4").exists()
            )

            # Resume: point = D4_unify (D3 artifact exists). D3 is NOT re-run.
            result = resume.resume_job(self.job_id)
            self.assertEqual(
                voiceover_upload.align_uploaded_voiceover.call_count, 1,
                "resume must NOT re-run the completed D3 stage",
            )
        finally:
            align_mock.stop()
            auto_run_mock.stop()
        self.assertIsNone(result["alignment"], "skipped stage reports None")
        self.assertEqual(result["final"]["status"], "ok")
        self.assertTrue((self.output_root / self.job_id / "final_video.mp4").exists())

    def test_resume_auto_chain_from_d4_skips_d2(self):
        _require_tools()
        self._write_subtitles()
        self._write_choice("auto_tts")
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(_wav_bytes(2.0))
        # D2 artifact already exists (auto timestamps); D4 missing.
        (self.job_dir / "timestamps_hi_auto.json").write_text(
            json.dumps(
                [
                    {"serial": 1, "start_sec": 0.0, "end_sec": 1.0},
                    {"serial": 2, "start_sec": 1.0, "end_sec": 2.0},
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        with mock.patch.object(
            voiceover_auto, "generate_auto_voiceover"
        ) as gen_mock, mock.patch.object(
            auto_cut, "_run", side_effect=self._mock_auto_run([])
        ):
            result = resume.resume_job(self.job_id)
        gen_mock.assert_not_called()
        self.assertIsNone(result["voiceover"])
        self.assertEqual(result["final"]["status"], "ok")

    def test_resume_complete_job_raises(self):
        _require_tools()
        self._write_subtitles()
        self._write_choice("auto_tts")
        (self.job_dir / "timestamps_hi_auto.json").write_text("[]", encoding="utf-8")
        (self.job_dir / "timestamps_hi_final.json").write_text("[]", encoding="utf-8")
        (self.job_dir / "edit_guideline.json").write_text("[]", encoding="utf-8")
        (self.job_dir / "draft_final_video.mp4").write_bytes(b"draft")
        (self.output_root / self.job_id).mkdir(parents=True, exist_ok=True)
        (self.output_root / self.job_id / "final_video.mp4").write_bytes(b"final")

        with self.assertRaises(RuntimeError):
            resume.resume_job(self.job_id)

    def test_resume_before_upload_done_raises(self):
        self._write_choice("auto_tts")
        with self.assertRaises(RuntimeError):
            resume.resume_job(self.job_id)


if __name__ == "__main__":
    unittest.main()
