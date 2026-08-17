"""Original-language transcript import (F12a).

Lets a user optionally upload their own subtitle/transcript file (.srt, .vtt
or free-form plain text) at ``/upload``. When present, the F1 Gemini
video-understanding stage is skipped entirely and the uploaded content is
written to ``uploads/<job_id>/subtitles_zh_raw.json`` in the EXACT schema
``subtitle_extract.extract_subtitles`` produces, so every downstream consumer
(B2 serialize, D1 whisper cross-check, C1 translate, D2-F3) runs unchanged.

The SRT / VTT parsers and the free-form fallback are kept as clearly separate
functions; ``parse_transcript`` dispatches by file extension. All parsers are
pure (no network, no ffmpeg) and raise :class:`TranscriptParseError` on
malformed / empty input so the ``/upload`` route can reject the whole upload
before anything is persisted.
"""

import json
import logging
import re
from pathlib import Path

from pipeline import video_ingest

logger = logging.getLogger(__name__)

# The transcript file is saved under one of these names in the job dir; the
# extension drives ``detect_format`` at import time (in the background chain).
TRANSCRIPT_FILE_NAMES = (
    "transcript_upload.srt",
    "transcript_upload.vtt",
    "transcript_upload.txt",
)


class TranscriptParseError(ValueError):
    """Raised when a transcript file cannot be parsed into subtitle entries."""


def _parse_time(token, filename="transcript"):
    """Parse a timestamp token to seconds.

    Accepts ``hh:mm:ss,mmm`` / ``hh:mm:ss.mmm`` (SRT/VTT) and the shorter
    ``mm:ss[,.]mmm`` form. Returns a float; raises TranscriptParseError on a
    non-timestamp token.
    """
    token = token.strip()
    match = re.fullmatch(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})", token)
    if match is None:
        match = re.fullmatch(r"(\d{1,2}):(\d{1,2})[,.](\d{1,3})", token)
        if match is None:
            raise TranscriptParseError(
                f"invalid timestamp {token!r} in {filename}"
            )
        hours, minutes, seconds, millis = "0", *match.groups()
    else:
        hours, minutes, seconds, millis = match.groups()
    return (
        int(hours) * 3600
        + int(minutes) * 60
        + int(seconds)
        + int(millis) / 10 ** len(millis)
    )


def _cue_timing(line, filename="transcript"):
    """Split a ``start --> end [cue settings]`` line into ``(start, end)``.

    Returns ``None`` when the line is not a timing line; raises
    TranscriptParseError when a timing line has an unparseable timestamp.
    """
    if "-->" not in line:
        return None
    left, _, right = line.partition("-->")
    start = _parse_time(left, filename)
    right = right.strip()
    end_token = right.split()[0] if right else ""
    end = _parse_time(end_token, filename)
    return start, end


def _collect_text(lines, index):
    """Collect the contiguous non-blank cue-text lines starting at ``index``."""
    text_lines = []
    while index < len(lines) and lines[index].strip():
        text_lines.append(lines[index].strip())
        index += 1
    return " ".join(text_lines).strip(), index


def _scan_cues(lines, skip_note_blocks, filename):
    """Walk cue timing lines, returning ``[{text, start_sec, end_sec}]``."""
    entries = []
    index = 0
    while index < len(lines):
        raw = lines[index].strip()
        if skip_note_blocks and raw.upper().startswith("NOTE") and "-->" not in raw:
            index += 1
            while index < len(lines) and lines[index].strip():
                index += 1
            continue
        if "-->" in raw:
            timing = _cue_timing(raw, filename)
            text, index = _collect_text(lines, index + 1)
            if timing is not None and text:
                start, end = timing
                entries.append(
                    {
                        "text": text,
                        "start_sec": round(float(start), 3),
                        "end_sec": round(float(end), 3),
                    }
                )
            continue
        index += 1
    if not entries:
        raise TranscriptParseError(
            f"no valid subtitle cues found in {filename}"
        )
    return entries


def parse_srt(content):
    """Parse SRT content into ``[{text, start_sec, end_sec}]``.

    Cue indices are optional; timestamps may use ``,`` or ``.`` as the
    millisecond separator. Raises TranscriptParseError when no valid cue is
    found or a timing line is malformed.
    """
    return _scan_cues(
        str(content or "").splitlines(), skip_note_blocks=False, filename="SRT file"
    )


