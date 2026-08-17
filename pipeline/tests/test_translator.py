"""Tests for pipeline.translator (mocked Gemini) + download endpoint."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import key_store, translator, video_ingest
from pipeline.gemini_rotation import CallBudget

ENTRIES = [
    {"serial": 1, "text_zh": "你好", "start_sec": 0.0, "end_sec": 3.2, "status": "ok"},
    {"serial": 2, "text_zh": "世界", "start_sec": 3.5, "end_sec": 5.0, "status": "ok"},
]


class TranslatorBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-c1"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(ENTRIES, ensure_ascii=False), encoding="utf-8"
        )
        self.store_path = Path(self._tmp) / "gemini_keys_store.json"
        self._orig_key_path = key_store.KEY_STORE_PATH
        key_store.KEY_STORE_PATH = self.store_path
        self.addCleanup(self._restore_key_path)

    def _restore_key_path(self):
        key_store.KEY_STORE_PATH = self._orig_key_path

    def _set_keys(self, keys):
        self.store_path.write_text(
            json.dumps({"keys": [{"id": "k1", "key": k}] for k in keys}),
            encoding="utf-8",
        )

    def _translate(self):
        return translator.translate_subtitles(
            self.job_id, upload_root=self.upload_root
        )

    def _read(self, name):
        return (self.job_dir / name).read_text(encoding="utf-8")


class TranslateSuccessTest(TranslatorBase):
    def test_happy_path_count_matches(self):
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text", return_value="Namaste\nDuniya"
        ) as fake:
            output = self._translate()

        self.assertEqual(len(output), 2)
        self.assertEqual([e["serial"] for e in output], [1, 2])
        self.assertEqual(output[0]["text_translated"], "Namaste")
        self.assertEqual(output[1]["text_translated"], "Duniya")
        self.assertFalse(output[0]["translation_fallback"])
        self.assertFalse(output[1]["translation_fallback"])
        self.assertEqual(output[0]["start_sec"], 0.0)
        self.assertEqual(output[0]["end_sec"], 3.2)
        fake.assert_called_once()

    def test_srt_and_plain_files_generated(self):
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text", return_value="Namaste\nDuniya"
        ):
            self._translate()

        srt = self._read("subtitles_hi.srt")
        self.assertIn("1\n00:00:00,000 --> 00:00:03,200\nNamaste", srt)
        self.assertIn("2\n00:00:03,500 --> 00:00:05,000\nDuniya", srt)

        plain = self._read("subtitles_hi_plain.txt")
        self.assertIn("1\tNamaste", plain)
        self.assertIn("2\tDuniya", plain)

        on_disk = json.loads(self._read("subtitles_hi.json"))
        self.assertEqual(len(on_disk), 2)


class CountMismatchTest(TranslatorBase):
    def test_mismatch_retry_then_fallback_with_original_text(self):
        # U3a: after the whole-batch normal + strict attempts both mismatch,
        # batch-split repair isolates the failures. Here every line genuinely
        # fails (even single-line chunks return nothing), so all of them fall
        # back to the original Chinese — the old whole-batch-fallback outcome.
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text",
            side_effect=["only one line", "still one line", "", ""],
        ) as fake:
            output = self._translate()

        self.assertEqual(fake.call_count, 4)
        self.assertEqual(len(output), 2)
        for entry in output:
            self.assertTrue(entry["translation_fallback"])
            self.assertEqual(entry["text_translated"], entry["text_zh"])

    def test_retry_fixes_count(self):
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text",
            side_effect=["merged line", "Namaste\nDuniya"],
        ) as fake:
            output = self._translate()

        self.assertEqual(fake.call_count, 2)
        self.assertFalse(output[0]["translation_fallback"])
        self.assertFalse(output[1]["translation_fallback"])
        self.assertEqual(output[0]["text_translated"], "Namaste")
        self.assertEqual(output[1]["text_translated"], "Duniya")

    def test_no_active_keys_falls_back(self):
        with mock.patch.object(translator, "_call_gemini_text") as fake:
            output = self._translate()
        fake.assert_not_called()
        for entry in output:
            self.assertTrue(entry["translation_fallback"])
            self.assertEqual(entry["text_translated"], entry["text_zh"])

    def test_call_budget_cap_falls_back_without_raising(self):
        # U2b: an exhausted per-job CallBudget must degrade to the original
        # Chinese fallback (translation_fallback) and never raise.
        self._set_keys(["key-a", "key-b"])
        with mock.patch.object(translator, "_call_gemini_text") as fake:
            output = translator.translate_subtitles(
                self.job_id, upload_root=self.upload_root,
                call_budget=CallBudget(max_calls=0),
            )
        fake.assert_not_called()
        for entry in output:
            self.assertTrue(entry["translation_fallback"])
            self.assertEqual(entry["text_translated"], entry["text_zh"])

    def test_empty_lines_are_kept_not_translated(self):
        entries = [
            {"serial": 1, "text_zh": "", "start_sec": 0.0, "end_sec": 3.0, "status": "extraction_failed"},
            {"serial": 2, "text_zh": "你好", "start_sec": 3.5, "end_sec": 5.0, "status": "ok"},
        ]
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        self._set_keys(["key-one"])
        with mock.patch.object(translator, "_call_gemini_text", return_value="Namaste") as fake:
            output = self._translate()

        self.assertEqual(len(output), 2)
        self.assertEqual(output[0]["text_translated"], "")
        self.assertFalse(output[0]["translation_fallback"])
        self.assertEqual(output[1]["text_translated"], "Namaste")
        self.assertFalse(output[1]["translation_fallback"])


class BatchSplitRepairTest(TranslatorBase):
    """U3a: batch-split auto-repair isolates only the truly failing lines."""

    def test_split_repair_recovers_when_halves_succeed(self):
        # Whole-batch normal + strict attempts both mismatch, but once the
        # batch is split each single-line half succeeds -> no fallback at all.
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text",
            side_effect=["merged line", "still merged", "Namaste", "Duniya"],
        ) as fake:
            output = self._translate()

        self.assertEqual(fake.call_count, 4)
        self.assertEqual(len(output), 2)
        self.assertEqual(output[0]["text_translated"], "Namaste")
        self.assertEqual(output[1]["text_translated"], "Duniya")
        self.assertFalse(output[0]["translation_fallback"])
        self.assertFalse(output[1]["translation_fallback"])

    def test_only_persistently_failing_line_falls_back(self):
        # "你好" keeps failing (single-line chunk returns nothing) while "世界"
        # translates fine -> only line 1 falls back, line 2 is translated.
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text",
            side_effect=["only one line", "still one line", "", "Duniya"],
        ) as fake:
            output = self._translate()

        self.assertEqual(fake.call_count, 4)
        self.assertTrue(output[0]["translation_fallback"])
        self.assertEqual(output[0]["text_translated"], "你好")
        self.assertFalse(output[1]["translation_fallback"])
        self.assertEqual(output[1]["text_translated"], "Duniya")

    def test_max_split_rounds_falls_back_gracefully(self):
        # max_split_rounds=1: the first split reaches the limit, the leftover
        # half-chunks fall back whole — gracefully, without raising.
        entries = [
            {"serial": i, "text_zh": f"行{i}", "start_sec": float(i), "end_sec": float(i) + 1.0, "status": "ok"}
            for i in range(1, 5)
        ]
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text",
            side_effect=["merged", "merged", "merged2", "merged2"],
        ) as fake:
            output = translator.translate_subtitles(
                self.job_id, upload_root=self.upload_root, max_split_rounds=1
            )

        self.assertEqual(fake.call_count, 4)
        self.assertEqual(len(output), 4)
        for entry in output:
            self.assertTrue(entry["translation_fallback"])
            self.assertEqual(entry["text_translated"], entry["text_zh"])


class LanguageAwareTest(TranslatorBase):
    """F12f: the C1 prompt and output files follow the job's target_lang."""

    def _write_config(self, lang):
        (self.job_dir / "job_config.json").write_text(
            json.dumps({"job_id": self.job_id, "target_lang": lang}),
            encoding="utf-8",
        )

    def test_hi_default_prompt_keeps_natural_hindi(self):
        # Regression: the pre-F12f prompt language for hi is unchanged.
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text", return_value="Namaste\nDuniya"
        ) as fake:
            self._translate()
        self.assertIn("natural Hindi", fake.call_args.args[1])

    def test_bn_job_prompts_bangla_and_writes_bn_files(self):
        self._write_config("bn")
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text", return_value="নমস্কার\nদুনিয়া"
        ) as fake:
            output = self._translate()

        prompt = fake.call_args.args[1]
        self.assertIn("natural Bangla", prompt)
        self.assertIn("Chinese lines:", prompt)
        self.assertEqual(output[0]["text_translated"], "নমস্কার")
        self.assertEqual(output[1]["text_translated"], "দুনিয়া")

        on_disk = json.loads(self._read("subtitles_bn.json"))
        self.assertEqual(on_disk[0]["text_translated"], "নমস্কার")
        self.assertNotIn("text_hi", on_disk[0])
        self.assertTrue((self.job_dir / "subtitles_bn.srt").exists())
        self.assertFalse((self.job_dir / "subtitles_hi.json").exists())

    def test_en_job_prompts_english_and_writes_en_files(self):
        self._write_config("en")
        self._set_keys(["key-one"])
        with mock.patch.object(
            translator, "_call_gemini_text", return_value="Hello\nWorld"
        ) as fake:
            output = self._translate()
        self.assertIn("natural English", fake.call_args.args[1])
        self.assertEqual(output[0]["text_translated"], "Hello")
        self.assertTrue((self.job_dir / "subtitles_en.json").exists())


