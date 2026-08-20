"""Tests for F15 Part 2A: api_limit_wait status schema + rate-limit helpers.

Covers the additive ``api_limit_wait`` state in ``job_status``, the
``api_limit_wait`` record block read/write helpers, the shared
``is_rate_limit_result`` detection helper and the ``compute_next_retry``
back-off schedule. F15 Part 2B (added later): the stage-level wiring —
extraction / translation / voiceover-auto transition to ``api_limit_wait``
and raise :class:`job_status.ApiLimitWaitError` on quota exhaustion, while
``run_stage``/``_write_error_status`` never clobber the wait state with an
``error`` status.
"""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import app as app_module
from pipeline import job_status, key_store, subtitle_extract, translator, video_ingest, voiceover_auto


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


# ---------------------------------------------------------------------------
# F15 Part 2B: stage wiring — ApiLimitWaitError, run_stage re-raise, and
# detection in the three Gemini stage functions.
# ---------------------------------------------------------------------------


class ApiLimitWaitStageWiringTest(unittest.TestCase):
    """run_stage re-raises ApiLimitWaitError without writing error status."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = Path(self._tmp.name) / "uploads"
        self.job_id = "job-api-wiring"
        self.job_dir = video_ingest.UPLOAD_ROOT / self.job_id
        self.job_dir.mkdir(parents=True)

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_root

    def test_run_stage_reraises_api_limit_wait_without_error_status(self):
        """run_stage must not write ``error`` when the stage raised ApiLimitWaitError."""
        def boom(job_id):
            job_status.record_api_limit_wait(
                job_id, "C1_translate", upload_root=video_ingest.UPLOAD_ROOT,
            )
            raise job_status.ApiLimitWaitError("rate-limited")

        with self.assertRaises(job_status.ApiLimitWaitError):
            job_status.run_stage(
                self.job_id, "C1_translate", boom, self.job_id,
            )

        data = job_status.read_status(self.job_id)
        self.assertEqual(data["state"], "api_limit_wait")
        self.assertEqual(
            data["stages"]["C1_translate"]["state"], "api_limit_wait",
        )

    def test_other_exceptions_still_write_error(self):
        """run_stage must still write ``error`` for non-ApiLimitWaitError exceptions."""
        def boom(job_id):
            raise RuntimeError("something else broke")

        with self.assertRaises(RuntimeError):
            job_status.run_stage(
                self.job_id, "extract", boom, self.job_id,
            )

        data = job_status.read_status(self.job_id)
        self.assertEqual(data["state"], "error")
        self.assertEqual(data["stages"]["extract"]["state"], "error")


class ExtractApiLimitWaitTest(unittest.TestCase):
    """extract_subtitles raises ApiLimitWaitError + records api_limit_wait."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = Path(self._tmp.name) / "uploads"
        self.job_id = "job-extract-rate-limit"
        self.job_dir = video_ingest.UPLOAD_ROOT / self.job_id
        self.job_dir.mkdir(parents=True)
        # A short source.mp4 (any bytes — ffprobe is never called because
        # job_meta.json provides duration_sec).
        (self.job_dir / "source.mp4").write_bytes(b"dummy video content")
        (self.job_dir / "job_meta.json").write_text(
            '{"duration_sec": 10}', encoding="utf-8",
        )
        self._keys_patch = mock.patch.object(
            key_store, "get_active_keys", return_value=["k1"],
        )
        self._keys_patch.start()
        self.addCleanup(self._keys_patch.stop)

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_root

    def test_rate_limit_raises_and_records_wait(self):
        with mock.patch.object(
            subtitle_extract, "call_with_rotation",
            return_value=(None, 0, {"type": "rate_limit", "message": "429 quota"}),
        ):
            with self.assertRaises(job_status.ApiLimitWaitError):
                subtitle_extract.extract_subtitles(
                    self.job_id, upload_root=video_ingest.UPLOAD_ROOT,
                )

        data = job_status.read_status(self.job_id)
        self.assertEqual(data["state"], "api_limit_wait")
        self.assertEqual(
            data["api_limit_wait"]["stage"], "F1_extract",
        )

    def test_other_error_keeps_old_behavior(self):
        """A non-rate-limit error does NOT raise — extraction returns normally."""
        with mock.patch.object(
            subtitle_extract, "call_with_rotation",
            return_value=(None, 0, {"type": "permanent", "message": "bad request"}),
        ):
            result = subtitle_extract.extract_subtitles(
                self.job_id, upload_root=video_ingest.UPLOAD_ROOT,
            )

        self.assertEqual(result["status"], "extraction_failed")


