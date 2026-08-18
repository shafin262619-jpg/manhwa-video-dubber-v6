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
is probed with ffprobe and must have both a video and an audio stream. The
duration is checked against the **source video's** duration (the same
ffprobe-derived length ``subtitle_qa.json`` reports) only when the voiceover
length is controllable — the auto-TTS path (a few source frames). For the
``user_upload`` path a translated voiceover is legitimately a different total
length from the source video, so a duration mismatch never blocks; it is only
reported (plus a non-blocking wrong-file warning when the ratio is extreme).
A failed structural validation raises :class:`DraftValidationError`.
"""

import json
import logging
import subprocess
from pathlib import Path

from pipeline import config, lang_files, video_ingest, voiceover_unify

logger = logging.getLogger(__name__)

DRAFT_CLIPS_DIR = "auto_cut_clips"


class DraftValidationError(Exception):
    """Raised when the rendered draft fails ffprobe validation."""


_ERROR_HINTS = (
    "error", "abort", "invalid", "failed", "no such", "cannot",
    "not found", "unable", "does not", "unsupported",
)


def _extract_ffmpeg_error(stderr):
    """Pull the meaningful error line out of an ffmpeg/ffprobe stderr dump.

    ffmpeg prints a long version banner (``ffmpeg version ...``,
    ``configuration: ...``) ahead of the actual failure. Taking the last
    non-banner line that mentions a failure gives the user a readable message
    (e.g. ``-to value smaller than -ss; aborting``) instead of the banner.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    if not lines:
        return "unknown ffmpeg error"
    hits = []
    for line in lines:
        low = line.lower()
        if (
            low.startswith("ffmpeg version")
            or low.startswith("configuration:")
            or "built with" in low
            or low.startswith("libavutil")
            or low.startswith("libavcodec")
        ):
            continue
        if any(hint in low for hint in _ERROR_HINTS):
            hits.append(line)
    if hits:
        return hits[-1]
    return lines[-1]


def _run(cmd, timeout=config.RENDER_TIMEOUT_SEC):
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"ffmpeg/ffprobe failed: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg error: {_extract_ffmpeg_error(result.stderr)}"
        )
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


def _source_duration(job_dir):
    """Source video duration (sec) — the single source of truth for the draft
    validation.

    Reads ``job_meta.json`` (written by ``video_ingest`` from an ffprobe of
    ``source.mp4``), falling back to probing the file directly. This is the
    same origin ``subtitle_builder`` uses for ``subtitle_qa.json``'s
    ``total_duration_sec``, so diagnostics and the draft validation agree on
    the real video length instead of two unrelated numbers.
    """
    meta_path = job_dir / "job_meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}
        duration = meta.get("duration_sec")
        if duration is not None:
            try:
                return float(duration)
            except (TypeError, ValueError):
                pass
    return _probe_duration(job_dir / "source.mp4")


def _draft_validation_tolerance(expected_duration_sec, source_probe, voice_source):
    """Draft-duration validation tolerance for a job's voice source.

    - ``auto_tts`` (and unset choice): strict — a few frames of the source
      frame rate. TTS clip durations are measured/precise, so the draft is
      expected to land essentially on the source duration.
    - ``user_upload``: loose — ``USER_UPLOAD_DURATION_TOLERANCE_SEC`` seconds
      or ``USER_UPLOAD_DURATION_TOLERANCE_RATIO`` of the source duration,
      whichever is larger. Since the duration check no longer blocks the
      user_upload path, this value is informational only (reported in the
      result diagnostics).
    """
    if voice_source == voiceover_unify.ALLOWED_MODES[1]:
        return max(
            config.USER_UPLOAD_DURATION_TOLERANCE_SEC,
            float(expected_duration_sec) * config.USER_UPLOAD_DURATION_TOLERANCE_RATIO,
        )
    fps = _first_video_frame_rate(source_probe)
    frame_duration = (1.0 / fps) if fps else (1.0 / config.RENDER_DEFAULT_FPS)
    return config.RENDER_TOLERANCE_FRAMES * frame_duration


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


