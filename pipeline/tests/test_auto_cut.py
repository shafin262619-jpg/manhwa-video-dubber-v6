"""Tests for pipeline.auto_cut (E2 FFmpeg draft video rendering).

All ffmpeg/ffprobe calls are mocked: we assert the exact command sequence that
would run (clip extract + speed-adjust -> concat -> mux) and that the
validation logic passes/fails correctly, without rendering anything.
"""

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from pipeline import auto_cut, video_ingest


def _ok_result(stdout=""):
    return types.SimpleNamespace(stdout=stdout, stderr="", returncode=0)


def _entry(serial, start, end, mult, target_start=0.0, target_end=None):
    # Target span defaults to mult * source duration so the guideline entry
    # has a healthy (non-degenerate) target like production D2/D3 output.
    if target_end is None:
        target_end = target_start + mult * (end - start)
    return {
        "serial": serial,
        "source_start_sec": start,
        "source_end_sec": end,
        "target_start_sec": target_start,
        "target_end_sec": target_end,
        "pts_multiplier": mult,
        "flagged": False,
        "flag_reason": None,
    }


class AutoCutBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-e2"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _write_inputs(self):
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        (self.job_dir / "voiceover_hi.wav").write_bytes(b"fake-audio")

    def _write_guideline(self, entries):
        (self.job_dir / "edit_guideline.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def _write_choice(self, mode):
        (self.job_dir / "voice_source_choice.json").write_text(
            json.dumps({"job_id": self.job_id, "mode": mode}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _probe_for(self, path):
        """Return the ffprobe JSON dict the mock should emit for a path."""
        name = Path(path).name
        if name == "voiceover_hi.wav":
            return {"format": {"duration": "12.0"}, "streams": []}
        if name == "source.mp4":
            return {
                "format": {"duration": "12.0"},
                "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
            }
        if name == "draft_final_video.mp4":
            return {
                "format": {"duration": "12.1"},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }
        return {"format": {}, "streams": []}

    def _run_with(self, probe_override=None):
        """Patch auto_cut._run, record commands, return (result, calls)."""
        calls = []

        def fake_run(cmd, timeout=None):
            calls.append(cmd)
            if cmd and cmd[0] == "ffprobe":
                path = cmd[-1]
                data = (
                    probe_override(path)
                    if probe_override
                    else self._probe_for(path)
                )
                return _ok_result(json.dumps(data))
            return _ok_result()

        with mock.patch.object(auto_cut, "_run", side_effect=fake_run):
            result = auto_cut.build_draft_video(self.job_id, upload_root=self.upload_root)
        return result, calls


class CommandBuildingTest(AutoCutBase):
    def test_builds_clip_concat_mux_commands_in_order(self):
        self._write_inputs()
        self._write_guideline(
            [
                _entry(1, 0.0, 6.0, 8.0 / 6.0),
                _entry(2, 6.0, 10.0, 0.8),
            ]
        )
        result, calls = self._run_with()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["clip_count"], 2)
        self.assertTrue(result["draft_path"].endswith("draft_final_video.mp4"))

        ffmpeg_cmds = [c for c in calls if c[0] == "ffmpeg"]
        self.assertEqual(len(ffmpeg_cmds), 4)  # 2 clips + concat + mux

        clip1 = ffmpeg_cmds[0]
        self.assertIn("-ss", clip1)
        self.assertIn("0.000", clip1)
        self.assertIn("6.000", clip1)
        self.assertIn("-an", clip1)
        self.assertTrue(any(arg == "setpts=1.333333*PTS" for arg in clip1))
        self.assertIn(str(self.job_dir / "source.mp4"), clip1)
        self.assertTrue(any(arg.endswith("serial_00000.mp4") for arg in clip1))

        clip2 = ffmpeg_cmds[1]
        self.assertTrue(any(arg == "setpts=0.800000*PTS" for arg in clip2))
        self.assertTrue(any(arg.endswith("serial_00001.mp4") for arg in clip2))

        concat = ffmpeg_cmds[2]
        self.assertIn("-f", concat)
        self.assertIn("concat", concat)
        self.assertIn("copy", concat)
        self.assertTrue(any(arg.endswith("concat.txt") for arg in concat))
        self.assertTrue(any(arg.endswith("concat_video.mp4") for arg in concat))

        mux = ffmpeg_cmds[3]
        self.assertIn(str(self.job_dir / "voiceover_hi.wav"), mux)
        self.assertIn(str(self.job_dir / "auto_cut_clips" / "concat_video.mp4"), mux)
        self.assertIn("0:v", mux)
        self.assertIn("1:a", mux)
        self.assertTrue(any(arg == auto_cut.config.RENDER_AUDIO_CODEC for arg in mux))
        self.assertTrue(any(arg.endswith("draft_final_video.mp4") for arg in mux))

    def test_clip_uses_guideline_cut_range(self):
        self._write_inputs()
        self._write_guideline([_entry(1, 12.5, 18.25, 1.0)])
        _, calls = self._run_with()
        clip = [c for c in calls if c[0] == "ffmpeg"][0]
        self.assertIn("12.500", clip)
        self.assertIn("18.250", clip)

    def test_ffmpeg_failure_raises_runtime_error(self):
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 6.0, 1.0)])

        def fake_run(cmd, timeout=None):
            if cmd and cmd[0] == "ffprobe":
                return _ok_result(json.dumps(self._probe_for(cmd[-1])))
            # simulate the real _run raising on a failed ffmpeg invocation
            raise RuntimeError("ffmpeg/ffprobe error: boom")

        with mock.patch.object(auto_cut, "_run", side_effect=fake_run):
            with self.assertRaises(RuntimeError):
                auto_cut.build_draft_video(self.job_id, upload_root=self.upload_root)


