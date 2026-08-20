"""Tests for the F14b Part 2 automated Gemini pre-review QA gate.

Verifies: the gate runs before a segment's done-transition; an all-clear
first check lets a segment proceed directly to ready-for-review with zero fix
attempts; a mismatch triggers exactly one targeted re-run (through
``rerun_segment_stage`` with a correctly shaped ``timing_mismatch`` payload)
before passing; repeated mismatches cap at ``config.MAX_AUTO_QA_FIX_ATTEMPTS``
with the Bengali note attached; a Gemini API failure counts as a failed
attempt without crashing the segment; the QA state is recorded in the
segment's job-status ``qa`` block; one segment's gate never touches another
segment's state; and F14a's review UI surfaces the capped note while still
showing the human review form after an auto-QA rerun. Every Gemini call is
mocked — no real API calls.
"""

import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import (
    config,
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


def _pass_lines(n):
    return [{"serial": i + 1, "pass": True} for i in range(n)]


def _mismatch_lines(*serials, reason="voice/scene mismatch"):
    return [
        {"serial": s, "pass": False, "reason": reason} for s in serials
    ]


def _fake_gemini(responses, checks=None):
    """A ``call_with_rotation`` fake serving responses one per check round.

    A ``{"error": {...}}`` entry simulates a Gemini API failure; anything
    else is treated as the parsed ``lines`` list returned by the check.
    """
    if checks is None:
        checks = []
    queue = list(responses)

    def fake(keys, rotation, callable_, *args, call_budget=None, logger_=None):
        checks.append(rotation)
        item = queue.pop(0)
        if isinstance(item, dict) and "error" in item:
            return None, (rotation + 1) % len(keys), item["error"]
        return item, (rotation + 1) % len(keys), None

    return fake


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


class AutoQaGateBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-qa"
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
                    "entries_count": 2,
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

    def _materialize_seg(self, seg_index, n_lines=2):
        seg_dir = segmentation.segment_dir(
            self.job_id, seg_index, upload_root=self.upload_root
        )
        seg_dir.mkdir(parents=True, exist_ok=True)
        entries = [
            {
                "serial": i + 1,
                "text_zh": f"中文{i + 1}",
                "text_translated": f"bangla line {i + 1}",
                "start_sec": i * 2.0,
                "end_sec": i * 2.0 + 1.0,
                "translation_fallback": False,
            }
            for i in range(n_lines)
        ]
        (seg_dir / "source.mp4").write_bytes(b"seg-source")
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
                        {"text": entries[i]["text_zh"],
                         "start_sec": entries[i]["start_sec"],
                         "end_sec": entries[i]["end_sec"]}
                        for i in range(n_lines)
                    ],
                }
            ),
            encoding="utf-8",
        )
        (seg_dir / "subtitles_zh.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        (seg_dir / "subtitle_qa_whisper.json").write_text(
            json.dumps({"status": "ok"}), encoding="utf-8"
        )
        (seg_dir / lang_files.subtitles_json("hi")).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        (seg_dir / lang_files.timestamps_auto("hi")).write_text(
            json.dumps(
                [
                    {
                        "serial": i + 1,
                        "start_sec": i * 2.0,
                        "end_sec": i * 2.0 + 1.0,
                        "tts_failed": False,
                    }
                    for i in range(n_lines)
                ]
            ),
            encoding="utf-8",
        )
        (seg_dir / lang_files.timestamps_final("hi")).write_text(
            json.dumps(
                [
                    {
                        "serial": i + 1,
                        "start_sec": i * 2.0,
                        "end_sec": i * 2.0 + 1.0,
                        "flagged": False,
                        "flag_reason": None,
                    }
                    for i in range(n_lines)
                ]
            ),
            encoding="utf-8",
        )
        (seg_dir / "edit_guideline.json").write_text(json.dumps([]), encoding="utf-8")
        (seg_dir / "draft_final_video.mp4").write_bytes(b"draft")
        final = segmented_pipeline.segment_final_path(self.job_id, seg_index)
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"final")
        return seg_dir

    def _run_gate(self, seg_index, responses, checks=None):
        """Run the gate with a mocked Gemini check; returns (result, seg_dir)."""
        seg_dir = self._materialize_seg(seg_index)
        final_path = segmented_pipeline.segment_final_path(self.job_id, seg_index)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(responses, checks),
            )
        ):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][seg_index], seg_dir,
                final_path, upload_root=self.upload_root, call_budget=None,
            )
        return result, seg_dir


