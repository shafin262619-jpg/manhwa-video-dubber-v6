"""Tests for F12c Part B: unresolved-segment flag + ask-user actions.

Covers the derivation of unresolved segments from the on-disk QA/translation
artifacts (repair regions left after the exhausted automatic retries +
``translation_fallback`` lines), the Bengali warning builder, the persist/
load registry, the user-initiated retry/accept actions in
``pipeline.unresolved``, and the HTTP wiring: the ``upload_pipeline`` status
extra carries the warning, ``POST /jobs/{id}/unresolved/retry`` and
``/accept`` update the registry + status, nothing-to-act-on is a clean 404,
and the upload result page surfaces the ask-user card non-blockingly.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import (
    job_config,
    job_status as job_status_store,
    key_store,
    render_final,
    subtitle_builder,
    translator,
    unresolved,
    video_ingest,
)


def _require_tools():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise unittest.SkipTest("ffmpeg/ffprobe not available")


def _write_qa_with_repair(job_dir, attempted=3):
    (job_dir / "subtitle_qa.json").write_text(
        json.dumps(
            {
                "job_id": job_dir.name,
                "total_duration_sec": 20.0,
                "covered_duration_sec": 15.0,
                "entries_count": 2,
                "gaps": [
                    {"gap_start_sec": 4.0, "gap_end_sec": 6.5, "gap_sec": 2.5}
                ],
                "duplicate_clusters": [],
                "collision_clusters": [],
                "repair": {
                    "attempted": attempted,
                    "succeeded": 0,
                    "failed": attempted,
                    "skipped_budget": [],
                },
            }
        ),
        encoding="utf-8",
    )


def _write_hi_with_fallback(job_dir, serial=7):
    (job_dir / "subtitles_hi.json").write_text(
        json.dumps(
            [
                {
                    "serial": serial,
                    "text_zh": "你好",
                    "text_hi": "你好",
                    "translation_fallback": True,
                    "start_sec": 0.0,
                    "end_sec": 1.5,
                }
            ]
        ),
        encoding="utf-8",
    )


def _write_zh_entries(job_dir):
    (job_dir / "subtitles_zh.json").write_text(
        json.dumps(
            [
                {"text_zh": "你好", "status": "ok", "start_sec": 0.0, "end_sec": 1.5},
                {"text_zh": "世界", "status": "ok", "start_sec": 3.0, "end_sec": 4.5},
            ]
        ),
        encoding="utf-8",
    )


class UnresolvedDerivationTest(unittest.TestCase):
    """Unit coverage of item derivation + warning building."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore_paths)
        self.job_dir = self.upload_root / "job-u"
        self.job_dir.mkdir(parents=True)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def test_warning_empty_when_nothing_unresolved(self):
        self.assertEqual(unresolved.build_warning_bn([]), "")

    def test_warning_names_regions_serials_and_actions(self):
        items = [
            {"id": "repair-a", "kind": "repair", "start_sec": 4.0,
             "end_sec": 6.5, "state": "unresolved"},
            {"id": "translation-7", "kind": "translation", "serial": 7,
             "state": "unresolved"},
        ]
        bn = unresolved.build_warning_bn(items)
        self.assertIn("4.0–6.5 সেকেন্ড", bn)
        self.assertIn("সিরিয়াল 7", bn)
        self.assertIn("একবার আবার চেষ্টা করুন", bn)
        self.assertIn("মেনে নিন / বাদ দিন", bn)

    def test_warning_caps_repair_and_translation_shown(self):
        repair = [
            {"id": f"r{i}", "kind": "repair", "start_sec": float(i),
             "end_sec": float(i) + 1.0, "state": "unresolved"}
            for i in range(5)
        ]
        translation = [
            {"id": f"t{i}", "kind": "translation", "serial": i,
             "state": "unresolved"}
            for i in range(8)
        ]
        bn = unresolved.build_warning_bn(repair + translation)
        self.assertIn("আরও 2টি", bn)
        self.assertIn("আরও 3টি", bn)

    def test_collect_derives_repair_items_from_qa(self):
        _write_qa_with_repair(self.job_dir)
        _write_zh_entries(self.job_dir)
        items, warning = unresolved.collect_unresolved("job-u")
        kinds = {i["kind"] for i in items}
        self.assertEqual(kinds, {"repair"})
        self.assertTrue(warning)
        self.assertEqual(items[0]["start_sec"], 4.0)
        self.assertEqual(items[0]["end_sec"], 6.5)

    def test_collect_derives_translation_items_from_fallback(self):
        _write_qa_with_repair(self.job_dir, attempted=0)
        _write_hi_with_fallback(self.job_dir)
        items, _warning = unresolved.collect_unresolved("job-u")
        self.assertEqual([i["kind"] for i in items], ["translation"])
        self.assertEqual(items[0]["serial"], 7)

    def test_load_missing_file_returns_empty(self):
        self.assertEqual(unresolved.load_unresolved("job-u"), [])

    def test_persist_roundtrip(self):
        items = [{"id": "x", "kind": "repair", "state": "unresolved"}]
        unresolved.persist_unresolved("job-u", items)
        self.assertEqual(unresolved.load_unresolved("job-u"), items)