class EmptyGuidelineTest(AutoCutBase):
    def test_empty_guideline_does_not_render(self):
        self._write_inputs()
        self._write_guideline([])
        result, calls = self._run_with()
        self.assertEqual(result["status"], "no_serials")
        self.assertIsNone(result["draft_path"])
        self.assertEqual(calls, [])


class ValidationUnitTest(AutoCutBase):
    def test_pass_when_video_audio_and_duration_close(self):
        probe = {
            "format": {"duration": "12.1"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }
        ok, details = auto_cut._validate_draft(probe, 12.0, 0.2)
        self.assertTrue(ok)
        self.assertTrue(details["has_video"])
        self.assertTrue(details["has_audio"])
        self.assertTrue(details["duration_ok"])
        self.assertTrue(details["duration_enforced"])

    def test_fail_when_audio_stream_missing(self):
        probe = {"format": {"duration": "12.0"}, "streams": [{"codec_type": "video"}]}
        ok, details = auto_cut._validate_draft(probe, 12.0, 0.2)
        self.assertFalse(ok)
        self.assertFalse(details["has_audio"])

    def test_fail_when_video_stream_missing(self):
        probe = {"format": {"duration": "12.0"}, "streams": [{"codec_type": "audio"}]}
        ok, _ = auto_cut._validate_draft(probe, 12.0, 0.2)
        self.assertFalse(ok)

    def test_fail_when_duration_out_of_tolerance(self):
        probe = {
            "format": {"duration": "20.0"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }
        ok, details = auto_cut._validate_draft(probe, 12.0, 0.2)
        self.assertFalse(ok)
        self.assertFalse(details["duration_ok"])

    def test_fail_when_duration_missing(self):
        probe = {
            "format": {},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }
        ok, _ = auto_cut._validate_draft(probe, 12.0, 0.2)
        self.assertFalse(ok)

    def test_duration_mismatch_passes_when_not_enforced(self):
        # user_upload semantics: a large duration mismatch is normal
        # translation drift and must NOT fail the structural check.
        probe = {
            "format": {"duration": "20.0"},
            "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
        }
        ok, details = auto_cut._validate_draft(probe, 12.0, 0.2, enforce_duration=False)
        self.assertTrue(ok)
        self.assertFalse(details["duration_ok"])
        self.assertFalse(details["duration_enforced"])

    def test_missing_streams_fail_even_when_duration_not_enforced(self):
        # Structural integrity is always required, even on the user_upload path.
        probe = {"format": {"duration": "20.0"}, "streams": [{"codec_type": "video"}]}
        ok, _ = auto_cut._validate_draft(probe, 12.0, 0.2, enforce_duration=False)
        self.assertFalse(ok)


class ValidationInBuildTest(AutoCutBase):
    def test_validation_failure_raises_draft_validation_error(self):
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 6.0, 1.0)])

        def probe_override(path):
            if Path(path).name == "draft_final_video.mp4":
                # grossly longer than the source video -> validation must fail
                return {
                    "format": {"duration": "30.0"},
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                }
            return self._probe_for(path)

        with self.assertRaises(auto_cut.DraftValidationError):
            self._run_with(probe_override=probe_override)

    def test_validation_pass_reports_durations(self):
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 6.0, 2.0)])
        result, _ = self._run_with()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["voiceover_duration_sec"], 12.0)
        self.assertEqual(result["duration_sec"], 12.1)
        # 3 frames at 25fps -> 0.12s tolerance
        self.assertAlmostEqual(result["tolerance_sec"], 0.12, places=2)