class AutoQaGateBehaviorTest(AutoQaGateBase):
    def setUp(self):
        super().setUp()
        self._make_job()

    def test_all_clear_first_check_passes_with_zero_fixes(self):
        result, _ = self._run_gate(0, [_pass_lines(2)])
        self.assertEqual(result["qa_state"], store.SEGMENT_QA_PASSED)
        self.assertIsNone(result["note_bn"])
        qa = store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root)
        self.assertEqual(qa["state"], store.SEGMENT_QA_PASSED)
        self.assertEqual(len(qa["attempts"]), 1)
        self.assertEqual(qa["attempts"][0]["outcome"], "pass")
        self.assertIs(qa["attempts"][0]["fixed"], False)
        entry = store.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(entry["qa"]["state"], store.SEGMENT_QA_PASSED)

    def test_mismatch_then_pass_one_fix_and_correct_payload(self):
        rerun_calls = []

        def fake_rerun(job_id, seg_index, round_no=None, review=None,
                       upload_root=None, call_budget=None, logger_=None):
            rerun_calls.append(review)
            return {"rerun": True, "status": "ok", "seg_index": seg_index,
                    "round": 2, "target_stage": "D4_unify",
                    "stages_rerun": ["D4_unify", "E1_guideline", "E2_draft", "F3_final"],
                    "correction": "corr"}

        seg_dir = self._materialize_seg(0)
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(
                    [
                        _mismatch_lines(1, reason="audio says A but scene shows B"),
                        _pass_lines(2),
                    ]
                ),
            )
        ), mock.patch.object(
            segmented_pipeline, "rerun_segment_stage", side_effect=fake_rerun
        ):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0], seg_dir, final_path,
                upload_root=self.upload_root, call_budget=None,
            )

        self.assertEqual(result["qa_state"], store.SEGMENT_QA_PASSED)
        self.assertIsNone(result["note_bn"])
        self.assertEqual(len(rerun_calls), 1, "exactly one correction re-run")
        review = rerun_calls[0]
        self.assertEqual(review["round"], 1)
        self.assertEqual(review["issues"], ["timing_mismatch"])
        self.assertIn("serial 1", review["notes"])
        self.assertIn("audio says A but scene shows B", review["notes"])

        qa = store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root)
        self.assertEqual(qa["state"], store.SEGMENT_QA_PASSED)
        self.assertEqual([a["outcome"] for a in qa["attempts"]],
                         ["mismatch", "pass"])
        self.assertEqual([a["fixed"] for a in qa["attempts"]], [True, False])
        self.assertEqual(qa["attempts"][0]["issues"], [1])

    def test_repeated_mismatch_caps_at_max_fix_attempts(self):
        rerun_calls = []

        def fake_rerun(job_id, seg_index, round_no=None, review=None,
                       upload_root=None, call_budget=None, logger_=None):
            rerun_calls.append(review)
            return {"rerun": True, "status": "ok", "seg_index": seg_index,
                    "round": 2, "target_stage": "D4_unify", "stages_rerun": [],
                    "correction": "corr"}

        max_attempts = config.MAX_AUTO_QA_FIX_ATTEMPTS
        responses = [_mismatch_lines(1, reason="persistent")] * (max_attempts + 1)
        seg_dir = self._materialize_seg(0)
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(responses),
            )
        ), mock.patch.object(
            segmented_pipeline, "rerun_segment_stage", side_effect=fake_rerun
        ):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0], seg_dir, final_path,
                upload_root=self.upload_root, call_budget=None,
            )

        self.assertEqual(result["qa_state"], store.SEGMENT_QA_CAPPED)
        self.assertEqual(result["note_bn"], store.SEGMENT_QA_CAP_NOTE_BN)
        self.assertEqual(
            len(rerun_calls), max_attempts,
            f"exactly {max_attempts} fix attempts, not more",
        )
        qa = store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root)
        self.assertEqual(qa["state"], store.SEGMENT_QA_CAPPED)
        self.assertEqual(qa["note_bn"], store.SEGMENT_QA_CAP_NOTE_BN)
        self.assertEqual(
            [a["outcome"] for a in qa["attempts"]],
            ["mismatch"] * (max_attempts + 1),
        )
        self.assertEqual(
            [a["fixed"] for a in qa["attempts"]],
            [True] * max_attempts + [False],
        )

    def test_gemini_failure_counts_toward_cap_without_crashing(self):
        checks = []
        # A non-rate-limit failure (transient/network) MUST still count toward
        # the cap — the old behavior.
        error = {"type": "transient", "message": "timeout"}
        responses = [{"error": error}] * config.MAX_AUTO_QA_FIX_ATTEMPTS
        seg_dir = self._materialize_seg(0)
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(responses, checks),
            )
        ):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0], seg_dir, final_path,
                upload_root=self.upload_root, call_budget=None,
            )

        self.assertEqual(result["qa_state"], store.SEGMENT_QA_CAPPED)
        qa = store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root)
        self.assertEqual(qa["state"], store.SEGMENT_QA_CAPPED)
        self.assertEqual(len(checks), config.MAX_AUTO_QA_FIX_ATTEMPTS)
        self.assertEqual(
            [a["outcome"] for a in qa["attempts"]],
            ["failed"] * config.MAX_AUTO_QA_FIX_ATTEMPTS,
        )
        for attempt in qa["attempts"]:
            self.assertTrue(attempt["error_bn"])
            self.assertTrue(
                any("\u0980" <= ch <= "\u09FF" for ch in attempt["error_bn"]),
                "failure message must be Bengali",
            )
        # Segment must remain processable (not crashed / errored).
        entry = store.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertNotEqual(entry["state"], "error")

    def test_rate_limit_returns_api_limit_wait_and_does_not_count_fix(self):
        """F15 Part 3: a rate-limit error returns api_limit_wait, not cap."""
        seg_dir = self._materialize_seg(0)
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(
                    [{"error": {"type": "rate_limit", "message": "429 quota"}}]
                ),
            )
        ):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0], seg_dir, final_path,
                upload_root=self.upload_root, call_budget=None,
            )

        self.assertEqual(result["qa_state"], store.SEGMENT_QA_API_LIMIT_WAIT)
        self.assertTrue(result["note_bn"])
        # The qa block must reflect the wait.
        qa = store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root)
        self.assertEqual(qa["state"], store.SEGMENT_QA_API_LIMIT_WAIT)
        # No fix attempts were consumed.
        self.assertNotIn("attempts", qa)
        # The segment-scoped api_limit_wait block must exist.
        block = store.get_segment_api_limit_wait(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertIsNotNone(block)
        self.assertEqual(block["stage"], "qa_gate")
        self.assertEqual(block["attempt_count"], 1)

    def test_failed_check_then_pass_resumes_normally(self):
        error = {"type": "transient", "message": "timeout"}
        seg_dir = self._materialize_seg(0)
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(
                    [{"error": error}, _pass_lines(2)]
                ),
            )
        ):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0], seg_dir, final_path,
                upload_root=self.upload_root, call_budget=None,
            )
        self.assertEqual(result["qa_state"], store.SEGMENT_QA_PASSED)
        qa = store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root)
        self.assertEqual(
            [a["outcome"] for a in qa["attempts"]], ["failed", "pass"]
        )

    def test_gate_skipped_when_no_final_video(self):
        self._materialize_seg(0)
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        final_path.unlink()
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0],
                segmentation.segment_dir(self.job_id, 0, self.upload_root),
                final_path, upload_root=self.upload_root,
            )
        self.assertIsNone(result["qa_state"])
        self.assertEqual(
            store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root),
            {},
        )


