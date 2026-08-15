"""Tests for pipeline.subtitle_qa (E1 combined QA summary).

``build_qa_summary`` is a pure aggregation over the two already-written JSON
files (subtitle_qa.json from subtitle_builder A3/B3, subtitle_qa_whisper.json
from subtitle_verify D1), so the tests write those files directly and never
touch Gemini/Whisper.
"""

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import subtitle_qa


def _make_job(upload_root, job_id):
    job_dir = upload_root / job_id
    job_dir.mkdir(parents=True)
    return job_dir


def _write_mechanical(job_dir, gaps=None, clusters=None, repair=None):
    data = {
        "job_id": job_dir.name,
        "total_duration_sec": 60.0,
        "covered_duration_sec": 50.0,
        "entries_count": 5,
        "gaps": gaps or [],
        "duplicate_clusters": clusters or [],
    }
    if repair is not None:
        data["repair"] = repair
    (job_dir / "subtitle_qa.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )


def _write_whisper(job_dir, status):
    (job_dir / "subtitle_qa_whisper.json").write_text(
        json.dumps(
            {
                "status": status,
                "reason": None,
                "whisper_spoken_sec": 30.0,
                "extracted_covered_sec": 25.0,
                "coverage_ratio": 0.83,
                "mismatch": status == "mismatch",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


class BuildQaSummaryBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-e1"
        self.job_dir = _make_job(self.upload_root, self.job_id)

    def _summary(self):
        return subtitle_qa.build_qa_summary(
            self.job_id, upload_root=self.upload_root
        )


class CleanFilesTest(BuildQaSummaryBase):
    def test_clean_files_give_ok_and_no_warnings(self):
        _write_mechanical(self.job_dir, gaps=[], clusters=[])
        _write_whisper(self.job_dir, "ok")
        summary = self._summary()
        self.assertEqual(summary["job_id"], "job-e1")
        self.assertEqual(summary["qa_status"], "ok")
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(summary["gaps_remaining"], 0)
        self.assertEqual(summary["duplicate_clusters_remaining"], 0)
        self.assertEqual(summary["repair_attempted"], 0)
        self.assertEqual(summary["repair_succeeded"], 0)
        self.assertEqual(summary["whisper_check_status"], "ok")


class FlaggedMechanicalTest(BuildQaSummaryBase):
    def test_gaps_and_clusters_flag_with_warnings(self):
        _write_mechanical(
            self.job_dir,
            gaps=[
                {
                    "after_serial": 1,
                    "before_serial": 2,
                    "gap_start_sec": 5.0,
                    "gap_end_sec": 37.0,
                    "gap_sec": 32.0,
                },
                {
                    "after_serial": 8,
                    "before_serial": 9,
                    "gap_start_sec": 44.0,
                    "gap_end_sec": 51.0,
                    "gap_sec": 7.0,
                },
            ],
            clusters=[
                {
                    "start_serial": 3,
                    "end_serial": 6,
                    "start_sec": 40.0,
                    "count": 4,
                    "reason": "same_start_timestamp",
                }
            ],
            repair={"attempted": 2, "succeeded": 1, "failed": 1, "skipped_budget": []},
        )
        _write_whisper(self.job_dir, "ok")
        summary = self._summary()
        self.assertEqual(summary["qa_status"], "flagged")
        self.assertEqual(summary["gaps_remaining"], 2)
        self.assertEqual(summary["duplicate_clusters_remaining"], 1)
        self.assertEqual(summary["repair_attempted"], 2)
        self.assertEqual(summary["repair_succeeded"], 1)
        self.assertEqual(len(summary["warnings"]), 3)
        self.assertTrue(
            any("সেকেন্ডের একটা অংশ হয়তো বাদ পড়ে গেছে" in w for w in summary["warnings"])
        )
        self.assertTrue(
            any("serial 1-2" in w for w in summary["warnings"])
        )
        self.assertTrue(
            any("ডুপ্লিকেট টাইমিং" in w for w in summary["warnings"])
        )
        self.assertTrue(
            any("4টা লাইনে" in w for w in summary["warnings"])
        )


class WhisperMismatchTest(BuildQaSummaryBase):
    def test_whisper_mismatch_flags_summary(self):
        _write_mechanical(self.job_dir, gaps=[], clusters=[])
        _write_whisper(self.job_dir, "mismatch")
        summary = self._summary()
        self.assertEqual(summary["qa_status"], "flagged")
        self.assertEqual(summary["whisper_check_status"], "mismatch")
        self.assertEqual(len(summary["warnings"]), 1)
        self.assertIn("কভারেজ", summary["warnings"][0])


class MissingMalformedTest(BuildQaSummaryBase):
    def test_both_missing_gives_ok_with_skipped_whisper(self):
        summary = self._summary()
        self.assertEqual(summary["qa_status"], "ok")
        self.assertEqual(summary["warnings"], [])
        self.assertEqual(summary["whisper_check_status"], "skipped")
        self.assertEqual(summary["gaps_remaining"], 0)

    def test_whisper_missing_but_gaps_present_still_flags(self):
        _write_mechanical(
            self.job_dir,
            gaps=[
                {
                    "after_serial": 1,
                    "before_serial": 2,
                    "gap_start_sec": 5.0,
                    "gap_end_sec": 37.0,
                    "gap_sec": 32.0,
                }
            ],
        )
        summary = self._summary()
        self.assertEqual(summary["qa_status"], "flagged")
        self.assertEqual(summary["whisper_check_status"], "skipped")
        self.assertEqual(len(summary["warnings"]), 1)

    def test_malformed_mechanical_does_not_raise(self):
        (self.job_dir / "subtitle_qa.json").write_text("{not json", encoding="utf-8")
        _write_whisper(self.job_dir, "ok")
        summary = self._summary()
        self.assertEqual(summary["qa_status"], "ok")
        self.assertEqual(summary["gaps_remaining"], 0)

    def test_malformed_whisper_treated_as_skipped(self):
        _write_mechanical(self.job_dir, gaps=[], clusters=[])
        (self.job_dir / "subtitle_qa_whisper.json").write_text(
            "[1, 2]", encoding="utf-8"
        )
        summary = self._summary()
        self.assertEqual(summary["whisper_check_status"], "skipped")
        self.assertEqual(summary["qa_status"], "ok")


if __name__ == "__main__":
    unittest.main()
