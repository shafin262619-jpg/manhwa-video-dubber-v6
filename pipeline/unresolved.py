"""Unresolved-segment tracking (F12c Part B).

When the automatic repair/translation retries are exhausted — the subtitle
repair leaves gaps/clusters after ``config.SUBTITLE_MAX_REPAIR_ATTEMPTS``
(3) attempts, or the translation leaves ``translation_fallback`` lines — the
upload pipeline flags those segments as *unresolved* instead of silently
accepting them. The flag is non-blocking: the job completes normally and the
segments keep their last imperfect state (``extraction_failed`` /
``translation_fallback``), marked unresolved.

The unresolved list is persisted in
``uploads/<job_id>/unresolved_segments.json`` so it survives a resume, and it
is mirrored into the ``upload_pipeline`` status extra
(``unresolved_warning_bn`` / ``unresolved_segments``) — the same status/
warning channel F12b Part C uses for gap-fill warnings.

The user can act on the flag without the pipeline re-running automatically:

- ``apply_retry`` — one more *user-initiated* attempt for each unresolved
  region/line (this is the explicit "one more retry" action; the automatic
  retry count itself never changes).
- ``apply_accept`` — mark the unresolved segments as acceptable/skip.

Both are best-effort against the current artifacts and never touch the
automatic retry logic in ``subtitle_builder`` / ``translator``.
"""

import json
from pathlib import Path

from pipeline import (
    config,
    gemini_rotation,
    lang_files,
    subtitle_builder,
    translator,
    video_ingest,
)

UNRESOLVED_JSON_NAME = "unresolved_segments.json"

# Region descriptions shown in the Bengali warning are capped so the message
# stays readable on the status/result pages.
WARNING_MAX_REPAIR_SHOWN = 3
WARNING_MAX_TRANSLATION_SHOWN = 5


def _job_dir(job_id, upload_root):
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    return root / job_id


