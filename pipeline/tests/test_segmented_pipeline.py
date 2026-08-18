"""Tests for the per-segment pipeline orchestrator (F13b).

Runs the real keyless-safe stages (materialize, B2, whisper cross-check, C1)
against per-segment mini-job dirs and mocks the voiceover chain stages, which
need TTS keys / speech synthesis.
"""

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import (
    job_status as store,
    lang_files,
    render_final,
    segmented_pipeline,
    segmentation,
    video_ingest,
    voiceover_unify,
)


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_video(path, seconds=1):
    _require_tools()
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"color=c=black:s=320x240:d={seconds}",
            "-f", "lavfi", "-i", f"anullsrc=r=16000:cl=mono:d={seconds}",
            "-pix_fmt", "yuv420p", "-shortest", str(path),
        ],
        check=True,
        capture_output=True,
    )


def _make_wav(path, seconds=10):
    _require_tools()
    subprocess.run(
        [
            "ffmpeg", "-y", "-f", "lavfi",
            "-i", f"anullsrc=r=16000:cl=mono:d={seconds}",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


class SegmentedPipelineBase(unittest.TestCase):
    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-seg"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _make_job(self, seconds=8, target=3):
        _make_video(self.job_dir / "source.mp4", seconds=seconds)
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"job_id": self.job_id, "duration_sec": float(seconds)}),
            encoding="utf-8",
        )
        raw = {
            "job_id": self.job_id,
            "status": "ok",
            "chunked": False,
            "segments_count": 1,
            "failed_segments": [],
            "errors": {},
            "subtitles": [
                {"text": f"line {i}", "start_sec": s, "end_sec": s + 0.5}
                for i, s in enumerate([0.5, 2.0, 3.5, 5.0, 6.5])
            ],
        }
        (self.job_dir / "subtitles_zh_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.plan = segmentation.build_segment_plan(
            self.job_id, upload_root=self.upload_root, target_duration_sec=target
        )
        store.init_segments(self.job_id, self.plan)


class VoiceoverChainFakes:
    """Real-looking D2/D4/E1/E2/F3 fakes that write artifacts into job_dir."""

    def __init__(self):
        self.calls = []

    def _seg(self, job_dir):
        return Path(job_dir).name

    def d2(self, job_id, upload_root=None, call_budget=None, job_dir=None):
        self.calls.append(("D2", self._seg(job_dir)))
        lang = lang_files.target_lang(job_id, upload_root)
        seg = Path(job_dir)
        (seg / lang_files.timestamps_auto(lang)).write_text(
            json.dumps([{"serial": 1, "start_sec": 0.1, "end_sec": 0.9}])
        )
        return {"status": "ok"}

    def d4(self, job_id, upload_root=None, job_dir=None):
        self.calls.append(("D4", self._seg(job_dir)))
        lang = lang_files.target_lang(job_id, upload_root)
        seg = Path(job_dir)
        (seg / lang_files.timestamps_final(lang)).write_text(
            json.dumps([{"serial": 1, "start_sec": 0.1, "end_sec": 0.9}])
        )
        return {"status": "ok"}

    def e1(self, job_id, upload_root=None, job_dir=None):
        self.calls.append(("E1", self._seg(job_dir)))
        seg = Path(job_dir)
        (seg / "edit_guideline.json").write_text(json.dumps({"mode": "dub"}))
        return {}

    def e2(self, job_id, upload_root=None, job_dir=None, progress_cb=None):
        self.calls.append(("E2", self._seg(job_dir)))
        seg = Path(job_dir)
        (seg / "draft_video.mp4").write_bytes(b"draft")
        return {}

    def f3(self, job_id, upload_root=None, output_root=None, job_dir=None,
           output_path=None):
        self.calls.append(("F3", self._seg(job_dir)))
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_bytes(b"final")
        return {"final_path": str(output_path)}


