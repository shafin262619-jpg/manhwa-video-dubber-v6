"""Tests for UI1 static-asset wiring (shared stylesheet).

Verifies the shared ``/static/style.css`` is served and that every page links
it via ``<link rel="stylesheet" href="/static/style.css">``.
"""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from pipeline import job_status, video_ingest

LINK = 'href="/static/style.css"'


class StaticAssetsTest(unittest.TestCase):
    def test_stylesheet_is_served(self):
        res = TestClient(app).get("/static/style.css")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/css", res.headers["content-type"])
        self.assertIn("site-header", res.text)

    def test_home_page_links_stylesheet(self):
        res = TestClient(app).get("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(LINK, res.text)

    def test_settings_page_links_stylesheet(self):
        res = TestClient(app).get("/settings")
        self.assertEqual(res.status_code, 200)
        self.assertIn(LINK, res.text)

    def test_review_page_links_stylesheet(self):
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            job_dir = upload_root / "job-ui"
            job_dir.mkdir(parents=True)
            (job_dir / "edit_guideline.json").write_text(
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
            orig = video_ingest.UPLOAD_ROOT
            video_ingest.UPLOAD_ROOT = upload_root
            try:
                res = TestClient(app).get("/review/job-ui")
            finally:
                video_ingest.UPLOAD_ROOT = orig
        self.assertEqual(res.status_code, 200)
        self.assertIn(LINK, res.text)

    def test_final_page_links_stylesheet(self):
        # U1c: GET /final/{job_id} backgrounds the render. The stylesheet-link
        # contract applies to both the intermediate polling page and the done
        # page; seed a done final_render status so this test deterministically
        # exercises the result render (no thread race).
        with tempfile.TemporaryDirectory() as tmp:
            upload_root = Path(tmp) / "uploads"
            job_dir = upload_root / "job-ui"
            job_dir.mkdir(parents=True)
            job_status.write_status(
                "job-ui",
                "final_render",
                "done",
                extra={"result": {"status": "ok", "duration_sec": 5.0}},
                upload_root=upload_root,
            )
            orig = video_ingest.UPLOAD_ROOT
            video_ingest.UPLOAD_ROOT = upload_root
            try:
                res = TestClient(app).get("/final/job-ui")
            finally:
                video_ingest.UPLOAD_ROOT = orig
        self.assertEqual(res.status_code, 200)
        self.assertIn("Status: <strong>ok</strong>", res.text)
        self.assertIn(LINK, res.text)


if __name__ == "__main__":
    unittest.main()