class AutoQaGateIsolationTest(AutoQaGateBase):
    def test_gate_on_one_segment_never_touches_another(self):
        self._make_job()
        rerun_calls = []

        def fake_rerun(job_id, seg_index, round_no=None, review=None,
                       upload_root=None, call_budget=None, logger_=None):
            rerun_calls.append(review)
            return {"rerun": True, "status": "ok", "seg_index": seg_index,
                    "round": 2, "target_stage": "D4_unify", "stages_rerun": [],
                    "correction": "corr"}

        max_attempts = config.MAX_AUTO_QA_FIX_ATTEMPTS
        responses = [_mismatch_lines(1)] * (max_attempts + 1)
        seg_dir = self._materialize_seg(0)
        self._materialize_seg(1)
        store.write_segment_status(
            self.job_id, 1, "C1_translate", "running",
            upload_root=self.upload_root,
        )
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(responses),
            )
        ), mock.patch.object(
            segmented_pipeline, "rerun_segment_stage", side_effect=fake_rerun
        ):
            segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0], seg_dir, final_path,
                upload_root=self.upload_root, call_budget=None,
            )
        # Segment 0 finished its (capped) gate; segment 1 is untouched.
        seg1 = store.read_segment_status(
            self.job_id, 1, upload_root=self.upload_root
        )
        self.assertEqual(seg1["state"], "running")
        self.assertEqual(seg1["stages"]["C1_translate"]["state"], "running")
        self.assertEqual(
            store.get_segment_qa(self.job_id, 1, upload_root=self.upload_root), {}
        )
        seg0 = store.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(seg0["qa"]["state"], store.SEGMENT_QA_CAPPED)


