"""Tests for pipeline.voiceover_upload (D3 user-uploaded voiceover alignment)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import job_config, key_store, video_ingest, voiceover_auto, voiceover_upload


def _make_audio(path, duration_sec):
    voiceover_auto._make_silence(duration_sec, path)
    return path


def _make_subtitles(job_dir, entries):
    (job_dir / "subtitles_hi.json").write_text(
        json.dumps(entries, ensure_ascii=False), encoding="utf-8"
    )


class VoiceoverUploadBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-d3"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

        self._keys_patch = mock.patch.object(
            key_store, "get_active_keys", return_value=["k1", "k2"]
        )
        self._keys_patch.start()
        self.addCleanup(self._keys_patch.stop)

    def _align(self):
        return voiceover_upload.align_uploaded_voiceover(
            self.job_id, upload_root=self.upload_root
        )

    def _timestamps(self):
        path = self.job_dir / "timestamps_hi_upload.json"
        return json.loads(path.read_text(encoding="utf-8"))


class GeminiHappyPathTest(VoiceoverUploadBase):
    def _gemini_result(self):
        return [
            {"serial": 1, "start_sec": 0.2, "end_sec": 2.4},
            {"serial": 2, "start_sec": 2.6, "end_sec": 5.0},
        ]

    def test_gemini_happy_path_aligns_every_serial(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "पहली पंक्ति"},
                {"serial": 2, "text_zh": "B", "text_hi": "दूसरी पंक्ति"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: self._gemini_result(),
        ):
            result = self._align()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["alignment_source"], "gemini")
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["fallback_serials"], [])
        self.assertEqual(result["entries_count"], 2)

        timestamps = self._timestamps()
        self.assertEqual(len(timestamps), 2)
        self.assertEqual(timestamps[0]["serial"], 1)
        self.assertAlmostEqual(timestamps[0]["start_sec"], 0.2, places=2)
        self.assertAlmostEqual(timestamps[0]["end_sec"], 2.4, places=2)
        self.assertFalse(timestamps[0]["alignment_fallback"])
        self.assertEqual(timestamps[0]["alignment_source"], "gemini")
        self.assertEqual(timestamps[1]["serial"], 2)
        self.assertAlmostEqual(timestamps[1]["start_sec"], 2.6, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 5.0, places=2)
        self.assertFalse(timestamps[1]["alignment_fallback"])

    def test_no_active_keys_still_aligns_via_fallback(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "कुछ भी"}],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        with mock.patch.object(key_store, "get_active_keys", return_value=[]):
            result = self._align()
        self.assertEqual(result["status"], "equal_split")
        self.assertTrue(result["fallback_used"])
        timestamps = self._timestamps()
        self.assertTrue(timestamps[0]["alignment_fallback"])


class WhisperFallbackTest(VoiceoverUploadBase):
    WHISPER_WORDS = [
        {"word": "नमस्ते", "start": 0.1, "end": 0.8},
        {"word": "दुनिया", "start": 0.9, "end": 1.6},
        {"word": "आज", "start": 2.0, "end": 2.5},
        {"word": "का", "start": 2.6, "end": 2.9},
        {"word": "दिन", "start": 3.0, "end": 3.6},
    ]

    def test_whisper_primary_matches_every_serial(self):
        # F8: Whisper is the primary authority. When it matches every line,
        # Gemini failing is irrelevant and the alignment is a clean "ok".
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते दुनिया"},
                {"serial": 2, "text_zh": "B", "text_hi": "आज का दिन"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)

        def boom(key, path, entries):
            raise RuntimeError("gemini down")

        with mock.patch.object(
            voiceover_upload, "_call_gemini_align", side_effect=boom
        ), mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=self.WHISPER_WORDS
        ):
            result = self._align()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["alignment_source"], "whisper")
        self.assertFalse(result["fallback_used"])
        self.assertEqual(result["fallback_serials"], [])

        timestamps = self._timestamps()
        for entry in timestamps:
            self.assertFalse(entry["alignment_fallback"])
        self.assertEqual(timestamps[0]["alignment_source"], "whisper")
        self.assertAlmostEqual(timestamps[0]["start_sec"], 0.1, places=2)
        self.assertAlmostEqual(timestamps[0]["end_sec"], 1.6, places=2)
        self.assertEqual(timestamps[1]["alignment_source"], "whisper")
        self.assertAlmostEqual(timestamps[1]["start_sec"], 2.0, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 3.6, places=2)

    def test_whisper_primary_ignores_bad_gemini_when_fully_matched(self):
        # F8: with Whisper matching every serial, Gemini (even a malformed
        # response) is never consulted — unmatched is empty.
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते दुनिया"},
                {"serial": 2, "text_zh": "B", "text_hi": "आज का दिन"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        # Gemini only returned serial 1 -> incomplete, but Whisper already
        # matched everything so this must not change the outcome.
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: [
                {"serial": 1, "start_sec": 0.0, "end_sec": 1.0}
            ],
        ), mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=self.WHISPER_WORDS
        ):
            result = self._align()
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["fallback_used"])
        timestamps = self._timestamps()
        self.assertAlmostEqual(timestamps[1]["start_sec"], 2.0, places=2)

    def test_unmatched_serial_resolved_by_gemini_within_speech_tail(self):
        # F8 secondary pass: Whisper hears only serial 1 (speech_end = 1.6s);
        # Gemini resolves serial 2 inside the speech tail -> gemini_assisted.
        words = self.WHISPER_WORDS[:2]  # "नमस्ते दुनिया" only
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते दुनिया"},
                {"serial": 2, "text_zh": "B", "text_hi": "आज का दिन"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        with mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=words
        ), mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: [
                {"serial": 2, "start_sec": 1.7, "end_sec": 2.5}
            ],
        ):
            result = self._align()

        self.assertEqual(result["status"], "gemini_assisted")
        self.assertEqual(result["alignment_source"], "whisper")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_serials"], [2])

        timestamps = self._timestamps()
        self.assertEqual(timestamps[0]["alignment_source"], "whisper")
        self.assertFalse(timestamps[0]["alignment_fallback"])
        self.assertEqual(timestamps[1]["alignment_source"], "gemini_assisted")
        self.assertTrue(timestamps[1]["alignment_fallback"])
        self.assertAlmostEqual(timestamps[1]["start_sec"], 1.7, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 2.5, places=2)
        self.assertTrue(
            any("resolved by Gemini" in w for w in result["warnings"])
        )

    def test_unmatched_serial_gemini_past_speech_tail_rejected(self):
        # F8: Gemini can never place audio past the Whisper speech tail
        # (speech_end 1.6 + WHISPER_TAIL_TOLERANCE_SEC 1.0 = 2.6). A proposed
        # end of 5.0s is rejected -> the serial stays equal_split.
        words = self.WHISPER_WORDS[:2]
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते दुनिया"},
                {"serial": 2, "text_zh": "B", "text_hi": "आज का दिन"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        with mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=words
        ), mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: [
                {"serial": 2, "start_sec": 4.5, "end_sec": 5.0}
            ],
        ):
            result = self._align()

        self.assertEqual(result["status"], "whisper")
        timestamps = self._timestamps()
        self.assertEqual(timestamps[1]["alignment_source"], "equal_split")
        self.assertTrue(timestamps[1]["alignment_fallback"])

    def test_whisper_partial_match_fills_gap(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते दुनिया"},
                {"serial": 2, "text_zh": "B", "text_hi": "  "},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: (_ for _ in ()).throw(
                RuntimeError("gemini down")
            ),
        ), mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=self.WHISPER_WORDS
        ):
            result = self._align()

        self.assertEqual(result["status"], "whisper")
        timestamps = self._timestamps()
        self.assertEqual(timestamps[0]["alignment_source"], "whisper")
        self.assertAlmostEqual(timestamps[0]["start_sec"], 0.1, places=2)
        self.assertEqual(timestamps[1]["alignment_source"], "equal_split")
        self.assertAlmostEqual(timestamps[1]["start_sec"], 1.6, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 6.0, places=2)


class EngineGatingTest(VoiceoverUploadBase):
    """F9: a ``gemini_only`` job skips the Whisper primary pass entirely even
    when Whisper is importable; only the pure-Gemini flow runs."""

    def _write_config(self, engine):
        job_config.write_config(
            self.job_id, engine=engine, upload_root=self.upload_root
        )

    def test_gemini_only_skips_whisper_primary(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते दुनिया"},
                {"serial": 2, "text_zh": "B", "text_hi": "आज का दिन"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        self._write_config("gemini_only")

        with mock.patch.object(
            voiceover_upload,
            "_transcribe_words",
            side_effect=AssertionError("whisper must not run for gemini_only"),
        ), mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: [
                {"serial": 1, "start_sec": 0.2, "end_sec": 1.0},
                {"serial": 2, "start_sec": 1.1, "end_sec": 1.9},
            ],
        ):
            result = self._align()

        self.assertEqual(result["alignment_source"], "gemini")
        self.assertFalse(result["fallback_used"])
        timestamps = self._timestamps()
        self.assertEqual(timestamps[0]["alignment_source"], "gemini")
        self.assertFalse(timestamps[0]["alignment_fallback"])
        self.assertAlmostEqual(timestamps[0]["start_sec"], 0.2, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 1.9, places=2)


class EqualSplitFallbackTest(VoiceoverUploadBase):
    def test_both_fail_uses_equal_split_and_flags(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "एक"},
                {"serial": 2, "text_zh": "B", "text_hi": "दो"},
                {"serial": 3, "text_zh": "C", "text_hi": "तीन"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)

        def boom(key, path, entries):
            raise RuntimeError("gemini down")

        with mock.patch.object(
            voiceover_upload, "_call_gemini_align", side_effect=boom
        ), mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=None
        ):
            result = self._align()

        self.assertEqual(result["status"], "equal_split")
        self.assertEqual(result["alignment_source"], "equal_split")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_serials"], [1, 2, 3])

        timestamps = self._timestamps()
        self.assertEqual(len(timestamps), 3)
        for i, entry in enumerate(timestamps):
            self.assertTrue(entry["alignment_fallback"])
            self.assertEqual(entry["alignment_source"], "equal_split")
            self.assertEqual(entry["serial"], i + 1)
            self.assertAlmostEqual(entry["start_sec"], round(i * 2.0, 3), places=2)
            self.assertAlmostEqual(entry["end_sec"], round((i + 1) * 2.0, 3), places=2)

    def test_whisper_unavailable_uses_equal_split(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "एक"}],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: (_ for _ in ()).throw(
                RuntimeError("gemini down")
            ),
        ), mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=None
        ):
            result = self._align()
        self.assertEqual(result["status"], "equal_split")
        self.assertTrue(result["fallback_used"])

    def test_equal_split_surfaces_non_blocking_warning(self):
        # A serious alignment fallback (no segment matched real audio) must be
        # surfaced as a warning on the result, but it never raises/blocks as
        # long as the audio itself has measurable content.
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "एक"}],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: (_ for _ in ()).throw(
                RuntimeError("gemini down")
            ),
        ), mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=None
        ):
            result = self._align()
        self.assertEqual(result["status"], "equal_split")
        self.assertTrue(result["warnings"])
        self.assertTrue(
            any("equal-split" in w for w in result["warnings"]),
            "warning must explain the equal-split fallback",
        )


class EdgeCaseTest(VoiceoverUploadBase):
    def test_empty_entries_writes_empty_timestamps(self):
        _make_subtitles(self.job_dir, [])
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        result = self._align()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self._timestamps(), [])

    def test_zero_duration_audio_raises_alignment_error(self):
        # No measurable audio content means no segment can be aligned to real
        # audio. This is a per-segment alignment failure (NOT a total-length
        # mismatch, which never blocks on the user_upload path) and it blocks.
        _make_subtitles(self.job_dir, [{"serial": 1, "text_hi": "एक"}])
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        with mock.patch.object(
            voiceover_upload, "_probe_audio_duration", return_value=0.0
        ):
            with self.assertRaises(voiceover_upload.VoiceoverAlignmentError):
                self._align()
        self.assertFalse((self.job_dir / "timestamps_hi_upload.json").exists())

    def test_missing_audio_raises(self):
        _make_subtitles(self.job_dir, [{"serial": 1, "text_hi": "एक"}])
        with self.assertRaises(FileNotFoundError):
            self._align()

    def test_missing_subtitles_raises(self):
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        with self.assertRaises(FileNotFoundError):
            self._align()


class DurationDriftRegressionTest(VoiceoverUploadBase):
    """Duration-drift fix (E9): the real-media QA job
    (6b2c0929-607f-4f79-a99a-76e0ed0dd5f1) rendered a 797.8s final video from
    a 522s voiceover (~53% longer, 111/226 segments flagged
    extreme_speed_ratio). Root cause: the alignment reported per-serial end
    times past the real audio length, so the target durations summed to more
    than the voiceover and E2 stretched every clip to an inflated target.
    Alignment must clamp to the probed audio duration so
    ``sum(target durations) == voiceover audio duration``."""

    def test_alignment_past_audio_end_is_clamped(self):
        # 522s of real audio (the QA job) but Gemini hallucinated timestamps
        # covering 0..797.8s. After clamping, targets must tile inside 522s.
        serials = list(range(1, 11))
        n = len(serials)
        total_sec = 522.0
        bad_total = 797.8
        _make_subtitles(
            self.job_dir,
            [{"serial": s, "text_zh": "A", "text_hi": "कुछ"} for s in serials],
        )
        (self.job_dir / "voiceover_hi.wav").write_bytes(b"\x00" * 16)
        seg = bad_total / n
        gemini_out = [
            {"serial": s, "start_sec": round((i - 1) * seg, 3),
             "end_sec": round(i * seg, 3)}
            for i, s in enumerate(serials, start=1)
        ]
        with mock.patch.object(
            voiceover_upload, "_probe_audio_duration", return_value=total_sec
        ), mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: gemini_out,
        ):
            result = self._align()

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["clamped_serials"])
        self.assertTrue(
            any("clamped" in w for w in result["warnings"]),
            "clamping must be surfaced as a warning",
        )
        timestamps = self._timestamps()
        for entry in timestamps:
            self.assertGreaterEqual(entry["start_sec"], 0.0)
            self.assertLessEqual(entry["start_sec"], total_sec)
            self.assertLessEqual(entry["end_sec"], total_sec)
        self.assertAlmostEqual(timestamps[-1]["end_sec"], total_sec, places=2)
        target_total = sum(e["end_sec"] - e["start_sec"] for e in timestamps)
        self.assertAlmostEqual(target_total, total_sec, places=2)
        self.assertAlmostEqual(result["target_total_sec"], total_sec, places=2)

    def test_within_audio_alignment_is_not_clamped(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "कुछ"},
                {"serial": 2, "text_zh": "B", "text_hi": "और"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: [
                {"serial": 1, "start_sec": 0.2, "end_sec": 2.4},
                {"serial": 2, "start_sec": 2.6, "end_sec": 5.0},
            ],
        ):
            result = self._align()
        self.assertEqual(result["clamped_serials"], [])
        self.assertFalse(result["warnings"])
        self.assertAlmostEqual(result["target_total_sec"], 4.6, places=2)


class WhisperPrimaryRegressionTest(VoiceoverUploadBase):
    """F8 acceptance: with Whisper as the primary authority, the written
    per-serial timestamps must never run past the probed audio duration —
    ``max(end_sec) <= total_sec + epsilon`` for the file — and the E9 clamp
    still protects the whisper-primary path."""

    def test_whisper_primary_timestamps_never_exceed_audio(self):
        words = [
            {"word": "एक", "start": 0.1, "end": 1.2},
            {"word": "दो", "start": 1.5, "end": 4.9},
        ]
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_hi": "एक"}, {"serial": 2, "text_hi": "दो"}],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 5.0)
        with mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=words
        ):
            result = self._align()

        self.assertEqual(result["status"], "ok")
        total = result["total_sec"]
        timestamps = self._timestamps()
        for entry in timestamps:
            self.assertLessEqual(entry["end_sec"], total + 1e-6)
        self.assertLessEqual(result["target_total_sec"], total + 1e-6)

    def test_whisper_primary_clamped_to_audio_length(self):
        # Whisper words (from the same file) end past the probed duration: the
        # E9 clamp must still pull the targets back inside the audio.
        words = [
            {"word": "एक", "start": 0.1, "end": 1.2},
            {"word": "दो", "start": 1.5, "end": 5.5},
        ]
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_hi": "एक"}, {"serial": 2, "text_hi": "दो"}],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 5.0)
        with mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=words
        ):
            result = self._align()

        total = result["total_sec"]
        self.assertTrue(result["clamped_serials"])
        timestamps = self._timestamps()
        for entry in timestamps:
            self.assertLessEqual(entry["end_sec"], total + 1e-6)
        self.assertAlmostEqual(timestamps[-1]["end_sec"], total, places=2)
        self.assertLessEqual(result["target_total_sec"], total + 1e-6)


class SaveUploadTest(VoiceoverUploadBase):
    def _wav_bytes(self, duration_sec):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "upload.wav"
            _make_audio(path, duration_sec)
            return path.read_bytes()

    def test_save_normalizes_audio_to_wav(self):
        path = voiceover_upload.save_uploaded_voiceover(
            self.job_id, self._wav_bytes(2.0), "voice.mp3",
            upload_root=self.upload_root,
        )
        self.assertEqual(path, self.job_dir / "voiceover_hi.wav")
        self.assertTrue(path.exists())
        duration = voiceover_auto._probe_audio_duration(path)
        self.assertAlmostEqual(duration, 2.0, places=1)

    def test_save_unknown_job_raises(self):
        with self.assertRaises(FileNotFoundError):
            voiceover_upload.save_uploaded_voiceover(
                "nope", b"data", "voice.wav", upload_root=self.upload_root
            )

    def test_save_unsupported_extension_raises(self):
        with self.assertRaises(voiceover_upload.UnsupportedAudioError):
            voiceover_upload.save_uploaded_voiceover(
                self.job_id, b"data", "voice.ogg", upload_root=self.upload_root
            )

    def test_save_corrupt_audio_raises(self):
        with self.assertRaises(RuntimeError):
            voiceover_upload.save_uploaded_voiceover(
                self.job_id, b"not really audio", "voice.wav",
                upload_root=self.upload_root,
            )


class VoiceoverUploadEndpointTest(VoiceoverUploadBase):
    def setUp(self):
        super().setUp()
        self._orig_upload_root = video_ingest.UPLOAD_ROOT
        video_ingest.UPLOAD_ROOT = self.upload_root
        self.addCleanup(self._restore)
        self.client = TestClient(app)
        self.audio_bytes = self._silence_bytes(2.0)

    def _restore(self):
        video_ingest.UPLOAD_ROOT = self._orig_upload_root

    def _silence_bytes(self, duration_sec):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "up.wav"
            _make_audio(path, duration_sec)
            return path.read_bytes()

    def test_upload_endpoint_saves_and_auto_continues(self):
        # FA-D2: the upload POST now saves the audio and immediately shows the
        # auto-continue polling page (user_audio_pipeline) — the old
        # "Voiceover saved — Align subtitles" page is gone. The align route is
        # still available manually (covered by test_align_page_*).
        res = self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.wav", self.audio_bytes, "audio/wav")},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue((self.job_dir / "voiceover_hi.wav").exists())
        self.assertIn("Processing your audio", res.text)
        self.assertIn("user_audio_pipeline", res.text)
        self.assertNotIn(f"/voiceover/{self.job_id}/align_uploaded", res.text)

    def test_upload_unsupported_format_400(self):
        res = self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.ogg", self.audio_bytes, "audio/ogg")},
        )
        self.assertEqual(res.status_code, 400)

    def test_upload_unknown_job_404(self):
        res = self.client.post(
            "/voiceover/missing-job/upload",
            files={"audio": ("voice.wav", self.audio_bytes, "audio/wav")},
        )
        self.assertEqual(res.status_code, 404)

    def test_upload_corrupt_audio_400(self):
        res = self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.wav", b"junk", "audio/wav")},
        )
        self.assertEqual(res.status_code, 400)

    def test_align_page_runs_and_links_downloads(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "पहली पंक्ति"}],
        )
        self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.wav", self.audio_bytes, "audio/wav")},
        )
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: [
                {"serial": 1, "start_sec": 0.2, "end_sec": 1.8}
            ],
        ):
            res = self.client.get(f"/voiceover/{self.job_id}/align_uploaded")
        self.assertEqual(res.status_code, 200)
        self.assertIn("<strong>ok</strong>", res.text)
        self.assertIn("timestamps_hi_upload.json", res.text)

    def test_align_page_flags_fallback(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "एक"}],
        )
        self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.wav", self.audio_bytes, "audio/wav")},
        )
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: (_ for _ in ()).throw(
                RuntimeError("gemini down")
            ),
        ), mock.patch.object(
            voiceover_upload, "_transcribe_words", return_value=None
        ):
            res = self.client.get(f"/voiceover/{self.job_id}/align_uploaded")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Alignment fallback used", res.text)
        self.assertIn("equal_split", res.text)

    def test_align_page_unknown_job_404(self):
        res = self.client.get("/voiceover/missing-job/align_uploaded")
        self.assertEqual(res.status_code, 404)

    def test_download_timestamps_json(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "एक"}],
        )
        self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.wav", self.audio_bytes, "audio/wav")},
        )
        with mock.patch.object(
            voiceover_upload,
            "_call_gemini_align",
            side_effect=lambda key, path, entries: [
                {"serial": 1, "start_sec": 0.2, "end_sec": 1.8}
            ],
        ):
            self.client.get(f"/voiceover/{self.job_id}/align_uploaded")
        res = self.client.get(
            f"/download/{self.job_id}/voiceover_upload?format=timestamps"
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data[0]["serial"], 1)
        self.assertFalse(data[0]["alignment_fallback"])

    def test_download_wav(self):
        _make_subtitles(
            self.job_dir,
            [{"serial": 1, "text_zh": "A", "text_hi": "एक"}],
        )
        self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.wav", self.audio_bytes, "audio/wav")},
        )
        res = self.client.get(
            f"/download/{self.job_id}/voiceover_upload?format=wav"
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["content-type"], "audio/wav")

    def test_download_bad_format_400(self):
        res = self.client.get(
            f"/download/{self.job_id}/voiceover_upload?format=bogus"
        )
        self.assertEqual(res.status_code, 400)

    def test_download_missing_job_404(self):
        res = self.client.get(
            "/download/missing-job/voiceover_upload?format=timestamps"
        )
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main()
