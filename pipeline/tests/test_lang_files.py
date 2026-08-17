"""Tests for pipeline.lang_files (F12b Part B filename generalization).

Proves the "hi" target produces byte-identical filenames to the pre-F12b
hardcoded values (regression) and that a non-Hindi target_lang produces
correctly generalized names, both at the helper level and through a real
translator run.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import job_config, key_store, lang_files, translator, video_ingest

ZH_ENTRIES = [
    {"serial": 1, "text_zh": "你好", "start_sec": 0.0, "end_sec": 1.5},
    {"serial": 2, "text_zh": "再见", "start_sec": 1.5, "end_sec": 3.0},
]


class FilenameHelpersTest(unittest.TestCase):
    def test_hi_outputs_are_byte_identical_to_hardcoded_names(self):
        self.assertEqual(lang_files.subtitles_json("hi"), "subtitles_hi.json")
        self.assertEqual(lang_files.subtitles_srt("hi"), "subtitles_hi.srt")
        self.assertEqual(lang_files.subtitles_plain("hi"), "subtitles_hi_plain.txt")
        self.assertEqual(lang_files.timestamps_auto("hi"), "timestamps_hi_auto.json")
        self.assertEqual(lang_files.timestamps_upload("hi"), "timestamps_hi_upload.json")
        self.assertEqual(lang_files.timestamps_final("hi"), "timestamps_hi_final.json")
        self.assertEqual(lang_files.voiceover_audio("hi"), "voiceover_hi.wav")

    def test_non_hi_lang_produces_generalized_names(self):
        self.assertEqual(lang_files.subtitles_json("bn"), "subtitles_bn.json")
        self.assertEqual(lang_files.subtitles_srt("bn"), "subtitles_bn.srt")
        self.assertEqual(lang_files.subtitles_plain("bn"), "subtitles_bn_plain.txt")
        self.assertEqual(lang_files.timestamps_auto("bn"), "timestamps_bn_auto.json")
        self.assertEqual(lang_files.timestamps_upload("bn"), "timestamps_bn_upload.json")
        self.assertEqual(lang_files.timestamps_final("bn"), "timestamps_bn_final.json")
        self.assertEqual(lang_files.voiceover_audio("bn"), "voiceover_bn.wav")

    def test_source_timestamps_name_keyed_by_mode_and_lang(self):
        from pipeline import voiceover_unify

        self.assertEqual(
            voiceover_unify.source_timestamps_name("auto_tts", "hi"),
            "timestamps_hi_auto.json",
        )
        self.assertEqual(
            voiceover_unify.source_timestamps_name("user_upload", "hi"),
            "timestamps_hi_upload.json",
        )
        self.assertEqual(
            voiceover_unify.source_timestamps_name("auto_tts", "bn"),
            "timestamps_bn_auto.json",
        )
        self.assertEqual(
            voiceover_unify.source_timestamps_name("user_upload", "bn"),
            "timestamps_bn_upload.json",
        )


class TargetLangTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-lang"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def test_returns_config_target_lang(self):
        job_config.write_config(
            self.job_id, target_lang="bn", upload_root=self.upload_root
        )
        self.assertEqual(lang_files.target_lang(self.job_id, self.upload_root), "bn")

    def test_falls_back_to_hi_without_config(self):
        self.assertEqual(lang_files.target_lang(self.job_id, self.upload_root), "hi")

    def test_missing_job_falls_back_to_hi(self):
        self.assertEqual(
            lang_files.target_lang("no-such-job", self.upload_root), "hi"
        )


class TranslatorFilenameIntegrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-tx-lang"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "subtitles_zh.json").write_text(
            json.dumps(ZH_ENTRIES), encoding="utf-8"
        )

    def _translate(self, lang):
        job_config.write_config(
            self.job_id, target_lang=lang, upload_root=self.upload_root
        )
        with mock.patch.object(
            key_store, "get_active_keys", return_value=["test-key"]
        ), mock.patch.object(
            translator, "_translate_lines", return_value=["नमस्ते", "अलविदा"]
        ):
            return translator.translate_subtitles(self.job_id, upload_root=self.upload_root)

    def test_hi_translation_writes_legacy_filenames(self):
        self._translate("hi")
        self.assertTrue((self.job_dir / "subtitles_hi.json").exists())
        self.assertTrue((self.job_dir / "subtitles_hi.srt").exists())
        self.assertTrue((self.job_dir / "subtitles_hi_plain.txt").exists())

    def test_non_hi_translation_writes_generalized_filenames(self):
        self._translate("bn")
        for name in ("subtitles_bn.json", "subtitles_bn.srt", "subtitles_bn_plain.txt"):
            self.assertTrue((self.job_dir / name).exists(), f"missing {name}")
        self.assertFalse((self.job_dir / "subtitles_hi.json").exists())

    def test_hi_output_content_is_unchanged(self):
        out = self._translate("hi")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["text_hi"], "नमस्ते")
        persisted = json.loads(
            (self.job_dir / "subtitles_hi.json").read_text(encoding="utf-8")
        )
        self.assertEqual(persisted[0]["serial"], 1)


if __name__ == "__main__":
    unittest.main()
