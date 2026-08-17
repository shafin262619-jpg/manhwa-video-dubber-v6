"""Tests for pipeline.transcript_import (F12a original-language transcript)."""

import json
import tempfile
import unittest
from pathlib import Path

from pipeline import transcript_import, video_ingest

SRT_SAMPLE = """\
1
00:00:01,000 --> 00:00:03,500
আমার নাম জন

2
00:00:04,000 --> 00:00:06,500
আমি এখানে থাকি
"""

VTT_SAMPLE = """\
WEBVTT

NOTE this is a comment block
it spans two lines

00:00:01.000 --> 00:00:03.500 align:start position:0%
আমার নাম জন

00:00:04.000 --> 00:00:06.500
আমি এখানে থাকি
"""


class ParseSrtTest(unittest.TestCase):
    def test_parses_cues_with_timing_and_text(self):
        entries = transcript_import.parse_srt(SRT_SAMPLE)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["text"], "আমার নাম জন")
        self.assertEqual(entries[0]["start_sec"], 1.0)
        self.assertEqual(entries[0]["end_sec"], 3.5)
        self.assertEqual(entries[1]["text"], "আমি এখানে থাকি")
        self.assertEqual(entries[1]["start_sec"], 4.0)
        self.assertEqual(entries[1]["end_sec"], 6.5)

    def test_accepts_dot_milliseconds_and_missing_indices(self):
        content = (
            "00:00:01.500 --> 00:00:02.250\n"
            "প্রথম লাইন\n\n"
            "00:00:02.750 --> 00:00:03.000\n"
            "দ্বিতীয় লাইন\n"
        )
        entries = transcript_import.parse_srt(content)
        self.assertEqual(entries[0]["start_sec"], 1.5)
        self.assertEqual(entries[0]["end_sec"], 2.25)
        self.assertEqual(len(entries), 2)

    def test_multiline_text_is_joined(self):
        content = (
            "1\n"
            "00:00:01,000 --> 00:00:03,000\n"
            "লাইন এক\n"
            "লাইন দুই\n\n"
        )
        entries = transcript_import.parse_srt(content)
        self.assertEqual(entries[0]["text"], "লাইন এক লাইন দুই")

    def test_empty_cues_are_skipped_but_valid_ones_kept(self):
        content = (
            "1\n"
            "00:00:01,000 --> 00:00:02,000\n"
            "\n\n"
            "2\n"
            "00:00:03,000 --> 00:00:04,000\n"
            "valid line\n"
        )
        entries = transcript_import.parse_srt(content)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["text"], "valid line")

    def test_no_valid_cues_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_srt("just some random text")

    def test_malformed_timing_line_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_srt(
                "00:00:xx,000 --> 00:00:02,000\nhello\n"
            )

    def test_empty_content_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_srt("")


class ParseVttTest(unittest.TestCase):
    def test_parses_cues_skipping_headers_and_notes(self):
        entries = transcript_import.parse_vtt(VTT_SAMPLE)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["text"], "আমার নাম জন")
        self.assertEqual(entries[0]["start_sec"], 1.0)
        self.assertEqual(entries[0]["end_sec"], 3.5)
        self.assertEqual(entries[1]["start_sec"], 4.0)
        self.assertEqual(entries[1]["end_sec"], 6.5)

    def test_cue_settings_after_end_time_ignored(self):
        content = (
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:02.000 align:start position:10%\n"
            "hello\n\n"
        )
        entries = transcript_import.parse_vtt(content)
        self.assertEqual(entries[0]["start_sec"], 1.0)
        self.assertEqual(entries[0]["end_sec"], 2.0)

    def test_no_valid_cues_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_vtt("WEBVTT\n\nNOTE nothing here\n")

    def test_malformed_timing_line_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_vtt("00:00:01.000 --> bogus\nhello\n")


class ParseFreeformTest(unittest.TestCase):
    def test_returns_non_empty_lines(self):
        lines = transcript_import.parse_freeform(
            "  প্রথম লাইন  \n\n  দ্বিতীয় লাইন\n\n\nতৃতীয় লাইন\n"
        )
        self.assertEqual(lines, ["প্রথম লাইন", "দ্বিতীয় লাইন", "তৃতীয় লাইন"])

    def test_blank_input_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_freeform("   \n\n  \n")

    def test_empty_input_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_freeform("")


