"""Tests for pipeline.whisper_align (F8 shared local-Whisper helpers).

Whisper is a heavy optional dependency and is not installed in this
environment, so the transcription helpers are tested with a fake ``whisper``
module injected via ``sys.modules`` (mirroring test_subtitle_verify).
"""

import sys
import types
import unittest
from unittest import mock

from pipeline import whisper_align


def _fake_whisper_patch(transcribe_impl):
    fake = types.ModuleType("whisper")

    def _load_model(name):
        return types.SimpleNamespace(transcribe=transcribe_impl)

    fake.load_model = _load_model
    return mock.patch.dict(sys.modules, {"whisper": fake})


def _no_whisper_patch():
    return mock.patch.dict(sys.modules, {"whisper": None})


def _segments_result(segments):
    return {"segments": segments}


class OverlapRatioTest(unittest.TestCase):
    def test_fully_inside_is_one(self):
        self.assertEqual(whisper_align.overlap_ratio(0.6, 1.4, 0.5, 1.5), 1.0)

    def test_partial_overlap_is_fraction(self):
        # [0, 6] vs [4, 10]: overlap [4,6] = 2 of a 6-length span.
        self.assertAlmostEqual(whisper_align.overlap_ratio(0.0, 6.0, 4.0, 10.0), 2.0 / 6.0)

    def test_disjoint_is_zero(self):
        self.assertEqual(whisper_align.overlap_ratio(0.0, 1.0, 2.0, 3.0), 0.0)
        self.assertEqual(whisper_align.overlap_ratio(2.0, 3.0, 0.0, 1.0), 0.0)

    def test_zero_length_span_is_zero(self):
        self.assertEqual(whisper_align.overlap_ratio(2.0, 2.0, 0.0, 4.0), 0.0)


class LastSpeechEndTest(unittest.TestCase):
    def test_segment_schema_uses_end_sec(self):
        items = [
            {"text": "a", "start_sec": 0.0, "end_sec": 1.5},
            {"text": "b", "start_sec": 2.0, "end_sec": 3.5},
        ]
        self.assertEqual(whisper_align.last_speech_end(items), 3.5)

    def test_word_schema_uses_end(self):
        items = [{"word": "a", "start": 0.1, "end": 1.2}, {"word": "b", "start": 1.5, "end": 4.9}]
        self.assertEqual(whisper_align.last_speech_end(items), 4.9)

    def test_empty_returns_zero(self):
        self.assertEqual(whisper_align.last_speech_end([]), 0.0)
        self.assertEqual(whisper_align.last_speech_end(None), 0.0)


class TranscribeSegmentsTest(unittest.TestCase):
    def test_returns_parsed_segments(self):
        def impl(path, **kwargs):
            self.assertEqual(kwargs.get("language"), None)
            return _segments_result(
                [
                    {"text": "你好", "start": 0.1, "end": 1.4},
                    {"text": "世界", "start": 1.6, "end": 2.2},
                ]
            )

        with _fake_whisper_patch(impl):
            segments = whisper_align.transcribe_segments("/tmp/a.wav")
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0], {"text": "你好", "start_sec": 0.1, "end_sec": 1.4})

    def test_no_whisper_returns_none(self):
        with _no_whisper_patch():
            self.assertIsNone(whisper_align.transcribe_segments("/tmp/a.wav"))

    def test_transcribe_failure_returns_none(self):
        def boom(path, **kwargs):
            raise RuntimeError("model exploded")

        with _fake_whisper_patch(boom):
            self.assertIsNone(whisper_align.transcribe_segments("/tmp/a.wav"))

    def test_empty_segments_returns_empty_list(self):
        with _fake_whisper_patch(lambda path, **kw: _segments_result([])):
            self.assertEqual(whisper_align.transcribe_segments("/tmp/a.wav"), [])


class TranscribeWordsTest(unittest.TestCase):
    def test_returns_word_timestamps(self):
        def impl(path, **kwargs):
            self.assertEqual(kwargs.get("language"), "hi")
            self.assertTrue(kwargs.get("word_timestamps"))
            return _segments_result(
                [{"words": [{"word": "नमस्ते", "start": 0.1, "end": 0.8}]}]
            )

        with _fake_whisper_patch(impl):
            words = whisper_align.transcribe_words("/tmp/a.wav", language="hi")
        self.assertEqual(words, [{"word": "नमस्ते", "start": 0.1, "end": 0.8}])

    def test_no_whisper_returns_none(self):
        with _no_whisper_patch():
            self.assertIsNone(whisper_align.transcribe_words("/tmp/a.wav"))


class MatchWordsToEntriesTest(unittest.TestCase):
    WORDS = [
        {"word": "नमस्ते", "start": 0.1, "end": 0.8},
        {"word": "दुनिया", "start": 0.9, "end": 1.6},
        {"word": "आज", "start": 2.0, "end": 2.5},
        {"word": "का", "start": 2.6, "end": 2.9},
        {"word": "दिन", "start": 3.0, "end": 3.6},
    ]

    def test_matches_sequential_entries(self):
        entries = [
            {"serial": 1, "text_hi": "नमस्ते दुनिया"},
            {"serial": 2, "text_hi": "आज का दिन"},
        ]
        matches = whisper_align.match_words_to_entries(self.WORDS, entries)
        self.assertEqual(set(matches), {1, 2})
        self.assertAlmostEqual(matches[1]["start_sec"], 0.1, places=2)
        self.assertAlmostEqual(matches[1]["end_sec"], 1.6, places=2)
        self.assertAlmostEqual(matches[2]["start_sec"], 2.0, places=2)
        self.assertAlmostEqual(matches[2]["end_sec"], 3.6, places=2)

    def test_blank_target_skipped(self):
        entries = [{"serial": 1, "text_hi": "  "}]
        self.assertEqual(whisper_align.match_words_to_entries(self.WORDS, entries), {})

    def test_below_min_ratio_not_matched(self):
        entries = [{"serial": 1, "text_hi": "पूरी तरह से अलग वाक्य"}]
        matches = whisper_align.match_words_to_entries(self.WORDS, entries)
        self.assertEqual(matches, {})

    def test_sequential_cursor_prevents_reuse(self):
        # Both lines must match "नमस्ते दुनिया" but only one occurrence exists:
        # the second line must not reuse words already consumed by the first.
        entries = [
            {"serial": 1, "text_hi": "नमस्ते दुनिया"},
            {"serial": 2, "text_hi": "नमस्ते दुनिया"},
        ]
        matches = whisper_align.match_words_to_entries(self.WORDS, entries)
        self.assertIn(1, matches)
        self.assertNotIn(2, matches)


if __name__ == "__main__":
    unittest.main()
