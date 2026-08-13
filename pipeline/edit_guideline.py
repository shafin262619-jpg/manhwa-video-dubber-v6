"""Speed-ratio edit guideline builder (E1).

Pure Python, no network, no ffmpeg. Compares the original Chinese subtitle
timing (``subtitles_zh.json``, B2 output) with the unified Hindi voiceover
timing (``timestamps_hi_final.json``, D4 output) and, for every serial,
computes the FFmpeg ``setpts`` multiplier needed to stretch/shrink the source
clip so its duration matches the voiceover line::

    pts_multiplier = target_duration / source_duration

A value greater than 1 slows the clip down (e.g. 8s of dialog vs 6s of source
-> 8/6 = 1.333), a value below 1 speeds it up.

Soft clamp (flag, never block):

- Multipliers outside ``SPEED_RATIO_MIN``..``SPEED_RATIO_MAX`` (default
  0.5..2.0) keep their real value but are flagged ``extreme_speed_ratio`` so
  the F-group review UI can highlight them.
- Zero or negative source/target durations never crash the job: they get a
  safe ``pts_multiplier = 1.0`` and are flagged ``invalid_duration``.

Output: ``uploads/<job_id>/edit_guideline.json`` with one entry per serial:
``[{"serial", "source_start_sec", "source_end_sec", "target_start_sec",
"target_end_sec", "pts_multiplier", "flagged", "flag_reason"}]``.
"""

import json
import logging
from pathlib import Path

from pipeline import config, video_ingest

logger = logging.getLogger(__name__)


def _load_json_list(path):
    if not path.exists():
        raise FileNotFoundError(f"missing file: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON in {path}: {exc}") from exc
    if not isinstance(data, list):
        raise ValueError(f"expected a list in {path}")
    return data


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _index_by_serial(entries):
    by_serial = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("serial") is None:
            continue
        try:
            serial = int(entry["serial"])
        except (TypeError, ValueError):
            continue
        by_serial[serial] = entry
    return by_serial


def _build_entry(serial, zh_entry, hi_entry):
    """Compute one guideline entry for a single serial."""
    source_start = _safe_float(zh_entry.get("start_sec"))
    source_end = _safe_float(zh_entry.get("end_sec"))
    target_start = _safe_float(hi_entry.get("start_sec"))
    target_end = _safe_float(hi_entry.get("end_sec"))

    source_duration = source_end - source_start
    target_duration = target_end - target_start

    if source_duration <= 0 or target_duration <= 0:
        logger.warning(
            "serial %d invalid duration (source %.3fs, target %.3fs); "
            "using pts_multiplier 1.0",
            serial, source_duration, target_duration,
        )
        pts_multiplier = 1.0
        flagged = True
        flag_reason = "invalid_duration"
    else:
        pts_multiplier = target_duration / source_duration
        if (
            pts_multiplier < config.SPEED_RATIO_MIN
            or pts_multiplier > config.SPEED_RATIO_MAX
        ):
            flagged = True
            flag_reason = "extreme_speed_ratio"
        else:
            flagged = False
            flag_reason = None

    return {
        "serial": serial,
        "source_start_sec": round(source_start, 3),
        "source_end_sec": round(source_end, 3),
        "target_start_sec": round(target_start, 3),
        "target_end_sec": round(target_end, 3),
        "pts_multiplier": round(pts_multiplier, 4),
        "flagged": flagged,
        "flag_reason": flag_reason,
    }


def build_edit_guideline(job_id, upload_root=None):
    """Build ``edit_guideline.json`` for a job. Returns a result dict.

    Raises FileNotFoundError when the job, ``subtitles_zh.json`` or
    ``timestamps_hi_final.json`` is missing; ValueError on malformed input.
    Never crashes on zero/negative durations (safe 1.0 multiplier + flag).
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    zh_path = job_dir / "subtitles_zh.json"
    hi_path = job_dir / "timestamps_hi_final.json"
    if not zh_path.exists():
        raise FileNotFoundError(f"no subtitles_zh.json for job {job_id}")
    if not hi_path.exists():
        raise FileNotFoundError(f"no timestamps_hi_final.json for job {job_id}")

    zh_by_serial = _index_by_serial(_load_json_list(zh_path))
    hi_by_serial = _index_by_serial(_load_json_list(hi_path))

    serials = sorted(set(zh_by_serial) & set(hi_by_serial))
    for serial in sorted(set(zh_by_serial) ^ set(hi_by_serial)):
        logger.warning(
            "job %s: serial %s present in only one input; skipped",
            job_id, serial,
        )

    guideline = [
        _build_entry(serial, zh_by_serial[serial], hi_by_serial[serial])
        for serial in serials
    ]

    out_path = job_dir / "edit_guideline.json"
    out_path.write_text(
        json.dumps(guideline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("job %s: edit guideline built for %d serial(s)", job_id, len(guideline))

    return {
        "job_id": job_id,
        "entries_count": len(guideline),
        "flagged_count": sum(1 for entry in guideline if entry["flagged"]),
        "guideline_path": str(out_path),
        "guideline": guideline,
    }