class ParseTranscriptDispatchTest(unittest.TestCase):
    def test_srt_extension_dispatches_to_srt(self):
        entries, kind = transcript_import.parse_transcript(SRT_SAMPLE, "subs.srt")
        self.assertEqual(kind, "srt")
        self.assertEqual(entries[0]["start_sec"], 1.0)

    def test_vtt_extension_dispatches_to_vtt(self):
        entries, kind = transcript_import.parse_transcript(VTT_SAMPLE, "subs.vtt")
        self.assertEqual(kind, "vtt")
        self.assertEqual(len(entries), 2)

    def test_unknown_extension_uses_freeform(self):
        entries, kind = transcript_import.parse_transcript(
            "লাইন এক\nলাইন দুই\n", "transcript.txt"
        )
        self.assertEqual(kind, "freeform")
        self.assertEqual([e["text"] for e in entries], ["লাইন এক", "লাইন দুই"])
        self.assertNotIn("start_sec", entries[0])

    def test_malformed_file_raises(self):
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.parse_transcript("totally broken", "subs.srt")


class ImportTranscriptTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.upload_root = Path(self._tmp) / "uploads"
        self.job_id = "job-tx"
        self.job_dir = self.upload_root / self.job_id
        self.job_dir.mkdir(parents=True)

    def _save(self, name, content):
        (self.job_dir / name).write_text(content, encoding="utf-8")

    def _read_raw(self):
        return json.loads(
            (self.job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )

    def test_import_srt_writes_f1_schema(self):
        self._save("transcript_upload.srt", SRT_SAMPLE)
        result = transcript_import.import_transcript(
            self.job_id, upload_root=self.upload_root
        )
        self.assertEqual(result["job_id"], self.job_id)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["chunked"])
        self.assertEqual(result["segments_count"], 1)
        self.assertEqual(result["failed_segments"], [])
        self.assertEqual(result["errors"], {})
        self.assertFalse(result["whisper_used"])
        self.assertEqual(result["whisper_segments_count"], 0)
        self.assertEqual(result["gemini_hallucinated_dropped"], 0)
        self.assertEqual(len(result["subtitles"]), 2)
        self.assertEqual(result["subtitles"][0]["start_sec"], 1.0)
        self.assertEqual(result["subtitles"][0]["end_sec"], 3.5)

        persisted = self._read_raw()
        self.assertEqual(persisted["subtitles"][1]["text"], "আমি এখানে থাকি")
        self.assertEqual(persisted["subtitles"][1]["start_sec"], 4.0)

    def test_import_vtt_writes_f1_schema(self):
        self._save("transcript_upload.vtt", VTT_SAMPLE)
        result = transcript_import.import_transcript(
            self.job_id, upload_root=self.upload_root
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["subtitles"]), 2)

    def test_import_freeform_distributes_over_video_duration(self):
        (self.job_dir / "job_meta.json").write_text(
            json.dumps({"duration_sec": 9.0}), encoding="utf-8"
        )
        self._save("transcript_upload.txt", "এক\nদুই\nতিন\n")
        result = transcript_import.import_transcript(
            self.job_id, upload_root=self.upload_root
        )
        subs = result["subtitles"]
        self.assertEqual(len(subs), 3)
        self.assertEqual([s["text"] for s in subs], ["এক", "দুই", "তিন"])
        self.assertEqual(subs[0]["start_sec"], 0.0)
        self.assertEqual(subs[0]["end_sec"], 3.0)
        self.assertEqual(subs[1]["start_sec"], 3.0)
        self.assertEqual(subs[1]["end_sec"], 6.0)
        self.assertEqual(subs[2]["start_sec"], 6.0)
        self.assertEqual(subs[2]["end_sec"], 9.0)

    def test_import_freeform_without_meta_uses_line_fallback(self):
        self._save("transcript_upload.txt", "এক\nদুই\n")
        result = transcript_import.import_transcript(
            self.job_id, upload_root=self.upload_root
        )
        subs = result["subtitles"]
        self.assertEqual(subs[0]["start_sec"], 0.0)
        self.assertEqual(subs[0]["end_sec"], 1.0)
        self.assertEqual(subs[1]["start_sec"], 1.0)
        self.assertEqual(subs[1]["end_sec"], 2.0)

    def test_import_missing_transcript_raises(self):
        with self.assertRaises(FileNotFoundError):
            transcript_import.import_transcript(
                self.job_id, upload_root=self.upload_root
            )

    def test_import_malformed_transcript_raises(self):
        self._save("transcript_upload.srt", "no timing at all")
        with self.assertRaises(transcript_import.TranscriptParseError):
            transcript_import.import_transcript(
                self.job_id, upload_root=self.upload_root
            )


if __name__ == "__main__":
    unittest.main()
