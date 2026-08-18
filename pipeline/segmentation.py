"""Transcript-gap video segmentation (F13b).

Long videos are split into sequential segments at natural transcript gaps
(``SEGMENT_TARGET_DURATION_SEC`` apart) so the whole downstream chain
(B2 -> whisper cross-check -> C1 -> D2/D3 -> D4 -> E1 -> E2 -> F3) runs
independently on one segment at a time and every segment yields a standalone
playable cut video with its own dubbed audio.

Boundary selection never cuts mid-dialogue: a segment boundary is only placed
inside a gap between two consecutive subtitle entries (a time window where no
speech plays), choosing the gap closest to ``start + TARGET``. A video with no
usable gaps, or shorter than the target, yields exactly one segment covering
the whole video — the caller keeps the existing single-video flow for that
case, so short videos behave byte-identically to today.

Per-segment artifacts live under ``uploads/<job_id>/segments_pipeline/seg_XXX/``.
Each directory mirrors a mini job so the existing stage functions run against
it unchanged through their ``job_dir`` override:

- ``source.mp4`` — the segment cut, re-encoded so its timestamps start exactly
  at 0 (a ``-c copy`` cut would start at the nearest keyframe and shift every
  segment-relative timestamp).
- ``job_meta.json`` — the parent meta with ``duration_sec`` set to the probed
  duration of the cut, plus ``segment_index`` / ``segment_start_sec`` /
  ``segment_end_sec``.
- ``subtitles_zh_raw.json`` — the segment's subtitle slice with timestamps
  re-based to segment-relative (``status`` is ``"ok"``, a clean contiguous
  slice has no failed segments).
- ``segment_plan.json`` — persisted next to the segments for the orchestrator.
"""

import json
import subprocess
from pathlib import Path

from pipeline import config, video_ingest

SEGMENTS_DIR_NAME = "segments_pipeline"
SEGMENT_DIR_PREFIX = "seg_"
PLAN_FILENAME = "segment_plan.json"

logger = __import__("logging").getLogger(__name__)


def segment_key(index):
    """Stable per-segment key (``seg_000``) shared by dirs and job status."""
    return f"{SEGMENT_DIR_PREFIX}{int(index):03d}"


def segments_root(job_id, upload_root=None):
    """The ``segments_pipeline/`` directory for a job."""
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    return root / job_id / SEGMENTS_DIR_NAME


def segment_dir(job_id, seg_index, upload_root=None):
    """The mini-job directory for one segment."""
    return segments_root(job_id, upload_root) / segment_key(seg_index)


def plan_path(job_id, upload_root=None):
    """Path to the persisted ``segment_plan.json`` for a job."""
    return segments_root(job_id, upload_root) / PLAN_FILENAME


def is_segmented(job_id, upload_root=None):
    """True when a job runs the segmented pipeline (F13b state exists).

    Only plans with more than one segment count: a single-segment plan means
    the caller kept the whole-video flow, so short jobs must not be routed
    through the segmented continuations (D2/D3 per-segment paths).
    """
    path = plan_path(job_id, upload_root)
    if not path.exists():
        return False
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return len(plan.get("segments") or []) > 1


def _sorted_subtitles(raw):
    """Chronological subtitle entries from a ``subtitles_zh_raw.json`` dict."""
    subs = []
    for item in (raw.get("subtitles") or []):
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start_sec", 0.0))
            end = float(item.get("end_sec", 0.0))
        except (TypeError, ValueError):
            continue
        subs.append({"text": item.get("text", ""), "start_sec": start, "end_sec": end})
    subs.sort(key=lambda s: (s["start_sec"], s["end_sec"]))
    return subs


def _gap_points(subtitles):
    """Absolute cut times inside dialogue-free gaps between consecutive lines.

    A gap between entry ``i`` and ``i+1`` exists when the next line starts
    after the previous one ends; the cut is placed at the gap midpoint, so it
    can never land inside a spoken line. Returns a sorted list of absolute
    seconds, all strictly between the first line's start and the last line's
    end.
    """
    points = []
    for i in range(len(subtitles) - 1):
        end = float(subtitles[i]["end_sec"])
        start = float(subtitles[i + 1]["start_sec"])
        if start > end:
            points.append((end + start) / 2.0)
    return points


def _choose_segment_end(seg_start, target, duration, cuts):
    """Pick the end of the segment starting at ``seg_start``.

    The boundary is the cut closest to ``seg_start + target`` (ties favour the
    later cut), but never a cut that would strand a tiny trailing piece
    (``< SEGMENT_MIN_TRAILING_RATIO * target``). Returns ``duration`` when the
    remaining video is below the target or no suitable cut exists.
    """
    remaining = duration - seg_start
    if remaining <= target + 1e-6:
        return duration
    candidates = [c for c in cuts if seg_start < c < duration]
    if not candidates:
        return duration
    best = min(candidates, key=lambda c: (abs(c - (seg_start + target)), -c))
    if duration - best < config.SEGMENT_MIN_TRAILING_RATIO * target:
        return duration
    return best


