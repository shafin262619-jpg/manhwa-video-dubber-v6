"""Tests for per-segment job status tracking (F13b)."""

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import job_status as store, segmentation, video_ingest


def _plan(n_segments=2):
    return {
        "job_id": "job-x",
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


class JobStatusSegmentsBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        (self.upload_root / "job-x").mkdir(parents=True)
        self._orig = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig


class InitSegmentsTest(JobStatusSegmentsBase):
    def test_init_writes_segments_map_and_summary(self):
        store.init_segments("job-x", _plan())
        data = json.loads(
            (self.upload_root / "job-x" / "job_status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(data["stage"], "segmented_pipeline")
        self.assertEqual(data["state"], "running")
        self.assertEqual(sorted(data["segments"]), ["seg_000", "seg_001"])
        self.assertEqual(data["segments"]["seg_000"]["index"], 0)
        self.assertEqual(data["segments"]["seg_000"]["state"], "pending")
        self.assertEqual(data["segments"]["seg_000"]["stage"], "pending")
        self.assertEqual(data["segments"]["seg_000"]["stages"], {})
        self.assertEqual(data["segments"]["seg_000"]["start_sec"], 0.0)
        self.assertEqual(data["segments"]["seg_000"]["end_sec"], 350.0)
        self.assertEqual(data["segmented"]["enabled"], True)
        self.assertEqual(data["segmented"]["total_count"], 2)
        self.assertEqual(data["segmented"]["completed_count"], 0)
        self.assertEqual(data["segmented"]["overall_state"], "running")

    def test_init_is_idempotent(self):
        store.init_segments("job-x", _plan())
        store.init_segments("job-x", _plan())
        data = store.read_status("job-x")
        self.assertEqual(len(data["segments"]), 2)


class SegmentStatusTest(JobStatusSegmentsBase):
    def setUp(self):
        super().setUp()
        store.init_segments("job-x", _plan())

    def test_write_segment_status_preserves_history(self):
        store.write_segment_status("job-x", 0, "B2_subtitles", "running")
        store.write_segment_status("job-x", 0, "B2_subtitles", "done")
        entry = store.read_segment_status("job-x", 0)
        self.assertEqual(entry["stage"], "B2_subtitles")
        self.assertEqual(entry["state"], "done")
        self.assertEqual(entry["stages"]["B2_subtitles"]["state"], "done")
        self.assertEqual(len(entry["stages"]["B2_subtitles"]), 2)  # running overwritten

    def test_write_segment_status_preserves_extra(self):
        store.write_segment_status(
            "job-x", 0, "B2_subtitles", "running",
            extra={"processed_count": 3, "total_count": 5},
        )
        entry = store.read_segment_status("job-x", 0)
        self.assertEqual(entry["stages"]["B2_subtitles"]["processed_count"], 3)

    def test_segments_are_independent(self):
        store.write_segment_status("job-x", 0, "B2_subtitles", "done")
        store.write_segment_status("job-x", 1, "C1_translate", "running")
        seg0 = store.read_segment_status("job-x", 0)
        seg1 = store.read_segment_status("job-x", 1)
        self.assertEqual(seg0["state"], "done")
        self.assertEqual(seg1["state"], "running")
        self.assertNotIn("C1_translate", seg0["stages"])
        self.assertNotIn("B2_subtitles", seg1["stages"])

    def test_mark_segment_done_sets_terminal_state_and_path(self):
        store.mark_segment_done(
            "job-x", 0, final_path="/outputs/job-x/segments/seg_000/final_video.mp4"
        )
        entry = store.read_segment_status("job-x", 0)
        self.assertEqual(entry["state"], "done")
        self.assertIn("completed_at", entry)
        self.assertEqual(
            entry["final_path"], "/outputs/job-x/segments/seg_000/final_video.mp4"
        )
        self.assertEqual(store.segmented_summary("job-x")["completed_count"], 1)
        self.assertEqual(store.segmented_summary("job-x")["overall_state"], "running")

    def test_overall_done_only_when_all_segments_done(self):
        store.mark_segment_done("job-x", 0)
        store.mark_segment_done("job-x", 1)
        data = store.read_status("job-x")
        self.assertEqual(data["segmented"]["completed_count"], 2)
        self.assertEqual(data["segmented"]["overall_state"], "done")
        self.assertEqual(data["state"], "done")

    def test_error_wins_and_top_level_syncs(self):
        store.mark_segment_done("job-x", 0)
        store.mark_segment_error("job-x", 1, message="boom")
        data = store.read_status("job-x")
        self.assertEqual(data["segmented"]["overall_state"], "error")
        self.assertEqual(data["state"], "error")
        entry = store.read_segment_status("job-x", 1)
        self.assertEqual(entry["state"], "error")
        self.assertEqual(entry["error_detail"], "boom")

    def test_error_detail_is_truncated(self):
        store.mark_segment_error("job-x", 1, message="x" * 1000)
        entry = store.read_segment_status("job-x", 1)
        self.assertEqual(len(entry["error_detail"]), 500)

    def test_mark_segment_done_with_extra_entries_count(self):
        store.mark_segment_done(
            "job-x", 0, final_path="/f.mp4",
            extra={"status": "ok", "entries_count": 5},
        )
        entry = store.read_segment_status("job-x", 0)
        self.assertEqual(entry["entries_count"], 5)


class SegmentSummaryTest(JobStatusSegmentsBase):
    def test_summary_none_for_non_segmented_job(self):
        self.assertIsNone(store.segmented_summary("job-x"))

    def test_read_segment_status_default(self):
        entry = store.read_segment_status("job-x", 3)
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(entry["index"], 3)

    def test_segment_key_matches_segmentation_key(self):
        store.init_segments("job-x", _plan())
        data = store.read_status("job-x")
        for seg in _plan()["segments"]:
            key = segmentation.segment_key(seg["index"])
            self.assertIn(key, data["segments"])
            self.assertEqual(data["segments"][key]["index"], seg["index"])
