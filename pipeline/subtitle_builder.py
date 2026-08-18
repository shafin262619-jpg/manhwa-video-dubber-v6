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


def _redistribute_collision_cluster(entries, window_start, anchor_start,
                                    first_serial):
    """Give a 3+ collision cluster non-zero, text-length-weighted durations.

    ``window_start`` is the running end cursor the raw entries collided with.
    When the next non-colliding entry (``anchor_start``, or ``None`` at end of
    list) leaves enough room for every entry to get at least
    ``SUBTITLE_MIN_SERIAL_DURATION_SEC``, the window is
    ``[window_start, anchor_start]`` and each entry gets a duration weighted by
    its text length (floored at the minimum). Otherwise the window is
    ``[window_start, window_start + n * min_duration]`` and every entry gets
    exactly the per-entry minimum fallback.

    This replaces the old behaviour of clamping every entry to ``prev_end``,
    which collapsed 20+ consecutive subtitles onto a single zero-length
    timestamp (the ffmpeg "-to value smaller than -ss" crash, E7).

    Returns ``(new_entries, next_serial, window_end)`` where ``new_entries``
    carry fresh serials starting at ``first_serial``.
    """
    n = len(entries)
    min_dur = config.SUBTITLE_MIN_SERIAL_DURATION_SEC
    if anchor_start is not None and (anchor_start - window_start) >= n * min_dur:
        window_end = anchor_start
        weights = [
            max(1.0, float(len(str(e.get("text_zh", "")).strip())))
            for e in entries
        ]
        total = sum(weights)
        width = window_end - window_start
        durations = [max(min_dur, width * w / total) for w in weights]
        total_dur = sum(durations)
        above_floor = total_dur - n * min_dur
        if total_dur > width and above_floor > 1e-9:
            scale = (width - n * min_dur) / above_floor
            durations = [min_dur + (d - min_dur) * scale for d in durations]
    else:
        window_end = window_start + n * min_dur
        durations = [min_dur] * n

    logger.warning(
        "subtitle collision cluster: serials %d-%d (%d entries) at %.3fs "
        "redistributed across [%.3fs, %.3fs] (min %.3fs each)",
        first_serial, first_serial + n - 1, n, window_start,
        window_start, window_end, min_dur,
    )

    cursor = window_start
    out = []
    for entry, dur in zip(entries, durations):
        end = cursor + dur
        out.append(
            {
                "serial": first_serial,
                "text_zh": entry["text_zh"],
                "start_sec": round(cursor, 3),
                "end_sec": round(end, 3),
                "status": entry["status"],
            }
        )
        first_serial += 1
        cursor = end
    return out, first_serial, cursor


def _serialize(entries):
    """Sort chronologically, assign serials, clamp overlaps, log fixes.

    Runs of ``SUBTITLE_COLLISION_CLUSTER_MIN_COUNT``+ consecutive raw entries
    that collide with the running end cursor are redistributed as one cluster
    (see :func:`_redistribute_collision_cluster`) so they never collapse into
    a zero-duration pile-up. Smaller overlaps keep the original clamp.
    """
    min_cluster = config.SUBTITLE_COLLISION_CLUSTER_MIN_COUNT
    entries = sorted(entries, key=lambda e: (e["start_sec"], e["end_sec"]))
    out = []
    prev_end = None
    serial = 1
    i = 0
    n = len(entries)
    while i < n:
        entry = entries[i]
        start = float(entry["start_sec"])
        end = float(entry["end_sec"])
        raw_zero_duration = start == end
        if prev_end is not None and start < prev_end:
            j = i + 1
            while j < n and float(entries[j]["start_sec"]) < prev_end:
                j += 1
            cluster = entries[i:j]
            if len(cluster) >= min_cluster:
                anchor_start = (
                    float(entries[j]["start_sec"]) if j < n else None
                )
                new_entries, serial, prev_end = _redistribute_collision_cluster(
                    cluster, prev_end, anchor_start, serial,
                )
                out.extend(new_entries)
                i = j
                continue
            logger.warning(
                "subtitle serial %d overlap: start %.3fs clamped to previous end %.3fs",
                serial, start, prev_end,
            )
            start = prev_end
        if end < start:
            logger.warning(
                "subtitle serial %d zero/negative duration after clamp "
                "(start %.3fs, original end %.3fs)",
                serial, start, entry["end_sec"],
            )
            end = start
        elif raw_zero_duration:
            logger.warning(
                "subtitle serial %d zero-duration entry (start %.3fs, end %.3fs)",
                serial, start, end,
            )
        prev_end = end
        out.append(
            {
                "serial": serial,
                "text_zh": entry["text_zh"],
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "status": entry["status"],
            }
        )
        serial += 1
        i += 1
    return out