class SrtTimestampTest(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(translator._srt_timestamp(0.0), "00:00:00,000")
        self.assertEqual(translator._srt_timestamp(3.2), "00:00:03,200")
        self.assertEqual(translator._srt_timestamp(3723.5), "01:02:03,500")


class DownloadEndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_dir = self.upload_root / "job-dl"
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "subtitles_hi.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nHi\n", encoding="utf-8")
        (self.job_dir / "subtitles_hi_plain.txt").write_text("1\tHi\n", encoding="utf-8")
        (self.job_dir / "subtitles_hi.json").write_text("[]", encoding="utf-8")
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def test_download_srt(self):
        res = self.client.get("/download/job-dl/subtitles?format=srt")
        self.assertEqual(res.status_code, 200)
        self.assertIn("00:00:00,000 --> 00:00:01,000", res.text)

    def test_download_txt(self):
        res = self.client.get("/download/job-dl/subtitles?format=txt")
        self.assertEqual(res.status_code, 200)
        self.assertIn("1\tHi", res.text)

    def test_download_json(self):
        res = self.client.get("/download/job-dl/subtitles?format=json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), [])

    def test_unsupported_format_returns_400(self):
        res = self.client.get("/download/job-dl/subtitles?format=mp3")
        self.assertEqual(res.status_code, 400)

    def test_missing_job_returns_404(self):
        res = self.client.get("/download/nope/subtitles?format=srt")
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
