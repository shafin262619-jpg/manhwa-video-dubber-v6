"""Tests for F15 Part 2A: api_limit_wait status schema + rate-limit helpers.

Covers the additive ``api_limit_wait`` state in ``job_status``, the
``api_limit_wait`` record block read/write helpers, the shared
``is_rate_limit_result`` detection helper and the ``compute_next_retry``
back-off schedule. No pipeline wiring is tested here — that is a later chunk.
"""

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from pipeline import job_status, subtitle_extract


class ApiLimitWaitStateTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.upload_root = Path(self._tmp.name) / "uploads"
        self.job_id = "job-api-limit-wait"

    def test_write_status_accepts_api_limit_wait_state(self):
        job_status.write_status(
            self.job_id, "translate", "api_limit_wait",
            extra={"detail": "all keys exhausted"},
            upload_root=self.upload_root,
        )
        data = job_status.read_status(self.job_id, upload_root=self.upload_root)
        self.assertEqual(data["stage"], "translate")
        self.assertEqual(data["state"], "api_limit_wait")
        entry = data["stages"]["translate"]
        self.assertEqual(entry["state"], "api_limit_wait")
        self.assertEqual(entry["detail"], "all keys exhausted")

    def test_existing_states_still_handled_after_addition(self):
        # The new state is additive: running/done/error keep working, and a
        # stage can move from api_limit_wait to done (e.g. after a retry).
        job_status.write_status(
            self.job_id, "translate", "api_limit_wait",
            upload_root=self.upload_root,
        )
        job_status.write_status(
            self.job_id, "translate", "running", upload_root=self.upload_root
        )
        job_status.write_status(
            self.job_id, "translate", "done", upload_root=self.upload_root
        )
        data = job_status.read_status(self.job_id, upload_root=self.upload_root)
        self.assertEqual(data["state"], "done")
        self.assertEqual(data["stages"]["translate"]["state"], "done")

    def test_write_segment_status_accepts_api_limit_wait_state(self):
        job_status.write_status(
            self.job_id, "segmented_pipeline", "running",
            upload_root=self.upload_root,
        )
        job_status.init_segments(
            self.job_id,
            {"segments": [{"index": 0, "start_sec": 0, "end_sec": 10}]},
            upload_root=self.upload_root,
        )
        job_status.write_segment_status(
            self.job_id, 0, "E2_draft", "api_limit_wait",
            upload_root=self.upload_root,
        )
        entry = job_status.read_segment_status(
            self.job_id, 0, upload_root=self.upload_root
        )
        self.assertEqual(entry["state"], "api_limit_wait")
        self.assertEqual(entry["stage"], "E2_draft")

    def test_write_status_still_rejects_unknown_states(self):
        with self.assertRaises(ValueError):
            job_status.write_status(
                self.job_id, "extract", "hacked", upload_root=self.upload_root
            )


class ComputeNextRetryTest(unittest.TestCase):
    def setUp(self):
        self.hit = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    def test_attempt_1_is_24h_after_hit(self):
        retry = job_status.compute_next_retry(self.hit, 1)
        self.assertEqual(retry, self.hit + timedelta(hours=24))

    def test_attempt_2_is_1h_after_attempt_1_not_another_24h(self):
        attempt_1 = job_status.compute_next_retry(self.hit, 1)
        attempt_2 = job_status.compute_next_retry(self.hit, 2)
        self.assertEqual(attempt_2, attempt_1 + timedelta(hours=1))
        self.assertEqual(attempt_2, self.hit + timedelta(hours=25))

    def test_attempt_3_is_1h_after_attempt_2(self):
        attempt_2 = job_status.compute_next_retry(self.hit, 2)
        attempt_3 = job_status.compute_next_retry(self.hit, 3)
        self.assertEqual(attempt_3, attempt_2 + timedelta(hours=1))
        self.assertEqual(attempt_3, self.hit + timedelta(hours=26))

    def test_accepts_iso_string_hit_at(self):
        retry = job_status.compute_next_retry(self.hit.isoformat(), 1)
        self.assertEqual(retry, self.hit + timedelta(hours=24))

    def test_naive_datetime_treated_as_utc(self):
        naive = self.hit.replace(tzinfo=None)
        retry = job_status.compute_next_retry(naive, 1)
        self.assertEqual(retry.tzinfo, timezone.utc)
        self.assertEqual(retry, self.hit + timedelta(hours=24))

    def test_attempt_number_below_1_raises(self):
        with self.assertRaises(ValueError):
            job_status.compute_next_retry(self.hit, 0)


class RecordApiLimitWaitTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.upload_root = Path(self._tmp.name) / "uploads"
        self.job_id = "job-api-limit-wait"
        self.hit = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    def test_record_writes_block_and_transitions_state(self):
        block = job_status.record_api_limit_wait(
            self.job_id, "translate", hit_at=self.hit,
            upload_root=self.upload_root,
        )
        self.assertEqual(block["stage"], "translate")
        self.assertEqual(block["hit_at"], self.hit.isoformat())
        self.assertEqual(block["next_retry_at"],
                         (self.hit + timedelta(hours=24)).isoformat())
        self.assertEqual(block["attempt_count"], 1)

        data = job_status.read_status(self.job_id, upload_root=self.upload_root)
        self.assertEqual(data["api_limit_wait"], block)
        self.assertEqual(data["stage"], "translate")
        self.assertEqual(data["state"], "api_limit_wait")
        entry = data["stages"]["translate"]
        self.assertEqual(entry["state"], "api_limit_wait")
        self.assertEqual(entry["attempt_count"], 1)
        self.assertEqual(entry["next_retry_at"], block["next_retry_at"])

    def test_attempt_count_increments_and_backoff_widens(self):
        job_status.record_api_limit_wait(
            self.job_id, "translate", hit_at=self.hit,
            upload_root=self.upload_root,
        )
        block = job_status.record_api_limit_wait(
            self.job_id, "translate", hit_at=self.hit,
            upload_root=self.upload_root,
        )
        self.assertEqual(block["attempt_count"], 2)
        self.assertEqual(block["next_retry_at"],
                         (self.hit + timedelta(hours=25)).isoformat())

    def test_explicit_attempt_count_is_respected(self):
        block = job_status.record_api_limit_wait(
            self.job_id, "voiceover", hit_at=self.hit, attempt_count=3,
            upload_root=self.upload_root,
        )
        self.assertEqual(block["attempt_count"], 3)
        self.assertEqual(block["next_retry_at"],
                         (self.hit + timedelta(hours=26)).isoformat())

    def test_get_api_limit_wait_returns_none_when_never_recorded(self):
        self.assertIsNone(
            job_status.get_api_limit_wait(self.job_id, upload_root=self.upload_root)
        )

    def test_get_api_limit_wait_returns_recorded_block(self):
        block = job_status.record_api_limit_wait(
            self.job_id, "extract", hit_at=self.hit,
            upload_root=self.upload_root,
        )
        self.assertEqual(
            job_status.get_api_limit_wait(self.job_id, upload_root=self.upload_root),
            block,
        )


class IsRateLimitResultTest(unittest.TestCase):
    def test_true_for_rate_limit_dict(self):
        self.assertTrue(
            subtitle_extract.is_rate_limit_result(
                {"type": "rate_limit", "message": "429 quota exceeded"}
            )
        )

    def test_false_for_network_transient_error(self):
        self.assertFalse(
            subtitle_extract.is_rate_limit_result(
                {"type": "transient", "message": "connection timed out"}
            )
        )
        self.assertFalse(
            subtitle_extract.is_rate_limit_result(
                {"type": "network", "message": "socket error"}
            )
        )

    def test_false_for_malformed_error(self):
        self.assertFalse(
            subtitle_extract.is_rate_limit_result(
                {"type": "permanent", "message": "malformed JSON"}
            )
        )
        self.assertFalse(
            subtitle_extract.is_rate_limit_result(
                {"type": "non_rotatable", "message": "invalid request"}
            )
        )

    def test_false_for_unrelated_dict(self):
        self.assertFalse(
            subtitle_extract.is_rate_limit_result({"status": "ok", "count": 5})
        )

    def test_false_for_none_result(self):
        self.assertFalse(subtitle_extract.is_rate_limit_result(None))

    def test_false_for_success_result(self):
        self.assertFalse(
            subtitle_extract.is_rate_limit_result(
                {"status": "ok", "errors": {}}
            )
        )

    def test_false_for_non_dict_input(self):
        self.assertFalse(subtitle_extract.is_rate_limit_result("rate_limit"))


if __name__ == "__main__":
    unittest.main()
