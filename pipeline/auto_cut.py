"""FFmpeg auto-cut: build the draft final video (E2).

For every serial in ``edit_guideline.json`` (E1 output) the matching
``[source_start_sec, source_end_sec]`` part of the source video is cut out
(**video stream only** — the original audio is discarded; the Hindi voiceover
is mixed in whole at the end), time-stretched/squeezed to exactly the target
duration with FFmpeg's ``setpts=<pts_multiplier>*PTS`` filter, the processed
clips are concatenated in serial order, and finally ``voiceover_hi.wav`` (D4)
is muxed in as the audio track.

Output: ``uploads/<job_id>/draft_final_video.mp4``. Resolution and aspect
ratio are inherited from the source video (no forced values).

Verification (mirrors the Auto Manhwa Maker ``render.py`` pattern): the draft
is probed with ffprobe and must have both a video and an audio stream and a
duration within a few frames of the voiceover duration. A failed validation
raises :class:`DraftValidationError`.
"""

import json
import logging
import subprocess
from pathlib import Path

from pipeline import config, video_ingest

logger = logging.getLogger(__name__)

DRAFT_CLIPS_DIR = "auto_cut_clips"


class DraftValidationError(Exception):
    """Raised when the rendered draft fails ffprobe validation."""


def _run(cmd, timeout=config.RENDER_TIMEOUT_SEC):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg/ffprobe failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg/ffprobe error: {result.stderr.strip()}")
    return result


def _probe(path):
    """Run ffprobe and return the full JSON (format + streams)."""
    cmd = [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path),
    ]
    result = _run(cmd, 60)
    return json.loads(result.stdout)


