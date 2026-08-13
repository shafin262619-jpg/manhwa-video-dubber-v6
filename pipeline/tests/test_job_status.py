"""Tests for pipeline.job_status and the /api/jobs/{job_id}/status endpoint."""

import json
import tempfile
import threading
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from pipeline import job_status, video_ingest


class JobStatusModuleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.upload_root = Path(self._tmp.name) / "uploads"
        self.job_id = "job-status-test"

    def test_status_path_under_uploads(self):
        path = job_status.status_path(self.job_id, upload_root=self.upload_root)
        self.assertEqual(
            path, self.upload_root / self.job_id / "job_status.json"
        )

    def test_read_status_missing_file_returns_default(self):
        self.assertEqual(
            job_status.read_status(self.job_id, upload_root=self.upload_root),
            {"stage": "unknown", "state": "not_started"},
        )

    def test_read_status_missing_file_never_raises(self):
        status = job_status.read_status("no-such-job", upload_root=self.upload_root)
        self.assertEqual(status["stage"], "unknown")

    def test_write_status_creates_file_with_history(self):
        job_status.write_status(
            self.job_id,
            "extract",
            "running",
            extra={"processed_count": 2, "total_count": 10},
            upload_root=self.upload_root,
        )
        job_status.write_status(
            self.job_id, "translate", "done", upload_root=self.upload_root
        )

        path = self.upload_root / self.job_id / "job_status.json"
        self.assertTrue(path.exists())
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["stage"], "translate")
        self.assertEqual(data["state"], "done")
        # Older stage history is preserved, not overwritten.
        self.assertIn("extract", data["stages"])
        self.assertIn("translate", data["stages"])
        self.assertEqual(data["stages"]["extract"]["state"], "running")
        self.assertEqual(data["stages"]["extract"]["processed_count"], 2)
        self.assertEqual(data["stages"]["extract"]["total_count"], 10)

    def test_write_status_does_not_drop_older_stages(self):
        for stage in ("extract", "translate", "voiceover", "final"):
            job_status.write_status(
                self.job_id, stage, "running", upload_root=self.upload_root
            )
        data = job_status.read_status(self.job_id, upload_root=self.upload_root)
        self.assertEqual(
            set(data["stages"]),
            {"extract", "translate", "voiceover", "final"},
        )

    def test_write_status_stage_can_move_running_to_done(self):
        job_status.write_status(
            self.job_id, "extract", "running", upload_root=self.upload_root
        )
        job_status.write_status(
            self.job_id, "extract", "done", upload_root=self.upload_root
        )
        data = job_status.read_status(self.job_id, upload_root=self.upload_root)
        self.assertEqual(data["stages"]["extract"]["state"], "done")

    def test_write_status_invalid_state_raises(self):
        with self.assertRaises(ValueError):
            job_status.write_status(
                self.job_id, "extract", "hacked", upload_root=self.upload_root
            )

    def test_concurrent_write_no_race_or_corruption(self):
        stages = [f"stage-{i}" for i in range(8)]
        barrier = threading.Barrier(len(stages))

        def _writer(stage):
            barrier.wait()
            for _ in range(5):
                job_status.write_status(
                    self.job_id,
                    stage,
                    "running",
                    extra={"stage_name": stage},
                    upload_root=self.upload_root,
                )
                job_status.write_status(
                    self.job_id,
                    stage,
                    "done",
                    extra={"stage_name": stage},
                    upload_root=self.upload_root,
                )

        threads = [threading.Thread(target=_writer, args=(s,)) for s in stages]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        path = self.upload_root / self.job_id / "job_status.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        # Valid JSON with every stage's history intact (no lost updates).
        self.assertEqual(set(data["stages"]), set(stages))
        for stage in stages:
            self.assertEqual(data["stages"][stage]["state"], "done")


class JobStatusEndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._orig_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = Path(self._tmp.name) / "uploads"
        self.client = TestClient(app)

    def tearDown(self):
        video_ingest.UPLOAD_ROOT = self._orig_root

    def test_status_unknown_job_returns_default(self):
        res = self.client.get("/api/jobs/no-such-job/status")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(
            res.json(), {"stage": "unknown", "state": "not_started"}
        )

    def test_status_returns_written_status(self):
        job_status.write_status(
            "known-job", "extract", "running", upload_root=video_ingest.UPLOAD_ROOT
        )
        res = self.client.get("/api/jobs/known-job/status")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(body["stage"], "extract")
        self.assertEqual(body["state"], "running")
        self.assertEqual(body["stages"]["extract"]["state"], "running")


if __name__ == "__main__":
    unittest.main()