def _validate_draft(probe_data, expected_duration_sec, tolerance_sec,
                    enforce_duration=True):
    """Check the draft has video+audio (always) and, when ``enforce_duration``
    is true, a duration near the expected value.

    ``enforce_duration=False`` is used for the ``user_upload`` path: a
    human-recorded / translated voiceover is legitimately a different total
    length from the source video, so a duration mismatch must never block the
    render. The duration is still computed and reported (``duration_ok``) so
    diagnostics and the non-blocking wrong-file warning have the number.

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
    ok = bool(has_video and has_audio and (duration_ok if enforce_duration else True))
    details = {
        "has_video": has_video,
        "has_audio": has_audio,
        "duration_sec": actual_duration,
        "expected_duration_sec": float(expected_duration_sec),
        "tolerance_sec": float(tolerance_sec),
        "duration_ok": duration_ok,
        "duration_enforced": bool(enforce_duration),
    }
    return ok, details


def _duration_warning(expected_duration_sec, actual_duration_sec):
    """Non-blocking "did you upload the right file?" warning for user_upload.

    Returns a message string when the draft (== uploaded voiceover) length
    differs from the source video by at least
    ``USER_UPLOAD_DURATION_WARNING_RATIO`` in either direction, else None.
    Never blocks — this only surfaces on the result page.
    """
    if expected_duration_sec is None or not actual_duration_sec:
        return None
    expected = float(expected_duration_sec)
    if expected <= 0:
        return None
    ratio = float(actual_duration_sec) / expected
    threshold = config.USER_UPLOAD_DURATION_WARNING_RATIO
    if ratio >= threshold or ratio <= 1.0 / threshold:
        return (
            f"The uploaded voiceover (~{float(actual_duration_sec):.0f}s) is "
            f"about {ratio:.1f}x the length of the original video "
            f"({expected:.0f}s). This can be normal translation drift, but if "
            "it looks wrong, double-check you uploaded the right audio file."
        )
    return None


def build_draft_video(job_id, upload_root=None, progress_cb=None, job_dir=None):
    """Render ``draft_final_video.mp4`` for a job. Returns a result dict.

    ``progress_cb(processed, total)`` (optional) is called after every serial
    clip is rendered, with the 1-based count over the total guideline entries,
    so the job-status wiring can report per-clip progress (F9).

    ``job_dir`` (optional) runs the stage against a different directory (a
    per-segment mini job, F13b) instead of ``upload_root / job_id``.

    Raises FileNotFoundError when the job, ``edit_guideline.json``,
    ``source.mp4`` or ``voiceover_hi.wav`` is missing; RuntimeError when an
    ffmpeg/ffprobe step fails; DraftValidationError when the rendered draft
    fails the ffprobe validation.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = Path(job_dir) if job_dir else upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    guideline_path = job_dir / "edit_guideline.json"
    source = job_dir / "source.mp4"
    voiceover_name = lang_files.voiceover_audio(
        lang_files.target_lang(job_id, upload_root)
    )
    voiceover = job_dir / voiceover_name
    for path, name in (
        (guideline_path, "edit_guideline.json"),
        (source, "source.mp4"),
        (voiceover, voiceover_name),
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
    expected_duration_sec = _source_duration(job_dir)
    voice_source = voiceover_unify.get_voice_source(job_id, upload_root)
    tolerance = _draft_validation_tolerance(
        expected_duration_sec, source_probe, voice_source
    )
    # The total-duration check only makes sense when the voiceover length is
    # controllable (auto-TTS clips are measured/precise). A user upload is
    # legitimately a different length from the source video (translation
    # drift), so for user_upload the duration is reported + warned about but
    # never blocks — only the per-segment alignment check can fail that path.
    enforce_duration = voice_source != voiceover_unify.ALLOWED_MODES[1]

    clips_dir = job_dir / DRAFT_CLIPS_DIR
    clips_dir.mkdir(parents=True, exist_ok=True)

    clip_paths = []
    for index, entry in enumerate(guideline):
        serial = entry.get("serial")
        start_sec = float(entry.get("source_start_sec", 0.0))
        end_sec = float(entry.get("source_end_sec", 0.0))
        target_start_sec = float(entry.get("target_start_sec", 0.0))
        target_end_sec = float(entry.get("target_end_sec", 0.0))
        pts_multiplier = float(entry.get("pts_multiplier", 1.0))
        out_path = clips_dir / f"serial_{index:05d}.mp4"

        seg_duration = end_sec - start_sec
        target_duration = target_end_sec - target_start_sec
        if (
            seg_duration <= config.RENDER_MIN_SEGMENT_DURATION_SEC
            or target_duration <= config.RENDER_MIN_SEGMENT_DURATION_SEC
        ):
            # Degenerate window (E7: zero/negative source; F8: collapsed
            # target): ffmpeg aborts with "-to value smaller than -ss" on a
            # zero/negative cut, and a collapsed target must render near-zero
            # instead of the full untouched source clip. Cut a minimal real
            # window and let the guideline multiplier (or the target, when
            # large enough) size it.
            if (
                seg_duration <= config.RENDER_MIN_SEGMENT_DURATION_SEC
                and target_duration <= config.RENDER_MIN_SEGMENT_DURATION_SEC
            ):
                degenerate_side = "degenerate source and target segment"
            elif seg_duration <= config.RENDER_MIN_SEGMENT_DURATION_SEC:
                degenerate_side = "degenerate source segment"
            else:
                degenerate_side = "degenerate target segment"
            logger.warning(
                "job %s: serial %s %s [%.3f..%.3f] -> target %.3fs; "
                "cutting minimal window so ffmpeg does not abort and the "
                "clip does not render full-length",
                job_id, serial, degenerate_side, start_sec, end_sec,
                target_duration,
            )
            min_window = config.RENDER_MIN_SEGMENT_DURATION_SEC
            if expected_duration_sec > min_window:
                cut_start = min(start_sec, expected_duration_sec - min_window)
                cut_end = cut_start + min_window
            else:
                cut_start = 0.0
                cut_end = expected_duration_sec
            start_sec, end_sec = cut_start, cut_end
            if target_duration > min_window:
                pts_multiplier = target_duration / min_window

        logger.info(
            "job %s: cutting serial %s [%.3f..%.3f] pts x%.4f",
            job_id, serial, start_sec, end_sec, pts_multiplier,
        )
        _run(build_clip_command(source, out_path, start_sec, end_sec, pts_multiplier))
        clip_paths.append(out_path)
        if progress_cb is not None:
            progress_cb(index + 1, len(guideline))

    concat_list = clips_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{path.name}'\n" for path in clip_paths), encoding="utf-8"
    )
    concat_video = clips_dir / "concat_video.mp4"
    _run(build_concat_command(concat_list, concat_video))

    draft_out = job_dir / "draft_final_video.mp4"
    _run(build_mux_command(concat_video, voiceover, draft_out))

    final_probe = _probe(draft_out)
    ok, details = _validate_draft(
        final_probe, expected_duration_sec, tolerance, enforce_duration
    )
    if not ok:
        logger.error("job %s: draft validation failed: %s", job_id, details)
        raise DraftValidationError(
            f"draft video validation failed for job {job_id}: {details}"
        )

    duration_warning = None
    if not enforce_duration and details.get("duration_sec") is not None:
        duration_warning = _duration_warning(
            expected_duration_sec, details["duration_sec"]
        )
        if duration_warning:
            logger.warning("job %s: %s", job_id, duration_warning)

    logger.info("job %s: draft rendered and validated (%s)", job_id, draft_out)
    return {
        "job_id": job_id,
        "status": "ok",
        "clip_count": len(clip_paths),
        "duration_sec": details["duration_sec"],
        "expected_duration_sec": round(expected_duration_sec, 3),
        "voice_source": voice_source,
        "voiceover_duration_sec": round(voiceover_duration, 3),
        "tolerance_sec": round(tolerance, 3),
        "duration_enforced": enforce_duration,
        "duration_warning": duration_warning,
        "draft_path": str(draft_out),
    }
