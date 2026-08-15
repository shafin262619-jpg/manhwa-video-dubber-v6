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

from pipeline import config, subtitle_extract, video_ingest

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
        raw_zero_duration = start == end
        if prev_end is not None and start < prev_end:
            logger.warning(
                "subtitle serial %d overlap: start %.3fs clamped to previous end %.3fs",
                index, start, prev_end,
            )
            start = prev_end
        if end < start:
            logger.warning(
                "subtitle serial %d zero/negative duration after clamp "
                "(start %.3fs, original end %.3fs)",
                index, start, entry["end_sec"],
            )
            end = start
        elif raw_zero_duration:
            logger.warning(
                "subtitle serial %d zero-duration entry (start %.3fs, end %.3fs)",
                index, start, end,
            )
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


def detect_duplicate_clusters(serialized_entries, min_count=None):
    """Flag degenerate runs of post-_serialize subtitle entries.

    Pure Python, no network, no side effects. Scans consecutive entries for
    runs that either share the same rounded ``start_sec`` or are
    zero-duration (``start_sec == end_sec``). When ``min_count`` is None,
    ``config.SUBTITLE_DUP_CLUSTER_MIN_COUNT`` is used (default 3).

    Each flagged cluster (3+ consecutive entries) is returned in serial order:
        {"start_serial", "end_serial", "start_sec", "count",
         "reason": "same_start_timestamp" | "zero_duration"}
    A run that qualifies under both reasons is reported as "zero_duration"
    (more severe takes precedence).
    """
    if min_count is None:
        min_count = config.SUBTITLE_DUP_CLUSTER_MIN_COUNT

    clusters = []
    i = 0
    n = len(serialized_entries)
    while i < n:
        start_sec = float(serialized_entries[i]["start_sec"])
        end_sec = float(serialized_entries[i]["end_sec"])
        zero_duration = start_sec == end_sec
        j = i + 1
        if zero_duration:
            reason = "zero_duration"
            while j < n:
                nxt = serialized_entries[j]
                if float(nxt["start_sec"]) == float(nxt["end_sec"]):
                    j += 1
                else:
                    break
        else:
            reason = "same_start_timestamp"
            while j < n:
                nxt = serialized_entries[j]
                nxt_start = float(nxt["start_sec"])
                if nxt_start == start_sec and nxt_start != float(nxt["end_sec"]):
                    j += 1
                else:
                    break
        count = j - i
        if count >= min_count:
            clusters.append(
                {
                    "start_serial": serialized_entries[i]["serial"],
                    "end_serial": serialized_entries[j - 1]["serial"],
                    "start_sec": start_sec,
                    "count": count,
                    "reason": reason,
                }
            )
        i = j
    return clusters


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


