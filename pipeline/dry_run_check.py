"""Pre-flight offline sanity check for a job (U5, final robustness chunk).

A small CLI that validates the *self-consistency* of the JSON artifacts
already produced for one job under ``uploads/<job_id>/`` — no network, no
ffmpeg, no Gemini calls — so big mistakes are caught in a few seconds before
starting expensive stages like F3 (final render) or even D2 (many Gemini TTS
calls).

Checks (only files that already exist; a missing file is an informational
"stage not done yet", never an error):

1. ``subtitles_zh.json`` (B2): every entry has ``serial`` / ``text_zh`` /
   ``start_sec`` / ``end_sec`` and serials are ``1..N`` with no gap/duplicate.
2. ``subtitles_hi.json`` (C1): serial count/order matches
   ``subtitles_zh.json`` exactly (the translator hard constraint) and prints
   the share of ``translation_fallback: true`` entries as a percentage.
3. ``timestamps_hi_final.json`` (D4): serial count matches
   ``subtitles_hi.json``, every entry has ``end_sec > start_sec`` (no
   invalid/zero duration) and no overlap (next ``start_sec >=`` previous
   ``end_sec``).
4. ``edit_guideline.json`` (E1): informational only — prints how many
   entries are ``flagged: true`` and the ``flag_reason`` distribution
   (``extreme_speed_ratio`` vs ``invalid_duration``); the F1 review page is
   where the user fixes these manually, so this never blocks.

Exit code: 0 = no blocking error (missing files are not blocking),
1 = at least one blocking error (serial mismatch / gap / duplicate /
invalid or overlapping duration) that must be fixed before rendering.

Usage::

    python3 -m pipeline.dry_run_check --job-id <job_id>
    python3 -m pipeline.dry_run_check --job-id <job_id> --upload-root uploads
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from pipeline import lang_files, video_ingest

BLOCKING_FIELDS_ZH = ("serial", "text_zh", "start_sec", "end_sec")


@dataclass
class CheckReport:
    """Collected notes, blocking errors and stats for one job check."""

    blocking_errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def exit_code(self):
        return 1 if self.blocking_errors else 0


def _load_json_list(path, report):
    """Load a JSON list from ``path``; on failure record a blocking error."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        report.blocking_errors.append(f"{path.name}: unreadable/malformed JSON ({exc})")
        return None
    if not isinstance(data, list):
        report.blocking_errors.append(
            f"{path.name}: expected a list, got {type(data).__name__}"
        )
        return None
    return data


def _as_int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _check_sequential_serials(serials, label, report):
    """Block on duplicate or non-consecutive ``1..N`` serials."""
    n = len(serials)
    duplicates = sorted({s for s in set(serials) if serials.count(s) > 1})
    if duplicates:
        report.blocking_errors.append(
            f"{label}: duplicate serial(s) {duplicates}"
        )
    unique = sorted(set(serials))
    if unique != list(range(1, n + 1)):
        missing = sorted(set(range(1, n + 1)) - set(serials))
        extra = sorted(set(serials) - set(range(1, n + 1)))
        msg = f"{label}: serials are not 1..{n} consecutive"
        if missing:
            msg += f" (missing {missing})"
        if extra:
            msg += f" (unexpected {extra})"
        report.blocking_errors.append(msg)


def _check_subtitles_zh(entries, report):
    """Check 1: required keys + sequential 1..N serials."""
    valid_serials = []
    for idx, entry in enumerate(entries):
        if not isinstance(entry, dict):
            report.blocking_errors.append(
                f"subtitles_zh.json entry {idx}: not an object"
            )
            continue
        missing = [k for k in BLOCKING_FIELDS_ZH if k not in entry]
        if missing:
            report.blocking_errors.append(
                f"subtitles_zh.json entry {idx}: missing key(s) {', '.join(missing)}"
            )
        serial = _as_int(entry.get("serial"))
        if serial is None:
            report.blocking_errors.append(
                f"subtitles_zh.json entry {idx}: serial is missing or not an integer"
            )
        else:
            valid_serials.append(serial)
        start = _as_float(entry.get("start_sec"))
        end = _as_float(entry.get("end_sec"))
        if start is None or end is None:
            report.blocking_errors.append(
                f"subtitles_zh.json serial {entry.get('serial')}: "
                "start_sec/end_sec are missing or not numeric"
            )
    if valid_serials and len(valid_serials) == len(entries):
        _check_sequential_serials(valid_serials, "subtitles_zh.json", report)
    report.stats["subtitles_zh_count"] = len(entries)


