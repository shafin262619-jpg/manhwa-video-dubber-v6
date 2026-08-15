"""Tests for pipeline.subtitle_builder (pure Python, no network)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import subtitle_builder, video_ingest


class SubtitleBuilderBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-b2"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _write_raw(self, raw):
        (self.job_dir / "subtitles_zh_raw.json").write_text(
            json.dumps(raw, ensure_ascii=False), encoding="utf-8"
        )

    def _write_meta(self, duration):
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"duration_sec": duration}), encoding="utf-8"
        )

    def _build(self):
        return subtitle_builder.build_subtitle_list(
            self.job_id, upload_root=self.upload_root
        )

    def _read_output(self):
        return json.loads(
            (self.job_dir / "subtitles_zh.json").read_text(encoding="utf-8")
        )


class CleanInputTest(SubtitleBuilderBase):
    def test_clean_input_unchanged(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "job_id": self.job_id,
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "你好", "start_sec": 0.0, "end_sec": 3.2},
                    {"text": "世界", "start_sec": 3.5, "end_sec": 5.0},
                ],
            }
        )
        result = self._build()
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["serial"], 1)
        self.assertEqual(result[0]["text_zh"], "你好")
        self.assertEqual(result[0]["start_sec"], 0.0)
        self.assertEqual(result[0]["end_sec"], 3.2)
        self.assertEqual(result[0]["status"], "ok")
        self.assertEqual(result[1]["serial"], 2)
        self.assertEqual(result[1]["text_zh"], "世界")
        self.assertEqual(result[1]["start_sec"], 3.5)
        self.assertEqual(result[1]["status"], "ok")
        self.assertEqual(self._read_output(), result)


class OverlapClampTest(SubtitleBuilderBase):
    def test_overlap_is_clamped_and_logged(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 1.5, "end_sec": 4.0},
                ],
            }
        )
        with self.assertLogs("pipeline.subtitle_builder", level="WARNING") as cm:
            result = self._build()
        self.assertTrue(any("clamped" in line for line in cm.output))
        self.assertEqual(result[1]["serial"], 2)
        self.assertEqual(result[1]["start_sec"], 2.0)
        self.assertEqual(result[1]["end_sec"], 4.0)
        self.assertGreaterEqual(
            result[1]["start_sec"], result[0]["end_sec"]
        )

    def test_gap_is_preserved(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 3.0, "end_sec": 5.0},
                ],
            }
        )
        result = self._build()
        self.assertEqual(result[1]["start_sec"], 3.0)
        self.assertEqual(result[1]["end_sec"], 5.0)


class ExtractionFailedPlaceholderTest(SubtitleBuilderBase):
    def test_failed_segment_kept_with_flag(self):
        self._write_meta(3.0)
        self._write_raw(
            {
                "status": "partial",
                "chunked": True,
                "segments_count": 2,
                "failed_segments": [1],
                "subtitles": [
                    {"text": "x", "start_sec": 0.5, "end_sec": 1.0},
                ],
            }
        )
        with (
            mock.patch("pipeline.config.LONG_VIDEO_CHUNK_THRESHOLD_SEC", 2.0),
            mock.patch("pipeline.config.SUBTITLE_OVERLAP_SEC", 1.0),
        ):
            result = self._build()

        statuses = [e["status"] for e in result]
        self.assertIn("extraction_failed", statuses)
        failed = [e for e in result if e["status"] == "extraction_failed"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["text_zh"], "")
        self.assertEqual(failed[0]["start_sec"], 1.0)
        self.assertEqual(failed[0]["end_sec"], 3.0)
        self.assertEqual(result[0]["status"], "ok")
        self.assertEqual(result[0]["serial"], 1)

    def test_whole_job_failure_kept_as_placeholder(self):
        self._write_meta(5.0)
        self._write_raw(
            {
                "status": "extraction_failed",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [],
            }
        )
        result = self._build()
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["status"], "extraction_failed")
        self.assertEqual(result[0]["text_zh"], "")
        self.assertEqual(result[0]["serial"], 1)
        self.assertEqual(result[0]["start_sec"], 0.0)
        self.assertEqual(result[0]["end_sec"], 5.0)

    def test_placeholder_not_dropped_when_ok_entries_exist(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "status": "extraction_failed",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [0],
                "subtitles": [
                    {"text": "kept", "start_sec": 6.0, "end_sec": 7.0},
                ],
            }
        )
        result = self._build()
        texts = [e["text_zh"] for e in result]
        statuses = [e["status"] for e in result]
        self.assertIn("kept", texts)
        self.assertIn("extraction_failed", statuses)
        self.assertEqual(result[0]["status"], "extraction_failed")


class MissingInputTest(SubtitleBuilderBase):
    def test_missing_raw_raises(self):
        with self.assertRaises(FileNotFoundError):
            self._build()




class SubtitleGapDetectionTest(unittest.TestCase):
    def test_no_gaps(self):
        entries = [
            {"serial": 1, "text_zh": "a", "start_sec": 1.0, "end_sec": 4.0, "status": "ok"},
            {"serial": 2, "text_zh": "b", "start_sec": 4.1, "end_sec": 6.0, "status": "ok"},
        ]
        gaps = subtitle_builder.detect_gaps(entries, threshold_sec=1.0)
        self.assertEqual(gaps, [])

    def test_one_big_gap(self):
        entries = [
            {"serial": 1, "text_zh": "a", "start_sec": 1.0, "end_sec": 4.0, "status": "ok"},
            {"serial": 2, "text_zh": "b", "start_sec": 10.5, "end_sec": 12.0, "status": "ok"},
        ]
        gaps = subtitle_builder.detect_gaps(entries, threshold_sec=6.0)
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["after_serial"], 1)
        self.assertEqual(gaps[0]["before_serial"], 2)
        self.assertEqual(gaps[0]["gap_start_sec"], 4.0)
        self.assertEqual(gaps[0]["gap_end_sec"], 10.5)
        self.assertEqual(gaps[0]["gap_sec"], 6.5)

    def test_boundary_gaps(self):
        entries = [
            {"serial": 1, "text_zh": "a", "start_sec": 0.0, "end_sec": 2.0, "status": "ok"},
            {"serial": 2, "text_zh": "b", "start_sec": 8.0, "end_sec": 10.0, "status": "ok"}, # gap is 6.0
            {"serial": 3, "text_zh": "c", "start_sec": 16.01, "end_sec": 18.0, "status": "ok"}, # gap is 6.01
        ]
        gaps = subtitle_builder.detect_gaps(entries) # default 6.0 from config
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["after_serial"], 2)
        self.assertEqual(gaps[0]["before_serial"], 3)
        self.assertEqual(gaps[0]["gap_sec"], 6.01)

    def test_multiple_gaps_chronological_order(self):
        entries = [
            {"serial": 1, "text_zh": "a", "start_sec": 0.0, "end_sec": 1.0, "status": "ok"},
            {"serial": 2, "text_zh": "b", "start_sec": 4.0, "end_sec": 5.0, "status": "ok"}, # gap 3.0
            {"serial": 3, "text_zh": "c", "start_sec": 10.0, "end_sec": 12.0, "status": "ok"}, # gap 5.0
        ]
        gaps = subtitle_builder.detect_gaps(entries, threshold_sec=2.0)
        self.assertEqual(len(gaps), 2)
        self.assertEqual(gaps[0]["after_serial"], 1)
        self.assertEqual(gaps[0]["before_serial"], 2)
        self.assertEqual(gaps[1]["after_serial"], 2)
        self.assertEqual(gaps[1]["before_serial"], 3)

    def test_custom_threshold_override(self):
        entries = [
            {"serial": 1, "text_zh": "a", "start_sec": 1.0, "end_sec": 3.0, "status": "ok"},
            {"serial": 2, "text_zh": "b", "start_sec": 5.0, "end_sec": 7.0, "status": "ok"}, # gap 2.0
        ]
        gaps_default = subtitle_builder.detect_gaps(entries) # threshold 6.0
        self.assertEqual(gaps_default, [])
        gaps_custom = subtitle_builder.detect_gaps(entries, threshold_sec=1.5)
        self.assertEqual(len(gaps_custom), 1)
        self.assertEqual(gaps_custom[0]["gap_sec"], 2.0)

if __name__ == "__main__":
    unittest.main()
