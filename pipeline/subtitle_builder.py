"""Subtitle list builder (B2).

Consumes ``subtitles_zh_raw.json`` (B1 output) and produces a clean,
serialized subtitle list with no overlapping consecutive entries. Pure
Python, no network.

Failed extraction parts are NOT dropped: they become flagged placeholder
entries (``status: "extraction_failed"``, empty text).

Output: ``uploads/<job_id>/subtitles_zh.json``
  ``[{"serial": 1, "text_zh": "...", "start_sec": 0.0, "end_sec": 3.2,
      "status": "ok" | "extraction_failed"}, ...]``
"""

import json
import logging
from pathlib import Path

from pipeline import config, video_ingest

logger = logging.getLogger(__name__)

EXTRACTION_FAILED_TEXT = ""


def _load_json(path):
    if not path.exists():
        raise FileNotFoundError(f"missing file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in {path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _load_json_optional(path):
    if not path.exists():
        return {}
    return _load_json(path)


def _segment_ranges(duration_sec):
    """Recompute B1 segment boundaries deterministically (pure math)."""
    threshold = config.LONG_VIDEO_CHUNK_THRESHOLD_SEC
    overlap = config.SUBTITLE_OVERLAP_SEC
    ranges = []
    start = 0.0
    while start < duration_sec - 1e-6:
        end = min(start + threshold, duration_sec)
        ranges.append((start, end))
        if end >= duration_sec - 1e-6:
            break
        start = max(end - overlap, start + 0.5)
    return ranges


def _duration_of(meta, raw_subs):
    duration = meta.get("duration_sec")
    if duration is not None:
        return duration
    ends = [float(s.get("end_sec", 0.0)) for s in raw_subs if isinstance(s, dict)]
    return max(ends) if ends else 0.0


def _build_entries(raw, duration):
    """Build raw (unsorted, unserialized) entries with failed parts kept."""
    entries = []
    chunked = bool(raw.get("chunked", False))
    failed = list(raw.get("failed_segments", []) or [])
    raw_subs = raw.get("subtitles", []) or []
    status = raw.get("status")

    if status == "extraction_failed" and not failed and not raw_subs:
        entries.append(
            {
                "text_zh": EXTRACTION_FAILED_TEXT,
                "status": "extraction_failed",
                "start_sec": 0.0,
                "end_sec": duration,
            }
        )
    else:
        ranges = _segment_ranges(duration) if chunked else None
        for idx in failed:
            if ranges and 0 <= idx < len(ranges):
                start, end = ranges[idx]
            else:
                start, end = 0.0, duration
            entries.append(
                {
                    "text_zh": EXTRACTION_FAILED_TEXT,
                    "status": "extraction_failed",
                    "start_sec": start,
                    "end_sec": end,
                }
            )
        for sub in raw_subs:
            if not isinstance(sub, dict):
                continue
            try:
                start = float(sub.get("start_sec", 0.0))
                end = float(sub.get("end_sec", 0.0))
            except (TypeError, ValueError):
                continue
            entries.append(
                {
                    "text_zh": str(sub.get("text", "")),
                    "status": "ok",
                    "start_sec": start,
                    "end_sec": end,
                }
            )
    return entries


def _serialize(entries):
    """Sort chronologically, assign serials, clamp overlaps, log fixes."""
    entries = sorted(entries, key=lambda e: (e["start_sec"], e["end_sec"]))
    out = []
    prev_end = None
    for index, entry in enumerate(entries, start=1):
        start = float(entry["start_sec"])
        end = float(entry["end_sec"])
        if prev_end is not None and start < prev_end:
            logger.warning(
                "subtitle serial %d overlap: start %.3fs clamped to previous end %.3fs",
                index, start, prev_end,
            )
            start = prev_end
        if end < start:
            end = start
        prev_end = end
        out.append(
            {
                "serial": index,
                "text_zh": entry["text_zh"],
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "status": entry["status"],
            }
        )
    return out


def detect_gaps(serialized_entries, threshold_sec=None):
    """Serialized (post-_serialize) এন্ট্রির consecutive জোড়ার মধ্যে gap বের করে।

    threshold_sec None হলে config.SUBTITLE_GAP_FLAG_THRESHOLD_SEC ব্যবহার করো
    (এই চাংকেই config.py-তে নতুন যোগ করো, ডিফল্ট 6.0)।

    প্রতিটা gap-এর জন্য (next.start_sec - prev.end_sec > threshold_sec):
        {"after_serial": prev["serial"], "before_serial": next["serial"],
         "gap_start_sec": prev["end_sec"], "gap_end_sec": next["start_sec"],
         "gap_sec": round(next["start_sec"] - prev["end_sec"], 3)}
    রিটার্ন করো লিস্ট, chronological order-এ। "status": "extraction_failed"
    এন্ট্রি gap-চেকে অংশ নেবে (এদের নিজেদের মধ্যেও gap হতে পারে) — শুধু
    এন্ট্রি-বাই-এন্ট্রি consecutive gap দেখো, ফিল্টার করার দরকার নেই।
    """
    if threshold_sec is None:
        threshold_sec = config.SUBTITLE_GAP_FLAG_THRESHOLD_SEC

    gaps = []
    for i in range(len(serialized_entries) - 1):
        prev = serialized_entries[i]
        nxt = serialized_entries[i + 1]
        prev_end = float(prev["end_sec"])
        nxt_start = float(nxt["start_sec"])
        gap_sec = nxt_start - prev_end
        if gap_sec > threshold_sec:
            gaps.append({
                "after_serial": prev["serial"],
                "before_serial": nxt["serial"],
                "gap_start_sec": prev_end,
                "gap_end_sec": nxt_start,
                "gap_sec": round(gap_sec, 3)
            })
    return gaps


def build_subtitle_list(job_id, upload_root=None):
    """Build ``subtitles_zh.json`` from ``subtitles_zh_raw.json``. Returns list."""
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    raw = _load_json(job_dir / "subtitles_zh_raw.json")
    meta = _load_json_optional(job_dir / "job_meta.json")
    duration = _duration_of(meta, raw.get("subtitles", []))
    entries = _build_entries(raw, duration)
    result = _serialize(entries)

    out_path = job_dir / "subtitles_zh.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