class RunSegmentedPipelineTest(SegmentedPipelineBase):
    def test_auto_tts_runs_voiceover_chain_per_segment_in_order(self):
        self._make_job()
        self.assertGreater(len(self.plan["segments"]), 1)
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            result = segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )

        self.assertEqual(result["segmented"], True)
        self.assertEqual(result["mode"], "auto_tts")
        self.assertEqual(len(result["segments"]), len(self.plan["segments"]))
        self.assertEqual(
            [s["final_path"] for s in result["segments"]],
            [
                str(segmented_pipeline.segment_final_path(self.job_id, i))
                for i in range(len(self.plan["segments"]))
            ],
        )

        # Strictly sequential: each segment completes its whole voiceover chain
        # before the next segment's chain starts.
        expected = []
        for seg in self.plan["segments"]:
            key = segmentation.segment_key(seg["index"])
            for stage in ("D2", "D4", "E1", "E2", "F3"):
                expected.append((stage, key))
        self.assertEqual(fakes.calls, expected)

    def test_per_segment_dirs_are_scoped_and_complete(self):
        self._make_job()
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )

        lang = lang_files.target_lang(self.job_id, self.upload_root)
        for seg in self.plan["segments"]:
            seg_dir = segmentation.segment_dir(
                self.job_id, seg["index"], self.upload_root
            )
            self.assertTrue((seg_dir / "source.mp4").exists())
            self.assertTrue((seg_dir / "subtitles_zh_raw.json").exists())
            self.assertTrue((seg_dir / "subtitles_zh.json").exists())
            self.assertTrue((seg_dir / "subtitle_qa_whisper.json").exists())
            self.assertTrue((seg_dir / lang_files.subtitles_json(lang)).exists())
            self.assertTrue((seg_dir / lang_files.timestamps_auto(lang)).exists())
            self.assertTrue((seg_dir / lang_files.timestamps_final(lang)).exists())
            self.assertTrue((seg_dir / "edit_guideline.json").exists())
            self.assertTrue(
                segmented_pipeline.segment_final_path(self.job_id, seg["index"]).exists()
            )

    def test_upload_chain_only_when_voice_source_is_not_auto_tts(self):
        self._make_job()
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            result = segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )
        # No voice source chosen: voiceover chain must not run.
        self.assertNotIn("D2", [c[0] for c in fakes.calls])
        self.assertEqual(result["mode"], None)
        # But per-segment upload-chain artifacts + status exist.
        lang = lang_files.target_lang(self.job_id, self.upload_root)
        for seg in self.plan["segments"]:
            seg_dir = segmentation.segment_dir(
                self.job_id, seg["index"], self.upload_root
            )
            self.assertTrue((seg_dir / lang_files.subtitles_json(lang)).exists())
            entry = store.read_segment_status(self.job_id, seg["index"])
            self.assertEqual(entry["state"], "done")

    def test_failure_marks_segment_error_and_reraises(self):
        self._make_job()
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        fakes = VoiceoverChainFakes()

        def _explode(job_id, upload_root=None, call_budget=None, job_dir=None):
            raise RuntimeError("tts exploded")

        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=_explode,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            with self.assertRaises(RuntimeError):
                segmented_pipeline.run_segmented_pipeline(
                    self.job_id, upload_root=self.upload_root, call_budget=0
                )
        entry = store.read_segment_status(self.job_id, 0)
        self.assertEqual(entry["state"], "error")
        self.assertIn("tts exploded", entry["error_detail"])
        data = store.read_status(self.job_id)
        self.assertEqual(data["segmented"]["overall_state"], "error")
        self.assertEqual(data["state"], "error")

    def test_all_segments_marked_done_and_overall_done(self):
        self._make_job()
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fakes.d2,
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )
        data = store.read_status(self.job_id)
        self.assertEqual(data["segmented"]["completed_count"], len(self.plan["segments"]))
        self.assertEqual(data["segmented"]["overall_state"], "done")
        self.assertEqual(data["state"], "done")
        self.assertEqual(
            data["segments"]["seg_000"]["entries_count"], 5
            if self.plan["segments"][0]["entries_count"] == 5
            else self.plan["segments"][0]["entries_count"],
        )


class RunSegmentUploadChainTest(SegmentedPipelineBase):
    def test_skips_stages_whose_artifacts_exist(self):
        self._make_job()
        seg_dir = segmentation.segment_dir(self.job_id, 0, self.upload_root)
        seg_dir.mkdir(parents=True)
        lang = lang_files.target_lang(self.job_id, self.upload_root)
        (seg_dir / "subtitles_zh.json").write_text("[]")
        (seg_dir / "subtitle_qa_whisper.json").write_text("{}")
        (seg_dir / lang_files.subtitles_json(lang)).write_text("[]")

        with mock.patch.object(
            segmented_pipeline.subtitle_builder, "build_subtitle_list",
            side_effect=AssertionError("should not be called"),
        ) as b2, mock.patch.object(
            segmented_pipeline.subtitle_verify, "whisper_cross_check",
            side_effect=AssertionError("should not be called"),
        ) as wh, mock.patch.object(
            segmented_pipeline.translator, "translate_subtitles",
            side_effect=AssertionError("should not be called"),
        ) as c1:
            segmented_pipeline.run_segment_upload_chain(
                self.job_id, self.plan["segments"][0], seg_dir,
                self.upload_root, call_budget=0,
            )
        b2.assert_not_called()
        wh.assert_not_called()
        c1.assert_not_called()

    def test_runs_missing_stages_in_order(self):
        self._make_job()
        seg_dir = segmentation.materialize_segment(
            self.job_id, self.plan, self.plan["segments"][0], self.upload_root
        )
        with mock.patch.object(
            segmented_pipeline.subtitle_builder, "build_subtitle_list",
            return_value={},
        ) as b2, mock.patch.object(
            segmented_pipeline.subtitle_verify, "whisper_cross_check",
            return_value={},
        ) as wh, mock.patch.object(
            segmented_pipeline.translator, "translate_subtitles",
            return_value=[],
        ) as c1:
            segmented_pipeline.run_segment_upload_chain(
                self.job_id, self.plan["segments"][0], seg_dir,
                self.upload_root, call_budget=0,
            )
        b2.assert_called_once()
        wh.assert_called_once()
        c1.assert_called_once()
        # Stages write per-segment status.
        entry = store.read_segment_status(self.job_id, 0)
        self.assertEqual(entry["stages"]["B2_subtitles"]["state"], "done")
        self.assertEqual(entry["stages"]["whisper_cross_check"]["state"], "done")
        self.assertEqual(entry["stages"]["C1_translate"]["state"], "done")