def build_segment_plan(job_id, upload_root=None, target_duration_sec=None):
    """Build the segment plan from the job's transcript + video duration.

    Reads ``subtitles_zh_raw.json`` (the full-video transcript, F1 output) and
    ``job_meta.json`` (falling back to probing ``source.mp4``), then places
    boundaries at transcript gaps. Persists the plan to
    ``segments_pipeline/segment_plan.json`` and returns it::

        {
            "job_id": ...,
            "source_duration_sec": ...,
            "target_duration_sec": ...,
            "strategy": "transcript_gap",
            "segments": [
                {"index": 0, "start_sec": ..., "end_sec": ...,
                 "duration_sec": ..., "entries_count": ...,
                 "first_subtitle_index": ..., "last_subtitle_index": ...},
            ],
        }

    Raises ``FileNotFoundError`` when the job has no ``subtitles_zh_raw.json``.
    """
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = root / job_id
    raw_path = job_dir / "subtitles_zh_raw.json"
    if not raw_path.exists():
        raise FileNotFoundError(f"no subtitles_zh_raw.json for job {job_id}")

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    subtitles = _sorted_subtitles(raw)

    meta = {}
    meta_path = job_dir / "job_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    duration = meta.get("duration_sec")
    if duration is None:
        duration = video_ingest.probe_video(job_dir / "source.mp4").get("duration_sec")
    duration = float(duration or 0.0)

    target = config.SEGMENT_TARGET_DURATION_SEC
    if target_duration_sec is not None:
        target = float(target_duration_sec)

    cuts = _gap_points(subtitles)

    segments = []
    seg_start = 0.0
    entry_idx = 0
    while seg_start < duration - 1e-6:
        seg_end = _choose_segment_end(seg_start, target, duration, cuts)
        first = entry_idx
        last = first - 1
        while entry_idx < len(subtitles) and float(subtitles[entry_idx]["start_sec"]) < seg_end:
            last = entry_idx
            entry_idx += 1
        segments.append(
            {
                "index": len(segments),
                "start_sec": round(seg_start, 3),
                "end_sec": round(seg_end, 3),
                "duration_sec": round(seg_end - seg_start, 3),
                "entries_count": (last - first + 1) if last >= first else 0,
                "first_subtitle_index": first,
                "last_subtitle_index": last,
            }
        )
        if seg_end >= duration - 1e-6:
            break
        seg_start = seg_end

    plan = {
        "job_id": job_id,
        "source_duration_sec": round(duration, 3),
        "target_duration_sec": target,
        "strategy": "transcript_gap",
        "segments": segments,
    }
    _save_plan(job_id, plan, upload_root)
    return plan


def _save_plan(job_id, plan, upload_root=None):
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    path = plan_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    return plan


def load_plan(job_id, upload_root=None):
    """Load the persisted segment plan; raises ``FileNotFoundError``."""
    path = plan_path(job_id, upload_root)
    if not path.exists():
        raise FileNotFoundError(f"no segment plan for job {job_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def _run_ffmpeg(args):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=300)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg error: {result.stderr.strip()}")


def _cut_segment_video(source, out_path, start_sec, end_sec):
    """Cut ``[start_sec, end_sec)`` from the source, re-encoding so the output
    starts exactly at 0 (a ``-c copy`` cut would start at a keyframe)."""
    _run_ffmpeg(
        [
            "ffmpeg", "-y",
            "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}",
            "-i", str(source),
            "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(out_path),
        ]
    )


def _probe_duration(path):
    data = video_ingest.probe_video(path)
    duration = data.get("duration_sec")
    return float(duration) if duration is not None else None


def materialize_segment(job_id, plan, segment, upload_root=None):
    """Prepare one segment's mini-job directory (idempotent).

    Cuts ``source.mp4``, writes ``job_meta.json`` (segment duration) and
    ``subtitles_zh_raw.json`` (segment-relative slice). Returns the segment
    directory. Already-materialized segments are returned as-is, so a resumed
    pipeline never re-cuts finished segments.
    """
    seg_dir = segment_dir(job_id, segment["index"], upload_root)
    if (seg_dir / "source.mp4").exists() and (seg_dir / "subtitles_zh_raw.json").exists():
        return seg_dir

    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = root / job_id
    source = job_dir / "source.mp4"
    if not source.exists():
        raise FileNotFoundError(f"no source.mp4 for job {job_id}")

    seg_dir.mkdir(parents=True, exist_ok=True)
    out_video = seg_dir / "source.mp4"
    _cut_segment_video(source, out_video, segment["start_sec"], segment["end_sec"])

    actual_duration = _probe_duration(out_video) or segment["duration_sec"]

    meta = {}
    meta_path = job_dir / "job_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
    meta["duration_sec"] = round(actual_duration, 3)
    meta["source_path"] = str(out_video)
    meta["segment_index"] = segment["index"]
    meta["segment_start_sec"] = segment["start_sec"]
    meta["segment_end_sec"] = segment["end_sec"]
    (seg_dir / "job_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    raw_path = job_dir / "subtitles_zh_raw.json"
    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    subtitles = _sorted_subtitles(raw)
    first = int(segment["first_subtitle_index"])
    last = int(segment["last_subtitle_index"])
    offset = float(segment["start_sec"])
    slice_subs = []
    for item in subtitles[first:last + 1] if last >= first else []:
        slice_subs.append(
            {
                "text": item["text"],
                "start_sec": round(float(item["start_sec"]) - offset, 3),
                "end_sec": round(float(item["end_sec"]) - offset, 3),
            }
        )

    seg_raw = {
        "job_id": job_id,
        "status": "ok",
        "chunked": False,
        "segments_count": 1,
        "failed_segments": [],
        "errors": {},
        "subtitles": slice_subs,
    }
    for key in ("whisper_used", "whisper_segments_count", "gemini_hallucinated_dropped"):
        if key in raw:
            seg_raw[key] = raw[key]
    (seg_dir / "subtitles_zh_raw.json").write_text(
        json.dumps(seg_raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return seg_dir