class TranslateApiLimitWaitTest(unittest.TestCase):
    """translate_subtitles raises ApiLimitWaitError + records api_limit_wait."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = Path(self._tmp.name) / "uploads"
        self.job_id = "job-translate-rate-limit"
        self.job_dir = video_ingest.UPLOAD_ROOT / self.job_id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps([
                {"serial": 1, "text_zh": "你好", "start_sec": 0.0, "end_sec": 3.2},
            ]),
            encoding="utf-8",
        )
        self._keys_patch = mock.patch.object(
            key_store, "get_active_keys", return_value=["k1"],
        )
        self._keys_patch.start()
        self.addCleanup(self._keys_patch.stop)

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_root

    def test_rate_limit_raises_and_records_wait(self):
        with mock.patch.object(
            subtitle_extract, "call_with_rotation",
            return_value=(None, 0, {"type": "rate_limit", "message": "429 quota"}),
        ):
            with self.assertRaises(job_status.ApiLimitWaitError):
                translator.translate_subtitles(
                    self.job_id, upload_root=video_ingest.UPLOAD_ROOT,
                )

        data = job_status.read_status(self.job_id)
        self.assertEqual(data["state"], "api_limit_wait")
        self.assertEqual(
            data["api_limit_wait"]["stage"], "C1_translate",
        )

    def test_call_budget_exceeded_keeps_old_behavior(self):
        """A call_budget_exceeded error falls back, never raises."""
        with mock.patch.object(
            subtitle_extract, "call_with_rotation",
            return_value=(
                None, 0, {"type": "call_budget_exceeded", "message": "budget", "used": 5, "max_calls": 5}
            ),
        ):
            output = translator.translate_subtitles(
                self.job_id, upload_root=video_ingest.UPLOAD_ROOT,
            )

        self.assertEqual(len(output), 1)
        self.assertTrue(output[0]["translation_fallback"])


class VoiceoverApiLimitWaitTest(unittest.TestCase):
    """generate_auto_voiceover raises ApiLimitWaitError + records api_limit_wait."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = Path(self._tmp.name) / "uploads"
        self.job_id = "job-voiceover-rate-limit"
        self.job_dir = video_ingest.UPLOAD_ROOT / self.job_id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "subtitles_hi.json").write_text(
            json.dumps([
                {"serial": 1, "text_zh": "你好", "text_hi": "Namaste",
                 "start_sec": 0.0, "end_sec": 3.2},
            ]),
            encoding="utf-8",
        )
        self._keys_patch = mock.patch.object(
            key_store, "get_active_keys", return_value=["k1"],
        )
        self._keys_patch.start()
        self.addCleanup(self._keys_patch.stop)

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_root

    def test_rate_limit_raises_and_records_wait(self):
        with mock.patch.object(
            subtitle_extract, "call_with_rotation",
            return_value=(None, 0, {"type": "rate_limit", "message": "429 quota"}),
        ):
            with self.assertRaises(job_status.ApiLimitWaitError):
                voiceover_auto.generate_auto_voiceover(
                    self.job_id, upload_root=video_ingest.UPLOAD_ROOT,
                )

        data = job_status.read_status(self.job_id)
        self.assertEqual(data["state"], "api_limit_wait")
        self.assertEqual(
            data["api_limit_wait"]["stage"], "D2_voiceover",
        )

    def test_other_tts_failure_keeps_silence_fallback(self):
        """A non-rate-limit TTS failure still uses silence placeholder."""
        with mock.patch.object(
            subtitle_extract, "call_with_rotation",
            return_value=(None, 0, {"type": "non_rotatable", "message": "content blocked"}),
        ):
            result = voiceover_auto.generate_auto_voiceover(
                self.job_id, upload_root=video_ingest.UPLOAD_ROOT,
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["failed_serials"], [1])


# ---------------------------------------------------------------------------
# F15 Part 2C: automatic retry execution once next_retry_at has passed.
# ---------------------------------------------------------------------------


