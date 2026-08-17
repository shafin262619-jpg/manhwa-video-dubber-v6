"""Tests for pipeline.job_config (F9 per-job config)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import job_config, video_ingest


class JobConfigBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-cfg"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _read_file(self):
        return json.loads(
            (self.job_dir / "job_config.json").read_text(encoding="utf-8")
        )


class WriteConfigTest(JobConfigBase):
    def test_write_config_persists_all_fields(self):
        data = job_config.write_config(
            self.job_id,
            engine="gemini_only",
            target_lang="hi",
            source_lang="zh",
            voice_source="user_upload",
            upload_root=self.upload_root,
        )
        self.assertEqual(data["job_id"], self.job_id)
        self.assertEqual(data["engine"], "gemini_only")
        self.assertEqual(data["source_lang"], "zh")
        self.assertEqual(data["target_lang"], "hi")
        self.assertEqual(data["voice_source"], "user_upload")
        self.assertIsNotNone(data["created_at"])

        persisted = self._read_file()
        self.assertEqual(persisted["engine"], "gemini_only")
        self.assertEqual(persisted["target_lang"], "hi")
        self.assertEqual(persisted["voice_source"], "user_upload")

    def test_write_config_defaults(self):
        data = job_config.write_config(
            self.job_id, voice_source="auto_tts", upload_root=self.upload_root
        )
        self.assertIn(data["engine"], job_config.ALLOWED_ENGINES)
        self.assertEqual(data["target_lang"], job_config.DEFAULT_TARGET_LANG)
        self.assertIsNone(data["source_lang"])

    def test_write_config_rejects_invalid_engine(self):
        with self.assertRaises(ValueError):
            job_config.write_config(
                self.job_id, engine="bogus_engine", upload_root=self.upload_root
            )

    def test_write_config_rejects_invalid_voice_source(self):
        with self.assertRaises(ValueError):
            job_config.write_config(
                self.job_id, voice_source="bogus_mode", upload_root=self.upload_root
            )

    def test_write_config_persists_subtitle_source(self):
        data = job_config.write_config(
            self.job_id,
            voice_source="auto_tts",
            subtitle_source="user_transcript",
            upload_root=self.upload_root,
        )
        self.assertEqual(data["subtitle_source"], "user_transcript")
        persisted = self._read_file()
        self.assertEqual(persisted["subtitle_source"], "user_transcript")

    def test_write_config_default_subtitle_source(self):
        data = job_config.write_config(
            self.job_id, voice_source="auto_tts", upload_root=self.upload_root
        )
        self.assertEqual(data["subtitle_source"], "gemini_extract")

    def test_write_config_rejects_invalid_subtitle_source(self):
        with self.assertRaises(ValueError):
            job_config.write_config(
                self.job_id, subtitle_source="bogus_source", upload_root=self.upload_root
            )

    def test_write_config_persists_bn_target_lang(self):
        data = job_config.write_config(
            self.job_id,
            voice_source="auto_tts",
            target_lang="bn",
            upload_root=self.upload_root,
        )
        self.assertEqual(data["target_lang"], "bn")
        persisted = self._read_file()
        self.assertEqual(persisted["target_lang"], "bn")

    def test_write_config_rejects_invalid_target_lang(self):
        with self.assertRaises(ValueError):
            job_config.write_config(
                self.job_id, target_lang="fr", upload_root=self.upload_root
            )


class ReadConfigTest(JobConfigBase):
    def test_read_config_round_trip(self):
        job_config.write_config(
            self.job_id,
            engine="gemini_only",
            target_lang="hi",
            voice_source="auto_tts",
            upload_root=self.upload_root,
        )
        cfg = job_config.read_config(self.job_id, self.upload_root)
        self.assertEqual(cfg["engine"], "gemini_only")
        self.assertEqual(cfg["target_lang"], "hi")
        self.assertEqual(cfg["voice_source"], "auto_tts")

    def test_read_config_pre_f9_job_returns_defaults(self):
        # A job dir exists but has no job_config.json (pre-F9 upload): reads
        # back sensible defaults instead of failing.
        cfg = job_config.read_config(self.job_id, self.upload_root)
        self.assertIn(cfg["engine"], job_config.ALLOWED_ENGINES)
        self.assertEqual(cfg["target_lang"], "hi")
        self.assertEqual(cfg["voice_source"], "auto_tts")

    def test_read_config_unknown_job_returns_none(self):
        self.assertIsNone(
            job_config.read_config("no-such-job", self.upload_root)
        )

    def test_read_config_corrupt_file_returns_defaults(self):
        (self.job_dir / "job_config.json").write_text(
            "{not json", encoding="utf-8"
        )
        cfg = job_config.read_config(self.job_id, self.upload_root)
        self.assertIn(cfg["engine"], job_config.ALLOWED_ENGINES)

    def test_read_config_subtitle_source_round_trip(self):
        job_config.write_config(
            self.job_id,
            voice_source="auto_tts",
            subtitle_source="user_transcript",
            upload_root=self.upload_root,
        )
        cfg = job_config.read_config(self.job_id, self.upload_root)
        self.assertEqual(cfg["subtitle_source"], "user_transcript")

    def test_read_config_pre_f9_defaults_to_gemini_extract(self):
        # A job dir that exists but has no config file (pre-F9) defaults to
        # the F1 extraction path, never the transcript path.
        cfg = job_config.read_config(self.job_id, self.upload_root)
        self.assertEqual(cfg["subtitle_source"], "gemini_extract")

    def test_read_config_sanitizes_invalid_subtitle_source(self):
        job_config.write_config(
            self.job_id,
            voice_source="auto_tts",
            subtitle_source="user_transcript",
            upload_root=self.upload_root,
        )
        path = self.job_dir / "job_config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["subtitle_source"] = "bogus_source"
        path.write_text(json.dumps(data), encoding="utf-8")
        cfg = job_config.read_config(self.job_id, self.upload_root)
        self.assertEqual(cfg["subtitle_source"], "gemini_extract")

    def test_read_config_sanitizes_invalid_target_lang(self):
        job_config.write_config(
            self.job_id,
            voice_source="auto_tts",
            target_lang="bn",
            upload_root=self.upload_root,
        )
        path = self.job_dir / "job_config.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["target_lang"] = "fr"
        path.write_text(json.dumps(data), encoding="utf-8")
        cfg = job_config.read_config(self.job_id, self.upload_root)
        self.assertEqual(cfg["target_lang"], job_config.DEFAULT_TARGET_LANG)


class DefaultEngineTest(JobConfigBase):
    def test_default_engine_reflects_whisper_importability(self):
        with mock.patch.object(job_config, "whisper_importable", return_value=True):
            self.assertEqual(job_config.default_engine(), "whisper_primary")
        with mock.patch.object(job_config, "whisper_importable", return_value=False):
            self.assertEqual(job_config.default_engine(), "gemini_only")


if __name__ == "__main__":
    unittest.main()
