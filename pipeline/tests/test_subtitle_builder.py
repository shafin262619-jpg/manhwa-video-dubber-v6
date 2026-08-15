"""Tests for pipeline.subtitle_builder (pure Python, no network)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import gemini_rotation, key_store, subtitle_builder, translator, video_ingest


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

class DetectDuplicateClustersTest(unittest.TestCase):
    def _entries(self, pairs):
        return [
            {"serial": i, "text_zh": "x", "start_sec": s, "end_sec": e, "status": "ok"}
            for i, (s, e) in enumerate(pairs, start=1)
        ]

    def test_no_clusters_returns_empty(self):
        entries = self._entries([(0.0, 3.0), (3.1, 5.0), (5.2, 7.0), (7.5, 9.0)])
        self.assertEqual(subtitle_builder.detect_duplicate_clusters(entries), [])

    def test_same_start_cluster_flagged(self):
        entries = self._entries([(0.0, 2.0), (3.0, 5.0), (3.0, 5.0), (3.0, 5.0), (8.0, 9.0)])
        clusters = subtitle_builder.detect_duplicate_clusters(entries)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(
            clusters[0],
            {
                "start_serial": 2,
                "end_serial": 4,
                "start_sec": 3.0,
                "count": 3,
                "reason": "same_start_timestamp",
            },
        )

    def test_zero_duration_cluster_flagged_with_reason(self):
        entries = self._entries([(0.0, 2.0), (4.0, 4.0), (4.0, 4.0), (4.0, 4.0), (9.0, 10.0)])
        clusters = subtitle_builder.detect_duplicate_clusters(entries)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["reason"], "zero_duration")
        self.assertEqual(clusters[0]["count"], 3)
        self.assertEqual(clusters[0]["start_serial"], 2)
        self.assertEqual(clusters[0]["end_serial"], 4)

    def test_below_min_count_not_flagged(self):
        entries = self._entries([(0.0, 2.0), (3.0, 5.0), (3.0, 5.0), (8.0, 9.0)])
        self.assertEqual(subtitle_builder.detect_duplicate_clusters(entries), [])

    def test_single_zero_duration_entry_flagged(self):
        entries = self._entries([(0.0, 2.0), (4.0, 4.0), (9.0, 10.0)])
        clusters = subtitle_builder.detect_duplicate_clusters(entries)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["reason"], "zero_duration")
        self.assertEqual(clusters[0]["count"], 1)
        self.assertEqual(clusters[0]["start_serial"], 2)

    def test_pair_zero_duration_flagged(self):
        entries = self._entries([(0.0, 2.0), (4.0, 4.0), (4.0, 4.0), (9.0, 10.0)])
        clusters = subtitle_builder.detect_duplicate_clusters(entries)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0]["reason"], "zero_duration")
        self.assertEqual(clusters[0]["count"], 2)

    def test_zero_duration_then_same_start_pair_flags_zero_duration_entry(self):
        entries = self._entries([(0.0, 2.0), (4.0, 4.0), (4.0, 5.0), (9.0, 10.0)])
        clusters = subtitle_builder.detect_duplicate_clusters(entries)
        reasons = [c["reason"] for c in clusters]
        self.assertIn("zero_duration", reasons)
        zero = [c for c in clusters if c["reason"] == "zero_duration"][0]
        self.assertEqual(zero["count"], 1)
        self.assertEqual(zero["start_serial"], 2)

    def test_multiple_clusters_in_serial_order(self):
        entries = self._entries(
            [
                (1.0, 2.0),
                (1.0, 2.0),
                (1.0, 2.0),
                (1.0, 2.0),
                (5.0, 6.0),
                (5.0, 6.0),
                (5.0, 6.0),
            ]
        )
        clusters = subtitle_builder.detect_duplicate_clusters(entries)
        self.assertEqual(len(clusters), 2)
        self.assertEqual([c["start_serial"] for c in clusters], [1, 5])
        self.assertEqual([c["reason"] for c in clusters], ["same_start_timestamp"] * 2)

    def test_custom_min_count_override(self):
        entries = self._entries([(0.0, 2.0), (3.0, 5.0), (3.0, 5.0), (8.0, 9.0)])
        self.assertEqual(
            subtitle_builder.detect_duplicate_clusters(entries, min_count=2)[0]["count"], 2
        )
        self.assertEqual(subtitle_builder.detect_duplicate_clusters(entries, min_count=4), [])

    def test_default_min_count_reads_config(self):
        entries = self._entries([(0.0, 2.0), (3.0, 5.0), (3.0, 5.0), (8.0, 9.0)])
        with mock.patch("pipeline.config.SUBTITLE_DUP_CLUSTER_MIN_COUNT", 2):
            self.assertEqual(
                subtitle_builder.detect_duplicate_clusters(entries)[0]["count"], 2
            )

    def test_zero_duration_precedence_in_mixed_run(self):
        entries = self._entries([(0.0, 2.0), (4.0, 4.0), (4.0, 4.0), (4.0, 4.0), (9.0, 10.0)])
        clusters = subtitle_builder.detect_duplicate_clusters(entries)
        self.assertEqual(clusters[0]["reason"], "zero_duration")


class SerializeZeroDurationLoggingTest(unittest.TestCase):
    def test_raw_zero_duration_entry_logged(self):
        entries = [
            {"text_zh": "a", "start_sec": 0.0, "end_sec": 2.0, "status": "ok"},
            {"text_zh": "b", "start_sec": 4.0, "end_sec": 4.0, "status": "ok"},
        ]
        with self.assertLogs("pipeline.subtitle_builder", level="WARNING") as cm:
            subtitle_builder._serialize(entries)
        self.assertTrue(any("zero-duration entry" in line for line in cm.output))

    def test_clamp_induced_zero_duration_logged(self):
        entries = [
            {"text_zh": "a", "start_sec": 0.0, "end_sec": 2.0, "status": "ok"},
            {"text_zh": "b", "start_sec": 1.5, "end_sec": 1.2, "status": "ok"},
        ]
        with self.assertLogs("pipeline.subtitle_builder", level="WARNING") as cm:
            result = subtitle_builder._serialize(entries)
        self.assertTrue(
            any("zero/negative duration after clamp" in line for line in cm.output)
        )
        self.assertEqual(result[1]["start_sec"], 2.0)
        self.assertEqual(result[1]["end_sec"], 2.0)


class SubtitleQaArtifactTest(SubtitleBuilderBase):
    def _read_qa(self):
        return json.loads(
            (self.job_dir / "subtitle_qa.json").read_text(encoding="utf-8")
        )

    def test_qa_written_with_gap_and_cluster_return_unchanged(self):
        self._write_meta(20.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "c", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "d", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "e", "start_sec": 15.0, "end_sec": 16.0},
                ],
            }
        )
        result = self._build()
        self.assertEqual(len(result), 5)
        self.assertEqual(result[0]["serial"], 1)
        self.assertEqual(result[-1]["serial"], 5)

        qa = self._read_qa()
        self.assertEqual(qa["job_id"], self.job_id)
        self.assertEqual(qa["total_duration_sec"], 20.0)
        self.assertEqual(qa["entries_count"], 5)
        self.assertEqual(len(qa["gaps"]), 1)
        self.assertEqual(qa["gaps"][0]["after_serial"], 4)
        self.assertEqual(qa["gaps"][0]["before_serial"], 5)
        self.assertEqual(qa["gaps"][0]["gap_sec"], 12.0)
        self.assertEqual(qa["covered_duration_sec"], 8.0)
        self.assertEqual(len(qa["duplicate_clusters"]), 1)
        self.assertEqual(qa["duplicate_clusters"][0]["reason"], "zero_duration")
        self.assertEqual(qa["duplicate_clusters"][0]["count"], 3)

    def test_clean_fixture_writes_empty_qa_lists(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 3.0},
                    {"text": "b", "start_sec": 3.5, "end_sec": 6.0},
                ],
            }
        )
        result = self._build()
        self.assertEqual(len(result), 2)
        qa = self._read_qa()
        self.assertEqual(qa["gaps"], [])
        self.assertEqual(qa["duplicate_clusters"], [])
        self.assertEqual(qa["covered_duration_sec"], 10.0)
        self.assertEqual(qa["total_duration_sec"], 10.0)

    def test_qa_matches_serialized_output(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 3.0, "end_sec": 4.0},
                    {"text": "c", "start_sec": 3.0, "end_sec": 4.0},
                    {"text": "d", "start_sec": 3.0, "end_sec": 4.0},
                ],
            }
        )
        result = self._build()
        self.assertEqual(self._read_output(), result)
        self.assertTrue((self.job_dir / "subtitle_qa.json").exists())


class LoadSubtitleQaTest(SubtitleBuilderBase):
    def test_reads_existing_file(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 8.5, "end_sec": 9.5},
                ],
            }
        )
        self._build()
        qa = subtitle_builder.load_subtitle_qa(self.job_id, upload_root=self.upload_root)
        self.assertEqual(qa["job_id"], self.job_id)
        self.assertEqual(len(qa["gaps"]), 1)
        self.assertEqual(len(qa["duplicate_clusters"]), 0)

    def test_missing_file_returns_defaults(self):
        qa = subtitle_builder.load_subtitle_qa(self.job_id, upload_root=self.upload_root)
        self.assertEqual(qa["gaps"], [])
        self.assertEqual(qa["duplicate_clusters"], [])
        self.assertEqual(qa["entries_count"], 0)

    def test_malformed_json_returns_defaults(self):
        (self.job_dir / "subtitle_qa.json").write_text("{not json", encoding="utf-8")
        qa = subtitle_builder.load_subtitle_qa(self.job_id, upload_root=self.upload_root)
        self.assertEqual(qa["gaps"], [])
        self.assertEqual(qa["duplicate_clusters"], [])
        self.assertEqual(qa["entries_count"], 0)

    def test_non_dict_json_returns_defaults(self):
        (self.job_dir / "subtitle_qa.json").write_text("[1, 2]", encoding="utf-8")
        qa = subtitle_builder.load_subtitle_qa(self.job_id, upload_root=self.upload_root)
        self.assertEqual(qa["gaps"], [])
        self.assertEqual(qa["duplicate_clusters"], [])


class RepairFlaggedRegionsTest(unittest.TestCase):
    def _raw_entries(self, pairs):
        return [
            {"text_zh": f"t{i}", "status": "ok", "start_sec": s, "end_sec": e}
            for i, (s, e) in enumerate(pairs, start=1)
        ]

    def _diagnostics(self, entries):
        serialized = subtitle_builder._serialize(entries)
        return {
            "gaps": subtitle_builder.detect_gaps(serialized),
            "duplicate_clusters": subtitle_builder.detect_duplicate_clusters(
                serialized
            ),
        }

    def _patch_extract(self, subs):
        return mock.patch.object(
            subtitle_builder.subtitle_extract,
            "extract_window",
            return_value=subs,
        )

    def test_gap_repair_inserts_new_entries(self):
        entries = self._raw_entries([(0.0, 2.0), (10.0, 12.0)])
        diag = self._diagnostics(entries)
        with self._patch_extract(
            [{"text": "new", "start_sec": 3.0, "end_sec": 5.0}]
        ) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag
            )
        fake.assert_called_once()
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["skipped_budget"], [])
        texts = [e["text_zh"] for e in repaired]
        self.assertEqual(texts, ["t1", "new", "t2"])
        self.assertEqual([e["serial"] for e in repaired], [1, 2, 3])

    def test_duplicate_cluster_repair_replaces_old_entries(self):
        entries = self._raw_entries([(0.0, 2.0), (3.0, 3.0), (3.0, 3.0), (3.0, 3.0)])
        diag = self._diagnostics(entries)
        self.assertTrue(diag["duplicate_clusters"])
        with self._patch_extract(
            [{"text": "fixed", "start_sec": 3.0, "end_sec": 5.0}]
        ) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag
            )
        self.assertEqual(summary["succeeded"], 1)
        texts = [e["text_zh"] for e in repaired]
        self.assertNotIn("t2", texts)
        self.assertNotIn("t3", texts)
        self.assertNotIn("t4", texts)
        self.assertIn("fixed", texts)

    def test_extract_failure_keeps_range_untouched(self):
        entries = self._raw_entries([(0.0, 2.0), (10.0, 12.0)])
        diag = self._diagnostics(entries)
        with self._patch_extract(None) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag
            )
        fake.assert_called_once()
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual([e["text_zh"] for e in repaired], ["t1", "t2"])

    def test_more_flags_than_max_attempts_uses_largest_first(self):
        entries = self._raw_entries(
            [(0.0, 2.0), (10.0, 12.0), (30.0, 32.0), (50.0, 52.0), (80.0, 82.0)]
        )
        diag = self._diagnostics(entries)
        with self._patch_extract(
            [{"text": "new", "start_sec": 3.0, "end_sec": 4.0}]
        ) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag, max_attempts=2
            )
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["succeeded"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(len(summary["skipped_budget"]), 2)
        skipped = sorted(
            (r["start_sec"], r["end_sec"]) for r in summary["skipped_budget"]
        )
        self.assertEqual(skipped, [(2.0, 10.0), (32.0, 50.0)])

    def test_overlapping_ranges_merged_single_call(self):
        entries = self._raw_entries([(0.0, 2.0), (10.0, 12.0), (30.0, 32.0)])
        diag = {
            "gaps": [
                {"after_serial": 1, "before_serial": 2,
                 "gap_start_sec": 2.0, "gap_end_sec": 12.0, "gap_sec": 10.0},
                {"after_serial": 2, "before_serial": 3,
                 "gap_start_sec": 11.0, "gap_end_sec": 32.0, "gap_sec": 21.0},
            ],
            "duplicate_clusters": [],
        }
        with self._patch_extract(
            [{"text": "new", "start_sec": 5.0, "end_sec": 7.0}]
        ) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag
            )
        fake.assert_called_once()
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(summary["succeeded"], 1)
        call_start = fake.call_args.args[1]
        call_end = fake.call_args.args[2]
        self.assertLessEqual(call_start, 2.0)
        self.assertGreaterEqual(call_end, 32.0)

    def test_no_flags_returns_entries_unchanged_no_calls(self):
        entries = self._raw_entries([(0.0, 2.0), (3.0, 5.0)])
        diag = {"gaps": [], "duplicate_clusters": []}
        with self._patch_extract([]) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag
            )
        fake.assert_not_called()
        self.assertEqual(summary["attempted"], 0)
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["skipped_budget"], [])
        self.assertEqual([e["text_zh"] for e in repaired], ["t1", "t2"])

    def test_call_budget_forwarded_to_extract_window(self):
        entries = self._raw_entries([(0.0, 2.0), (10.0, 12.0)])
        diag = self._diagnostics(entries)
        budget = object()
        with self._patch_extract(
            [{"text": "new", "start_sec": 3.0, "end_sec": 5.0}]
        ) as fake:
            subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag, call_budget=budget
            )
        self.assertIs(fake.call_args.kwargs["call_budget"], budget)

    def test_default_max_attempts_reads_config(self):
        entries = self._raw_entries(
            [(0.0, 2.0), (10.0, 12.0), (30.0, 32.0), (50.0, 52.0), (80.0, 82.0)]
        )
        diag = self._diagnostics(entries)
        with (
            self._patch_extract(
                [{"text": "new", "start_sec": 3.0, "end_sec": 4.0}]
            ) as fake,
            mock.patch("pipeline.config.SUBTITLE_MAX_REPAIR_ATTEMPTS", 1),
        ):
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag
            )
        self.assertEqual(fake.call_count, 1)
        self.assertEqual(summary["attempted"], 1)
        self.assertEqual(len(summary["skipped_budget"]), 3)


class RepairBudgetEdgeCaseTest(SubtitleBuilderBase):
    """B4: repair must never raise when the shared CallBudget runs out mid-way."""

    def _raw_entries(self, pairs):
        return [
            {"text_zh": f"t{i}", "status": "ok", "start_sec": s, "end_sec": e}
            for i, (s, e) in enumerate(pairs, start=1)
        ]

    def _diagnostics(self, entries):
        serialized = subtitle_builder._serialize(entries)
        return {
            "gaps": subtitle_builder.detect_gaps(serialized),
            "duplicate_clusters": subtitle_builder.detect_duplicate_clusters(
                serialized
            ),
        }

    def test_budget_exhausted_mid_repair_skips_gracefully(self):
        entries = self._raw_entries([(0.0, 2.0), (10.0, 12.0), (30.0, 32.0)])
        diag = self._diagnostics(entries)
        budget = gemini_rotation.CallBudget(1)
        calls = []

        def fake_extract(*args, **kwargs):
            try:
                budget.consume()
            except gemini_rotation.CallBudgetExceeded:
                return None
            calls.append(1)
            return [{"text": "new", "start_sec": 5.0, "end_sec": 7.0}]

        with mock.patch.object(
            subtitle_builder.subtitle_extract, "extract_window",
            side_effect=fake_extract,
        ) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag, call_budget=budget
            )
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(summary["attempted"], 2)
        self.assertEqual(summary["succeeded"], 1)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["skipped_budget"], [])
        self.assertEqual(len(calls), 1)

    def test_all_repair_calls_fail_pipeline_continues(self):
        entries = self._raw_entries([(0.0, 2.0), (10.0, 12.0), (30.0, 32.0)])
        diag = self._diagnostics(entries)
        with mock.patch.object(
            subtitle_builder.subtitle_extract, "extract_window",
            return_value=None,
        ) as fake:
            repaired, summary = subtitle_builder.repair_flagged_regions(
                "job-x", entries, diag
            )
        self.assertEqual(fake.call_count, 2)
        self.assertEqual(summary["succeeded"], 0)
        self.assertEqual(summary["failed"], 2)
        texts = [e["text_zh"] for e in repaired]
        self.assertEqual(texts, ["t1", "t2", "t3"])


class BuildSubtitleListAutoRepairTest(SubtitleBuilderBase):
    def _read_qa(self):
        return json.loads(
            (self.job_dir / "subtitle_qa.json").read_text(encoding="utf-8")
        )

    def test_auto_repair_runs_repair_and_rediagnoses(self):
        self._write_meta(20.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "c", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "d", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "e", "start_sec": 15.0, "end_sec": 16.0},
                ],
            }
        )
        with mock.patch.object(
            subtitle_builder.subtitle_extract,
            "extract_window",
            return_value=[{"text": "fixed", "start_sec": 3.0, "end_sec": 5.0}],
        ) as fake:
            result = self._build()

        fake.assert_called_once()
        qa = self._read_qa()
        self.assertEqual(qa["repair"]["attempted"], 1)
        self.assertEqual(qa["repair"]["succeeded"], 1)
        self.assertIn("repair", qa)
        texts = [e["text_zh"] for e in result]
        self.assertIn("fixed", texts)
        self.assertLessEqual(len(qa["duplicate_clusters"]), 1)

    def test_auto_repair_false_skips_repair(self):
        self._write_meta(20.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "c", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "d", "start_sec": 3.0, "end_sec": 3.0},
                ],
            }
        )
        with mock.patch.object(
            subtitle_builder.subtitle_extract, "extract_window"
        ) as fake:
            result = subtitle_builder.build_subtitle_list(
                self.job_id, upload_root=self.upload_root, auto_repair=False
            )
        fake.assert_not_called()
        qa = self._read_qa()
        self.assertNotIn("repair", qa)
        self.assertEqual(len(qa["duplicate_clusters"]), 1)
        self.assertEqual(len(result), 4)

    def test_clean_input_no_repair_calls(self):
        self._write_meta(10.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 3.0},
                    {"text": "b", "start_sec": 3.5, "end_sec": 6.0},
                ],
            }
        )
        with mock.patch.object(
            subtitle_builder.subtitle_extract, "extract_window"
        ) as fake:
            result = self._build()
        fake.assert_not_called()
        qa = self._read_qa()
        self.assertNotIn("repair", qa)

    def test_all_repair_calls_fail_keeps_old_entries_and_diagnostics(self):
        self._write_meta(20.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "c", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "d", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "e", "start_sec": 15.0, "end_sec": 16.0},
                ],
            }
        )
        with mock.patch.object(
            subtitle_builder.subtitle_extract, "extract_window",
            return_value=None,
        ) as fake:
            result = self._build()
        fake.assert_called_once()
        qa = self._read_qa()
        self.assertEqual(qa["repair"]["attempted"], 1)
        self.assertEqual(qa["repair"]["succeeded"], 0)
        self.assertEqual(qa["repair"]["failed"], 1)
        texts = [e["text_zh"] for e in result]
        self.assertEqual(texts, ["a", "b", "c", "d", "e"])
        self.assertEqual(qa["entries_count"], 5)

    def test_single_pass_no_recursive_repair(self):
        self._write_meta(20.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "b", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "c", "start_sec": 3.0, "end_sec": 3.0},
                    {"text": "d", "start_sec": 3.0, "end_sec": 3.0},
                ],
            }
        )
        fixed_subs = [
            {"text": "fixed1", "start_sec": 3.0, "end_sec": 3.0},
            {"text": "fixed2", "start_sec": 3.0, "end_sec": 3.0},
            {"text": "fixed3", "start_sec": 3.0, "end_sec": 3.0},
        ]
        with mock.patch.object(
            subtitle_builder.subtitle_extract, "extract_window",
            return_value=fixed_subs,
        ) as fake:
            result = self._build()
        fake.assert_called_once()
        qa = self._read_qa()
        self.assertEqual(qa["repair"]["succeeded"], 1)
        self.assertTrue(qa["duplicate_clusters"])
        texts = [e["text_zh"] for e in result]
        self.assertIn("fixed1", texts)
        self.assertIn("fixed2", texts)


class RepairSrtWritebackRegressionTest(SubtitleBuilderBase):
    """End-to-end: when repair reports success, the final written
    ``subtitles_hi.srt`` must contain no zero/negative-duration or
    duplicate-start-time blocks (regression for real job
    edb1b1ef-5041-491e-bb3f-c8aa3617794a, where ``subtitle_qa.json`` claimed
    ``repair.succeeded == 2`` yet the delivered SRT still had zero-duration
    entries at serials 89 and 154)."""

    def setUp(self):
        super().setUp()
        self.store_path = Path(self._tmp) / "gemini_keys_store.json"
        self._orig_key_path = key_store.KEY_STORE_PATH
        key_store.KEY_STORE_PATH = self.store_path
        self.addCleanup(self._restore_key_path)

    def _restore_key_path(self):
        key_store.KEY_STORE_PATH = self._orig_key_path

    @staticmethod
    def _to_ms(timestamp):
        h, m, rest = timestamp.split(":")
        s, ms = rest.split(",")
        return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)

    def test_single_zero_duration_leaks_nothing_into_final_srt(self):
        # Synthetic raw input reproducing the reported degenerate shapes with
        # NO gaps (so only the zero-duration fix can trigger repair): a single
        # zero-duration entry D (like serial 89/154) followed by a non-zero
        # entry E sharing the same start timestamp (like serial 155). These
        # fall below the old 3-entry cluster floor, so they were never
        # flagged, never repaired, and leaked into the final SRT.
        self._write_meta(20.0)
        self._write_raw(
            {
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": [
                    {"text": "A", "start_sec": 0.0, "end_sec": 2.0},
                    {"text": "B", "start_sec": 2.0, "end_sec": 4.0},
                    {"text": "C", "start_sec": 4.0, "end_sec": 6.0},
                    {"text": "D", "start_sec": 6.0, "end_sec": 6.0},
                    {"text": "E", "start_sec": 6.0, "end_sec": 8.0},
                    {"text": "F", "start_sec": 8.0, "end_sec": 10.0},
                    {"text": "G", "start_sec": 10.0, "end_sec": 12.0},
                    {"text": "H", "start_sec": 12.0, "end_sec": 14.0},
                ],
            }
        )
        with mock.patch.object(
            subtitle_builder.subtitle_extract, "extract_window",
            return_value=[{"text": "replaced", "start_sec": 6.0, "end_sec": 7.5}],
        ) as fake:
            result = self._build()

        self.assertTrue(fake.called)

        # Repair reported success and the intermediate subtitles_zh.json (the
        # source of the final SRT timing) has no zero-duration entries.
        qa = json.loads(
            (self.job_dir / "subtitle_qa.json").read_text(encoding="utf-8")
        )
        self.assertEqual(qa["repair"]["succeeded"], 1)
        for entry in self._read_output():
            self.assertGreater(entry["end_sec"], entry["start_sec"])

        # No Gemini keys configured: translator falls back to the original
        # text but still writes the final SRT from subtitles_zh.json timing.
        translator.translate_subtitles(self.job_id, upload_root=self.upload_root)
        srt = (self.job_dir / "subtitles_hi.srt").read_text(encoding="utf-8")

        starts = []
        for block in srt.strip().split("\n\n"):
            timeline = next(line for line in block.splitlines() if "-->" in line)
            start, end = timeline.split("-->")
            start_ms = self._to_ms(start.strip())
            end_ms = self._to_ms(end.strip())
            self.assertGreater(
                end_ms, start_ms,
                f"zero/negative-duration block leaked into final SRT: {timeline}",
            )
            starts.append(start_ms)
        for a, b in zip(starts, starts[1:]):
            self.assertNotEqual(
                a, b, "duplicate start-time blocks leaked into final SRT"
            )


class CollisionClusterRedistributionTest(unittest.TestCase):
    """E7: a 3+ consecutive overlap run must be redistributed with non-zero
    durations instead of collapsing into a zero-duration pile-up (the source
    of the ffmpeg "-to value smaller than -ss" job crash)."""

    def _entry(self, start, end, text="x"):
        return {"text_zh": text, "status": "ok", "start_sec": start, "end_sec": end}

    def _assert_valid_timeline(self, result):
        serials = [e["serial"] for e in result]
        self.assertEqual(serials, list(range(1, len(result) + 1)))
        for e in result:
            self.assertGreater(
                e["end_sec"], e["start_sec"],
                f"zero/negative-duration entry leaked: {e}",
            )
        for prev, nxt in zip(result, result[1:]):
            self.assertGreaterEqual(nxt["start_sec"], prev["end_sec"])

    def test_twenty_seven_entry_collision_run_redistributed(self):
        # Replicates the reported QA log: a long entry ending at 100s followed
        # by 27 entries whose raw starts (60..86s) all collide with it.
        entries = [self._entry(0.0, 2.0, "a"), self._entry(3.0, 100.0, "b")]
        for k in range(27):
            s = 60.0 + k
            entries.append(self._entry(s, s + 1.0, f"line-{k}"))

        with self.assertLogs("pipeline.subtitle_builder", level="WARNING") as cm:
            result = subtitle_builder._serialize(entries)

        self.assertEqual(len(result), 29)
        self._assert_valid_timeline(result)
        self.assertTrue(
            any("collision cluster" in line for line in cm.output)
        )
        # First cluster entry starts exactly at the previous end; every entry
        # gets the per-entry fallback minimum (no anchor after the run).
        self.assertEqual(result[2]["start_sec"], 100.0)
        self.assertAlmostEqual(
            result[2]["end_sec"] - result[2]["start_sec"],
            subtitle_builder.config.SUBTITLE_MIN_SERIAL_DURATION_SEC,
            places=2,
        )
        self.assertAlmostEqual(result[-1]["end_sec"], 121.6, places=2)

    def test_weighted_redistribution_fits_anchor_window(self):
        # 3 colliding entries sit between a previous entry ending at 100 and an
        # anchor at 150: the cluster is redistributed across [100, 150] with
        # text-length-weighted durations.
        entries = [
            self._entry(0.0, 2.0, "a"),
            self._entry(50.0, 100.0, "b"),
            self._entry(60.0, 61.0, "c"),
            self._entry(61.0, 62.0, "dd"),
            self._entry(62.0, 63.0, "eee"),
            self._entry(150.0, 160.0, "f"),
        ]
        result = subtitle_builder._serialize(entries)

        self.assertEqual(len(result), 6)
        self._assert_valid_timeline(result)
        self.assertEqual(result[2]["start_sec"], 100.0)
        self.assertAlmostEqual(result[4]["end_sec"], 150.0, places=2)
        # Longer text gets a longer duration: "eee" > "dd" > "c".
        dur = [round(e["end_sec"] - e["start_sec"], 3) for e in result[2:5]]
        self.assertGreater(dur[2], dur[1])
        self.assertGreater(dur[1], dur[0])

    def test_small_overlap_keeps_old_clamp(self):
        # A 2-entry overlap (below the cluster floor) keeps the legacy
        # clamp-to-prev-end behaviour, including the induced zero-duration.
        entries = [
            self._entry(0.0, 2.0, "a"),
            self._entry(1.5, 1.2, "b"),
            self._entry(2.5, 3.0, "c"),
        ]
        with self.assertLogs("pipeline.subtitle_builder", level="WARNING") as cm:
            result = subtitle_builder._serialize(entries)
        self.assertEqual(len(result), 3)
        self.assertEqual(result[1]["start_sec"], 2.0)
        self.assertEqual(result[1]["end_sec"], 2.0)
        self.assertTrue(
            any("zero/negative duration after clamp" in line for line in cm.output)
        )


class DetectCollisionClustersTest(unittest.TestCase):
    def _entry(self, start, end):
        return {"text_zh": "x", "status": "ok", "start_sec": start, "end_sec": end}

    def test_three_plus_colliding_run_flagged(self):
        entries = [
            self._entry(0.0, 2.0),
            self._entry(50.0, 100.0),
            self._entry(60.0, 61.0),
            self._entry(61.0, 62.0),
            self._entry(62.0, 63.0),
        ]
        clusters = subtitle_builder.detect_collision_clusters(entries)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(
            clusters[0],
            {
                "start_serial": 3,
                "end_serial": 5,
                "start_sec": 60.0,
                "count": 3,
                "reason": "collision_cluster",
            },
        )

    def test_clean_input_not_flagged(self):
        entries = [
            self._entry(0.0, 2.0),
            self._entry(3.0, 5.0),
            self._entry(5.2, 7.0),
        ]
        self.assertEqual(subtitle_builder.detect_collision_clusters(entries), [])

    def test_below_min_count_not_flagged(self):
        entries = [
            self._entry(0.0, 2.0),
            self._entry(50.0, 100.0),
            self._entry(60.0, 61.0),
            self._entry(61.0, 62.0),
        ]
        self.assertEqual(subtitle_builder.detect_collision_clusters(entries), [])

    def test_custom_min_count_override(self):
        entries = [
            self._entry(0.0, 2.0),
            self._entry(50.0, 100.0),
            self._entry(60.0, 61.0),
            self._entry(61.0, 62.0),
        ]
        self.assertEqual(
            subtitle_builder.detect_collision_clusters(entries, min_count=2)[0]["count"],
            2,
        )
        self.assertEqual(
            subtitle_builder.detect_collision_clusters(entries, min_count=4), []
        )

    def test_default_min_count_reads_config(self):
        entries = [
            self._entry(0.0, 2.0),
            self._entry(50.0, 100.0),
            self._entry(60.0, 61.0),
            self._entry(61.0, 62.0),
        ]
        with mock.patch(
            "pipeline.config.SUBTITLE_COLLISION_CLUSTER_MIN_COUNT", 2
        ):
            self.assertEqual(
                len(subtitle_builder.detect_collision_clusters(entries)), 1
            )


class CollisionClusterQaTest(SubtitleBuilderBase):
    def _read_qa(self):
        return json.loads(
            (self.job_dir / "subtitle_qa.json").read_text(encoding="utf-8")
        )

    def test_qa_flags_collision_cluster_and_output_is_clean(self):
        self._write_meta(130.0)
        raw_subs = [
            {"text": "a", "start_sec": 0.0, "end_sec": 2.0},
            {"text": "b", "start_sec": 3.0, "end_sec": 100.0},
        ]
        for k in range(27):
            s = 60.0 + k
            raw_subs.append({"text": f"line-{k}", "start_sec": s, "end_sec": s + 1.0})
        self._write_raw(
            {
                "job_id": self.job_id,
                "status": "ok",
                "chunked": False,
                "segments_count": 1,
                "failed_segments": [],
                "subtitles": raw_subs,
            }
        )
        result = subtitle_builder.build_subtitle_list(
            self.job_id, upload_root=self.upload_root, auto_repair=False
        )

        for e in result:
            self.assertGreater(e["end_sec"], e["start_sec"])
        qa = self._read_qa()
        self.assertEqual(qa["duplicate_clusters"], [])
        self.assertTrue(
            any(c["reason"] == "collision_cluster" for c in qa["collision_clusters"])
        )
        self.assertEqual(len(qa["collision_clusters"]), 1)


if __name__ == "__main__":
    unittest.main()
