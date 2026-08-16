"""Shared local-Whisper helpers for the pipeline (F8).

Whisper is the primary timing authority for two jobs:

- Chinese subtitle extraction (F1-F3, ``subtitle_extract.py``) uses
  ``transcribe_segments`` for the segment-level timing and text.
- User-uploaded Hindi voiceover alignment (D3, ``voiceover_upload.py``) uses
  ``transcribe_words`` (word-level) plus ``match_words_to_entries`` for the
  sequential fuzzy matching of subtitle lines onto the word stream.

This module owns every Whisper interaction so both callers share the same
model selection, language handling and failure semantics: these helpers never
raise — they return ``None`` when Whisper is unavailable or the transcription
fails, and an empty list when Whisper ran but detected no speech.

``voiceover_upload.py`` keeps its private ``_transcribe_words`` /
``_match_words_to_entries`` names as aliases of the shared functions so the
call site and existing mocks keep working.
"""

import logging
import re
import unicodedata
from difflib import SequenceMatcher

from pipeline import config

logger = logging.getLogger(__name__)


def transcribe_segments(audio_path, language=None, model=None, logger_=None):
    """Transcribe an audio file with local Whisper, segment-level.

    Returns a chronological list of ``{"text", "start_sec", "end_sec"}``
    dicts, or ``None`` when Whisper is unavailable or the transcription fails.
    An empty list means Whisper ran but detected no speech (the caller decides
    whether that is usable).

    ``language`` is passed straight to ``whisper.Whisper.transcribe`` (``None``
    = auto-detect). ``model`` overrides ``config.WHISPER_MODEL`` (used by the
    per-language model overrides ``WHISPER_MODEL_ZH`` / ``WHISPER_MODEL_HI``).
    """
    log = logger_ or logger
    try:
        import whisper  # lazy: heavy optional dependency
    except ImportError as exc:
        log.error("whisper not installed; skipping whisper transcription: %s", exc)
        return None
    try:
        loaded = whisper.load_model(model or config.WHISPER_MODEL)
        result = loaded.transcribe(str(audio_path), language=language)
    except Exception as exc:  # noqa: BLE001 - resilience: callers must survive
        log.error("whisper transcription failed: %s", exc)
        return None

    segments = []
    for segment in result.get("segments", []) or []:
        if not isinstance(segment, dict):
            continue
        try:
            segments.append(
                {
                    "text": str(segment.get("text", "")),
                    "start_sec": float(segment.get("start", 0.0)),
                    "end_sec": float(segment.get("end", 0.0)),
                }
            )
        except (TypeError, ValueError):
            continue
    return segments


def transcribe_words(audio_path, language=None, model=None, logger_=None):
    """Transcribe an audio file with local Whisper, word-level timestamps.

    Returns a chronological list of ``{"word", "start", "end"}`` dicts, or
    ``None`` when Whisper is unavailable or the transcription fails. An empty
    list means Whisper ran but detected no speech.
    """
    log = logger_ or logger
    try:
        import whisper  # lazy: heavy optional dependency
    except ImportError as exc:
        log.error("whisper not installed; skipping whisper transcription: %s", exc)
        return None
    try:
        loaded = whisper.load_model(model or config.WHISPER_MODEL)
        result = loaded.transcribe(
            str(audio_path), language=language, word_timestamps=True
        )
    except Exception as exc:  # noqa: BLE001 - resilience: callers must survive
        log.error("whisper transcription failed: %s", exc)
        return None

    words = []
    for segment in result.get("segments", []) or []:
        for word in segment.get("words", []) or []:
            try:
                words.append(
                    {
                        "word": str(word.get("word", "")),
                        "start": float(word.get("start", 0.0)),
                        "end": float(word.get("end", 0.0)),
                    }
                )
            except (TypeError, ValueError):
                continue
    return words


def last_speech_end(items):
    """Latest end time across Whisper segments/words, or ``0.0`` when empty.

    Accepts either the segment schema (``end_sec``) or the word schema
    (``end``).
    """
    ends = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        for key in ("end_sec", "end"):
            if key in item:
                try:
                    ends.append(float(item[key]))
                except (TypeError, ValueError):
                    pass
                break
    return max(ends) if ends else 0.0


def overlap_ratio(a_start, a_end, b_start, b_end):
    """Fraction of the ``[a_start, a_end]`` span that overlaps ``[b_start, b_end]``.

    ``1.0`` when ``a`` is fully inside ``b``, ``0.0`` when the spans do not
    overlap at all (or either span is zero-length/negative). Used by the
    F1-F3 whisper-primary merge to decide whether a Gemini subtitle line is
    covered by a Whisper segment: the line is passed as ``a`` and the segment
    as ``b``.
    """
    a_start, a_end = float(a_start), float(a_end)
    b_start, b_end = float(b_start), float(b_end)
    overlap = min(a_end, b_end) - max(a_start, b_start)
    a_span = max(a_end - a_start, 0.0)
    if overlap <= 0 or a_span <= 0:
        return 0.0
    return max(overlap, 0.0) / a_span


def _norm_text(text):
    """Normalize text for fuzzy matching: strip punctuation, keep letters."""
    text = unicodedata.normalize("NFKC", str(text or ""))
    text = re.sub(r"[^\w\u0900-\u097F]+", "", text, flags=re.UNICODE)
    return text.lower()


def match_words_to_entries(words, entries, min_ratio=None):
    """Sequential fuzzy match of subtitle lines onto the Whisper word stream.

    Returns ``{serial: {"start_sec", "end_sec"}}`` for the lines that matched
    above ``WHISPER_MATCH_MIN_RATIO``; lines without usable text are skipped.
    Matching is strictly chronological — once a line consumes words, later
    lines only look past that point.
    """
    tokens = [w for w in words if _norm_text(w.get("word"))]
    if not tokens:
        return {}

    min_ratio = config.WHISPER_MATCH_MIN_RATIO if min_ratio is None else min_ratio
    n = len(tokens)
    cursor = 0
    matches = {}
    for entry in entries:
        target = _norm_text(entry.get("text_hi"))
        if not target:
            continue
        best = None
        for start_idx in range(cursor, n):
            acc = ""
            best_ratio = 0.0
            best_end = start_idx
            for end_idx in range(start_idx, n):
                acc += _norm_text(tokens[end_idx].get("word"))
                if len(acc) > len(target) * 1.6 + 4:
                    break
                ratio = SequenceMatcher(None, acc, target).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_end = end_idx
                if best_ratio >= 0.95:
                    break
            if best_ratio >= min_ratio:
                best = (start_idx, best_end)
                break
        if best is not None:
            start_idx, end_idx = best
            matches[entry["serial"]] = {
                "start_sec": float(tokens[start_idx]["start"]),
                "end_sec": float(tokens[end_idx]["end"]),
            }
            cursor = end_idx + 1
    return matches