def load_unresolved(job_id, upload_root=None):
    """Read the persisted unresolved list; ``[]`` when absent. Never raises."""
    job_dir = _job_dir(job_id, upload_root)
    try:
        data = json.loads((job_dir / UNRESOLVED_JSON_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("items") if isinstance(data, dict) else None
    return items if isinstance(items, list) else []


def persist_unresolved(job_id, items, upload_root=None):
    """Best-effort persist of the unresolved list. Never raises."""
    job_dir = _job_dir(job_id, upload_root)
    try:
        (job_dir / UNRESOLVED_JSON_NAME).write_text(
            json.dumps(
                {"job_id": job_id, "items": items}, ensure_ascii=False, indent=2
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def _active(items):
    return [i for i in items if i.get("state") != "accepted"]


def _cluster_range(job_dir, cluster):
    """Span of a duplicate cluster's first..last entry, or None."""
    try:
        entries = json.loads(
            (job_dir / "subtitles_zh.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    by_serial = {
        e.get("serial"): e
        for e in entries
        if isinstance(e, dict) and e.get("serial") is not None
    }
    first = by_serial.get(cluster.get("start_serial"))
    last = by_serial.get(cluster.get("end_serial"))
    if first is None or last is None:
        return None
    try:
        start = min(float(first["start_sec"]), float(first["end_sec"]))
        end = max(float(last["start_sec"]), float(last["end_sec"]))
    except (TypeError, ValueError, KeyError):
        return None
    if end <= start:
        return None
    return start, end


def _repair_items(job_id, upload_root=None):
    """Unresolved regions left by the exhausted automatic subtitle repair."""
    job_dir = _job_dir(job_id, upload_root)
    qa = subtitle_builder.load_subtitle_qa(job_id, upload_root=upload_root)
    repair = qa.get("repair") or {}
    attempted = int(repair.get("attempted", 0) or 0)
    if attempted <= 0:
        return []

    items = []
    seen = set()

    def _add(start, end):
        if end <= start:
            return
        key = f"repair-{round(start, 3)}-{round(end, 3)}"
        if key in seen:
            return
        seen.add(key)
        items.append(
            {
                "id": key,
                "kind": "repair",
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
                "state": "unresolved",
                "user_retries": 0,
            }
        )

    for g in qa.get("gaps") or []:
        try:
            _add(float(g["gap_start_sec"]), float(g["gap_end_sec"]))
        except (TypeError, ValueError, KeyError):
            continue
    for c in qa.get("duplicate_clusters") or []:
        span = _cluster_range(job_dir, c)
        if span is not None:
            _add(*span)
    for r in repair.get("skipped_budget") or []:
        try:
            _add(float(r["start_sec"]), float(r["end_sec"]))
        except (TypeError, ValueError, KeyError):
            continue
    return items


def _translation_items(job_id, upload_root=None):
    """Unresolved lines: entries that kept ``translation_fallback``."""
    job_dir = _job_dir(job_id, upload_root)
    path = job_dir / lang_files.subtitles_json(
        lang_files.target_lang(job_id, upload_root)
    )
    if not path.exists():
        return []
    items = []
    try:
        entries = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    for e in entries:
        if not isinstance(e, dict) or not e.get("translation_fallback"):
            continue
        serial = e.get("serial")
        if serial is None:
            continue
        items.append(
            {
                "id": f"translation-{int(serial)}",
                "kind": "translation",
                "serial": int(serial),
                "state": "unresolved",
                "user_retries": 0,
            }
        )
    return items


def collect_unresolved(job_id, upload_root=None):
    """Derive the unresolved segments from the on-disk artifacts.

    Returns ``(items, warning_bn)`` where ``warning_bn`` is ``""`` when nothing
    is unresolved. Pure read of ``subtitle_qa.json`` / ``subtitles_hi.json`` —
    the automatic retry logic is never re-run here.
    """
    items = _repair_items(job_id, upload_root) + _translation_items(
        job_id, upload_root
    )
    return items, build_warning_bn(items)


def build_warning_bn(items):
    """Bengali warning naming the unresolved regions/serials + the two actions."""
    active = _active(items)
    if not active:
        return ""
    repair = [i for i in active if i["kind"] == "repair"]
    translation = [i for i in active if i["kind"] == "translation"]

    desc = []
    if repair:
        shown = repair[:WARNING_MAX_REPAIR_SHOWN]
        ranges = ", ".join(
            f"{i['start_sec']:.1f}–{i['end_sec']:.1f} সেকেন্ড" for i in shown
        )
        more = (
            f" (আরও {len(repair) - len(shown)}টি)"
            if len(repair) > len(shown)
            else ""
        )
        desc.append(f"সময়ের অংশ {ranges}{more}")
    if translation:
        shown = translation[:WARNING_MAX_TRANSLATION_SHOWN]
        serials = ", ".join(str(i["serial"]) for i in shown)
        more = (
            f" (আরও {len(translation) - len(shown)}টি)"
            if len(translation) > len(shown)
            else ""
        )
        desc.append(f"সাবটাইটেল সিরিয়াল {serials}{more}")

    return (
        "৩ বার স্বয়ংক্রিয় মেরামত/অনুবাদের পরও কিছু অংশ অমীমাংসিত রয়ে গেছে: "
        f"{'; '.join(desc)}। আপনি চাইলে “একবার আবার চেষ্টা করুন” বা "
        "“মেনে নিন / বাদ দিন” বেছে নিতে পারেন — বাকি পাইপলাইন স্বাভাবিকভাবে "
        "শেষ হয়েছে এবং এটি কোনো কাজ থামায় না।"
    )


def _merged_after(items_before, items_after):
    """Re-attach persisted per-item state (``user_retries``) to new items."""
    before = {i["id"]: i for i in _active(items_before)}
    merged = []
    for item in items_after:
        prev = before.get(item["id"])
        if prev is not None:
            item["user_retries"] = int(prev.get("user_retries", 0)) + 1
        merged.append(item)
    return merged


def apply_retry(job_id, upload_root=None):
    """One more user-initiated attempt for every unresolved segment.

    Re-attempts each unresolved repair region via ``repair_flagged_regions``
    (an explicit, user-requested pass on top of the automatic
    ``config.SUBTITLE_MAX_REPAIR_ATTEMPTS``), refreshes the QA diagnostics
    without triggering another automatic repair round, then re-runs the C1
    translation so ``subtitles_hi.json`` stays in sync. Segments that still
    fail keep their imperfect state and stay unresolved (``user_retries``
    incremented).

    Returns ``(items, warning_bn)``. Raises ``RuntimeError`` when the job has
    no unresolved segments, ``FileNotFoundError`` / ``ValueError`` when the
    artifacts needed to retry are missing/corrupted.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    items = load_unresolved(job_id, upload_root)
    active = _active(items)
    if not active:
        raise RuntimeError(f"job {job_id} has no unresolved segments to retry")

    budget = gemini_rotation.CallBudget(config.MAX_API_CALLS_PER_JOB)
    repair_items = [i for i in active if i["kind"] == "repair"]
    translation_items = [i for i in active if i["kind"] == "translation"]

    ran_repair = False
    if repair_items:
        entries = json.loads(
            (job_dir / "subtitles_zh.json").read_text(encoding="utf-8")
        )
        if not isinstance(entries, list):
            raise ValueError(f"malformed subtitles_zh.json for job {job_id}")
        diagnostics = {
            "gaps": [
                {
                    "gap_start_sec": i["start_sec"],
                    "gap_end_sec": i["end_sec"],
                    "gap_sec": round(i["end_sec"] - i["start_sec"], 3),
                }
                for i in repair_items
            ],
            "duplicate_clusters": [],
        }
        repaired, summary = subtitle_builder.repair_flagged_regions(
            job_id,
            entries,
            diagnostics,
            upload_root=upload_root,
            call_budget=budget,
            max_attempts=len(repair_items),
        )
        subtitle_builder.refresh_qa(
            job_id,
            repaired,
            upload_root=upload_root,
            repair_summary=summary,
        )
        ran_repair = True

    if ran_repair or translation_items:
        translator.translate_subtitles(job_id, upload_root=upload_root, call_budget=budget)

    fresh, warning = collect_unresolved(job_id, upload_root)
    merged = _merged_after(items, fresh)
    persist_unresolved(job_id, merged, upload_root)
    return merged, build_warning_bn(merged)


def apply_accept(job_id, upload_root=None):
    """Mark all unresolved segments as accepted/skip (user decision).

    The segments keep their imperfect on-disk state (``extraction_failed`` /
    ``translation_fallback``) but are no longer reported as actionable.
    Returns ``(items, warning_bn)`` with an empty warning after accept.
    Raises ``RuntimeError`` when the job has nothing to accept.
    """
    items = load_unresolved(job_id, upload_root)
    if not items:
        raise RuntimeError(f"job {job_id} has no unresolved segments")
    for item in items:
        item["state"] = "accepted"
    persist_unresolved(job_id, items, upload_root)
    return items, build_warning_bn(items)
