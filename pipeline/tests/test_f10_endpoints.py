"""App-level tests for the F10 endpoints (progress/logs/history) and F11 detail_bn.

Reuses the F9 endpoint harness (mocked Gemini/TTS/ffmpeg) so uploads actually
run. Covers:

- GET /api/jobs/{job_id}/logs — never raises, incremental ``since_line``.
- GET /history — HTML page for 0/1/3 jobs with badges + resume buttons
  (error jobs and stale-running jobs only).
- POST /jobs/{job_id}/confirm-start with ``delete_files=false`` keeps files.
- Error paths persist ``detail_bn`` next to ``detail`` (F11).
"""

import json
import os
import time
import unittest
from unittest import mock

from pipeline import (
    history_store,
    job_config,
    job_logging,
    job_status,
    subtitle_extract,
    video_ingest,
)

from pipeline.tests.test_f9_endpoints import F9EndpointsTest


class LogsEndpointTest(F9EndpointsTest):
    def test_logs_missing_job_never_raises(self):
        res = self.client.get("/api/jobs/no-such-job/logs")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"lines": [], "next_line": 0})

    def test_logs_incremental_since_line(self):
        job_logging.get_job_logger("job-logs").info("first line")
        job_logging.get_job_logger("job-logs").info("second line")
        res0 = self.client.get("/api/jobs/job-logs/logs?since_line=0")
        self.assertEqual(res0.status_code, 200)
        body0 = res0.json()
        self.assertEqual(len(body0["lines"]), 2)
        self.assertEqual(body0["next_line"], 2)

        res1 = self.client.get("/api/jobs/job-logs/logs?since_line=1")
        body1 = res1.json()
        self.assertEqual(len(body1["lines"]), 1)
        self.assertEqual(body1["next_line"], 2)

        res2 = self.client.get("/api/jobs/job-logs/logs?since_line=2")
        body2 = res2.json()
        self.assertEqual(body2["lines"], [])
        self.assertEqual(body2["next_line"], 2)

    def test_logs_negative_and_past_end_clamped(self):
        job_logging.get_job_logger("job-logs-clamp").info("only line")
        res = self.client.get("/api/jobs/job-logs-clamp/logs?since_line=-3")
        self.assertEqual(res.json()["next_line"], 1)
        res = self.client.get("/api/jobs/job-logs-clamp/logs?since_line=50")
        self.assertEqual(res.json(), {"lines": [], "next_line": 1})


class HistoryPageTest(F9EndpointsTest):
    def _register(self, job_id, **cfg):
        job_dir = self.upload_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job_config.write_config(
            job_id,
            engine=cfg.get("engine", "whisper_primary"),
            target_lang=cfg.get("target_lang", "hi"),
            voice_source=cfg.get("voice_source", "auto_tts"),
        )
        history_store.register_job(
            job_id, meta={"target_video_name": f"{job_id}.mp4"}
        )

    def test_history_html_empty(self):
        res = self.client.get("/history")
        self.assertEqual(res.status_code, 200)
        self.assertIn("No jobs yet", res.text)
        self.assertIn("ইতিহাস", res.text)

    def test_history_html_one_job(self):
        self._register("job-one")
        res = self.client.get("/history")
        self.assertEqual(res.status_code, 200)
        self.assertIn("job-one", res.text)
        self.assertIn("history-card", res.text)

    def test_history_html_three_jobs_with_meta(self):
        for i in ("a", "b", "c"):
            self._register(f"job-{i}")
        res = self.client.get("/history")
        self.assertEqual(res.status_code, 200)
        for j in ("job-a", "job-b", "job-c"):
            self.assertIn(j, res.text)
        self.assertIn("job-c.mp4", res.text)
        self.assertIn("auto_tts", res.text)

    def test_history_badges_and_resume_button(self):
        self._register("job-err")
        self._register("job-run")
        job_status.write_status("job-err", "D2_voiceover", "error")
        job_status.write_status("job-run", "D2_voiceover", "running")
        html = self.client.get("/history").text
        self.assertIn("badge-error", html)
        self.assertIn("badge-running", html)
        # Resume is offered for the error job; the fresh running job is not
        # stale, so it gets no resume button.
        self.assertIn('data-job="job-err"', html)
        self.assertNotIn('data-job="job-run"', html)

    def test_history_done_job_no_resume_button(self):
        self._register("job-done")
        job_status.write_status("job-done", "F3_final", "done")
        html = self.client.get("/history").text
        self.assertIn("badge-done", html)
        self.assertNotIn('class="resume-form"', html)

    def test_history_stale_running_gets_resume_button(self):
        self._register("job-stale")
        job_status.write_status("job-stale", "D2_voiceover", "running")
        path = job_status.status_path("job-stale")
        old = time.time() - 11 * 60
        os.utime(path, (old, old))
        html = self.client.get("/history").text
        self.assertIn('data-job="job-stale"', html)

    def test_history_json_sibling_endpoint(self):
        self._register("job-json")
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["limit"], 3)
        self.assertEqual([e["job_id"] for e in body["history"]], ["job-json"])