def _check_subtitles_hi(zh, hi, report):
    """Check 2: exact serial count/order match + fallback share."""
    hi_serials = []
    for idx, entry in enumerate(hi):
        if not isinstance(entry, dict):
            report.blocking_errors.append(
                f"subtitles_hi.json entry {idx}: not an object"
            )
            continue
        serial = _as_int(entry.get("serial"))
        if serial is None:
            report.blocking_errors.append(
                f"subtitles_hi.json entry {idx}: serial is missing or not an integer"
            )
        else:
            hi_serials.append(serial)

    if zh is not None:
        zh_serials = [_as_int(e.get("serial")) for e in zh]
        if zh_serials == hi_serials:
            report.notes.append(
                "C1 (subtitles_hi.json): serial count/order matches subtitles_zh.json"
            )
        else:
            report.blocking_errors.append(
                "subtitles_hi.json serial count/order does NOT match "
                f"subtitles_zh.json: {hi_serials} != {zh_serials}"
            )
    else:
        report.notes.append(
            "C1 (subtitles_hi.json): subtitles_zh.json missing — "
            "serial-match comparison skipped"
        )

    total = len(hi)
    fallback = sum(1 for e in hi if bool(e.get("translation_fallback", False)))
    pct = (100.0 * fallback / total) if total else 0.0
    report.stats["translation_fallback_count"] = fallback
    report.stats["translation_fallback_pct"] = round(pct, 1)
    report.notes.append(
        f"C1 fallback: {fallback}/{total} ({pct:.1f}%) entries have "
        "translation_fallback=true"
    )


def _check_timestamps_final(hi, ts, report):
    """Check 3: count match + end>start + no overlap (blocking)."""
    if hi is None:
        report.notes.append(
            "D4 (timestamps_hi_final.json): subtitles_hi.json missing — "
            "count comparison skipped"
        )
    elif len(ts) != len(hi):
        report.blocking_errors.append(
            f"timestamps_hi_final.json entry count {len(ts)} does not match "
            f"subtitles_hi.json count {len(hi)}"
        )
    else:
        report.notes.append(
            "D4 (timestamps_hi_final.json): entry count matches subtitles_hi.json"
        )

    ordered = sorted(
        enumerate(ts),
        key=lambda item: (_as_int(item[1].get("serial")) if isinstance(item[1], dict)
                          else None) or (10 ** 9),
    )
    prev_end = None
    for idx, entry in ordered:
        if not isinstance(entry, dict):
            report.blocking_errors.append(
                f"timestamps_hi_final.json entry {idx}: not an object"
            )
            continue
        serial = entry.get("serial")
        start = _as_float(entry.get("start_sec"))
        end = _as_float(entry.get("end_sec"))
        if start is None or end is None:
            report.blocking_errors.append(
                f"timestamps_hi_final.json serial {serial}: "
                "start_sec/end_sec are missing or not numeric"
            )
            continue
        if not end > start:
            report.blocking_errors.append(
                f"timestamps_hi_final.json serial {serial}: invalid duration "
                f"(start={start}, end={end})"
            )
        if prev_end is not None and start < prev_end:
            report.blocking_errors.append(
                f"timestamps_hi_final.json serial {serial}: overlaps previous "
                f"entry (start={start} < previous end={prev_end})"
            )
        prev_end = end if prev_end is None else max(prev_end, end)

    report.stats["timestamps_final_count"] = len(ts)


