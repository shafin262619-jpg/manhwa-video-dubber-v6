"""Tests for pipeline.edit_guideline (E1 speed-ratio edit guideline)."""

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import edit_guideline, video_ingest


def _zh_entry(serial, start, end):
    return {
        "serial": serial,
        "text_zh": "text",
        "start_sec": start,
        "end_sec": end,
        "status": "ok",
    }


def _hi_entry(serial, start, end, flagged=False, flag_reason=None):
    return {
        "serial": serial,
        "start_sec": start,
        "end_sec": end,
        "flagged": flagged,
        "flag_reason": flag_reason,
    }


class EditGuidelineBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-e1"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _write_zh(self, entries):
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def _write_hi(self, entries):
        (self.job_dir / "timestamps_hi_final.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def _build(self):
        return edit_guideline.build_edit_guideline(
            self.job_id, upload_root=self.upload_root
        )

    def _guideline(self):
        return json.loads(
            (self.job_dir / "edit_guideline.json").read_text(encoding="utf-8")
        )

    def _guideline_from_build(self):
        self._build()
        return self._guideline()


class NormalRatioTest(EditGuidelineBase):
    def test_slow_down_multiplier_above_one(self):
        # 6s source vs 8s dialog -> 8/6 = 1.3333
        self._write_zh([_zh_entry(1, 0.0, 6.0)])
        self._write_hi([_hi_entry(1, 0.0, 8.0)])
        result = self._build()

        self.assertEqual(result["entries_count"], 1)
        self.assertEqual(result["flagged_count"], 0)
        entry = self._guideline()[0]
        self.assertEqual(entry["serial"], 1)
        self.assertAlmostEqual(entry["source_start_sec"], 0.0, places=2)
        self.assertAlmostEqual(entry["source_end_sec"], 6.0, places=2)
        self.assertAlmostEqual(entry["target_start_sec"], 0.0, places=2)
        self.assertAlmostEqual(entry["target_end_sec"], 8.0, places=2)
        self.assertAlmostEqual(entry["pts_multiplier"], 8.0 / 6.0, places=4)
        self.assertFalse(entry["flagged"])
        self.assertIsNone(entry["flag_reason"])

    def test_speed_up_multiplier_below_one(self):
        # 8s source vs 4s dialog -> 0.5 (boundary, not flagged)
        self._write_zh([_zh_entry(1, 0.0, 8.0)])
        self._write_hi([_hi_entry(1, 0.0, 4.0)])
        entry = self._guideline_from_build()[0]
        self.assertAlmostEqual(entry["pts_multiplier"], 0.5, places=4)
        self.assertFalse(entry["flagged"])
        self.assertIsNone(entry["flag_reason"])

    def test_multiple_serials_preserve_order_and_timing(self):
        self._write_zh([_zh_entry(1, 0.0, 3.0), _zh_entry(2, 3.0, 7.0)])
        self._write_hi([_hi_entry(1, 0.0, 2.0), _hi_entry(2, 2.0, 6.0)])
        entries = self._guideline_from_build()
        self.assertEqual([e["serial"] for e in entries], [1, 2])
        self.assertAlmostEqual(entries[0]["pts_multiplier"], 2.0 / 3.0, places=4)
        self.assertAlmostEqual(entries[1]["pts_multiplier"], 4.0 / 4.0, places=4)
        self.assertFalse(entries[0]["flagged"])
        self.assertFalse(entries[1]["flagged"])


class ExtremeRatioTest(EditGuidelineBase):
    def test_too_fast_ratio_flagged_but_kept(self):
        # 10s dialog vs 2s source -> 5.0 > MAX
        self._write_zh([_zh_entry(1, 0.0, 2.0)])
        self._write_hi([_hi_entry(1, 0.0, 10.0)])
        result = self._build()

        self.assertEqual(result["entries_count"], 1)
        self.assertEqual(result["flagged_count"], 1)
        entry = self._guideline()[0]
        self.assertAlmostEqual(entry["pts_multiplier"], 5.0, places=4)
        self.assertTrue(entry["flagged"])
        self.assertEqual(entry["flag_reason"], "extreme_speed_ratio")

    def test_too_slow_ratio_flagged_but_kept(self):
        # 0.1s dialog vs 10s source -> 0.01 < MIN
        self._write_zh([_zh_entry(1, 0.0, 10.0)])
        self._write_hi([_hi_entry(1, 0.0, 0.1)])
        result = self._build()

        self.assertEqual(result["entries_count"], 1)
        entry = self._guideline()[0]
        self.assertAlmostEqual(entry["pts_multiplier"], 0.01, places=4)
        self.assertTrue(entry["flagged"])
        self.assertEqual(entry["flag_reason"], "extreme_speed_ratio")

    def test_extreme_entry_does_not_block_neighbours(self):
        self._write_zh([_zh_entry(1, 0.0, 2.0), _zh_entry(2, 2.0, 8.0)])
        self._write_hi([_hi_entry(1, 0.0, 10.0), _hi_entry(2, 10.0, 13.0)])
        entries = self._guideline_from_build()
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0]["flagged"])
        self.assertEqual(entries[0]["flag_reason"], "extreme_speed_ratio")
        self.assertFalse(entries[1]["flagged"])


