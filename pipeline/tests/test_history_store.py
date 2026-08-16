"""Tests for pipeline.history_store (F9: 3-job history + confirm eviction)."""

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import history_store, job_config, job_status, video_ingest, voiceover_unify


class HistoryStoreBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.upload_root.mkdir(parents=True)

    def _make_job(self, job_id):
        job_dir = self.upload_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        return job_dir


class RegisterJobTest(HistoryStoreBase):
    def test_register_three_jobs_all_added(self):
        for i in range(3):
            self._make_job(f"job-{i}")
            res = history_store.register_job(f"job-{i}", upload_root=self.upload_root)
            self.assertEqual(res, {"added": True})

    def test_fourth_job_asks_for_confirm_with_oldest(self):
        for i in range(3):
            self._make_job(f"job-{i}")
            history_store.register_job(f"job-{i}", upload_root=self.upload_root)
        self._make_job("job-3")
        res = history_store.register_job("job-3", upload_root=self.upload_root)
        self.assertEqual(res["added"], False)
        self.assertEqual(res["needs_confirm"], True)
        self.assertEqual(res["would_evict"], "job-0")  # oldest, never silently dropped

    def test_register_replaces_existing_same_job(self):
        self._make_job("job-dup")
        history_store.register_job("job-dup", upload_root=self.upload_root)
        res = history_store.register_job("job-dup", upload_root=self.upload_root)
        self.assertEqual(res["added"], True)  # no duplicate entry
        self.assertEqual(len(history_store.list_history(self.upload_root)), 1)


class EvictJobTest(HistoryStoreBase):
    def _fill(self):
        for i in range(3):
            self._make_job(f"job-{i}")
            history_store.register_job(f"job-{i}", upload_root=self.upload_root)

    def test_evict_without_delete_keeps_files(self):
        self._fill()
        res = history_store.evict_job("job-0", delete_files=False, upload_root=self.upload_root)
        self.assertTrue(res["evicted"])
        self.assertTrue((self.upload_root / "job-0").is_dir())
        remaining = history_store.list_history(self.upload_root)
        self.assertNotIn("job-0", [e["job_id"] for e in remaining])

    def test_evict_with_delete_removes_dir(self):
        self._fill()
        history_store.evict_job("job-0", delete_files=True, upload_root=self.upload_root)
        self.assertFalse((self.upload_root / "job-0").exists())

    def test_evict_unknown_job_reports_not_evicted(self):
        self._fill()
        res = history_store.evict_job("no-such", upload_root=self.upload_root)
        self.assertFalse(res["evicted"])

    def test_confirm_flow_frees_a_slot_for_the_new_job(self):
        # The two-step flow: 4th job gets a 409 with would_evict; after the
        # user confirms, evict the oldest then register succeeds.
        self._fill()
        self._make_job("job-4")
        res = history_store.register_job("job-4", upload_root=self.upload_root)
        self.assertEqual(res["needs_confirm"], True)
        history_store.evict_job(res["would_evict"], delete_files=False, upload_root=self.upload_root)
        res2 = history_store.register_job("job-4", upload_root=self.upload_root)
        self.assertEqual(res2, {"added": True})


class ListHistoryTest(HistoryStoreBase):
    def test_list_history_newest_first_with_metadata(self):
        for i, job_id in enumerate(("job-a", "job-b", "job-c")):
            self._make_job(job_id)
            job_config.write_config(
                job_id,
                engine="gemini_only",
                target_lang="hi",
                voice_source="auto_tts",
                upload_root=self.upload_root,
            )
            voiceover_unify.set_voice_source(job_id, "auto_tts", self.upload_root)
            history_store.register_job(
                job_id,
                meta={"target_video_name": f"{job_id}.mp4"},
                upload_root=self.upload_root,
            )
        job_status.write_status("job-c", "upload_pipeline", "done", upload_root=self.upload_root)

        entries = history_store.list_history(self.upload_root)
        self.assertEqual([e["job_id"] for e in entries], ["job-c", "job-b", "job-a"])
        newest = entries[0]
        self.assertEqual(newest["target_video_name"], "job-c.mp4")
        self.assertEqual(newest["engine"], "gemini_only")
        self.assertEqual(newest["voice_source"], "auto_tts")
        self.assertEqual(newest["stage"], "upload_pipeline")
        self.assertEqual(newest["state"], "done")

    def test_list_history_skips_missing_job_dirs(self):
        self._make_job("job-x")
        history_store.register_job("job-x", upload_root=self.upload_root)
        # Simulate the dir being deleted on disk but the index entry remaining.
        import shutil
        shutil.rmtree(self.upload_root / "job-x")
        entries = history_store.list_history(self.upload_root)
        self.assertEqual(entries, [])

    def test_list_history_empty_index(self):
        self.assertEqual(history_store.list_history(self.upload_root), [])


if __name__ == "__main__":
    unittest.main()