def _probe_duration(path):
    data = _probe(path)
    try:
        return float(data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        return 0.0


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


def _first_video_frame_rate(probe_data):
    """Extract the frame rate (fps) of the first video stream, if possible."""
    for stream in probe_data.get("streams", []) or []:
        if stream.get("codec_type") != "video":
            continue
        for key in ("avg_frame_rate", "r_frame_rate"):
            value = stream.get(key)
            if not value:
                continue
            try:
                if "/" in value:
                    num, den = value.split("/")
                    fps = float(num) / float(den)
                else:
                    fps = float(value)
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            if fps and fps > 0:
                return fps
        return None
    return None


def build_clip_command(source, out_path, start_sec, end_sec, pts_multiplier):
    """ffmpeg command that cuts one source segment and speed-adjusts it.

    Video stream only (``-an``); the original audio is never carried over.
    ``setpts=<pts_multiplier>*PTS`` makes the clip exactly
    ``pts_multiplier`` times longer, matching the target duration.
    """
    return [
        "ffmpeg", "-y",
        "-ss", f"{start_sec:.3f}", "-to", f"{end_sec:.3f}",
        "-i", str(source),
        "-an",
        "-vf", f"setpts={pts_multiplier:.6f}*PTS",
        "-c:v", config.RENDER_VIDEO_CODEC,
        "-preset", config.RENDER_VIDEO_PRESET,
        "-pix_fmt", config.RENDER_PIX_FMT,
        str(out_path),
    ]


def build_concat_command(concat_list, out_path):
    """ffmpeg command that concatenates the processed clips (stream copy)."""
    return [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(out_path),
    ]


def build_mux_command(concat_video, voiceover, out_path):
    """ffmpeg command that muxes the voiceover as the draft's audio track."""
    return [
        "ffmpeg", "-y",
        "-i", str(concat_video),
        "-i", str(voiceover),
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", config.RENDER_AUDIO_CODEC,
        str(out_path),
    ]


def _validate_draft(probe_data, expected_duration_sec, tolerance_sec):
    """Check the draft has video+audio and a duration near the voiceover.

    Returns ``(ok, details)`` where ``details`` carries the individual checks
    (useful for logging / error messages).
    """
    streams = probe_data.get("streams", []) or []
    has_video = any(s.get("codec_type") == "video" for s in streams)
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    try:
        actual_duration = float(probe_data.get("format", {}).get("duration"))
    except (TypeError, ValueError):
        actual_duration = None

    duration_ok = (
        actual_duration is not None
        and abs(actual_duration - float(expected_duration_sec))
        <= float(tolerance_sec)
    )
    ok = bool(has_video and has_audio and duration_ok)
    details = {
        "has_video": has_video,
        "has_audio": has_audio,
        "duration_sec": actual_duration,
        "expected_duration_sec": float(expected_duration_sec),
        "tolerance_sec": float(tolerance_sec),
        "duration_ok": duration_ok,
    }
    return ok, details


def build_draft_video(job_id, upload_root=None):
    """Render ``draft_final_video.mp4`` for a job. Returns a result dict.

    Raises FileNotFoundError when the job, ``edit_guideline.json``,
    ``source.mp4`` or ``voiceover_hi.wav`` is missing; RuntimeError when an
    ffmpeg/ffprobe step fails; DraftValidationError when the rendered draft
    fails the ffprobe validation.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    guideline_path = job_dir / "edit_guideline.json"
    source = job_dir / "source.mp4"
    voiceover = job_dir / "voiceover_hi.wav"
    for path, name in (
        (guideline_path, "edit_guideline.json"),
        (source, "source.mp4"),
        (voiceover, "voiceover_hi.wav"),
    ):
        if not path.exists():
            raise FileNotFoundError(f"no {name} for job {job_id}")

    guideline = _load_json_list(guideline_path)
    if not guideline:
        logger.warning("job %s: empty guideline, nothing to render", job_id)
        return {
            "job_id": job_id,
            "status": "no_serials",
            "clip_count": 0,
            "draft_path": None,
        }

    voiceover_duration = _probe_duration(voiceover)

    source_probe = _probe(source)
    fps = _first_video_frame_rate(source_probe)
    frame_duration = (1.0 / fps) if fps else (1.0 / config.RENDER_DEFAULT_FPS)
    tolerance = config.RENDER_TOLERANCE_FRAMES * frame_duration

    clips_dir = job_dir / DRAFT_CLIPS_DIR
    clips_dir.mkdir(parents=True, exist_ok=True)

    clip_paths = []
    for index, entry in enumerate(guideline):
        serial = entry.get("serial")
        start_sec = float(entry.get("source_start_sec", 0.0))
        end_sec = float(entry.get("source_end_sec", 0.0))
        pts_multiplier = float(entry.get("pts_multiplier", 1.0))
        out_path = clips_dir / f"serial_{index:05d}.mp4"
        logger.info(
            "job %s: cutting serial %s [%.3f..%.3f] pts x%.4f",
            job_id, serial, start_sec, end_sec, pts_multiplier,
        )
        _run(build_clip_command(source, out_path, start_sec, end_sec, pts_multiplier))
        clip_paths.append(out_path)

    concat_list = clips_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{path.name}'\n" for path in clip_paths), encoding="utf-8"
    )
    concat_video = clips_dir / "concat_video.mp4"
    _run(build_concat_command(concat_list, concat_video))

    draft_out = job_dir / "draft_final_video.mp4"
    _run(build_mux_command(concat_video, voiceover, draft_out))

    final_probe = _probe(draft_out)
    ok, details = _validate_draft(final_probe, voiceover_duration, tolerance)
    if not ok:
        logger.error("job %s: draft validation failed: %s", job_id, details)
        raise DraftValidationError(
            f"draft video validation failed for job {job_id}: {details}"
        )

    logger.info("job %s: draft rendered and validated (%s)", job_id, draft_out)
    return {
        "job_id": job_id,
        "status": "ok",
        "clip_count": len(clip_paths),
        "duration_sec": details["duration_sec"],
        "voiceover_duration_sec": round(voiceover_duration, 3),
        "tolerance_sec": round(tolerance, 3),
        "draft_path": str(draft_out),
    }