class EdgeCaseTest(EditGuidelineBase):
    def test_zero_source_duration_safe_default(self):
        self._write_zh([_zh_entry(1, 5.0, 5.0)])
        self._write_hi([_hi_entry(1, 0.0, 2.0)])
        result = self._build()
        self.assertEqual(result["entries_count"], 1)
        self.assertEqual(result["flagged_count"], 1)
        entry = self._guideline()[0]
        self.assertAlmostEqual(entry["pts_multiplier"], 1.0, places=4)
        self.assertTrue(entry["flagged"])
        self.assertEqual(entry["flag_reason"], "invalid_duration")

    def test_negative_source_duration_safe_default(self):
        self._write_zh([_zh_entry(1, 5.0, 3.0)])
        self._write_hi([_hi_entry(1, 0.0, 2.0)])
        entry = self._guideline_from_build()[0]
        self.assertAlmostEqual(entry["pts_multiplier"], 1.0, places=4)
        self.assertTrue(entry["flagged"])
        self.assertEqual(entry["flag_reason"], "invalid_duration")

    def test_zero_target_duration_safe_default(self):
        self._write_zh([_zh_entry(1, 0.0, 6.0)])
        self._write_hi([_hi_entry(1, 0.0, 0.0)])
        entry = self._guideline_from_build()[0]
        self.assertAlmostEqual(entry["pts_multiplier"], 1.0, places=4)
        self.assertTrue(entry["flagged"])
        self.assertEqual(entry["flag_reason"], "invalid_duration")

    def test_negative_target_duration_safe_default(self):
        self._write_zh([_zh_entry(1, 0.0, 6.0)])
        self._write_hi([_hi_entry(1, 2.0, 1.0)])
        entry = self._guideline_from_build()[0]
        self.assertAlmostEqual(entry["pts_multiplier"], 1.0, places=4)
        self.assertTrue(entry["flagged"])
        self.assertEqual(entry["flag_reason"], "invalid_duration")

    def test_invalid_edge_does_not_crash_job(self):
        self._write_zh(
            [_zh_entry(1, 0.0, 6.0), _zh_entry(2, 6.0, 6.0), _zh_entry(3, 6.0, 10.0)]
        )
        self._write_hi(
            [_hi_entry(1, 0.0, 4.0), _hi_entry(2, 4.0, 4.0), _hi_entry(3, 4.0, 8.0)]
        )
        entries = self._guideline_from_build()
        self.assertEqual(len(entries), 3)
        self.assertFalse(entries[0]["flagged"])
        self.assertTrue(entries[1]["flagged"])
        self.assertEqual(entries[1]["flag_reason"], "invalid_duration")
        self.assertAlmostEqual(entries[2]["pts_multiplier"], 1.0, places=4)


class InputErrorTest(EditGuidelineBase):
    def test_missing_job_raises(self):
        with self.assertRaises(FileNotFoundError):
            edit_guideline.build_edit_guideline(
                "nope", upload_root=self.upload_root
            )

    def test_missing_zh_raises(self):
        self._write_hi([_hi_entry(1, 0.0, 1.0)])
        with self.assertRaises(FileNotFoundError):
            self._build()

    def test_missing_hi_raises(self):
        self._write_zh([_zh_entry(1, 0.0, 1.0)])
        with self.assertRaises(FileNotFoundError):
            self._build()

    def test_malformed_zh_raises(self):
        (self.job_dir / "subtitles_zh.json").write_text("{not json", encoding="utf-8")
        self._write_hi([_hi_entry(1, 0.0, 1.0)])
        with self.assertRaises(ValueError):
            self._build()

    def test_serial_only_in_one_input_is_skipped(self):
        self._write_zh([_zh_entry(1, 0.0, 6.0), _zh_entry(2, 6.0, 10.0)])
        self._write_hi([_hi_entry(1, 0.0, 3.0)])
        result = self._build()
        self.assertEqual(result["entries_count"], 1)
        entries = self._guideline()
        self.assertEqual([e["serial"] for e in entries], [1])


if __name__ == "__main__":
    unittest.main()