class AutoQaRealRerunIntegrationTest(AutoQaGateBase):
    def test_real_rerun_path_records_round_and_fixes_once(self):
        self._make_job()
        events = []
        seg_dir = self._materialize_seg(0)
        final_path = segmented_pipeline.segment_final_path(self.job_id, 0)
        with mock.patch.object(key_store, "get_active_keys", return_value=["k1"]), (
            mock.patch.object(
                segmented_pipeline.subtitle_extract, "call_with_rotation",
                side_effect=_fake_gemini(
                    [_mismatch_lines(1, reason="bad timing"), _pass_lines(2)]
                ),
            )
        ), _patch_all_stages(events):
            result = segmented_pipeline.run_auto_qa_gate(
                self.job_id, self.plan["segments"][0], seg_dir, final_path,
                upload_root=self.upload_root, call_budget=None,
            )

        self.assertEqual(result["qa_state"], store.SEGMENT_QA_PASSED)
        self.assertEqual(
            events,
            ["D4_unify", "E1_guideline", "E2_draft", "F3_final"],
            "timing_mismatch -> D4_unify cascade, nothing upstream",
        )
        round1 = store.get_segment_reviews(
            self.job_id, 0, round_no=1, upload_root=self.upload_root
        )
        self.assertIsNotNone(round1)
        self.assertTrue(round1["rerun"])
        self.assertEqual(round1["issues"], ["timing_mismatch"])
        self.assertEqual(round1["target_stage"], "D4_unify")
        self.assertIn("ভয়েস ও দৃশ্যের টাইমিং মিসম্যাচ", round1["correction"])
        self.assertIn("bad timing", round1["correction"])
        # After the fix + re-check pass, the segment is done.
        entry = store.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(entry["state"], "done")
        self.assertEqual(entry["qa"]["state"], store.SEGMENT_QA_PASSED)


