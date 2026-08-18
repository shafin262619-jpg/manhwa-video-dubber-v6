"""Tests for the F14b Part 1 targeted per-segment correction re-run.

Verifies: the issue-tag -> owning-stage mapping, correction-instruction
assembly (issue labels + free text + previous output must all reach the
underlying model call), cascade correctness (an early-stage fix re-runs its
downstream dependents; a late-stage fix never touches earlier stages), the
isolation of one segment's re-run from every other segment, the round-number
increment in job status, and graceful recording of a failed correction
attempt (Bengali message, no crash, last-good round preserved).
"""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from pipeline import (
    edit_guideline,
    job_status as store,
    key_store,
    lang_files,
    render_final,
    segmented_pipeline,
    segmentation,
    subtitle_builder,
    subtitle_verify,
    translator,
    video_ingest,
    voiceover_auto,
    voiceover_unify,
)
from pipeline import auto_cut


def _wav_bytes(duration_sec):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "clip.wav"
        voiceover_auto._make_silence(duration_sec, path)
        return path.read_bytes()


@contextmanager
def _patch_all_stages(events, exclude=()):
    """Patch every re-run stage (except any in ``exclude``) with a recording
    fake returning ``{"status": "ok"}``. ``events`` collects the ordered stage
    names actually invoked."""
    targets = {
        "B2_subtitles": (segmented_pipeline.subtitle_builder, "build_subtitle_list"),
        "whisper_cross_check": (segmented_pipeline.subtitle_verify, "whisper_cross_check"),
        "C1_translate": (segmented_pipeline.translator, "translate_subtitles"),
        "D2_voiceover": (segmented_pipeline.voiceover_auto, "generate_auto_voiceover"),
        "D4_unify": (segmented_pipeline.voiceover_unify, "unify_voiceover_timestamps"),
        "E1_guideline": (segmented_pipeline.edit_guideline, "build_edit_guideline"),
        "E2_draft": (segmented_pipeline.auto_cut, "build_draft_video"),
        "F3_final": (segmented_pipeline.render_final, "finalize_video"),
    }
    patches = [
        mock.patch.object(
            mod, attr, lambda *a, _name=name, **k: (events.append(_name), {"status": "ok"})[1]
        )
        for name, (mod, attr) in targets.items()
        if name not in exclude
    ]
    for patch in patches:
        patch.start()
    try:
        yield
    finally:
        for patch in patches:
            patch.stop()


class SegmentRerunBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-rerun"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _make_job(self, n_segments=2):
        self.plan = {
            "job_id": self.job_id,
            "strategy": "transcript_gap",
            "target_duration_sec": 300,
            "source_duration_sec": 700,
            "segments": [
                {
                    "index": i,
                    "start_sec": i * 350.0,
                    "end_sec": (i + 1) * 350.0,
                    "duration_sec": 350.0,
                    "entries_count": 1,
                }
                for i in range(n_segments)
            ],
        }
        plan_file = segmentation.plan_path(self.job_id, upload_root=self.upload_root)
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        store.init_segments(self.job_id, self.plan, upload_root=self.upload_root)
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        (self.job_dir / "job_config.json").write_text(
            json.dumps({"job_id": self.job_id, "target_lang": "hi"}),
            encoding="utf-8",
        )

    def _materialize_seg(self, seg_index, prev_hi="আগের অনুবাদ"):
        seg_dir = segmentation.segment_dir(
            self.job_id, seg_index, upload_root=self.upload_root
        )
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "subtitles_zh_raw.json").write_text(
            json.dumps(
                {
                    "job_id": self.job_id,
                    "status": "ok",
                    "chunked": False,
                    "segments_count": 1,
                    "failed_segments": [],
                    "errors": {},
                    "subtitles": [
                        {"text": "中文", "start_sec": 0.0, "end_sec": 0.5}
                    ],
                }
            ),
            encoding="utf-8",
        )
        (seg_dir / "job_meta.json").write_text(
            json.dumps({"duration_sec": 350.0}), encoding="utf-8"
        )
        (seg_dir / "subtitles_zh.json").write_text(
            json.dumps(
                [
                    {
                        "serial": 1,
                        "text_zh": "中文",
                        "text_translated": prev_hi,
                        "start_sec": 0.0,
                        "end_sec": 0.5,
                        "translation_fallback": False,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (seg_dir / "subtitle_qa.json").write_text(
            json.dumps({"covered_duration_sec": 0.5}), encoding="utf-8"
        )
        (seg_dir / "subtitle_qa_whisper.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8"
        )
        (seg_dir / lang_files.subtitles_json("hi")).write_text(
            json.dumps(
                [
                    {
                        "serial": 1,
                        "text_zh": "中文",
                        "text_translated": prev_hi,
                        "start_sec": 0.0,
                        "end_sec": 0.5,
                        "translation_fallback": False,
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (seg_dir / lang_files.timestamps_auto("hi")).write_text(
            json.dumps(
                [
                    {
                        "serial": 1,
                        "start_sec": 0.0,
                        "end_sec": 1.0,
                        "tts_failed": False,
                    }
                ]
            ),
            encoding="utf-8",
        )
        (seg_dir / lang_files.voiceover_audio("hi")).write_bytes(b"wav")
        (seg_dir / lang_files.timestamps_final("hi")).write_text(
            json.dumps(
                [
                    {
                        "serial": 1,
                        "start_sec": 0.0,
                        "end_sec": 1.0,
                        "flagged": False,
                        "flag_reason": None,
                    }
                ]
            ),
            encoding="utf-8",
        )
        (seg_dir / "edit_guideline.json").write_text(
            json.dumps(
                [
                    {
                        "serial": 1,
                        "source_start_sec": 0.0,
                        "source_end_sec": 0.5,
                        "target_start_sec": 0.0,
                        "target_end_sec": 1.0,
                        "pts_multiplier": 2.0,
                        "flagged": False,
                        "flag_reason": None,
                    }
                ]
            ),
            encoding="utf-8",
        )
        (seg_dir / "draft_final_video.mp4").write_bytes(b"draft")
        return seg_dir

    def _mark_done(self, seg_index):
        final = segmented_pipeline.segment_final_path(self.job_id, seg_index)
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"final")
        store.mark_segment_done(
            self.job_id, seg_index, final_path=str(final),
            upload_root=self.upload_root,
        )
        return final

    def _record_review(self, seg_index, issues, notes=None, round_no=1):
        store.record_segment_review(
            self.job_id, seg_index, issues=issues, notes=notes, round_no=round_no,
            upload_root=self.upload_root,
        )


class TagToStageMappingTest(unittest.TestCase):
    def test_every_category_maps_to_an_ordered_stage(self):
        expected = {
            "bad_translation": "C1_translate",
            "subtitle_timing": "B2_subtitles",
            "tts_quality": "D2_voiceover",
            "timing_mismatch": "D4_unify",
            "audio_glitch": "E2_draft",
            "other": "C1_translate",
        }
        self.assertEqual(segmented_pipeline.ISSUE_TAG_TO_STAGE, expected)
        order = segmented_pipeline.SEGMENT_STAGE_ORDER
        for tag, stage in expected.items():
            self.assertIn(tag, store.SEGMENT_REVIEW_ISSUE_CATEGORIES)
            self.assertIn(stage, order, f"{tag} -> {stage} not in stage order")

    def test_every_review_category_is_mapped(self):
        for tag in store.SEGMENT_REVIEW_ISSUE_CATEGORIES:
            self.assertIn(tag, segmented_pipeline.ISSUE_TAG_TO_STAGE)

    def test_audio_glitch_targets_mux_not_upstream(self):
        self.assertEqual(
            segmented_pipeline.ISSUE_TAG_TO_STAGE["audio_glitch"], "E2_draft"
        )


class CorrectionInstructionAssemblyTest(SegmentRerunBase):
    def test_instruction_contains_labels_notes_and_previous_output(self):
        instruction = segmented_pipeline.build_correction_instruction(
            ["bad_translation"],
            [store.SEGMENT_REVIEW_ISSUE_CATEGORIES["bad_translation"]],
            "দ্বিতীয় লাইন ভুল",
            '{"serial": 1, "text_translated": "আগের অনুবাদ"}',
        )
        self.assertIn("ভুল বা দুর্বল অনুবাদ", instruction)
        self.assertIn("দ্বিতীয় লাইন ভুল", instruction)
        self.assertIn("আগের অনুবাদ", instruction)

    def test_translation_correction_reaches_gemini_prompt(self):
        self._make_job()
        self._materialize_seg(0, prev_hi="আগের অনুবাদ")
        self._mark_done(0)
        self._record_review(
            0, ["bad_translation"], notes="দ্বিতীয় লাইন ভুল"
        )
        prompts = []
        events = []

        def fake_gemini(key, prompt):
            prompts.append(prompt)
            return "নতুন অনুবাদ"

        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(translator, "_call_gemini_text", side_effect=fake_gemini)
        ), _patch_all_stages(events, exclude=("C1_translate",)):
            # C1 keeps its real implementation so the correction hint flows
            # through the real prompt builder; only the Gemini call is faked.
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["target_stage"], "C1_translate")
        self.assertTrue(prompts, "no Gemini prompt was captured")
        prompt = prompts[0]
        self.assertIn("ভুল বা দুর্বল অনুবাদ", prompt)
        self.assertIn("দ্বিতীয় লাইন ভুল", prompt)
        self.assertIn("আগের অনুবাদ", prompt)
        self.assertIn("CORRECTION", prompt)

    def test_tts_stage_forwards_hint_and_disables_clip_reuse(self):
        self._make_job()
        seg_dir = self._materialize_seg(0)
        clips_dir = seg_dir / voiceover_auto.CLIP_DIR_NAME
        clips_dir.mkdir(exist_ok=True)
        (clips_dir / "serial_1.wav").write_bytes(_wav_bytes(3.0))
        hints = []

        def fake_tts(key, text, voice_name, correction_hint=None):
            hints.append(correction_hint)
            return _wav_bytes(3.0)

        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(voiceover_auto, "_call_tts", side_effect=fake_tts)
        ):
            result = voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=self.upload_root, job_dir=seg_dir,
                correction_hint="ভুল বা দুর্বল উচ্চারণ\nআগের টাইমিং",
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(hints, ["ভুল বা দুর্বল উচ্চারণ\nআগের টাইমিং"])

    def test_tts_without_hint_reuses_existing_clip(self):
        self._make_job()
        seg_dir = self._materialize_seg(0)
        clips_dir = seg_dir / voiceover_auto.CLIP_DIR_NAME
        clips_dir.mkdir(exist_ok=True)
        (clips_dir / "serial_1.wav").write_bytes(_wav_bytes(3.0))
        calls = []

        def fake_tts(key, text, voice_name, correction_hint=None):
            calls.append(correction_hint)
            return _wav_bytes(3.0)

        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(voiceover_auto, "_call_tts", side_effect=fake_tts)
        ):
            result = voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=self.upload_root, job_dir=seg_dir,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls, [], "existing healthy clip should be reused")

    def test_d2_correction_string_carries_labels_notes_previous(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, ["tts_quality"], notes="উচ্চারণ স্পষ্ট নয়")
        seen = {}

        def fake_d2(job_id, upload_root=None, call_budget=None, job_dir=None,
                    correction_hint=None):
            seen["hint"] = correction_hint
            return {"status": "ok"}

        with mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=fake_d2,
        ), _patch_all_stages([], exclude=("D2_voiceover",)):
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        self.assertEqual(result["target_stage"], "D2_voiceover")
        hint = seen.get("hint")
        self.assertIsNotNone(hint)
        self.assertIn("উচ্চারণ বা ভয়েস কোয়ালিটি সমস্যা", hint)
        self.assertIn("উচ্চারণ স্পষ্ট নয়", hint)
        self.assertIn("tts_failed", hint)
        self.assertIn('"serial"', hint)


class CascadeCorrectnessTest(SegmentRerunBase):
    def test_bad_translation_reruns_c1_and_downstream_not_upstream(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, ["bad_translation"])
        events = []
        with _patch_all_stages(events):
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["target_stage"], "C1_translate")
        self.assertEqual(
            events,
            ["C1_translate", "D2_voiceover", "D4_unify", "E1_guideline",
             "E2_draft", "F3_final"],
        )

    def test_audio_glitch_reruns_e2_and_f3_only(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, ["audio_glitch"])
        events = []
        with _patch_all_stages(events):
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        self.assertEqual(result["target_stage"], "E2_draft")
        self.assertEqual(events, ["E2_draft", "F3_final"])

    def test_subtitle_timing_reruns_every_downstream_stage(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, ["subtitle_timing"])
        events = []
        with _patch_all_stages(events):
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        self.assertEqual(result["target_stage"], "B2_subtitles")
        self.assertEqual(
            events,
            ["B2_subtitles", "whisper_cross_check", "C1_translate",
             "D2_voiceover", "D4_unify", "E1_guideline", "E2_draft", "F3_final"],
        )

    def test_multi_tag_targets_earliest_stage(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(
            0, ["audio_glitch", "bad_translation"], notes="both"
        )
        events = []
        with _patch_all_stages(events):
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        self.assertEqual(result["target_stage"], "C1_translate")
        self.assertEqual(events[0], "C1_translate")
        self.assertIn("E2_draft", events)

    def test_other_tag_falls_back_to_translation_forward(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, ["other"], notes="দৃশ্য ঠিক নয়")
        events = []
        with _patch_all_stages(events):
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        self.assertEqual(result["target_stage"], "C1_translate")
        self.assertEqual(
            events,
            ["C1_translate", "D2_voiceover", "D4_unify", "E1_guideline",
             "E2_draft", "F3_final"],
        )


class IsolationAndRoundsTest(SegmentRerunBase):
    def test_rerun_does_not_touch_other_segments(self):
        self._make_job(n_segments=2)
        self._materialize_seg(0)
        self._materialize_seg(1)
        self._mark_done(0)
        self._mark_done(1)
        store.write_segment_status(
            self.job_id, 1, "C1_translate", "running",
            upload_root=self.upload_root,
        )
        self._record_review(0, ["bad_translation"])
        with _patch_all_stages([]):
            segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        seg1 = store.read_segment_status(
            self.job_id, 1, upload_root=self.upload_root
        )
        self.assertEqual(seg1["state"], "running")
        self.assertEqual(
            seg1["stages"]["C1_translate"]["state"], "running"
        )
        self.assertEqual(
            store.get_segment_reviews(self.job_id, 1, upload_root=self.upload_root),
            {},
        )
        seg0 = store.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(seg0["state"], "done")

    def test_round_number_increments_after_rerun(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, ["bad_translation"], round_no=1)
        self.assertEqual(
            store.next_review_round(self.job_id, 0, upload_root=self.upload_root),
            2,
        )
        with _patch_all_stages([]):
            segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        round2 = store.get_segment_reviews(
            self.job_id, 0, round_no=2, upload_root=self.upload_root
        )
        self.assertIsNotNone(round2)
        self.assertEqual(round2["round"], 2)
        self.assertTrue(round2["rerun"])
        self.assertEqual(round2["rerun_of_round"], 1)
        self.assertEqual(round2["rerun_status"], "ok")
        self.assertEqual(round2["target_stage"], "C1_translate")
        self.assertEqual(round2["issues"], ["bad_translation"])
        self.assertEqual(
            store.next_review_round(self.job_id, 0, upload_root=self.upload_root),
            3,
        )

    def test_rerun_does_not_autoloop(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, ["bad_translation"], round_no=1)
        with _patch_all_stages([]):
            segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )
        reviews = store.get_segment_reviews(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(sorted(reviews.keys()), ["1", "2"])


class FailedCorrectionTest(SegmentRerunBase):
    def test_failed_attempt_recorded_gracefully_and_state_preserved(self):
        self._make_job()
        self._materialize_seg(0)
        final = self._mark_done(0)
        self._record_review(0, ["bad_translation"], notes="ভুল অনুবাদ")

        def boom(*args, **kwargs):
            raise RuntimeError("synthetic tts failure")

        with _patch_all_stages([], exclude=("D2_voiceover",)), mock.patch.object(
            segmented_pipeline.voiceover_auto, "generate_auto_voiceover",
            side_effect=boom,
        ):
            result = segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )

        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error_bn"])
        self.assertTrue(
            any("\u0980" <= ch <= "\u09FF" for ch in result["error_bn"]),
            "failure message must be Bengali",
        )

        round2 = store.get_segment_reviews(
            self.job_id, 0, round_no=2, upload_root=self.upload_root
        )
        self.assertIsNotNone(round2)
        self.assertTrue(round2["rerun"])
        self.assertEqual(round2["rerun_status"], "failed")
        self.assertTrue(round2["rerun_error_bn"])
        self.assertIn("correction", round2)

        seg0 = store.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(seg0["state"], "done")
        self.assertEqual(seg0["final_path"], str(final))
        round1 = store.get_segment_reviews(
            self.job_id, 0, round_no=1, upload_root=self.upload_root
        )
        self.assertEqual(round1["issues"], ["bad_translation"])


class RerunValidationTest(SegmentRerunBase):
    def test_no_review_round_raises(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        with self.assertRaises(ValueError):
            segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )

    def test_clean_review_raises(self):
        self._make_job()
        self._materialize_seg(0)
        self._mark_done(0)
        self._record_review(0, [])
        with self.assertRaises(ValueError):
            segmented_pipeline.rerun_segment_stage(
                self.job_id, 0, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )

    def test_unknown_segment_raises(self):
        self._make_job()
        self._record_review(0, ["bad_translation"])
        with self.assertRaises(FileNotFoundError):
            segmented_pipeline.rerun_segment_stage(
                self.job_id, 5, round_no=1,
                upload_root=self.upload_root, call_budget=None,
            )


if __name__ == "__main__":
    unittest.main()
