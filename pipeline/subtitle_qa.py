"""Combined subtitle-extraction QA summary (E1).

Merges the mechanical diagnostics from subtitle_builder (coverage gaps,
duplicate/degenerate-timestamp clusters, repair summary — groups A/B) with
the independent whisper_cross_check verification (group D) into one
human-readable summary the user sees before recording/uploading their
voiceover (group E wiring, E2).
"""

import json
import logging
from pathlib import Path

from pipeline import subtitle_builder, video_ingest

logger = logging.getLogger(__name__)

WHISPER_QA_NAME = "subtitle_qa_whisper.json"


def _load_whisper_check(job_id, upload_root):
    """Read ``subtitle_qa_whisper.json`` (D1) as a dict; never raises.

    Missing or malformed file yields an empty dict; callers treat an absent
    whisper signal as ``"skipped"`` (no flags).
    """
    path = upload_root / job_id / WHISPER_QA_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _gap_warning(gap):
    """One human-readable warning line for a single flagged gap."""
    gap_sec = round(float(gap.get("gap_sec", 0.0)))
    after = gap.get("after_serial")
    before = gap.get("before_serial")
    return (
        f"~{gap_sec} সেকেন্ডের একটা অংশ হয়তো বাদ পড়ে গেছে "
        f"(serial {after}-{before}-এর মাঝে)"
    )


def _cluster_warning(cluster):
    """One human-readable warning line for a single flagged cluster."""
    count = cluster.get("count", 0)
    start = cluster.get("start_serial")
    end = cluster.get("end_serial")
    return (
        f"{count}টা লাইনে সন্দেহজনক ডুপ্লিকেট টাইমিং পাওয়া গেছে "
        f"(serial {start}-{end})"
    )


def build_qa_summary(job_id, upload_root=None):
    """Combine ``subtitle_qa.json`` (A3, post-repair from B3) and
    ``subtitle_qa_whisper.json`` (D1) into one user-facing summary dict.

    Both files are loaded defensively — a missing or malformed file never
    raises and simply contributes no flags: a missing ``subtitle_qa.json``
    means zero gaps/clusters/repair counts, a missing ``subtitle_qa_whisper.json``
    means ``whisper_check_status: "skipped"`` (the cross-check could not run).
    When both are missing the summary is ``qa_status: "ok"`` with empty
    warnings.

    Returns::

        {
            "job_id": job_id,
            "qa_status": "ok" | "flagged",
            "warnings": [<short, non-technical lines for the user>],
            "gaps_remaining": <count>,
            "duplicate_clusters_remaining": <count>,
            "repair_attempted": <count or 0>,
            "repair_succeeded": <count or 0>,
            "whisper_check_status": "ok" | "mismatch" | "skipped",
        }

    ``qa_status`` is ``"flagged"`` when any of ``gaps_remaining > 0``,
    ``duplicate_clusters_remaining > 0``, or
    ``whisper_check_status == "mismatch"`` holds; otherwise ``"ok"``. Each
    flag contributes exactly one human-readable warning line. This is a pure
    aggregation function: no new Gemini/Whisper calls, only the two already
    written JSON files are read.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    mechanical = subtitle_builder.load_subtitle_qa(job_id, upload_root=upload_root)
    whisper = _load_whisper_check(job_id, upload_root)

    gaps = mechanical.get("gaps") or []
    clusters = mechanical.get("duplicate_clusters") or []
    repair = mechanical.get("repair") or {}

    gaps_remaining = len(gaps)
    duplicate_clusters_remaining = len(clusters)
    repair_attempted = int(repair.get("attempted", 0) or 0)
    repair_succeeded = int(repair.get("succeeded", 0) or 0)

    whisper_status = whisper.get("status")
    if whisper_status not in ("ok", "mismatch", "skipped"):
        whisper_status = "skipped"

    warnings = []
    for gap in gaps:
        warnings.append(_gap_warning(gap))
    for cluster in clusters:
        warnings.append(_cluster_warning(cluster))
    if whisper_status == "mismatch":
        warnings.append(
            "সাবটাইটেল এক্সট্রাকশনের কভারেজ অডিওর তুলনায় অস্বাভাবিক কম — "
            "আপলোডের আগে সমস্যাটা পরীক্ষা করে দেখুন"
        )

    flagged = (
        gaps_remaining > 0
        or duplicate_clusters_remaining > 0
        or whisper_status == "mismatch"
    )

    return {
        "job_id": job_id,
        "qa_status": "flagged" if flagged else "ok",
        "warnings": warnings,
        "gaps_remaining": gaps_remaining,
        "duplicate_clusters_remaining": duplicate_clusters_remaining,
        "repair_attempted": repair_attempted,
        "repair_succeeded": repair_succeeded,
        "whisper_check_status": whisper_status,
    }
