"""App-level tests for the F14a Part 2 per-segment review UI.

Builds a real segmented job status (plan file + ``init_segments`` + done
segments with a real final file), then hits the app through FastAPI's
TestClient: the segmented result page must render a playable player and
review controls only for done segments, review submissions must flow through
``job_status.record_segment_review`` (no issues / tagged+notes are
distinguishable), submitting for one segment must never touch another
segment's state, and the F13b result page core must stay intact for a job
with no reviews submitted.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

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


class SegmentReviewUITest(unittest.TestCase):
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

    def test_done_segment_renders_player_and_controls_pending_does_not(self):
        self._mark_done(0, with_file=True)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn(f'src="/download/{self.job_id}/segment/0"', res.text)
        self.assertIn('name="verdict" value="issues"', res.text)
        self.assertIn('name="verdict" value="clean"', res.text)
        self.assertIn('value="timing_mismatch"', res.text)
        self.assertIn('value="audio_glitch"', res.text)
        self.assertIn("ভয়েস ও দৃশ্যের টাইমিং মিসম্যাচ", res.text)
        self.assertNotIn(f'src="/download/{self.job_id}/segment/1"', res.text)
        self.assertNotIn("সেগমেন্ট seg_001 — রিভিউ", res.text)

    def test_submit_no_issues_records_explicit_clean_state(self):
        self._mark_done(0, with_file=True)
        res = self.client.post(
            f"/segment-review/{self.job_id}/0",
            data={"verdict": "clean"},
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)
        entry = job_status.get_segment_reviews(
            self.job_id, 0, round_no=1, upload_root=self.upload_root
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry["issues"], [])
        self.assertNotIn("notes", entry)
        page = self.client.get(f"/upload/{self.job_id}?reviewed=0&verdict=clean")
        self.assertIn("কোনো সমস্যা নেই", page.text)
        self.assertIn("রিভিউ রেকর্ড হয়েছে", page.text)

    def test_submit_tagged_issues_and_notes_are_distinguishable(self):
        self._mark_done(1, with_file=True)
        res = self.client.post(
            f"/segment-review/{self.job_id}/1",
            data={
                "verdict": "issues",
                "issues": ["timing_mismatch", "audio_glitch"],
                "notes": "শব্দ পরিষ্কার নয়",
            },
            follow_redirects=False,
        )
        self.assertEqual(res.status_code, 303)
        entry = job_status.get_segment_reviews(
            self.job_id, 1, round_no=1, upload_root=self.upload_root
        )
        self.assertEqual(entry["issues"], ["timing_mismatch", "audio_glitch"])
        self.assertEqual(entry["notes"], "শব্দ পরিষ্কার নয়")
        unreviewed = job_status.get_segment_reviews(
            self.job_id, 0, round_no=1, upload_root=self.upload_root
        )
        self.assertIsNone(unreviewed)

    def test_review_for_segment_does_not_alter_other_segment_state(self):
        self._mark_done(0, with_file=False)
        job_status.write_segment_status(
            self.job_id, 1, "C1_translate", "running",
            upload_root=self.upload_root,
        )
        self.client.post(
            f"/segment-review/{self.job_id}/0",
            data={"verdict": "clean"},
        )
        seg1 = job_status.read_segment_status(
            self.job_id, 1, upload_root=self.upload_root
        )
        self.assertEqual(seg1["state"], "running")
        self.assertEqual(seg1["stages"]["C1_translate"]["state"], "running")
        seg0 = job_status.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(seg0["state"], "done")

    def test_f13b_result_page_unchanged_with_no_reviews(self):
        self._mark_done(0, with_file=True)
        res = self.client.get(f"/upload/{self.job_id}")
        self.assertEqual(res.status_code, 200)
        self.assertIn("1/2 segments complete", res.text)
        self.assertIn("<table class=\"keys-table\">", res.text)
        self.assertIn("<code>seg_000</code>", res.text)
        self.assertIn("<code>seg_001</code>", res.text)
        self.assertIn("badge-done", res.text)
        self.assertIn("badge-idle", res.text)
        self.assertIn(
            f'href="/download/{self.job_id}/segment/0"', res.text
        )
        self.assertNotIn("রিভিউ রেকর্ড হয়েছে", res.text)

    def test_unknown_segment_review_404(self):
        res = self.client.post(
            f"/segment-review/{self.job_id}/99",
            data={"verdict": "clean"},
        )
        self.assertEqual(res.status_code, 404)

    def test_unknown_job_404(self):
        res = self.client.post(
            "/segment-review/unknown-job/0",
            data={"verdict": "clean"},
        )
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
