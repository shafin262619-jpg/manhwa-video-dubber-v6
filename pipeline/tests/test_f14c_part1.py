"""F14c Part 1 tests: all-segments-done-reviewing trigger + final video assembly.

Covers ``all_segments_review_complete`` (per-segment done-reviewing on the
latest round; mixed round numbers; single-segment jobs; partially-processed
jobs return False, never error), ``assemble_final_video`` (ordered concat of
each segment's LATEST-round output, not round 1 for all), the automatic
trigger right after the clean review that completes the last segment,
re-assembly after a new issue reverts the job, and graceful Bengali failure
when a segment's video file is missing.
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
from app import app
from pipeline import (
    job_status as store,
    render_final,
    segmented_pipeline,
    segmentation,
    video_ingest,
)


def _require_tools():
    if not (subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0
            and subprocess.run(["which", "ffprobe"], capture_output=True).returncode == 0):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _make_video(path, seconds=1):
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


def _probe_duration(path):
    return video_ingest.probe_video(path)["duration_sec"]


class F14cPart1Base(unittest.TestCase):
    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.output_root = Path(self._tmp) / "outputs"
        self.job_id = "job-f14c"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        render_final.OUTPUT_ROOT = self.output_root

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _make_job(self, n_segments=2):
        plan = {
            "job_id": self.job_id,
            "strategy": "transcript_gap",
            "target_duration_sec": 300,
            "source_duration_sec": 700,
            "segments": [
                {
                    "index": i,
                    "start_sec": i * 10.0,
                    "end_sec": (i + 1) * 10.0,
                    "duration_sec": 10.0,
                    "entries_count": 5,
                }
                for i in range(n_segments)
            ],
        }
        plan_path = segmentation.plan_path(self.job_id, upload_root=self.upload_root)
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_path.write_text(json.dumps(plan), encoding="utf-8")
        store.init_segments(self.job_id, plan, upload_root=self.upload_root)
        return plan

    def _segment_video(self, seg_index, seconds):
        path = segmented_pipeline.segment_final_path(
            self.job_id, seg_index, output_root=self.output_root
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        _make_video(path, seconds=seconds)
        return path

    def _mark_done_with_video(self, seg_index, seconds=1):
        path = self._segment_video(seg_index, seconds)
        store.mark_segment_done(
            self.job_id, seg_index, final_path=str(path),
            upload_root=self.upload_root,
        )
        return path

    def _mark_done_no_video(self, seg_index):
        missing = self.output_root / "missing" / f"seg_{seg_index:03d}.mp4"
        store.mark_segment_done(
            self.job_id, seg_index, final_path=str(missing),
            upload_root=self.upload_root,
        )

    def _clean_review(self, seg_index):
        round_no = store.next_review_round(
            self.job_id, seg_index, upload_root=self.upload_root
        )
        store.record_segment_review(
            self.job_id, seg_index, round_no=round_no, issues=[],
            upload_root=self.upload_root,
        )

    def _issue_review(self, seg_index, issue="audio_glitch"):
        round_no = store.next_review_round(
            self.job_id, seg_index, upload_root=self.upload_root
        )
        store.record_segment_review(
            self.job_id, seg_index, round_no=round_no, issues=[issue],
            upload_root=self.upload_root,
        )
        return round_no


class AllSegmentsReviewCompleteTest(F14cPart1Base):
    def test_job_without_segments_is_false(self):
        self.assertFalse(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )

    def test_never_reviewed_segments_is_false(self):
        self._make_job(2)
        self._mark_done_with_video(0)
        self._mark_done_with_video(1)
        self.assertFalse(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )

    def test_one_clean_one_unreviewed_is_false(self):
        self._make_job(2)
        self._mark_done_with_video(0)
        self._clean_review(0)
        self._mark_done_with_video(1)
        self.assertFalse(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )

    def test_all_clean_is_true(self):
        self._make_job(2)
        for index in (0, 1):
            self._mark_done_with_video(index)
            self._clean_review(index)
        self.assertTrue(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )

    def test_mixed_round_numbers_all_clean_is_true(self):
        self._make_job(2)
        # seg 0 went through a fix loop: issue round 1, rerun round 2, clean round 3.
        self._mark_done_with_video(0)
        store.record_segment_review(
            self.job_id, 0, round_no=1, issues=["timing_mismatch"],
            upload_root=self.upload_root,
        )
        store.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["timing_mismatch"],
            target_stage="D4_unify", status=store.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        self._mark_done_with_video(0)
        store.record_segment_review(
            self.job_id, 0, round_no=3, issues=[],
            upload_root=self.upload_root,
        )
        # seg 1 reviewed clean once (round 1).
        self._mark_done_with_video(1)
        self._clean_review(1)
        self.assertTrue(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )
        self.assertEqual(
            store.segment_latest_review_rounds(self.job_id, self.upload_root),
            {"seg_000": 3, "seg_001": 1},
        )

    def test_latest_round_being_a_rerun_is_false(self):
        self._make_job(1)
        self._mark_done_with_video(0)
        self._issue_review(0)
        store.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["audio_glitch"],
            target_stage="E2_draft", status=store.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        self.assertFalse(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )

    def test_latest_round_reporting_issues_is_false(self):
        self._make_job(1)
        self._mark_done_with_video(0)
        self._issue_review(0)
        self.assertFalse(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )

    def test_single_segment_job(self):
        self._make_job(1)
        self._mark_done_with_video(0)
        self.assertFalse(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )
        self._clean_review(0)
        self.assertTrue(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )


class AssembleFinalVideoTest(F14cPart1Base):
    def test_concatenates_latest_round_outputs_in_order(self):
        self._make_job(2)
        # seg 0 ends up on round 3 after fixes; seg 1 is round 1.
        self._mark_done_with_video(0, seconds=1)
        store.record_segment_review(
            self.job_id, 0, round_no=1, issues=["timing_mismatch"],
            upload_root=self.upload_root,
        )
        store.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["timing_mismatch"],
            target_stage="D4_unify", status=store.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        self._mark_done_with_video(0, seconds=1)
        store.record_segment_review(
            self.job_id, 0, round_no=3, issues=[],
            upload_root=self.upload_root,
        )
        self._mark_done_with_video(1, seconds=2)
        self._clean_review(1)

        result = segmented_pipeline.assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["segment_count"], 2)
        self.assertEqual(result["segment_rounds"], {"seg_000": 3, "seg_001": 1})

        final = Path(result["final_path"])
        self.assertTrue(final.exists())
        self.assertEqual(final, render_final.final_video_path(self.job_id))
        duration = _probe_duration(final)
        self.assertIsNotNone(duration)
        self.assertLessEqual(abs(duration - 3.0), 0.2)

    def test_single_segment_job_assembles_that_segment(self):
        self._make_job(1)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        result = segmented_pipeline.assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["segment_count"], 1)
        duration = _probe_duration(Path(result["final_path"]))
        self.assertLessEqual(abs(duration - 1.0), 0.2)


class TriggerTimingTest(F14cPart1Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def _clean_review_via_ui(self, seg_index):
        res = self.client.post(
            f"/segment-review/{self.job_id}/{seg_index}",
            data={"verdict": "clean"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)

    def test_no_assembly_until_last_segment_reviewed(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review_via_ui(0)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertNotEqual(
            data["segmented"].get("review_state"),
            store.SEGMENT_REVIEW_FINAL_READY,
        )
        self.assertFalse(
            (self.output_root / self.job_id / "final_video.mp4").exists()
        )

    def test_clean_review_completing_last_segment_assembles_automatically(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review_via_ui(0)
        self._mark_done_with_video(1, seconds=2)
        self._clean_review_via_ui(1)

        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_FINAL_READY
        )
        assembly = data["segmented"]["final_assembly"]
        self.assertEqual(assembly["state"], store.SEGMENT_ASSEMBLY_READY)
        self.assertEqual(assembly["version"], 1)
        self.assertEqual(assembly["segment_rounds"], {"seg_000": 1, "seg_001": 1})
        final = Path(assembly["final_path"])
        self.assertTrue(final.exists())
        duration = _probe_duration(final)
        self.assertLessEqual(abs(duration - 3.0), 0.2)

    def test_assembly_is_idempotent_for_current_rounds(self):
        self._make_job(2)
        for index in (0, 1):
            self._mark_done_with_video(index)
            self._clean_review(index)
        first = segmented_pipeline.maybe_assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertEqual(first["status"], "ok")
        final = Path(first["final_path"])
        first_size = final.stat().st_size
        second = segmented_pipeline.maybe_assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertEqual(second["status"], "already_ready")
        self.assertEqual(final.stat().st_size, first_size)

    def test_no_assembly_while_another_segment_mid_fix(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review_via_ui(0)
        # seg 1 mid-fix: issue round + rerun round (latest is a rerun).
        self._mark_done_with_video(1, seconds=2)
        self._issue_review(1)
        store.record_segment_rerun(
            self.job_id, 1, triggered_by_round=1, issues=["audio_glitch"],
            target_stage="E2_draft", status=store.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        self.assertFalse(
            store.all_segments_review_complete(self.job_id, self.upload_root)
        )
        self._clean_review_via_ui(0)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertNotEqual(
            data["segmented"].get("review_state"),
            store.SEGMENT_REVIEW_FINAL_READY,
        )


class ReassemblyTest(F14cPart1Base):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app)

    def _clean_review_via_ui(self, seg_index):
        res = self.client.post(
            f"/segment-review/{self.job_id}/{seg_index}",
            data={"verdict": "clean"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)

    def test_new_issue_reverts_state_then_reassembly_bumps_version(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review_via_ui(0)
        self._mark_done_with_video(1, seconds=2)
        self._clean_review_via_ui(1)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_FINAL_READY
        )
        self.assertEqual(data["segmented"]["final_assembly"]["version"], 1)

        # A NEW issue on segment 1 after the final video exists.
        with mock.patch.object(app_module, "_run_segment_rerun"):
            res = self.client.post(
                f"/segment-review/{self.job_id}/1",
                data={"verdict": "issues", "issues": ["audio_glitch"]},
                follow_redirects=False,
            )
        self.assertEqual(res.status_code, 303)
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_IN_REVIEW
        )
        self.assertEqual(
            data["segmented"]["final_assembly"]["state"],
            store.SEGMENT_ASSEMBLY_STALE,
        )

        # Fix round completes and the human approves the corrected output.
        store.record_segment_rerun(
            self.job_id, 1, triggered_by_round=2, issues=["audio_glitch"],
            target_stage="E2_draft", status=store.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        self._mark_done_with_video(1, seconds=3)
        self._clean_review_via_ui(1)

        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_FINAL_READY
        )
        assembly = data["segmented"]["final_assembly"]
        self.assertEqual(assembly["state"], store.SEGMENT_ASSEMBLY_READY)
        self.assertEqual(assembly["version"], 2)
        self.assertEqual(assembly["segment_rounds"], {"seg_000": 1, "seg_001": 4})
        final = Path(assembly["final_path"])
        self.assertTrue(final.exists())
        duration = _probe_duration(final)
        self.assertLessEqual(abs(duration - 4.0), 0.2)


class AssemblyFailureTest(F14cPart1Base):
    def test_missing_segment_video_fails_gracefully_and_is_retryable(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        # seg 1's recorded final video is missing/corrupted on disk.
        self._mark_done_no_video(1)
        self._clean_review(1)

        result = segmented_pipeline.maybe_assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["error_bn"])

        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"],
            store.SEGMENT_REVIEW_ASSEMBLY_FAILED,
        )
        assembly = data["segmented"]["final_assembly"]
        self.assertEqual(assembly["state"], store.SEGMENT_ASSEMBLY_FAILED)
        self.assertTrue(assembly["error_bn"])
        self.assertTrue(assembly["error_at"])

        # Retry: after the segment video is restored, re-triggering succeeds.
        self._mark_done_with_video(1, seconds=2)
        retry = segmented_pipeline.maybe_assemble_final_video(
            self.job_id, self.upload_root
        )
        self.assertEqual(retry["status"], "ok")
        data = store.read_status(self.job_id, self.upload_root)
        self.assertEqual(
            data["segmented"]["review_state"], store.SEGMENT_REVIEW_FINAL_READY
        )

    def test_failure_never_crashes_review_submission(self):
        self._make_job(2)
        self._mark_done_with_video(0, seconds=1)
        self._clean_review(0)
        self._mark_done_no_video(1)
        self._clean_review(1)
        client = TestClient(app)
        res = client.post(
            f"/segment-review/{self.job_id}/0",
            data={"verdict": "clean"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)


if __name__ == "__main__":
    unittest.main()