class UnresolvedActionTest(unittest.TestCase):
    """Unit coverage of apply_retry / apply_accept."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore_paths)
        self.job_dir = self.upload_root / "job-a"
        self.job_dir.mkdir(parents=True)
        self._mocks = []
        self.addCleanup(self._stop_mocks)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _stop_mocks(self):
        for patch in self._mocks:
            patch.stop()

    def _patch(self, target, attribute, **kwargs):
        patch = mock.patch.object(target, attribute, **kwargs)
        patch.start()
        self._mocks.append(patch)
        return getattr(target, attribute)

    def test_apply_accept_marks_all_and_clears_warning(self):
        items = [{"id": "r", "kind": "repair", "state": "unresolved"}]
        unresolved.persist_unresolved("job-a", items)
        accepted, warning = unresolved.apply_accept("job-a")
        self.assertTrue(all(i["state"] == "accepted" for i in accepted))
        self.assertEqual(warning, "")
        self.assertTrue(
            all(i["state"] == "accepted" for i in unresolved.load_unresolved("job-a"))
        )

    def test_apply_accept_raises_when_nothing(self):
        with self.assertRaises(RuntimeError):
            unresolved.apply_accept("job-a")

    def test_apply_retry_raises_when_nothing(self):
        with self.assertRaises(RuntimeError):
            unresolved.apply_retry("job-a")

    def test_apply_retry_runs_repair_refresh_translate_and_increments(self):
        _write_qa_with_repair(self.job_dir)
        _write_zh_entries(self.job_dir)
        _write_hi_with_fallback(self.job_dir)
        items, _warning = unresolved.collect_unresolved("job-a")
        unresolved.persist_unresolved("job-a", items)

        repair = self._patch(
            subtitle_builder, "repair_flagged_regions",
            return_value=(
                [
                    {"text_zh": "你好", "status": "ok",
                     "start_sec": 0.0, "end_sec": 1.5},
                ],
                {
                    "attempted": 1, "succeeded": 0, "failed": 1,
                    "skipped_budget": [],
                },
            ),
        )
        refresh = self._patch(subtitle_builder, "refresh_qa")
        translate = self._patch(translator, "translate_subtitles")

        merged, warning = unresolved.apply_retry("job-a")

        repair.assert_called_once()
        refresh.assert_called_once()
        translate.assert_called_once_with("job-a", upload_root=self.upload_root, call_budget=mock.ANY)
        self.assertTrue(all(i["user_retries"] >= 1 for i in merged))
        self.assertTrue(warning)
        persisted = unresolved.load_unresolved("job-a")
        self.assertEqual(len(persisted), len(items))
        self.assertTrue(all(i["user_retries"] >= 1 for i in persisted))

    def test_apply_retry_malformed_zh_raises_valueerror(self):
        unresolved.persist_unresolved(
            "job-a", [{"id": "r", "kind": "repair", "state": "unresolved"}]
        )
        (self.job_dir / "subtitles_zh.json").write_text(
            "{bad", encoding="utf-8"
        )
        with self.assertRaises(ValueError):
            unresolved.apply_retry("job-a")


class UnresolvedHttpTest(unittest.TestCase):
    """HTTP-level: endpoints + the ask-user card on the upload result page."""

    def setUp(self):
        _require_tools()
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.key_store_path = Path(self._tmp) / "gemini_keys_store.json"
        self.output_root = Path(self._tmp) / "outputs"
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        self._orig_key_store = key_store.KEY_STORE_PATH
        self._orig_output_root = render_final.OUTPUT_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        key_store.KEY_STORE_PATH = self.key_store_path
        render_final.OUTPUT_ROOT = self.output_root
        self.addCleanup(self._restore_paths)

        self.client = TestClient(app)
        self.client.post("/settings/keys", data={"key": "test-gemini-key"})
        self._mocks = []
        self.addCleanup(self._stop_mocks)

    def _restore_paths(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root
        key_store.KEY_STORE_PATH = self._orig_key_store
        render_final.OUTPUT_ROOT = self._orig_output_root

    def _stop_mocks(self):
        for patch in self._mocks:
            patch.stop()

    def _patch(self, target, attribute, **kwargs):
        patch = mock.patch.object(target, attribute, **kwargs)
        patch.start()
        self._mocks.append(patch)
        return getattr(target, attribute)

    def _make_job(self, job_id, items, stage_extra=None):
        job_dir = self.upload_root / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        job_config.write_config(
            job_id, voice_source="user_upload", subtitle_source="gemini_extract"
        )
        unresolved.persist_unresolved(job_id, items, self.upload_root)
        extra = {
            "extraction_status": "ok",
            "serials": 2,
            "whisper_check_status": "ok",
        }
        if stage_extra:
            extra.update(stage_extra)
        job_status_store.write_status(
            job_id, "upload_pipeline", "done", extra=extra,
            upload_root=self.upload_root,
        )
        return job_dir

    def _item(self, kind, **kw):
        base = {"id": f"{kind}-1", "kind": kind, "state": "unresolved"}
        base.update(kw)
        return base

    def test_retry_endpoint_refreshes_status_and_returns_items(self):
        item = self._item("repair", start_sec=4.0, end_sec=6.5)
        self._make_job("job-retry", [item])

        def _apply_retry(job_id, upload_root=None):
            merged = [{**item, "user_retries": 1}]
            unresolved.persist_unresolved(job_id, merged, upload_root)
            return merged, unresolved.build_warning_bn(merged)

        apply = self._patch(unresolved, "apply_retry", side_effect=_apply_retry)

        res = self.client.post("/jobs/job-retry/unresolved/retry")
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        apply.assert_called_once_with("job-retry")
        self.assertEqual(body["unresolved"][0]["user_retries"], 1)
        self.assertIn("একবার আবার চেষ্টা করুন", body["warning_bn"])

        stage = self.client.get("/api/jobs/job-retry/status").json()[
            "stages"
        ]["upload_pipeline"]
        self.assertIn("unresolved_warning_bn", stage)
        self.assertIn("unresolved_segments", stage)
        self.assertEqual(stage["unresolved_segments"][0]["user_retries"], 1)

    def test_retry_endpoint_404_when_nothing_unresolved(self):
        self._make_job("job-noretry", [])
        self._patch(
            unresolved, "apply_retry",
            side_effect=RuntimeError("job job-noretry has no unresolved segments"),
        )
        res = self.client.post("/jobs/job-noretry/unresolved/retry")
        self.assertEqual(res.status_code, 404)
        self.assertIn("no unresolved", res.text)

    def test_accept_endpoint_clears_warning_from_status(self):
        item = self._item("translation", serial=7)
        self._make_job("job-acc", [item])

        def _apply_accept(job_id, upload_root=None):
            accepted = [{**item, "state": "accepted"}]
            unresolved.persist_unresolved(job_id, accepted, upload_root)
            return accepted, ""

        self._patch(unresolved, "apply_accept", side_effect=_apply_accept)

        res = self.client.post("/jobs/job-acc/unresolved/accept")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["accepted"])

        stage = self.client.get("/api/jobs/job-acc/status").json()[
            "stages"
        ]["upload_pipeline"]
        self.assertNotIn("unresolved_warning_bn", stage)
        self.assertTrue(stage.get("unresolved_accepted"))
        self.assertTrue(
            all(
                i["state"] == "accepted"
                for i in stage.get("unresolved_segments", [])
            )
        )

    def test_accept_endpoint_404_when_nothing_to_accept(self):
        self._make_job("job-noacc", [])
        self._patch(
            unresolved, "apply_accept",
            side_effect=RuntimeError("job job-noacc has no unresolved segments"),
        )
        res = self.client.post("/jobs/job-noacc/unresolved/accept")
        self.assertEqual(res.status_code, 404)

    def test_upload_page_shows_ask_user_card_when_unresolved(self):
        item = self._item("repair", start_sec=4.0, end_sec=6.5)
        warning_bn = unresolved.build_warning_bn([item])
        self._make_job(
            "job-card",
            [item],
            stage_extra={
                "unresolved_warning_bn": warning_bn,
                "unresolved_segments": [item],
            },
        )
        res = self.client.get("/upload/job-card")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("সমস্যাযুক্ত সেগমেন্ট", res.text)
        self.assertIn("একবার আবার চেষ্টা করুন", res.text)
        self.assertIn("মেনে নিন / বাদ দিন", res.text)
        self.assertIn("/jobs/job-card/unresolved/retry", res.text)

    def test_upload_page_hides_card_when_nothing_unresolved(self):
        self._make_job("job-nocard", [])
        res = self.client.get("/upload/job-nocard")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertNotIn("সমস্যাযুক্ত সেগমেন্ট", res.text)


if __name__ == "__main__":
    unittest.main()