class DurationValidationRegressionTest(AutoCutBase):
    """Regression (duration-check removal): the draft's STRUCTURE is always
    validated, but the total-duration check is only enforced on the auto-TTS
    path (whose clip durations are measured/precise). A user upload is
    legitimately a different length from the source video (translation
    drift), so on the user_upload path any duration mismatch proceeds — the
    per-segment alignment check (voiceover_unify) is the real accuracy gate,
    and an extreme mismatch at most produces a non-blocking warning.
    """

    def _write(self, source_dur, draft_dur, mode=None):
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 6.0, 1.0)])
        if mode is not None:
            self._write_choice(mode)

        def probe_override(path):
            name = Path(path).name
            if name == "source.mp4":
                return {
                    "format": {"duration": str(source_dur)},
                    "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
                }
            if name == "draft_final_video.mp4":
                return {
                    "format": {"duration": str(draft_dur)},
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                }
            return self._probe_for(path)

        return self._run_with(probe_override=probe_override)

    def test_auto_tts_expected_duration_matches_draft(self):
        # A correct auto-TTS draft lands on the source duration; the reported
        # expected_duration_sec must equal the draft's real ffprobe duration.
        result, _ = self._write(60.0, 60.0, mode="auto_tts")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expected_duration_sec"], 60.0)
        self.assertEqual(result["duration_sec"], 60.0)
        self.assertEqual(result["voice_source"], "auto_tts")
        self.assertAlmostEqual(result["tolerance_sec"], 0.12, places=2)

    def test_user_upload_expected_duration_matches_draft(self):
        result, _ = self._write(60.0, 60.0, mode="user_upload")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expected_duration_sec"], 60.0)
        self.assertEqual(result["duration_sec"], 60.0)
        self.assertEqual(result["voice_source"], "user_upload")

    def test_expected_duration_reads_job_meta_single_source_of_truth(self):
        # The validation's expected_duration_sec and subtitle_qa's
        # total_duration_sec must derive from the same ffprobe value: the
        # source video, recorded in job_meta.json. Even if a direct source
        # probe would say otherwise, job_meta wins (it is the value
        # subtitle_builder reports as total_duration_sec).
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": self.job_id, "duration_sec": 303.021}),
            encoding="utf-8",
        )
        result, _ = self._write(60.0, 303.0, mode="user_upload")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expected_duration_sec"], 303.021)

    def test_user_upload_accepts_few_seconds_variation(self):
        # A few seconds of human-pacing variance on a user recording is fine.
        result, _ = self._write(60.0, 62.5, mode="user_upload")
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["tolerance_sec"], 3.0, places=3)
        self.assertFalse(result["duration_enforced"])
        self.assertIsNone(result["duration_warning"])

    def test_user_upload_accepts_50_percent_longer(self):
        # The real-media failure: 523s of uploaded audio on a 303s video
        # (~73% longer). This must proceed — it is normal translation drift.
        result, _ = self._write(303.0, 523.0, mode="user_upload")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["expected_duration_sec"], 303.0)
        self.assertAlmostEqual(result["duration_sec"], 523.0, places=3)
        self.assertFalse(result["duration_enforced"])
        self.assertIsNone(result["duration_warning"])

    def test_user_upload_accepts_50_percent_shorter(self):
        # A translated voiceover can also be much shorter than the source;
        # it must not be blocked either.
        result, _ = self._write(60.0, 25.0, mode="user_upload")
        self.assertEqual(result["status"], "ok")
        self.assertAlmostEqual(result["duration_sec"], 25.0, places=3)
        self.assertFalse(result["duration_enforced"])

    def test_user_upload_large_mismatch_proceeds_with_warning(self):
        # An extreme mismatch (~5x+) is a possible wrong-file signal: it
        # still does NOT block, but surfaces as a non-blocking warning.
        result, _ = self._write(60.0, 320.0, mode="user_upload")
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["duration_warning"])
        self.assertIn("right audio file", result["duration_warning"])

    def test_user_upload_extremely_short_proceeds_with_warning(self):
        result, _ = self._write(60.0, 10.0, mode="user_upload")
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["duration_warning"])

    def test_user_upload_still_requires_video_and_audio_streams(self):
        # The duration check is gone for user_upload, but structural
        # validation (video + audio streams present) still applies.
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 6.0, 1.0)])
        self._write_choice("user_upload")

        def probe_override(path):
            name = Path(path).name
            if name == "source.mp4":
                return {
                    "format": {"duration": "60.0"},
                    "streams": [{"codec_type": "video", "r_frame_rate": "25/1"}],
                }
            if name == "draft_final_video.mp4":
                return {
                    "format": {"duration": "95.0"},
                    "streams": [{"codec_type": "video"}],  # audio missing
                }
            return self._probe_for(path)

        with self.assertRaises(auto_cut.DraftValidationError):
            self._run_with(probe_override=probe_override)

    def test_auto_tts_keeps_strict_tolerance(self):
        # The auto-TTS path keeps the frames-strict tolerance: a 3s drift is
        # rejected even though the user_upload path would accept it.
        with self.assertRaises(auto_cut.DraftValidationError):
            self._write(60.0, 63.0, mode="auto_tts")

    def test_auto_tts_mismatch_still_blocks_even_if_extreme(self):
        # Enforced duration is on for auto_tts, so a huge mismatch blocks too.
        with self.assertRaises(auto_cut.DraftValidationError):
            self._write(60.0, 320.0, mode="auto_tts")