class RunSegmentedPipelineQaTest(AutoQaGateBase):
    def test_pipeline_runs_gate_before_marking_segment_done(self):
        self._make_job()
        # Pre-materialize every segment so materialize + the upload/voiceover
        # chains short-circuit on existing artifacts and only the QA gate
        # actually runs inside run_segmented_pipeline.
        for seg in self.plan["segments"]:
            self._materialize_seg(seg["index"])

        checks = []
        n_segs = len(self.plan["segments"])
        with mock.patch.object(
            key_store, "get_active_keys", return_value=["k1"]
        ), mock.patch.object(
            segmented_pipeline.subtitle_extract, "call_with_rotation",
            side_effect=_fake_gemini([_pass_lines(2)] * n_segs, checks),
        ):
            result = segmented_pipeline.run_segmented_pipeline(
                self.job_id, upload_root=self.upload_root, call_budget=0
            )
        self.assertEqual(result["segmented"], True)
        self.assertEqual(len(checks), n_segs,
                         "gate ran once per segment")
        for seg in self.plan["segments"]:
            entry = store.read_segment_status(
                self.job_id, seg["index"], upload_root=self.upload_root
            )
            self.assertEqual(entry["state"], "done")
            self.assertEqual(entry["qa"]["state"], store.SEGMENT_QA_PASSED)


class JobStatusQaSchemaTest(AutoQaGateBase):
    def test_record_and_read_qa_block(self):
        self._make_job()
        store.record_segment_qa(
            self.job_id, 0, store.SEGMENT_QA_CHECKING, upload_root=self.upload_root
        )
        store.record_segment_qa(
            self.job_id, 0, "qa_fixing_attempt_1", attempt=1, outcome="mismatch",
            issues=[2], fixed=True, upload_root=self.upload_root,
        )
        store.record_segment_qa(
            self.job_id, 0, store.SEGMENT_QA_PASSED, attempt=2, outcome="pass",
            fixed=False, upload_root=self.upload_root,
        )
        qa = store.get_segment_qa(self.job_id, 0, upload_root=self.upload_root)
        self.assertEqual(qa["state"], store.SEGMENT_QA_PASSED)
        self.assertEqual(len(qa["attempts"]), 2)
        self.assertEqual(qa["attempts"][0]["attempt"], 1)
        self.assertEqual(qa["attempts"][0]["outcome"], "mismatch")
        self.assertEqual(qa["attempts"][0]["issues"], [2])
        self.assertIs(qa["attempts"][0]["fixed"], True)
        self.assertIn("at", qa["attempts"][0])
        # Other segment untouched.
        self.assertEqual(
            store.get_segment_qa(self.job_id, 1, upload_root=self.upload_root), {}
        )

    def test_invalid_attempt_raises(self):
        self._make_job()
        with self.assertRaises(ValueError):
            store.record_segment_qa(
                self.job_id, 0, store.SEGMENT_QA_CHECKING, attempt=0,
                upload_root=self.upload_root,
            )


class AutoQaUiTest(AutoQaGateBase):
    def setUp(self):
        super().setUp()
        self._make_job()
        self.client = TestClient(app)

    def _mark_done(self, seg_index):
        final = segmented_pipeline.segment_final_path(self.job_id, seg_index)
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(b"video-bytes")
        store.mark_segment_done(
            self.job_id, seg_index, final_path=str(final),
            upload_root=self.upload_root,
        )

    def test_capped_note_visible_and_review_form_still_present(self):
        store.record_segment_qa(
            self.job_id, 0, store.SEGMENT_QA_CAPPED,
            note_bn=store.SEGMENT_QA_CAP_NOTE_BN, upload_root=self.upload_root,
        )
        self._mark_done(0)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(store.SEGMENT_QA_CAP_NOTE_BN, res.text)
        self.assertIn('name="verdict" value="issues"', res.text)
        self.assertIn('name="verdict" value="clean"', res.text)

    def test_auto_rerun_round_one_still_shows_human_form(self):
        store.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["timing_mismatch"],
            target_stage="D4_unify", upload_root=self.upload_root,
        )
        self._mark_done(0)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn('name="verdict" value="issues"', res.text)
        self.assertIn("সেগমেন্ট seg_000 — রিভিউ", res.text)


if __name__ == "__main__":
    unittest.main()