class ApiLimitWaitRetryTest(unittest.TestCase):
    """Auto-retry dispatcher tests — the poll-triggered retry mechanism."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = Path(self._tmp.name) / "uploads"
        self.job_id = "job-api-retry"
        self.hit = datetime(2020, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_root

    # -- helpers -----------------------------------------------------------

    def _record_wait(self, stage="C1_translate", hit_at=None):
        job_status.record_api_limit_wait(
            self.job_id, stage, hit_at=hit_at or self.hit,
            upload_root=video_ingest.UPLOAD_ROOT,
        )

    # -- guard: future next_retry_at ---------------------------------------

    def test_stays_waiting_while_next_retry_in_future(self):
        # Fresh hit at now -> next_retry_at = now + 24h (still in the future).
        job_status.record_api_limit_wait(
            self.job_id, "C1_translate",
            upload_root=video_ingest.UPLOAD_ROOT,
        )
        with mock.patch.object(app_module, "_start_stage") as start:
            started = app_module._retry_due_api_limit_wait(self.job_id)
        self.assertFalse(started)
        start.assert_not_called()
        data = job_status.read_status(self.job_id)
        self.assertEqual(data["state"], "api_limit_wait")

    # -- dispatch: whole-video stages --------------------------------------

    def test_f1_extract_retry_via_upload_pipeline(self):
        self._record_wait(stage="F1_extract")
        with mock.patch.object(app_module, "_start_stage") as start:
            started = app_module._retry_due_api_limit_wait(self.job_id)
        self.assertTrue(started)
        start.assert_called_once_with(
            self.job_id, "upload_pipeline", app_module._run_upload_pipeline,
        )

    def test_c1_translate_retry_via_upload_pipeline(self):
        self._record_wait(stage="C1_translate")
        with mock.patch.object(app_module, "_start_stage") as start:
            started = app_module._retry_due_api_limit_wait(self.job_id)
        self.assertTrue(started)
        start.assert_called_once_with(
            self.job_id, "upload_pipeline", app_module._run_upload_pipeline,
        )

    def test_d2_voiceover_retry_via_voiceover_auto(self):
        self._record_wait(stage="D2_voiceover")
        with mock.patch.object(app_module, "_start_stage") as start:
            started = app_module._retry_due_api_limit_wait(self.job_id)
        self.assertTrue(started)
        start.assert_called_once_with(
            self.job_id, "voiceover_auto", app_module._run_voiceover_auto,
        )

    # -- dispatch: segmented job routes to resume --------------------------

    def test_segmented_job_retry_via_resume(self):
        # Create a segment plan so is_segmented returns True.
        from pipeline import segmentation
        seg_dir = segmentation.segments_root(self.job_id)
        seg_dir.mkdir(parents=True, exist_ok=True)
        (seg_dir / "segment_plan.json").write_text(
            json.dumps({
                "segments": [
                    {"index": 0, "start_sec": 0, "end_sec": 10},
                    {"index": 1, "start_sec": 10, "end_sec": 20},
                ],
            }),
            encoding="utf-8",
        )
        self._record_wait(stage="C1_translate")
        with mock.patch.object(app_module, "_start_stage") as start:
            started = app_module._retry_due_api_limit_wait(self.job_id)
        self.assertTrue(started)
        start.assert_called_once_with(
            self.job_id, "resume", app_module._run_resume,
        )

    # -- stale block guard ------------------------------------------------

    def test_stale_block_after_done_does_not_retrigger(self):
        self._record_wait()
        job_status.write_status(
            self.job_id, "C1_translate", "done",
            upload_root=video_ingest.UPLOAD_ROOT,
        )
        # Block exists but state is "done" — no retry.
        with mock.patch.object(app_module, "_start_stage") as start:
            started = app_module._retry_due_api_limit_wait(self.job_id)
        self.assertFalse(started)
        start.assert_not_called()

    # -- second exhaustion recomputes next_retry_at -----------------------

    def test_second_exhaustion_recomputes_next_retry(self):
        attempt_1 = job_status.record_api_limit_wait(
            self.job_id, "C1_translate", hit_at=self.hit,
            upload_root=video_ingest.UPLOAD_ROOT,
        )
        self.assertEqual(attempt_1["attempt_count"], 1)
        self.assertEqual(
            attempt_1["next_retry_at"],
            (self.hit + timedelta(hours=24)).isoformat(),
        )

        attempt_2 = job_status.record_api_limit_wait(
            self.job_id, "C1_translate", hit_at=self.hit,
            upload_root=video_ingest.UPLOAD_ROOT,
        )
        self.assertEqual(attempt_2["attempt_count"], 2)
        self.assertEqual(
            attempt_2["next_retry_at"],
            (self.hit + timedelta(hours=25)).isoformat(),
        )

    # -- _run_resume guard ------------------------------------------------

    def test_run_resume_skips_error_status_on_api_limit_wait(self):
        self._record_wait()
        with mock.patch.object(
            app_module.resume, "resume_job",
            side_effect=job_status.ApiLimitWaitError("wait"),
        ):
            app_module._run_resume(self.job_id)
        # State must still be api_limit_wait — the error handler skipped it.
        data = job_status.read_status(self.job_id)
        self.assertEqual(data["state"], "api_limit_wait")

    # -- _retry_time_passed ------------------------------------------------

    def test_retry_time_passed_past(self):
        self.assertTrue(
            app_module._retry_time_passed(
                (self.hit + timedelta(hours=1)).isoformat()
            )
        )

    def test_retry_time_passed_future(self):
        future = datetime.now(timezone.utc) + timedelta(hours=48)
        self.assertFalse(app_module._retry_time_passed(future.isoformat()))

    def test_retry_time_passed_none(self):
        self.assertFalse(app_module._retry_time_passed(None))

    def test_retry_time_passed_invalid(self):
        self.assertFalse(app_module._retry_time_passed("not-a-date"))


if __name__ == "__main__":
    unittest.main()