class ConfirmKeepFilesTest(F9EndpointsTest):
    def test_confirm_start_delete_files_false_keeps_oldest_files(self):
        # Fill history, upload a 4th job (409), then confirm with
        # delete_files=false — the oldest job is evicted from history but its
        # files stay on disk and the new job starts.
        for i in range(3):
            job_id = f"job-{i}"
            job_dir = self.upload_root / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            (job_dir / "job_meta.json").write_text(
                json.dumps({"source_filename": f"video-{i}.mp4"}), encoding="utf-8"
            )
            history_store.register_job(
                job_id, meta={"target_video_name": f"video-{i}.mp4"}
            )

        res = self.client.post(
            "/upload",
            data={"voice_source": "auto_tts"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 409, res.text)
        new_id = res.json()["detail"]["job_id"]

        confirm = self.client.post(
            f"/jobs/{new_id}/confirm-start",
            params={"evict_job_id": "job-0", "delete_files": False},
        )
        self.assertEqual(confirm.status_code, 200, confirm.text)
        self.assertTrue((self.upload_root / "job-0").is_dir())
        self.assertEqual(
            [e["job_id"] for e in self.client.get("/api/history").json()["history"]],
            [new_id, "job-2", "job-1"],
        )
        self._wait_stage(new_id, "auto_full_render")


class ErrorDetailBnTest(F9EndpointsTest):
    def _wait_state(self, job_id, state, timeout=20.0, interval=0.1):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.client.get(f"/api/jobs/{job_id}/status").json()
            if last.get("state") == state:
                return last
            time.sleep(interval)
        self.fail(f"job {job_id} did not reach {state!r} within {timeout}s; last={last}")

    def test_upload_pipeline_error_writes_detail_bn(self):
        with mock.patch.object(
            subtitle_extract,
            "extract_subtitles",
            side_effect=RuntimeError("ffmpeg error: broken pipe"),
        ):
            res = self.client.post(
                "/upload",
                data={"voice_source": "auto_tts"},
                files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
            )
            self.assertEqual(res.status_code, 200, res.text)
            job_id = res.json()["job_id"]

        status = self._wait_state(job_id, "error")
        entry = status["stages"]["upload_pipeline"]
        self.assertIn("detail", entry)
        self.assertIn("detail_bn", entry)
        self.assertIn("ffmpeg", entry["detail"])
        self.assertTrue(any(0x0980 <= ord(ch) <= 0x09FF for ch in entry["detail_bn"]))
        self.assertEqual(status["stage"], "upload_pipeline")

    def test_polling_page_renders_progress_panel_and_log_panel(self):
        res = self.client.get("/upload/no-such")
        self.assertEqual(res.status_code, 404)
        # A real polling page (fresh upload, still running) carries the
        # progress bar, stage list and log panel markup.
        res = self.client.post(
            "/upload",
            data={"voice_source": "auto_tts"},
            files={"file": ("sample.mp4", self.video_bytes, "video/mp4")},
        )
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job_id"]
        page = self.client.get(f"/upload/{job_id}")
        self.assertEqual(page.status_code, 200)
        for marker in (
            "progress-fill",
            "stage-list",
            "log-panel",
            "STAGE_LABELS_BN",
            "Processing",
        ):
            self.assertIn(marker, page.text)
        self._wait_stage(job_id, "auto_full_render")


if __name__ == "__main__":
    unittest.main()