def detect_duplicate_clusters(serialized_entries, min_count=None):
    """Flag degenerate runs of post-_serialize subtitle entries.

    Pure Python, no network, no side effects. Scans consecutive entries for
    runs that either share the same rounded ``start_sec`` or are
    zero-duration (``start_sec == end_sec``). When ``min_count`` is None,
    ``config.SUBTITLE_DUP_CLUSTER_MIN_COUNT`` is used (default 3).

    A zero-duration run is flagged even when it is a single entry — a
    zero-duration subtitle is always degenerate and would otherwise silently
    leak through into the final SRT (QA repair, B2). Same-start-timestamp
    runs still require ``min_count`` (default 3+) consecutive entries.

    Each flagged cluster is returned in serial order:
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
        if zero_duration or count >= min_count:
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


def detect_collision_clusters(entries, min_count=None):
    """Flag runs of 3+ raw entries colliding with the running end cursor.

    Operates on PRE-serialization raw entries (each dict needs ``text_zh``,
    ``start_sec``, ``end_sec``). A collision run is consecutive sorted entries
    whose ``start_sec < running end`` — the pattern that used to pile up into
    a zero-duration cluster during serialization (E7). After serialization the
    overlap is already resolved by :func:`_redistribute_collision_cluster`, so
    this is a distinct QA diagnostic (reason ``"collision_cluster"``) from
    :func:`detect_duplicate_clusters`, which scans post-serialize output.

    Returns clusters in serial order:
        {"start_serial", "end_serial", "start_sec", "count",
         "reason": "collision_cluster"}
    ``min_count`` defaults to ``config.SUBTITLE_COLLISION_CLUSTER_MIN_COUNT``.
    """
    if min_count is None:
        min_count = config.SUBTITLE_COLLISION_CLUSTER_MIN_COUNT

    ordered = sorted(entries, key=lambda e: (e["start_sec"], e["end_sec"]))
    clusters = []
    prev_end = None
    i = 0
    n = len(ordered)
    while i < n:
        start = float(ordered[i]["start_sec"])
        end = float(ordered[i]["end_sec"])
        if prev_end is not None and start < prev_end:
            j = i + 1
            while j < n and float(ordered[j]["start_sec"]) < prev_end:
                j += 1
            if (j - i) >= min_count:
                clusters.append(
                    {
                        "start_serial": i + 1,
                        "end_serial": j,
                        "start_sec": start,
                        "count": j - i,
                        "reason": "collision_cluster",
                    }
                )
            cluster_max_end = max(
                float(e["end_sec"]) for e in ordered[i:j]
            )
            prev_end = max(prev_end, cluster_max_end)
            i = j
            continue
        prev_end = end
        i += 1
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
                           call_budget=None, logger_=None, max_attempts=None,
                           job_dir=None, time_offset_sec=0.0):
    """Bounded targeted-repair orchestration over A3 diagnostics.

    Builds a merged, weight-ordered list of time ranges from
    ``diagnostics["gaps"]`` and ``diagnostics["duplicate_clusters"]``, runs
    ``subtitle_extract.extract_window()`` for at most ``max_attempts`` ranges
    (largest first, ``config.SUBTITLE_MAX_REPAIR_ATTEMPTS`` by default), and
    replaces the raw entries overlapping each successfully-repaired window
    with the freshly extracted absolute-timed subtitles. The whole list is
    re-serialized at the end.

    ``job_dir`` (optional) points the artifact writes at a different
    directory (a per-segment mini job, F13b) than ``upload_root / job_id``.
    ``time_offset_sec`` (optional, F13b) re-bases segment-relative repair
    windows back to absolute video time for ``extract_window`` (which always
    cuts the whole ``source.mp4``) and re-bases the extracted subtitles back
    to segment-relative.

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
            job_id, start + time_offset_sec, end + time_offset_sec,
            upload_root=upload_root, call_budget=call_budget, logger_=log,
        )
        if new_subs is None:
            failed += 1
            continue
        if time_offset_sec:
            new_subs = [
                dict(
                    s,
                    start_sec=round(float(s.get("start_sec", 0.0)) - time_offset_sec, 3),
                    end_sec=round(float(s.get("end_sec", 0.0)) - time_offset_sec, 3),
                )
                for s in new_subs
            ]
        succeeded += 1
        repaired = _replace_range_entries(repaired, start, end, new_subs)

    summary = {
        "attempted": attempted,
        "succeeded": succeeded,
        "failed": failed,
        "skipped_budget": skipped_budget,
    }
    return _serialize(repaired), summary


