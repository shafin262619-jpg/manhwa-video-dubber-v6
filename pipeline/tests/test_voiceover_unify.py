"""Tests for pipeline.voiceover_unify and the /voiceover/{job_id}/choose UI."""

import json
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from app import app
from pipeline import video_ingest, voiceover_unify


class VoiceoverUnifyBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-d1"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _choice_file(self):
        return self.job_dir / "voice_source_choice.json"


class VoiceSourceModuleTest(VoiceoverUnifyBase):
    def test_set_auto_tts_saves_file(self):
        data = voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        self.assertEqual(data, {"job_id": self.job_id, "mode": "auto_tts"})
        saved = json.loads(self._choice_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "auto_tts")
        self.assertEqual(
            voiceover_unify.get_voice_source(self.job_id, upload_root=self.upload_root),
            "auto_tts",
        )

    def test_set_user_upload_saves_file(self):
        data = voiceover_unify.set_voice_source(
            self.job_id, "user_upload", upload_root=self.upload_root
        )
        self.assertEqual(data["mode"], "user_upload")
        saved = json.loads(self._choice_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "user_upload")

    def test_set_overwrites_previous_choice(self):
        voiceover_unify.set_voice_source(
            self.job_id, "auto_tts", upload_root=self.upload_root
        )
        voiceover_unify.set_voice_source(
            self.job_id, "user_upload", upload_root=self.upload_root
        )
        self.assertEqual(
            voiceover_unify.get_voice_source(self.job_id, upload_root=self.upload_root),
            "user_upload",
        )

    def test_invalid_mode_raises(self):
        with self.assertRaises(voiceover_unify.InvalidVoiceSourceError):
            voiceover_unify.set_voice_source(
                self.job_id, "weird_mode", upload_root=self.upload_root
            )
        self.assertFalse(self._choice_file().exists())

    def test_unknown_job_raises(self):
        with self.assertRaises(FileNotFoundError):
            voiceover_unify.set_voice_source(
                "nope", "auto_tts", upload_root=self.upload_root
            )

    def test_get_voice_source_returns_none_when_unset(self):
        self.assertIsNone(
            voiceover_unify.get_voice_source(self.job_id, upload_root=self.upload_root)
        )