class InputErrorTest(AutoCutBase):
    def test_missing_job_raises(self):
        with self.assertRaises(FileNotFoundError):
            auto_cut.build_draft_video("nope", upload_root=self.upload_root)

    def test_missing_guideline_raises(self):
        self._write_inputs()
        with self.assertRaises(FileNotFoundError):
            auto_cut.build_draft_video(self.job_id, upload_root=self.upload_root)

    def test_missing_source_raises(self):
        (self.job_dir / "voiceover_hi.wav").write_bytes(b"fake-audio")
        self._write_guideline([_entry(1, 0.0, 6.0, 1.0)])
        with self.assertRaises(FileNotFoundError):
            auto_cut.build_draft_video(self.job_id, upload_root=self.upload_root)

    def test_missing_voiceover_raises(self):
        (self.job_dir / "source.mp4").write_bytes(b"fake-source")
        self._write_guideline([_entry(1, 0.0, 6.0, 1.0)])
        with self.assertRaises(FileNotFoundError):
            auto_cut.build_draft_video(self.job_id, upload_root=self.upload_root)

    def test_malformed_guideline_raises(self):
        self._write_inputs()
        (self.job_dir / "edit_guideline.json").write_text("{not json", encoding="utf-8")
        with self.assertRaises(ValueError):
            auto_cut.build_draft_video(self.job_id, upload_root=self.upload_root)


class FfmpegErrorExtractionTest(unittest.TestCase):
    """E7 Fix C: the user-facing message must carry the real ffmpeg error
    line, not the version banner."""

    def _banner(self):
        return (
            "ffmpeg version 5.1.9-0+deb12u1 Copyright (c) 2000-2023 FFmpeg developers\n"
            "  built with gcc 12 (Debian 12.2.0-14)\n"
            "  configuration: --prefix=/usr --enable-gpl --enable-nonfree\n"
            "  libavutil 57.28.100 / 57.28.100\n"
            "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'source.mp4':\n"
            "  Duration: 00:01:00.00, start: 0.000000, bitrate: 100 kb/s\n"
            "  Stream #0:0[0x1](und): Video: h264 (High), yuv420p\n"
        )

    def test_returns_real_error_line_not_banner(self):
        stderr = self._banner() + "-to value smaller than -ss; aborting\n"
        self.assertEqual(
            auto_cut._extract_ffmpeg_error(stderr),
            "-to value smaller than -ss; aborting",
        )

    def test_error_hint_line_picked_over_plain_last_line(self):
        stderr = self._banner() + "some informational line\n"
        self.assertEqual(
            auto_cut._extract_ffmpeg_error(stderr), "some informational line"
        )

    def test_last_line_fallback_for_empty_hint(self):
        self.assertEqual(
            auto_cut._extract_ffmpeg_error(""), "unknown ffmpeg error"
        )
        self.assertEqual(
            auto_cut._extract_ffmpeg_error("  \n \n"), "unknown ffmpeg error"
        )