def build_subtitle_list(job_id, upload_root=None, call_budget=None, auto_repair=True,
                        job_dir=None, time_offset_sec=0.0):
    """Build ``subtitles_zh.json`` from ``subtitles_zh_raw.json``. Returns list.

    Side artifact: also writes ``subtitle_qa.json`` with coverage-gap and
    duplicate-cluster diagnostics (QA diagnostics, A3). When ``auto_repair``
    is true and the diagnostics flag any gaps/clusters, ``repair_flagged_regions``
    is run (bounded targeted re-extraction, B2) and diagnostics are recomputed
    on the repaired list; the ``"repair"`` summary is added to the QA artifact.
    The return value is unchanged (still the serialized entries list) for
    backward compatibility. ``call_budget`` and ``auto_repair`` default to
    ``None`` / ``True`` so existing callers keep working.

    ``job_dir`` (optional) runs the stage against a different directory (a
    per-segment mini job, F13b) instead of ``upload_root / job_id``;
    ``time_offset_sec`` (optional, F13b) is forwarded to the auto-repair pass
    so its segment-relative windows are re-based for the whole-video
    ``extract_window`` cuts.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = Path(job_dir) if job_dir else upload_root / job_id
    raw = _load_json(job_dir / "subtitles_zh_raw.json")
    meta = _load_json_optional(job_dir / "job_meta.json")
    duration = _duration_of(meta, raw.get("subtitles", []))
    entries = _build_entries(raw, duration)
    collision_clusters = detect_collision_clusters(entries)

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
                job_dir=job_dir, time_offset_sec=time_offset_sec,
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
        "collision_clusters": collision_clusters,
    }
    if repair_summary is not None:
        diagnostics["repair"] = repair_summary
    refresh_qa(
        job_id, result, upload_root=upload_root, job_dir=job_dir,
        repair_summary=repair_summary, collision_clusters=collision_clusters,
    )
    return result


def refresh_qa(job_id, serialized_entries, upload_root=None, repair_summary=None,
               collision_clusters=None, job_dir=None):
    """Rewrite ``subtitle_qa.json`` + ``subtitles_zh.json`` from entries.

    Recomputes the QA diagnostics (gaps, duplicate clusters, collision
    clusters, coverage) for an already-built/repaired entry list *without*
    re-running the automatic repair — the F12c Part B user-initiated retry uses
    this so a targeted re-extraction updates QA without triggering another
    auto-repair round. ``serialized_entries`` must be in the
    ``subtitles_zh.json`` schema. ``collision_clusters`` defaults to clusters
    computed over the serialized entries when not provided. Returns the
    diagnostics dict.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = Path(job_dir) if job_dir else upload_root / job_id
    meta = _load_json_optional(job_dir / "job_meta.json")
    duration = _duration_of(meta, serialized_entries)
    if collision_clusters is None:
        collision_clusters = detect_collision_clusters(serialized_entries)
    gaps = detect_gaps(serialized_entries)
    duplicate_clusters = detect_duplicate_clusters(serialized_entries)
    covered_sec = duration - sum(g["gap_sec"] for g in gaps)
    if covered_sec < 0.0:
        covered_sec = 0.0
    diagnostics = {
        "job_id": job_id,
        "total_duration_sec": round(duration, 3),
        "covered_duration_sec": round(covered_sec, 3),
        "entries_count": len(serialized_entries),
        "gaps": gaps,
        "duplicate_clusters": duplicate_clusters,
        "collision_clusters": collision_clusters,
    }
    if repair_summary is not None:
        diagnostics["repair"] = repair_summary
    (job_dir / "subtitle_qa.json").write_text(
        json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (job_dir / "subtitles_zh.json").write_text(
        json.dumps(serialized_entries, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return diagnostics


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


def load_subtitle_qa(job_id, upload_root=None, job_dir=None):
    """Read ``subtitle_qa.json`` and return its dict.

    Never raises: when the file is missing or malformed, returns a default
    dict with empty diagnostics lists.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = Path(job_dir) if job_dir else upload_root / job_id
    path = job_dir / "subtitle_qa.json"
    if not path.exists():
        return {
            "job_id": job_id,
            "total_duration_sec": 0.0,
            "covered_duration_sec": 0.0,
            "entries_count": 0,
            "gaps": [],
            "duplicate_clusters": [],
            "collision_clusters": [],
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
        "collision_clusters": [],
    }
    defaults.update(data)
    if not isinstance(defaults["gaps"], list):
        defaults["gaps"] = []
    if not isinstance(defaults["duplicate_clusters"], list):
        defaults["duplicate_clusters"] = []
    if not isinstance(defaults["collision_clusters"], list):
        defaults["collision_clusters"] = []
    return defaults