def parse_vtt(content):
    """Parse WebVTT content into ``[{text, start_sec, end_sec}]``.

    Skips ``WEBVTT``/``STYLE``/``REGION`` headers and ``NOTE`` comment blocks;
    cue settings after the end time are ignored. Raises TranscriptParseError
    when no valid cue is found or a timing line is malformed.
    """
    return _scan_cues(
        str(content or "").splitlines(), skip_note_blocks=True, filename="VTT file"
    )


def parse_freeform(content):
    """Parse free-form plain text into non-empty trimmed lines.

    Returns a list of text lines (timing is unknown — it is distributed across
    the video duration by :func:`import_transcript`). Raises
    TranscriptParseError when the text is empty.
    """
    lines = [line.strip() for line in str(content or "").splitlines()]
    texts = [line for line in lines if line]
    if not texts:
        raise TranscriptParseError("free-form transcript has no text")
    return texts


def detect_format(filename):
    """``"srt"`` / ``"vtt"`` / ``"freeform"`` from the file-name extension."""
    suffix = Path(filename or "").suffix.lower()
    if suffix == ".srt":
        return "srt"
    if suffix == ".vtt":
        return "vtt"
    return "freeform"


def parse_transcript(content, filename):
    """Parse uploaded transcript content into F1-schema subtitle dicts.

    Returns ``(entries, kind)``. For ``kind == "freeform"`` the entries carry
    only ``text`` (no timestamps yet — they are distributed across the video
    duration by :func:`import_transcript`); SRT/VTT entries carry full
    ``text`` / ``start_sec`` / ``end_sec``. Raises TranscriptParseError on
    malformed / empty input.
    """
    kind = detect_format(filename)
    if kind == "srt":
        return parse_srt(content), "srt"
    if kind == "vtt":
        return parse_vtt(content), "vtt"
    texts = parse_freeform(content)
    return [{"text": text} for text in texts], "freeform"


def transcript_path(job_dir):
    """Path of the job's saved transcript file, or ``None`` when absent."""
    for name in TRANSCRIPT_FILE_NAMES:
        path = job_dir / name
        if path.exists():
            return path
    return None


def _video_duration_sec(job_dir):
    """The video duration in seconds from ``job_meta.json`` (0.0 fallback)."""
    try:
        meta = json.loads((job_dir / "job_meta.json").read_text(encoding="utf-8"))
        duration = meta.get("duration_sec")
        if duration is not None:
            return float(duration)
    except (OSError, ValueError, TypeError):
        pass
    return 0.0


def _distribute_freeform(texts, duration_sec):
    """Give each free-form line an equal slice of the video duration."""
    count = len(texts)
    if count == 0:
        return []
    if duration_sec <= 0:
        duration_sec = float(count)
    step = duration_sec / count
    entries = []
    for index, text in enumerate(texts):
        start = round(index * step, 3)
        end = round((index + 1) * step, 3)
        entries.append({"text": text, "start_sec": start, "end_sec": end})
    return entries


def import_transcript(job_id, upload_root=None):
    """Import the job's saved transcript into ``subtitles_zh_raw.json``.

    Reads ``uploads/<job_id>/transcript_upload.<ext>``, parses it and writes
    ``subtitles_zh_raw.json`` in the exact F1 schema, returning the result
    dict. Raises FileNotFoundError when no transcript is saved; the parsers
    raise TranscriptParseError on malformed content.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    source = transcript_path(job_dir)
    if source is None:
        raise FileNotFoundError(f"no saved transcript for job {job_id}")
    content = source.read_text(encoding="utf-8-sig", errors="replace")
    entries, kind = parse_transcript(content, source.name)
    if kind == "freeform":
        texts = [entry["text"] for entry in entries]
        entries = _distribute_freeform(texts, _video_duration_sec(job_dir))

    result = {
        "job_id": job_id,
        "status": "ok",
        "chunked": False,
        "segments_count": 1,
        "failed_segments": [],
        "errors": {},
        "subtitles": entries,
        "whisper_used": False,
        "whisper_segments_count": 0,
        "gemini_hallucinated_dropped": 0,
    }
    path = job_dir / "subtitles_zh_raw.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result
