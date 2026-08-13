"""Tests for pipeline.dry_run_check (U5 pre-flight offline sanity gate).

Each blocking-error class gets its own test (serial gap / duplicate / count
and order mismatch / overlap / invalid duration / malformed input), plus a
clean happy-path (exit 0) and a missing-file case that must NOT block.
Fixtures are written to a temp job directory — no real job is needed.
"""

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import dry_run_check


def _write_job(root, job_id, files):
    job_dir = Path(root) / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (job_dir / name).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return job_dir


def _zh_entries(n=2):
    return [
        {"serial": i, "text_zh": f"line-{i}", "start_sec": float(i - 1),
         "end_sec": float(i)}
        for i in range(1, n + 1)
    ]


def _hi_entries(n=2, fallback_serials=()):
    entries = []
    for i in range(1, n + 1):
        entries.append({
            "serial": i,
            "text_zh": f"line-{i}",
            "text_hi": "नमस्ते" if i not in fallback_serials else "",
            "start_sec": float(i - 1),
            "end_sec": float(i),
            "translation_fallback": i in fallback_serials,
        })
    return entries


def _timestamps(n=2):
    return [
        {"serial": i, "start_sec": float(i - 1), "end_sec": float(i),
         "flagged": False, "flag_reason": None}
        for i in range(1, n + 1)
    ]


def _edit_guideline(n=2, flagged_serials=()):
    entries = []
    for i in range(1, n + 1):
        flagged = i in flagged_serials
        entries.append({
            "serial": i,
            "source_start_sec": float(i - 1),
            "source_end_sec": float(i),
            "target_start_sec": float(i - 1),
            "target_end_sec": float(i),
            "pts_multiplier": 3.0 if flagged else 1.0,
            "flagged": flagged,
            "flag_reason": "extreme_speed_ratio" if flagged else None,
        })
    return entries


class DryRunCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.root = self._tmp
        self.job_id = "dry-job"
        self.job_dir = Path(self.root) / self.job_id

    def _check(self, files):
        _write_job(self.root, self.job_id, files)
        return dry_run_check.run_checks(self.job_id, upload_root=self.root)

    def test_happy_path_all_consistent_exits_zero(self):
        report = self._check({
            "subtitles_zh.json": _zh_entries(2),
            "subtitles_hi.json": _hi_entries(2, fallback_serials=(1,)),
            "timestamps_hi_final.json": _timestamps(2),
            "edit_guideline.json": _edit_guideline(2, flagged_serials=(2,)),
        })
        self.assertEqual(report.exit_code, 0)
        self.assertEqual(report.blocking_errors, [])
        self.assertEqual(report.stats["subtitles_zh_count"], 2)
        self.assertEqual(report.stats["translation_fallback_pct"], 50.0)
        self.assertEqual(report.stats["edit_guideline_flagged"], 1)
        self.assertEqual(
            report.stats["edit_guideline_flag_reasons"], {"extreme_speed_ratio": 1}
        )

    def test_missing_files_are_not_blocking(self):
        report = self._check({"subtitles_zh.json": _zh_entries(2)})
        self.assertEqual(report.exit_code, 0)
        self.assertFalse(report.blocking_errors)
        joined = " ".join(report.notes)
        self.assertIn("not found — stage not done yet, skipped", joined)

    def test_serial_gap_blocks(self):
        entries = _zh_entries(3)
        entries = [e for e in entries if e["serial"] != 2]
        report = self._check({"subtitles_zh.json": entries})
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any("missing [2]" in e for e in report.blocking_errors))

    def test_duplicate_serial_blocks(self):
        entries = _zh_entries(2)
        entries[1]["serial"] = 1
        report = self._check({"subtitles_zh.json": entries})
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any("duplicate serial" in e for e in report.blocking_errors))

    def test_missing_required_key_blocks(self):
        entries = _zh_entries(1)
        del entries[0]["text_zh"]
        report = self._check({"subtitles_zh.json": entries})
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any("missing key(s) text_zh" in e for e in report.blocking_errors))

    def test_hi_count_mismatch_blocks(self):
        report = self._check({
            "subtitles_zh.json": _zh_entries(2),
            "subtitles_hi.json": _hi_entries(1),
        })
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(
            any("does NOT match" in e for e in report.blocking_errors)
        )

    def test_hi_order_mismatch_blocks(self):
        hi = _hi_entries(2)
        hi[0]["serial"], hi[1]["serial"] = 2, 1
        report = self._check({
            "subtitles_zh.json": _zh_entries(2),
            "subtitles_hi.json": hi,
        })
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(
            any("does NOT match" in e for e in report.blocking_errors)
        )

    def test_timestamps_count_mismatch_blocks(self):
        report = self._check({
            "subtitles_zh.json": _zh_entries(2),
            "subtitles_hi.json": _hi_entries(2),
            "timestamps_hi_final.json": _timestamps(1),
        })
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(
            any("does not match" in e for e in report.blocking_errors)
        )

    def test_invalid_duration_blocks(self):
        ts = _timestamps(1)
        ts[0]["end_sec"] = ts[0]["start_sec"]
        report = self._check({
            "subtitles_zh.json": _zh_entries(1),
            "subtitles_hi.json": _hi_entries(1),
            "timestamps_hi_final.json": ts,
        })
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any("invalid duration" in e for e in report.blocking_errors))

    def test_overlap_blocks(self):
        ts = _timestamps(2)
        ts[1]["start_sec"] = 0.5
        report = self._check({
            "subtitles_zh.json": _zh_entries(2),
            "subtitles_hi.json": _hi_entries(2),
            "timestamps_hi_final.json": ts,
        })
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any("overlaps previous" in e for e in report.blocking_errors))

    def test_malformed_json_blocks(self):
        job_dir = self.job_dir
        job_dir.mkdir(parents=True)
        (job_dir / "subtitles_zh.json").write_text("{not json", encoding="utf-8")
        report = dry_run_check.run_checks(self.job_id, upload_root=self.root)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(any("malformed" in e for e in report.blocking_errors))

    def test_unknown_job_blocks(self):
        report = dry_run_check.run_checks("does-not-exist", upload_root=self.root)
        self.assertEqual(report.exit_code, 1)
        self.assertTrue(
            any("job directory not found" in e for e in report.blocking_errors)
        )

    def test_cli_main_returns_exit_code(self):
        _write_job(self.root, self.job_id, {"subtitles_zh.json": _zh_entries(2)})
        self.assertEqual(
            dry_run_check.main(["--job-id", self.job_id, "--upload-root", self.root]),
            0,
        )
        bad_id = self.job_id + "-bad"
        _write_job(self.root, bad_id, {"subtitles_zh.json": [{"serial": 1}]})
        self.assertEqual(
            dry_run_check.main(["--job-id", bad_id, "--upload-root", self.root]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