def _overlap(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def _build_repair_ranges(entries, diagnostics):
    """Build sorted, merged repair ranges from A3 diagnostics.

    Gaps map to their exact ``[gap_start_sec, gap_end_sec)`` window;
    duplicate clusters map to the span of their first/last entry plus half a
    ``SUBTITLE_OVERLAP_SEC`` padding on each side. Overlapping ranges are
    merged (redundant Gemini calls avoided). Returns ``[start, end, weight]``
    sorted by weight descending (most important first).
    """
    pad = config.SUBTITLE_OVERLAP_SEC / 2.0
    serialized = _serialize(entries)
    by_serial = {e["serial"]: e for e in serialized}

    ranges = []
    for g in diagnostics.get("gaps", []):
        start = float(g["gap_start_sec"])
        end = float(g["gap_end_sec"])
        weight = float(g.get("gap_sec", 0.0))
        if end > start:
            ranges.append([start, end, weight])
    for c in diagnostics.get("duplicate_clusters", []):
        first = by_serial.get(c.get("start_serial"))
        last = by_serial.get(c.get("end_serial"))
        if first is None or last is None:
            continue
        start = min(float(first["start_sec"]), float(first["end_sec"])) - pad
        end = max(float(last["start_sec"]), float(last["end_sec"])) + pad
        if start < 0.0:
            start = 0.0
        weight = float(c.get("count", 0))
        if end > start:
            ranges.append([start, end, weight])

    ranges.sort(key=lambda r: r[0])
    merged = []
    for r in ranges:
        if merged and _overlap(merged[-1][0], merged[-1][1], r[0], r[1]):
            merged[-1][0] = min(merged[-1][0], r[0])
            merged[-1][1] = max(merged[-1][1], r[1])
            merged[-1][2] = max(merged[-1][2], r[2])
        else:
            merged.append(list(r))

    merged.sort(key=lambda r: r[2], reverse=True)
    return merged


def _replace_range_entries(entries, start, end, new_subs):
    """Drop raw entries overlapping ``[start, end]`` and insert new subs."""
    kept = []
    for e in entries:
        e_start = float(e["start_sec"])
        e_end = float(e["end_sec"])
        if e_start < end and e_end > start:
            continue
        kept.append(e)
    for sub in new_subs:
        kept.append(
            {
                "text_zh": sub["text"],
                "status": "ok",
                "start_sec": float(sub["start_sec"]),
                "end_sec": float(sub["end_sec"]),
            }
        )
    return kept


def repair_flagged_regions(job_id, entries, diagnostics, upload_root=None,
                           call_budget=None, logger_=None, max_attempts=None):
    """Bounded targeted-repair orchestration over A3 diagnostics.

    Builds a merged, weight-ordered list of time ranges from
    ``diagnostics["gaps"]`` and ``diagnostics["duplicate_clusters"]``, runs
    ``subtitle_extract.extract_window()`` for at most ``max_attempts`` ranges
    (largest first, ``config.SUBTITLE_MAX_REPAIR_ATTEMPTS`` by default), and
    replaces the raw entries overlapping each successfully-repaired window
    with the freshly extracted absolute-timed subtitles. The whole list is
    re-serialized at the end.

    Never raises: a range whose extraction returns ``None`` is skipped and the
    next one proceeds. Ranges beyond ``max_attempts`` are not attempted and are
    reported in ``skipped_budget``. ``call_budget`` is forwarded to
    ``extract_window`` so repair shares the job's per-job CallBudget.

    Returns ``(repaired_entries, repair_summary)`` where ``repaired_entries``
    is the re-serialized entry list and ``repair_summary`` is
    ``{"attempted", "succeeded", "failed", "skipped_budget": [ranges]}``.
    """
    if max_attempts is None:
        max_attempts = config.SUBTITLE_MAX_REPAIR_ATTEMPTS
    log = logger_ or logger

    ranges = _build_repair_ranges(entries, diagnostics)

    attempted = 0
    succeeded = 0
    failed = 0
    skipped_budget = []
    repaired = list(entries)

    for start, end, _weight in ranges:
        if attempted >= max_attempts:
            skipped_budget.append({"start_sec": start, "end_sec": end})
            continue
        attempted += 1
        new_subs = subtitle_extract.extract_window(
            job_id, start, end,
            upload_root=upload_root, call_budget=call_budget, logger_=log,
        )
        if new_subs is None:
            failed += 1
            continue
        succeeded += 1
        repaired = _replace_range_entries(repaired, start, end, new_subs)

    summary = {
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_budget": skipped_budget,
    }
    return _serialize(repaired), summary


def build_subtitle_list(job_id, upload_root=None, call_budget=None, auto_repair=True):
    """Build ``subtitles_zh.json`` from ``subtitles_zh_raw.json``. Returns list.

    Side artifact: also writes ``subtitle_qa.json`` with coverage-gap and
    duplicate-cluster diagnostics (QA diagnostics, A3). When ``auto_repair``
    is true and the diagnostics flag any gaps/clusters, ``repair_flagged_regions``
    is run (bounded targeted re-extraction, B2) and diagnostics are recomputed
    on the repaired list; the ``"repair"`` summary is added to the QA artifact.
    The return value is unchanged (still the serialized entries list) for
    backward compatibility. ``call_budget`` and ``auto_repair`` default to
    ``None`` / ``True`` so existing callers keep working.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    raw = _load_json(job_dir / "subtitles_zh_raw.json")
    meta = _load_json_optional(job_dir / "job_meta.json")
    duration = _duration_of(meta, raw.get("subtitles", []))
    entries = _build_entries(raw, duration)

    repair_summary = None
    if auto_repair:
        result = _serialize(entries)
        gaps = detect_gaps(result)
        duplicate_clusters = detect_duplicate_clusters(result)
        if gaps or duplicate_clusters:
            diagnostics = {
                "gaps": gaps,
                "duplicate_clusters": duplicate_clusters,
            }
            result, repair_summary = repair_flagged_regions(
                job_id, entries, diagnostics,
                upload_root=upload_root, call_budget=call_budget,
            )
            entries = _entries_from_serialized(result)
    else:
        result = _serialize(entries)

    gaps = detect_gaps(result)
    duplicate_clusters = detect_duplicate_clusters(result)
    covered_sec = duration - sum(g["gap_sec"] for g in gaps)
    if covered_sec < 0.0:
        covered_sec = 0.0
    diagnostics = {
        "job_id": job_id,
        "total_duration_sec": round(duration, 3),
        "covered_duration_sec": round(covered_sec, 3),
        "entries_count": len(result),
        "gaps": gaps,
        "duplicate_clusters": duplicate_clusters,
    }
    if repair_summary is not None:
        diagnostics["repair"] = repair_summary
    qa_path = job_dir / "subtitle_qa.json"
    qa_path.write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    out_path = job_dir / "subtitles_zh.json"
    out_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def _entries_from_serialized(serialized):
    """Convert serialized entries back to raw entry dicts for re-serializing."""
    return [
        {
            "text_zh": e["text_zh"],
            "status": e["status"],
            "start_sec": e["start_sec"],
            "end_sec": e["end_sec"],
        }
        for e in serialized
    ]


def load_subtitle_qa(job_id, upload_root=None):
    """Read ``subtitle_qa.json`` and return its dict.

    Never raises: when the file is missing or malformed, returns a default
    dict with empty diagnostics lists.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    path = job_dir / "subtitle_qa.json"
    if not path.exists():
        return {
            "job_id": job_id,
            "total_duration_sec": 0.0,
            "covered_duration_sec": 0.0,
            "entries_count": 0,
            "gaps": [],
            "duplicate_clusters": [],
        }
    try:
        data = _load_json(path)
    except (ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    defaults = {
        "job_id": job_id,
        "total_duration_sec": 0.0,
        "covered_duration_sec": 0.0,
        "entries_count": 0,
        "gaps": [],
        "duplicate_clusters": [],
    }
    defaults.update(data)
    if not isinstance(defaults["gaps"], list):
        defaults["gaps"] = []
    if not isinstance(defaults["duplicate_clusters"], list):
        defaults["duplicate_clusters"] = []
    return defaults
