"""App-level tests for the F10.5 review-page state handling.

Lightweight harness (no ffmpeg needed): patch ``video_ingest.UPLOAD_ROOT`` to
a temp dir and seed ``job_status.json`` directly. Covers the two crash modes
fixed by F10.5:

- a job still ``running`` must never hit the raw-JSON 404 — GET /review/{job_id}
  redirects (3xx) to the live progress page ``/upload/{job_id}``;
- a done job whose review artifact is missing (e.g. ``edit_guideline.json``)
  returns an HTML 404 page in Bengali (``রিভিউ পাওয়া যায়নি`` + an
  "ইতিহাসে ফিরে যান" link), never a JSON body.

Also pins the regressions: a done job with artifacts still renders 200, and a
completely unknown job still 404s (now as HTML, not JSON).
"""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from pipeline import job_status, video_ingest

LINK = 'href="/static/style.css"'


class ReviewStateHandlingTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _job_dir(self, job_id):
        path = self.upload_root / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_guideline(self, job_id):
        (self._job_dir(job_id) / "edit_guideline.json").write_text(
            json.dumps(
                [
                    {
                        "serial": 1,
                        "source_start_sec": 0.0,
                        "source_end_sec": 6.0,
                        "target_start_sec": 0.0,
                        "target_end_sec": 8.0,
                        "pts_multiplier": 8.0 / 6.0,
                        "flagged": False,
                        "flag_reason": None,
                    }
                ]
            ),
            encoding="utf-8",
        )

    def test_running_job_redirects_to_polling_page(self):
        job_id = "job-running"
        self._job_dir(job_id)
        job_status.write_status(job_id, "D2_voiceover", "running")
        res = self.client.get(f"/review/{job_id}", follow_redirects=False)
        self.assertEqual(res.status_code, 302, res.text)
        self.assertEqual(res.headers["location"], f"/upload/{job_id}")

    def test_done_job_missing_artifact_is_html_404(self):
        job_id = "job-done-missing"
        self._job_dir(job_id)
        job_status.write_status(job_id, "F3_final", "done")
        res = self.client.get(f"/review/{job_id}")
        self.assertEqual(res.status_code, 404)
        self.assertIn("text/html", res.headers["content-type"])
        self.assertNotIn('"detail"', res.text)
        self.assertIn("রিভিউ পাওয়া যায়নি", res.text)
        self.assertIn("ইতিহাসে ফিরে যান", res.text)
        self.assertIn(LINK, res.text)

    def test_done_job_with_artifact_renders_200(self):
        job_id = "job-done-ok"
        self._write_guideline(job_id)
        job_status.write_status(job_id, "F3_final", "done")
        res = self.client.get(f"/review/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(f'src="/review/{job_id}/clip/1"', res.text)

    def test_unknown_job_is_html_404(self):
        res = self.client.get("/review/missing-job")
        self.assertEqual(res.status_code, 404)
        self.assertIn("text/html", res.headers["content-type"])
        self.assertNotIn('"detail"', res.text)
        self.assertIn("রিভিউ পাওয়া যায়নি", res.text)

    def test_error_job_with_artifact_still_renders_200(self):
        job_id = "job-error-renderable"
        self._write_guideline(job_id)
        job_status.write_status(job_id, "D2_voiceover", "error")
        res = self.client.get(f"/review/{job_id}")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(f'src="/review/{job_id}/clip/1"', res.text)


if __name__ == "__main__":
    unittest.main()