class UserAudioPipelineTest(SegmentedPipelineBase):
    def _setup_user_audio_job(self):
        self._make_job()
        lang = lang_files.target_lang(self.job_id, self.upload_root)
        ts_name = lang_files.timestamps_upload(lang)
        audio_name = lang_files.voiceover_audio(lang)

        # Run per-segment upload chain (voice source unset -> no voiceover).
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )

        merged, seg_meta = segmented_pipeline._merge_segment_translations(
            self.job_id, self.plan, self.upload_root
        )
        _make_wav(self.job_dir / audio_name, seconds=10)
        global_ts = []
        t = 0.5
        for entry in merged:
            global_ts.append(
                {
                    "serial": entry["serial"],
                    "start_sec": t,
                    "end_sec": t + 0.8,
                }
            )
            t += 1.2
        (self.job_dir / ts_name).write_text(
            json.dumps(global_ts, ensure_ascii=False, indent=2)
        )
        self.seg_meta = seg_meta
        return lang

    def test_user_audio_slices_and_renders_each_segment(self):
        lang = self._setup_user_audio_job()
        ts_name = lang_files.timestamps_upload(lang)
        audio_name = lang_files.voiceover_audio(lang)
        fakes = VoiceoverChainFakes()
        with mock.patch.object(
            segmented_pipeline.voiceover_upload, "align_uploaded_voiceover",
            return_value={},
        ), mock.patch.object(
            segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps",
            side_effect=fakes.d4,
        ), mock.patch.object(
            segmented_pipeline.edit_guideline, "build_edit_guideline",
            side_effect=fakes.e1,
        ), mock.patch.object(
            segmented_pipeline.auto_cut, "build_draft_video", side_effect=fakes.e2,
        ), mock.patch.object(
            segmented_pipeline.render_final, "finalize_video", side_effect=fakes.f3,
        ):
            result = segmented_pipeline.run_segmented_user_audio_pipeline(
                self.job_id, upload_root=self.upload_root
            )

        self.assertEqual(result["mode"], "user_upload")
        self.assertEqual(len(result["segments"]), len(self.plan["segments"]))

        merged = json.loads(
            (self.job_dir / lang_files.subtitles_json(lang)).read_text(encoding="utf-8")
        )
        self.assertEqual(len(merged), sum(self.plan["segments"][i]["entries_count"]
                                          for i in range(len(self.plan["segments"]))))
        serials = [e["serial"] for e in merged]
        self.assertEqual(serials, list(range(1, len(merged) + 1)))

        first_global, count = self.seg_meta[0]
        seen_serials = []
        for seg in self.plan["segments"]:
            index = seg["index"]
            seg_dir = segmentation.segment_dir(self.job_id, index, self.upload_root)
            self.assertTrue((seg_dir / audio_name).exists())
            local_ts = json.loads(
                (seg_dir / ts_name).read_text(encoding="utf-8")
            )
            self.assertGreater(len(local_ts), 0)
            local_serials = [e["serial"] for e in local_ts]
            self.assertEqual(local_serials, list(range(1, len(local_ts) + 1)))
            seen_serials.extend(local_serials)
            entry = store.read_segment_status(self.job_id, index)
            self.assertEqual(entry["state"], "done")
            self.assertIn("D3_align", entry["stages"])
            self.assertIn("D4_unify", entry["stages"])
            self.assertTrue(
                segmented_pipeline.segment_final_path(self.job_id, index).exists()
            )
        # Every segment-1..N line is covered exactly once across segments.
        self.assertEqual(len(seen_serials), len(merged))
        first_serial_total = first_global
        self.assertEqual(first_serial_total, 1)

    def test_user_audio_raises_when_no_aligned_serials(self):
        self._setup_user_audio_job()
        lang = lang_files.target_lang(self.job_id, self.upload_root)
        ts_name = lang_files.timestamps_upload(lang)
        # Drop every global timestamp so the first segment has no aligned line.
        (self.job_dir / ts_name).write_text(json.dumps([]))
        with mock.patch.object(
            segmented_pipeline.voiceover_upload, "align_uploaded_voiceover",
            return_value={},
        ):
            with self.assertRaises(voiceover_unify.VoiceoverAlignmentError):
                segmented_pipeline.run_segmented_user_audio_pipeline(
                    self.job_id, upload_root=self.upload_root
                )


class SegmentFinalPathTest(SegmentedPipelineBase):
    def test_final_path_layout(self):
        p = segmented_pipeline.segment_final_path("job-x", 2)
        self.assertTrue(str(p).endswith("outputs/job-x/segments/seg_002/final_video.mp4"))
