"""App-level tests for the F14b Part 3 per-segment correction UI.

Builds a real segmented job status (plan + ``init_segments`` + done segment
with a real final file) and drives the app through FastAPI's TestClient.
Verifies that an issue submission records the review on the segment's next
free round and kicks off the targeted correction for THAT round; that the
card transitions to the "ঠিক করা হচ্ছে" state while the correction runs;
that a successful rerun round re-renders the issue form with the previous
round's reported problems as context; that a clean next round yields the
done-reviewing summary; that a failed correction shows a Bengali error and a
pre-checked retry form; and that a human review never overwrites an
automated-QA rerun round.
"""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import app as app_module
from app import app
from pipeline import job_status, segmentation, video_ingest


def _plan(n_segments=2):
    return {
        "job_id": "job-seg",
        "strategy": "transcript_gap",
        "target_duration_sec": 300,
        "source_duration_sec": 700,
        "segments": [
            {
                "index": i,
                "start_sec": i * 350.0,
                "end_sec": (i + 1) * 350.0,
                "duration_sec": 350.0,
                "entries_count": 5,
            }
            for i in range(n_segments)
        ],
    }


class SegmentRerunUITest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-seg"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.plan = _plan()
        plan_file = segmentation.plan_path(
            self.job_id, upload_root=self.upload_root
        )
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        plan_file.write_text(json.dumps(self.plan), encoding="utf-8")
        job_status.init_segments(
            self.job_id, self.plan, upload_root=self.upload_root
        )
        (self.job_dir / "voice_source_choice.json").write_text(
            json.dumps({"job_id": self.job_id, "mode": "auto_tts"}),
            encoding="utf-8",
        )
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _mark_done(self, seg_index, with_file=True):
        final = None
        if with_file:
            final = self.upload_root / "out" / f"seg_{seg_index:03d}.mp4"
            final.parent.mkdir(parents=True, exist_ok=True)
            final.write_bytes(b"video-bytes")
        job_status.mark_segment_done(
            self.job_id, seg_index, final_path=str(final) if final else None,
            upload_root=self.upload_root,
        )

    @staticmethod
    def _wait_until(predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return predicate()

    def test_issue_submission_starts_background_rerun_for_that_round(self):
        self._mark_done(0, with_file=True)
        calls = []
        with mock.patch.object(
            app_module, "_run_segment_rerun",
            side_effect=lambda job_id, seg, round_no: calls.append((job_id, seg, round_no)),
        ):
            res = self.client.post(
                f"/segment-review/{self.job_id}/0",
                data={"verdict": "issues", "issues": ["timing_mismatch"]},
                follow_redirects=False,
            )
        self.assertEqual(res.status_code, 303)
        self.assertTrue(self._wait_until(lambda: calls))
        self.assertEqual(calls, [(self.job_id, 0, 1)])
        round1 = job_status.get_segment_reviews(
            self.job_id, 0, round_no=1, upload_root=self.upload_root
        )
        self.assertEqual(round1["issues"], ["timing_mismatch"])
        self.assertTrue(round1["reviewed_at"])

    def test_clean_submission_does_not_trigger_rerun(self):
        self._mark_done(0, with_file=True)
        with mock.patch.object(
            app_module, "_run_segment_rerun",
        ) as rerun:
            res = self.client.post(
                f"/segment-review/{self.job_id}/0",
                data={"verdict": "clean"},
                follow_redirects=False,
            )
        self.assertEqual(res.status_code, 303)
        rerun.assert_not_called()

    def test_issue_submission_renders_being_fixed_card(self):
        self._mark_done(0, with_file=True)
        with mock.patch.object(app_module, "_run_segment_rerun"):
            self.client.post(
                f"/segment-review/{self.job_id}/0",
                data={"verdict": "issues", "issues": ["audio_glitch"]},
            )
        page = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("সেগমেন্ট seg_000 — ঠিক করা হচ্ছে", page.text)
        self.assertNotIn('name="verdict" value="issues"', page.text)
        self.assertNotIn('name="verdict" value="clean"', page.text)

    def test_being_fixed_card_visible_while_stage_running(self):
        self._mark_done(0, with_file=True)
        with mock.patch.object(app_module, "_run_segment_rerun"):
            self.client.post(
                f"/segment-review/{self.job_id}/0",
                data={"verdict": "issues", "issues": ["timing_mismatch"]},
            )
        job_status.write_segment_status(
            self.job_id, 0, "D4_unify", "running",
            upload_root=self.upload_root,
        )
        page = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("সেগমেন্ট seg_000 — ঠিক করা হচ্ছে", page.text)
        self.assertIn("badge-running", page.text)

    def test_successful_rerun_round_shows_form_with_previous_context(self):
        self._mark_done(0, with_file=True)
        job_status.record_segment_review(
            self.job_id, 0, round_no=1,
            issues=["timing_mismatch"], notes="ভয়েস সামনে এগিয়ে",
            upload_root=self.upload_root,
        )
        job_status.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["timing_mismatch"],
            target_stage="D4_unify", status=job_status.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        self._mark_done(0, with_file=True)
        page = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("পূর্ববর্তী রাউন্ডে রিপোর্ট করা সমস্যা:", page.text)
        self.assertIn("ভয়েস ও দৃশ্যের টাইমিং মিসম্যাচ", page.text)
        self.assertIn("ভয়েস সামনে এগিয়ে", page.text)
        self.assertIn('name="verdict" value="issues"', page.text)
        self.assertIn('name="verdict" value="clean"', page.text)

    def test_clean_next_round_renders_done_reviewing_summary(self):
        self._mark_done(0, with_file=True)
        job_status.record_segment_review(
            self.job_id, 0, round_no=1,
            issues=["timing_mismatch"], upload_root=self.upload_root,
        )
        job_status.record_segment_review(
            self.job_id, 0, round_no=2, issues=[],
            upload_root=self.upload_root,
        )
        page = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("রিভিউ সম্পন্ন", page.text)
        self.assertIn("কোনো সমস্যা নেই", page.text)
        self.assertNotIn('name="verdict" value="issues"', page.text)

    def test_failed_rerun_shows_bengali_error_and_retry_form(self):
        self._mark_done(0, with_file=True)
        job_status.record_segment_review(
            self.job_id, 0, round_no=1,
            issues=["timing_mismatch"], upload_root=self.upload_root,
        )
        job_status.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["timing_mismatch"],
            target_stage="D4_unify", status=job_status.SEGMENT_RERUN_FAILED,
            error_message="সিন্থেটিক ব্যর্থতা", upload_root=self.upload_root,
        )
        self._mark_done(0, with_file=True)
        page = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn("ঠিক করা ব্যর্থ হয়েছে", page.text)
        self.assertIn("সিন্থেটিক ব্যর্থতা", page.text)
        self.assertIn('name="verdict" value="issues"', page.text)
        self.assertIn('value="timing_mismatch" checked', page.text)

    def test_human_review_lands_on_next_round_after_qa_gate_rerun(self):
        self._mark_done(0, with_file=True)
        job_status.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["timing_mismatch"],
            target_stage="D4_unify", status=job_status.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        calls = []
        with mock.patch.object(
            app_module, "_run_segment_rerun",
            side_effect=lambda job_id, seg, round_no: calls.append((job_id, seg, round_no)),
        ):
            res = self.client.post(
                f"/segment-review/{self.job_id}/0",
                data={"verdict": "issues", "issues": ["audio_glitch"]},
                follow_redirects=False,
            )
        self.assertEqual(res.status_code, 303)
        self.assertTrue(self._wait_until(lambda: calls))
        self.assertEqual(calls, [(self.job_id, 0, 2)])
        round1 = job_status.get_segment_reviews(
            self.job_id, 0, round_no=1, upload_root=self.upload_root
        )
        self.assertTrue(round1["rerun"])
        round2 = job_status.get_segment_reviews(
            self.job_id, 0, round_no=2, upload_root=self.upload_root
        )
        self.assertEqual(round2["issues"], ["audio_glitch"])
        self.assertNotIn("rerun", round2)

    def test_qa_gate_rerun_round_still_shows_human_form(self):
        self._mark_done(0, with_file=True)
        job_status.record_segment_rerun(
            self.job_id, 0, triggered_by_round=1, issues=["timing_mismatch"],
            target_stage="D4_unify", status=job_status.SEGMENT_RERUN_OK,
            upload_root=self.upload_root,
        )
        page = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(page.status_code, 200)
        self.assertIn('name="verdict" value="issues"', page.text)
        self.assertIn('name="verdict" value="clean"', page.text)
        self.assertNotIn("পূর্ববর্তী রাউন্ডে রিপোর্ট করা সমস্যা:", page.text)


if __name__ == "__main__":
    unittest.main()