def _check_edit_guideline(entries, report):
    """Check 4: informational flagged/flag_reason distribution (never blocks)."""
    total = len(entries)
    flagged = [e for e in entries if bool(e.get("flagged"))]
    reasons = {}
    for entry in flagged:
        reason = entry.get("flag_reason") or "unknown"
        reasons[reason] = reasons.get(reason, 0) + 1
    report.stats["edit_guideline_count"] = total
    report.stats["edit_guideline_flagged"] = len(flagged)
    report.stats["edit_guideline_flag_reasons"] = dict(reasons)
    report.notes.append(
        f"E1 (edit_guideline.json): {len(flagged)}/{total} entries flagged; "
        f"flag_reason distribution: {reasons or 'none'} (informational, not blocking)"
    )


def run_checks(job_id, upload_root=None):
    """Run every applicable check for a job; return a ``CheckReport``."""
    report = CheckReport()
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = root / job_id
    if not job_dir.is_dir():
        report.blocking_errors.append(f"job directory not found: {job_dir}")
        return report

    lang = lang_files.target_lang(job_id, upload_root)
    sub_name = lang_files.subtitles_json(lang)
    ts_name = lang_files.timestamps_final(lang)
    zh_path = job_dir / "subtitles_zh.json"
    hi_path = job_dir / sub_name
    ts_path = job_dir / ts_name
    eg_path = job_dir / "edit_guideline.json"

    zh = None
    if zh_path.exists():
        zh = _load_json_list(zh_path, report)
        if zh is not None:
            _check_subtitles_zh(zh, report)
        report.notes.append("B2 (subtitles_zh.json): present — checked")
    else:
        report.notes.append(
            "B2 (subtitles_zh.json): not found — stage not done yet, skipped"
        )

    if hi_path.exists():
        hi = _load_json_list(hi_path, report)
        if hi is not None:
            _check_subtitles_hi(zh, hi, report)
        report.notes.append(f"C1 ({sub_name}): present — checked")
    else:
        hi = None
        report.notes.append(
            f"C1 ({sub_name}): not found — stage not done yet, skipped"
        )

    if ts_path.exists():
        ts = _load_json_list(ts_path, report)
        if ts is not None:
            _check_timestamps_final(hi, ts, report)
        report.notes.append(f"D4 ({ts_name}): present — checked")
    else:
        report.notes.append(
            f"D4 ({ts_name}): not found — stage not done yet, skipped"
        )

    if eg_path.exists():
        eg = _load_json_list(eg_path, report)
        if eg is not None:
            _check_edit_guideline(eg, report)
        report.notes.append("E1 (edit_guideline.json): present — checked")
    else:
        report.notes.append(
            "E1 (edit_guideline.json): not found — stage not done yet, skipped"
        )

    return report


def _build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Offline pre-flight sanity check for one job's JSON "
        "artifacts (no network/ffmpeg/Gemini). Exit 0 = no blocking error, "
        "1 = a blocking error (serial mismatch/gap/duplicate or invalid/"
        "overlapping duration)."
    )
    parser.add_argument(
        "--job-id", required=True, help="job directory under --upload-root"
    )
    parser.add_argument(
        "--upload-root",
        default=None,
        help="uploads root directory (default: video_ingest.UPLOAD_ROOT)",
    )
    return parser


def main(argv=None):
    """CLI entry point. Returns the process exit code."""
    args = _build_arg_parser().parse_args(argv)
    report = run_checks(args.job_id, args.upload_root)

    print(f"dry-run check for job {args.job_id}")
    print(f"job dir: {Path(args.upload_root or video_ingest.UPLOAD_ROOT) / args.job_id}")
    for note in report.notes:
        print(f"[info] {note}")
    for error in report.blocking_errors:
        print(f"[ERROR] {error}")
    if report.exit_code == 0:
        print("RESULT: OK — no blocking errors (missing files are not blocking)")
    else:
        print(
            "RESULT: BLOCKING ERROR(S) FOUND — fix these before running "
            "expensive TTS/render stages"
        )
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