class DegenerateSegmentTest(AutoCutBase):
    """E7 Fix A: a zero/negative source window must never reach ffmpeg's
    ``-ss``/``-to`` (which aborts with "-to value smaller than -ss"); it is
    replaced by a minimal real window stretched to the target duration."""

    def _degenerate_entry(self, start=100.0, end=100.0, target=2.0):
        return {
            "serial": 1,
            "source_start_sec": start,
            "source_end_sec": end,
            "target_start_sec": 0.0,
            "target_end_sec": target,
            "pts_multiplier": 1.0,
            "flagged": True,
            "flag_reason": "invalid_duration",
        }

    def test_degenerate_segment_uses_minimal_window(self):
        self._write_inputs()
        self._write_guideline([self._degenerate_entry()])

        with self.assertLogs("pipeline.auto_cut", level="WARNING") as cm:
            result, calls = self._run_with()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["clip_count"], 1)
        self.assertTrue(
            any("degenerate source segment" in line for line in cm.output)
        )
        clip = [c for c in calls if c[0] == "ffmpeg"][0]
        # Source duration is 12.0 in the mock: the minimal window is clamped
        # inside the source and stretched to the 2.0s target.
        self.assertIn("11.950", clip)
        self.assertIn("12.000", clip)
        self.assertTrue(any(arg == "setpts=40.000000*PTS" for arg in clip))

    def test_zero_target_degenerate_segment_still_renders(self):
        self._write_inputs()
        self._write_guideline([self._degenerate_entry(target=0.0)])
        result, calls = self._run_with()
        self.assertEqual(result["status"], "ok")
        clip = [c for c in calls if c[0] == "ffmpeg"][0]
        self.assertTrue(any(arg == "setpts=1.000000*PTS" for arg in clip))

    def test_healthy_segment_unaffected(self):
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 6.0, 1.0)])
        _, calls = self._run_with()
        clip = [c for c in calls if c[0] == "ffmpeg"][0]
        self.assertIn("0.000", clip)
        self.assertIn("6.000", clip)
        self.assertTrue(any(arg == "setpts=1.000000*PTS" for arg in clip))


class DurationDriftRegressionTest(AutoCutBase):
    """Duration-drift invariant (E9): the final video duration must equal the
    voiceover audio duration. E2 stretches each clip to its exact target
    duration (no cap on the multiplier) and concatenates, so the draft equals
    the sum of the targets — which D3/D4 clamp to the real audio length. These
    tests pin that no cap creeps into the render command and that the draft
    lands on the voiceover length."""

    def test_extreme_20x_multiplier_passes_uncapped(self):
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 2.0, 20.0)])
        result, calls = self._run_with()
        self.assertEqual(result["status"], "ok")
        clip = [c for c in calls if c[0] == "ffmpeg"][0]
        self.assertTrue(any(arg == "setpts=20.000000*PTS" for arg in clip))

    def test_draft_lands_on_voiceover_duration(self):
        # Given a guideline whose target durations sum to the voiceover length
        # (12.0s), the rendered draft reports exactly that length — the
        # invariant "final video duration == voiceover audio duration".
        self._write_inputs()
        self._write_guideline([_entry(1, 0.0, 6.0, 2.0)])
        self._write_choice("user_upload")

        def probe_override(path):
            name = Path(path).name
            if name == "draft_final_video.mp4":
                return {
                    "format": {"duration": "12.0"},
                    "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                }
            return self._probe_for(path)

        result, _ = self._run_with(probe_override=probe_override)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["duration_sec"], 12.0)
        self.assertEqual(result["voiceover_duration_sec"], 12.0)
        self.assertEqual(result["duration_sec"], result["voiceover_duration_sec"])


if __name__ == "__main__":
    unittest.main()
