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


if __name__ == "__main__":
    unittest.main()