class VoiceoverChooseEndpointTest(VoiceoverUnifyBase):
    def setUp(self):
        super().setUp()
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def test_choose_page_shows_both_options(self):
        res = self.client.get(f"/voiceover/{self.job_id}/choose")
        self.assertEqual(res.status_code, 200)
        self.assertIn("auto_tts", res.text)
        self.assertIn("user_upload", res.text)

    def test_post_auto_tts_saves_and_shows_link(self):
        res = self.client.post(
            f"/voiceover/{self.job_id}/choose", data={"mode": "auto_tts"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(f"/voiceover/{self.job_id}/auto_tts", res.text)
        saved = json.loads(self._choice_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "auto_tts")

    def test_post_user_upload_saves_and_shows_form(self):
        res = self.client.post(
            f"/voiceover/{self.job_id}/choose", data={"mode": "user_upload"}
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(f"/voiceover/{self.job_id}/upload", res.text)
        self.assertIn("enctype=\"multipart/form-data\"", res.text)
        saved = json.loads(self._choice_file().read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "user_upload")

    def test_post_invalid_mode_returns_400(self):
        res = self.client.post(
            f"/voiceover/{self.job_id}/choose", data={"mode": "bogus"}
        )
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self._choice_file().exists())

    def test_post_unknown_job_returns_404(self):
        res = self.client.post(
            "/voiceover/missing-job/choose", data={"mode": "auto_tts"}
        )
        self.assertEqual(res.status_code, 404)


class VoiceoverUnifyTimestampsBase(VoiceoverUnifyBase):
    def setUp(self):
        super().setUp()
        self.job_id = "job-d4"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True, exist_ok=True)

    def _set_mode(self, mode):
        voiceover_unify.set_voice_source(
            self.job_id, mode, upload_root=self.upload_root
        )

    def _write_source(self, name, entries):
        (self.job_dir / name).write_text(
            json.dumps(entries, ensure_ascii=False), encoding="utf-8"
        )

    def _make_audio(self):
        (self.job_dir / "voiceover_hi.wav").write_bytes(b"\x00" * 16)

    def _unify(self):
        return voiceover_unify.unify_voiceover_timestamps(
            self.job_id, upload_root=self.upload_root
        )

    def _final(self):
        return json.loads(
            (self.job_dir / "timestamps_hi_final.json").read_text(encoding="utf-8")
        )


class VoiceoverUnifyTimestampsTest(VoiceoverUnifyTimestampsBase):
    def test_auto_tts_mode_unifies_correctly(self):
        self._set_mode("auto_tts")
        self._write_source(
            "timestamps_hi_auto.json",
            [
                {"serial": 1, "start_sec": 0.0, "end_sec": 1.0, "tts_failed": False},
                {"serial": 2, "start_sec": 1.0, "end_sec": 3.5, "tts_failed": True},
            ],
        )
        self._make_audio()
        result = self._unify()

        self.assertEqual(result["mode"], "auto_tts")
        self.assertEqual(result["source_timestamps"], "timestamps_hi_auto.json")
        self.assertEqual(result["entries_count"], 2)
        self.assertEqual(result["flagged_count"], 1)
        self.assertEqual(result["clamped_serials"], [])
        self.assertTrue(result["voiceover_path"].endswith("voiceover_hi.wav"))

        final = self._final()
        self.assertEqual(final[0]["serial"], 1)
        self.assertAlmostEqual(final[0]["start_sec"], 0.0, places=2)
        self.assertAlmostEqual(final[0]["end_sec"], 1.0, places=2)
        self.assertFalse(final[0]["flagged"])
        self.assertIsNone(final[0]["flag_reason"])
        self.assertEqual(final[1]["serial"], 2)
        self.assertAlmostEqual(final[1]["start_sec"], 1.0, places=2)
        self.assertAlmostEqual(final[1]["end_sec"], 3.5, places=2)
        self.assertTrue(final[1]["flagged"])
        self.assertEqual(final[1]["flag_reason"], "tts_failed")

    def test_user_upload_mode_unifies_correctly(self):
        self._set_mode("user_upload")
        self._write_source(
            "timestamps_hi_upload.json",
            [
                {
                    "serial": 1, "start_sec": 0.2, "end_sec": 2.4,
                    "alignment_fallback": False, "alignment_source": "gemini",
                },
                {
                    "serial": 2, "start_sec": 2.6, "end_sec": 5.0,
                    "alignment_fallback": True, "alignment_source": "whisper",
                },
            ],
        )
        self._make_audio()
        result = self._unify()

        self.assertEqual(result["mode"], "user_upload")
        self.assertEqual(result["source_timestamps"], "timestamps_hi_upload.json")
        self.assertEqual(result["flagged_count"], 1)

        final = self._final()
        self.assertFalse(final[0]["flagged"])
        self.assertIsNone(final[0]["flag_reason"])
        self.assertTrue(final[1]["flagged"])
        self.assertEqual(final[1]["flag_reason"], "alignment_fallback")

    def test_overlap_clamping_works(self):
        self._set_mode("auto_tts")
        self._write_source(
            "timestamps_hi_auto.json",
            [
                {"serial": 1, "start_sec": 0.0, "end_sec": 2.0, "tts_failed": False},
                {"serial": 2, "start_sec": 1.5, "end_sec": 3.0, "tts_failed": False},
                {"serial": 3, "start_sec": 2.8, "end_sec": 4.5, "tts_failed": False},
            ],
        )
        self._make_audio()
        result = self._unify()

        self.assertEqual(result["clamped_serials"], [2, 3])
        final = self._final()
        self.assertAlmostEqual(final[0]["end_sec"], 2.0, places=2)
        self.assertAlmostEqual(final[1]["start_sec"], 2.0, places=2)
        self.assertAlmostEqual(final[1]["end_sec"], 3.0, places=2)
        self.assertAlmostEqual(final[2]["start_sec"], 3.0, places=2)
        self.assertAlmostEqual(final[2]["end_sec"], 4.5, places=2)
        for i in range(1, len(final)):
            self.assertGreaterEqual(
                final[i]["start_sec"], final[i - 1]["end_sec"]
            )

    def test_collapsed_range_clamped_to_zero_length(self):
        self._set_mode("auto_tts")
        self._write_source(
            "timestamps_hi_auto.json",
            [
                {"serial": 1, "start_sec": 0.0, "end_sec": 2.0, "tts_failed": False},
                {"serial": 2, "start_sec": 1.0, "end_sec": 1.2, "tts_failed": False},
            ],
        )
        self._make_audio()
        result = self._unify()
        self.assertEqual(result["clamped_serials"], [2])
        final = self._final()
        self.assertAlmostEqual(final[1]["start_sec"], 2.0, places=2)
        self.assertAlmostEqual(final[1]["end_sec"], 2.0, places=2)

    def test_tts_failed_wins_over_alignment_fallback(self):
        self._set_mode("auto_tts")
        self._write_source(
            "timestamps_hi_auto.json",
            [
                {
                    "serial": 1, "start_sec": 0.0, "end_sec": 1.0,
                    "tts_failed": True, "alignment_fallback": True,
                },
            ],
        )
        self._make_audio()
        result = self._unify()
        final = self._final()
        self.assertTrue(final[0]["flagged"])
        self.assertEqual(final[0]["flag_reason"], "tts_failed")

    def test_entries_sorted_by_serial(self):
        self._set_mode("auto_tts")
        self._write_source(
            "timestamps_hi_auto.json",
            [
                {"serial": 2, "start_sec": 1.0, "end_sec": 2.0, "tts_failed": False},
                {"serial": 1, "start_sec": 0.0, "end_sec": 1.0, "tts_failed": False},
            ],
        )
        self._make_audio()
        result = self._unify()
        final = self._final()
        self.assertEqual([e["serial"] for e in final], [1, 2])

    def test_missing_choice_raises(self):
        self._write_source(
            "timestamps_hi_auto.json",
            [{"serial": 1, "start_sec": 0.0, "end_sec": 1.0, "tts_failed": False}],
        )
        self._make_audio()
        with self.assertRaises(FileNotFoundError):
            self._unify()

    def test_missing_source_file_raises(self):
        self._set_mode("auto_tts")
        self._make_audio()
        with self.assertRaises(FileNotFoundError):
            self._unify()

    def test_missing_audio_raises(self):
        self._set_mode("auto_tts")
        self._write_source(
            "timestamps_hi_auto.json",
            [{"serial": 1, "start_sec": 0.0, "end_sec": 1.0, "tts_failed": False}],
        )
        with self.assertRaises(FileNotFoundError):
            self._unify()

    def test_unknown_job_raises(self):
        with self.assertRaises(FileNotFoundError):
            voiceover_unify.unify_voiceover_timestamps(
                "nope", upload_root=self.upload_root
            )

    def test_wrong_source_file_for_mode_raises(self):
        self._set_mode("user_upload")
        self._write_source(
            "timestamps_hi_auto.json",
            [{"serial": 1, "start_sec": 0.0, "end_sec": 1.0, "tts_failed": False}],
        )
        self._make_audio()
        with self.assertRaises(FileNotFoundError):
            self._unify()


class VoiceoverAlignmentCompletenessTest(VoiceoverUnifyTimestampsBase):
    """Per-segment alignment validation (duration-check removal): every
    subtitle segment must have a matching voiceover timestamp. A subtitle
    serial with no timestamp means no audio was aligned for that segment,
    which is the real user_upload blocking condition — unlike a total-length
    mismatch, which is normal translation drift and never blocks."""

    def _write_subtitles(self, serials):
        self._write_source(
            "subtitles_hi.json",
            [{"serial": s, "text_zh": "A", "text_hi": "कुछ"} for s in serials],
        )

    def test_missing_subtitle_serial_raises(self):
        self._set_mode("user_upload")
        self._write_subtitles([1, 2])
        self._write_source(
            "timestamps_hi_upload.json",
            [
                {
                    "serial": 1, "start_sec": 0.0, "end_sec": 1.0,
                    "alignment_fallback": False, "alignment_source": "gemini",
                },
            ],
        )
        self._make_audio()
        with self.assertRaises(voiceover_unify.VoiceoverAlignmentError) as ctx:
            self._unify()
        self.assertIn("segment(s)", str(ctx.exception))
        self.assertFalse((self.job_dir / "timestamps_hi_final.json").exists())

    def test_all_subtitle_serials_present_passes(self):
        self._set_mode("user_upload")
        self._write_subtitles([1, 2])
        self._write_source(
            "timestamps_hi_upload.json",
            [
                {
                    "serial": 1, "start_sec": 0.0, "end_sec": 1.0,
                    "alignment_fallback": False, "alignment_source": "gemini",
                },
                {
                    "serial": 2, "start_sec": 1.0, "end_sec": 2.0,
                    "alignment_fallback": False, "alignment_source": "gemini",
                },
            ],
        )
        self._make_audio()
        result = self._unify()
        self.assertEqual(result["entries_count"], 2)
        self.assertEqual(result["missing_serials"], [])
        self.assertTrue((self.job_dir / "timestamps_hi_final.json").exists())

    def test_missing_serials_reported_in_result_when_extra_subtitle_only(self):
        # When subtitles exist and all their serials are covered, missing is [].
        self._set_mode("auto_tts")
        self._write_subtitles([1])
        self._write_source(
            "timestamps_hi_auto.json",
            [{"serial": 1, "start_sec": 0.0, "end_sec": 1.0, "tts_failed": False}],
        )
        self._make_audio()
        result = self._unify()
        self.assertEqual(result["missing_serials"], [])

    def test_without_subtitles_file_skips_completeness_check(self):
        # Backward compat: callers that only work with timestamps (no
        # subtitles_hi.json on disk) are unaffected by the check.
        self._set_mode("auto_tts")
        self._write_source(
            "timestamps_hi_auto.json",
            [{"serial": 1, "start_sec": 0.0, "end_sec": 1.0, "tts_failed": False}],
        )
        self._make_audio()
        result = self._unify()
        self.assertEqual(result["entries_count"], 1)
        self.assertEqual(result["missing_serials"], [])


if __name__ == "__main__":
    unittest.main()
