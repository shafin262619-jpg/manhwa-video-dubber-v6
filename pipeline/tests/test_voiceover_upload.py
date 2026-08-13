"""Tests for pipeline.voiceover_upload (D3 user-uploaded voiceover alignment)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from app import app
from pipeline import key_store, video_ingest, voiceover_auto, voiceover_upload


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

    def test_gemini_failure_triggers_whisper_and_flags(self):
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

        self.assertEqual(result["status"], "whisper")
        self.assertEqual(result["alignment_source"], "whisper")
        self.assertTrue(result["fallback_used"])
        self.assertEqual(result["fallback_serials"], [1, 2])

        timestamps = self._timestamps()
        for entry in timestamps:
            self.assertTrue(entry["alignment_fallback"])
        self.assertEqual(timestamps[0]["alignment_source"], "whisper")
        self.assertAlmostEqual(timestamps[0]["start_sec"], 0.1, places=2)
        self.assertAlmostEqual(timestamps[0]["end_sec"], 1.6, places=2)
        self.assertEqual(timestamps[1]["alignment_source"], "whisper")
        self.assertAlmostEqual(timestamps[1]["start_sec"], 2.0, places=2)
        self.assertAlmostEqual(timestamps[1]["end_sec"], 3.6, places=2)

    def test_malformed_gemini_response_triggers_fallback(self):
        _make_subtitles(
            self.job_dir,
            [
                {"serial": 1, "text_zh": "A", "text_hi": "नमस्ते दुनिया"},
                {"serial": 2, "text_zh": "B", "text_hi": "आज का दिन"},
            ],
        )
        _make_audio(self.job_dir / "voiceover_hi.wav", 6.0)
        # Gemini only returned serial 1 -> incomplete -> must fall back.
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
        self.assertEqual(result["status"], "whisper")
        self.assertTrue(result["fallback_used"])
        timestamps = self._timestamps()
        self.assertAlmostEqual(timestamps[1]["start_sec"], 2.0, places=2)

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


class EdgeCaseTest(VoiceoverUploadBase):
    def test_empty_entries_writes_empty_timestamps(self):
        _make_subtitles(self.job_dir, [])
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        result = self._align()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(self._timestamps(), [])

    def test_missing_audio_raises(self):
        _make_subtitles(self.job_dir, [{"serial": 1, "text_hi": "एक"}])
        with self.assertRaises(FileNotFoundError):
            self._align()

    def test_missing_subtitles_raises(self):
        _make_audio(self.job_dir / "voiceover_hi.wav", 4.0)
        with self.assertRaises(FileNotFoundError):
            self._align()


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

    def test_upload_endpoint_saves_and_links_alignment(self):
        res = self.client.post(
            f"/voiceover/{self.job_id}/upload",
            files={"audio": ("voice.wav", self.audio_bytes, "audio/wav")},
        )
        self.assertEqual(res.status_code, 200)
        self.assertIn(f"/voiceover/{self.job_id}/align_uploaded", res.text)
        self.assertTrue((self.job_dir / "voiceover_hi.wav").exists())

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
