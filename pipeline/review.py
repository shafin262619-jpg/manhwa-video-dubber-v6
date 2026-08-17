"""Per-clip review UI + apply review edits (F1 + F2).

F1 builds a review page (``/review/{job_id}``) with one block per serial of
the E1 ``edit_guideline.json``:

- a clip player that streams the serial's segment of ``draft_final_video.mp4``
  (extracted on-the-fly with ffmpeg from ``[target_start_sec,
  target_end_sec]`` — the browser never downloads the whole draft),
- a trim timeline (source start/end controls) used by F2,
- the translated subtitle text (``text_translated`` from the target-language
  ``subtitles_<target_lang>.json``) and the line's target duration.

Serials flagged ``extreme_speed_ratio`` / ``invalid_duration`` are pulled out
into a highlighted section (banner on top + a highlighted box).

F2's ``apply_clip_edit`` applies one serial's trim edit to the draft: only the
edited serial's guideline entry changes (target timing stays fixed), the
matching source segment is re-cut with the new range + a recomputed
``pts_multiplier`` and the draft is re-spliced by re-running the concat/mux
steps on the existing clips — the rest of the video is never re-encoded
(partial re-render pattern, mirroring the Auto Manhwa Maker render flow).
"""

import html
import json
import logging
from pathlib import Path

from pipeline import auto_cut, config, lang_files, ui, video_ingest, voiceover_unify
from pipeline.auto_cut import DraftValidationError

logger = logging.getLogger(__name__)

REVIEW_CLIPS_DIR = "review_clips"


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


def _parse_optional(value, default):
    """Parse an optional numeric field; empty/None keep the default."""
    if value is None:
        return default
    text = str(value).strip()
    if text == "":
        return default
    try:
        return float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid number: {value!r}") from exc


def _index_by_serial(entries):
    by_serial = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("serial") is None:
            continue
        try:
            by_serial[int(entry["serial"])] = entry
        except (TypeError, ValueError):
            continue
    return by_serial


def _find_guideline_entry(guideline, serial):
    for entry in guideline:
        if not isinstance(entry, dict):
            continue
        try:
            if int(entry.get("serial")) == serial:
                return entry
        except (TypeError, ValueError):
            continue
    return None


def get_review_items(job_id, upload_root=None):
    """Build the list of per-serial review items for a job.

    Each item joins the E1 guideline timing with the B2 translated subtitle
    text: ``{"serial", "text_translated", "source_start_sec", "source_end_sec",
    "target_start_sec", "target_end_sec", "source_duration_sec",
    "target_duration_sec", "pts_multiplier", "flagged", "flag_reason"}``.

    Raises FileNotFoundError when the job or ``edit_guideline.json`` is
    missing. The target-language ``subtitles_<lang>.json`` is optional
    (missing text -> empty string).
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    guideline_path = job_dir / "edit_guideline.json"
    if not guideline_path.exists():
        raise FileNotFoundError(f"no edit_guideline.json for job {job_id}")

    guideline = _load_json_list(guideline_path)

    text_by_serial = {}
    subtitles_path = job_dir / lang_files.subtitles_json(
        lang_files.target_lang(job_id, upload_root)
    )
    if subtitles_path.exists():
        try:
            text_by_serial = {
                serial: lang_files.entry_text(entry) or ""
                for serial, entry in _index_by_serial(
                    _load_json_list(subtitles_path)
                ).items()
            }
        except ValueError:
            logger.warning("job %s: malformed subtitles json; ignoring", job_id)

    items = []
    for entry in guideline:
        if not isinstance(entry, dict):
            continue
        try:
            serial = int(entry.get("serial"))
        except (TypeError, ValueError):
            continue
        source_start = _safe_float(entry.get("source_start_sec"))
        source_end = _safe_float(entry.get("source_end_sec"))
        target_start = _safe_float(entry.get("target_start_sec"))
        target_end = _safe_float(entry.get("target_end_sec"))
        items.append(
            {
                "serial": serial,
                "text_translated": text_by_serial.get(serial, ""),
                "source_start_sec": round(source_start, 3),
                "source_end_sec": round(source_end, 3),
                "target_start_sec": round(target_start, 3),
                "target_end_sec": round(target_end, 3),
                "source_duration_sec": round(source_end - source_start, 3),
                "target_duration_sec": round(target_end - target_start, 3),
                "pts_multiplier": round(_safe_float(entry.get("pts_multiplier"), 1.0), 4),
                "flagged": bool(entry.get("flagged", False)),
                "flag_reason": entry.get("flag_reason"),
            }
        )
    items.sort(key=lambda item: item["serial"])
    return items


def _render_item_block(job_id, item):
    serial = item["serial"]
    flagged_class = " review-box flagged" if item["flagged"] else " review-box"
    flag_html = ""
    if item["flagged"]:
        flag_html = (
            '<p class="flag-note">FLAGGED: '
            f'{html.escape(item["flag_reason"] or "unknown")}</p>'
        )
    return f"""
    <section class="{flagged_class.strip()}" id="serial-{serial}">
      <h2>Serial {serial}</h2>
      <video controls preload="metadata" src="/review/{job_id}/clip/{serial}"></video>
      <p class="subtitle"><strong>text_translated:</strong> {html.escape(item["text_translated"])}</p>
      <p class="duration">Target duration: {item["target_duration_sec"]}s
        (source {item["source_duration_sec"]}s, pts x{item["pts_multiplier"]})</p>
      {flag_html}
      <form method="post" action="/review/{job_id}/edit" class="trim-form">
        <input type="hidden" name="serial" value="{serial}">
        <label>source start (s)
          <input type="number" name="new_source_start" step="0.1" min="0"
                 value="{item["source_start_sec"]}">
        </label>
        <label>source end (s)
          <input type="number" name="new_source_end" step="0.1" min="0"
                 value="{item["source_end_sec"]}">
        </label>
        <button type="submit">Apply edit</button>
      </form>
    </section>
    """


def build_review_page(job_id, upload_root=None):
    """Render the review HTML page for a job. Returns the page string."""
    items = get_review_items(job_id, upload_root)
    flagged = [item for item in items if item["flagged"]]

    flagged_banner = ""
    if flagged:
        serial_links = ", ".join(
            f'<a href="#serial-{item["serial"]}">#{item["serial"]}</a> '
            f'({html.escape(item["flag_reason"] or "flagged")})'
            for item in flagged
        )
        flagged_banner = (
            '<div class="flagged-banner">'
            f"<strong>{len(flagged)} flagged serial(s):</strong> {serial_links}"
            "</div>"
        )

    blocks = "\n".join(_render_item_block(job_id, item) for item in items)
    body = f"""<h1>Per-clip review — job {job_id}</h1>
  <p><a href="/final/{job_id}">Final Render →</a></p>
  {flagged_banner}
  {blocks}
"""
    return ui.page(f"Review — job {job_id} — Manhwa Video Dubber", body)


def extract_clip(job_id, serial, upload_root=None):
    """Extract one serial's segment of the draft video into a playable mp4.

    The draft is cut from the serial's ``target_start_sec`` to
    ``target_end_sec`` (its position in ``draft_final_video.mp4``) with ffmpeg
    so the browser player never downloads the whole draft. The clip is always
    re-extracted on demand (fresh after any F2 edit) and written under
    ``review_clips/serial_<serial>.mp4``.

    Returns the clip path. Raises FileNotFoundError when the job, the
    guideline, the serial's entry or the draft video is missing.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    guideline_path = job_dir / "edit_guideline.json"
    if not guideline_path.exists():
        raise FileNotFoundError(f"no edit_guideline.json for job {job_id}")
    entry = _find_guideline_entry(_load_json_list(guideline_path), serial)
    if entry is None:
        raise FileNotFoundError(
            f"serial {serial} not in edit_guideline.json for job {job_id}"
        )

    draft = job_dir / "draft_final_video.mp4"
    if not draft.exists():
        raise FileNotFoundError(f"no draft_final_video.mp4 for job {job_id}")

    start = _safe_float(entry.get("target_start_sec"))
    end = _safe_float(entry.get("target_end_sec"))

    out_dir = job_dir / REVIEW_CLIPS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"serial_{serial:05d}.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
        "-i", str(draft),
        "-c:v", config.RENDER_VIDEO_CODEC,
        "-preset", config.RENDER_VIDEO_PRESET,
        "-pix_fmt", config.RENDER_PIX_FMT,
        "-c:a", config.RENDER_AUDIO_CODEC,
        "-movflags", "+faststart",
        str(out_path),
    ]
    auto_cut._run(cmd)
    logger.info(
        "job %s: review clip for serial %s extracted -> %s",
        job_id, serial, out_path,
    )
    return out_path


def _recompute_flag(pts_multiplier):
    if (
        pts_multiplier < config.SPEED_RATIO_MIN
        or pts_multiplier > config.SPEED_RATIO_MAX
    ):
        return True, "extreme_speed_ratio"
    return False, None


def apply_clip_edit(
    job_id,
    serial,
    new_source_start=None,
    new_source_end=None,
    upload_root=None,
):
    """Apply one serial's trim edit to the draft video (F2).

    Only the edited serial's entry in ``edit_guideline.json`` is updated
    (target timing stays fixed; ``pts_multiplier`` is recomputed as
    ``target_duration / new_source_duration`` and the flag re-evaluated), the
    matching source segment is re-cut with the new range and the draft video
    is re-spliced by re-running the concat/mux steps over the existing clips —
    the rest of the video is never re-encoded (partial re-render pattern).

    Returns a result dict describing the edit. Raises FileNotFoundError when
    the job / guideline / serial / source / voiceover / draft clip is missing;
    ValueError for an invalid range (start >= end, negative times, or no edit
    given); DraftValidationError when the re-spliced draft fails ffprobe
    validation.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    if not job_dir.is_dir():
        raise FileNotFoundError(f"job not found: {job_id}")

    guideline_path = job_dir / "edit_guideline.json"
    source = job_dir / "source.mp4"
    voiceover_name = lang_files.voiceover_audio(
        lang_files.target_lang(job_id, upload_root)
    )
    voiceover = job_dir / voiceover_name
    draft_out = job_dir / "draft_final_video.mp4"
    for path, name in (
        (guideline_path, "edit_guideline.json"),
        (source, "source.mp4"),
        (voiceover, voiceover_name),
        (draft_out, "draft_final_video.mp4"),
    ):
        if not path.exists():
            raise FileNotFoundError(f"no {name} for job {job_id}")

    guideline = _load_json_list(guideline_path)
    entry = _find_guideline_entry(guideline, serial)
    if entry is None:
        raise FileNotFoundError(
            f"serial {serial} not in edit_guideline.json for job {job_id}"
        )

    if new_source_start is None and new_source_end is None:
        raise ValueError("no edit given: pass new_source_start and/or new_source_end")

    new_start = _parse_optional(new_source_start, entry.get("source_start_sec", 0.0))
    new_end = _parse_optional(new_source_end, entry.get("source_end_sec", 0.0))
    if new_start < 0 or new_end < 0:
        raise ValueError("negative source times are not allowed")
    if new_end <= new_start:
        raise ValueError("new source end must be after new source start")

    target_duration = (
        _safe_float(entry.get("target_end_sec"))
        - _safe_float(entry.get("target_start_sec"))
    )
    if target_duration <= 0:
        raise ValueError(
            f"serial {serial} has a non-positive target duration; cannot re-render"
        )

    new_duration = new_end - new_start
    pts_multiplier = target_duration / new_duration
    flagged, flag_reason = _recompute_flag(pts_multiplier)

    entry["source_start_sec"] = round(new_start, 3)
    entry["source_end_sec"] = round(new_end, 3)
    entry["pts_multiplier"] = round(pts_multiplier, 4)
    entry["flagged"] = flagged
    entry["flag_reason"] = flag_reason

    guideline_path.write_text(
        json.dumps(guideline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "job %s: serial %s source range updated to [%.3f, %.3f] (pts x%.4f)",
        job_id, serial, new_start, new_end, pts_multiplier,
    )

    index = guideline.index(entry)
    clips_dir = job_dir / auto_cut.DRAFT_CLIPS_DIR
    clip_paths = [
        clips_dir / f"serial_{i:05d}.mp4" for i in range(len(guideline))
    ]
    for path in clip_paths:
        if not path.exists():
            raise FileNotFoundError(f"missing draft clip {path.name}")

    clip_path = clip_paths[index]
    auto_cut._run(
        auto_cut.build_clip_command(source, clip_path, new_start, new_end, pts_multiplier)
    )

    concat_list = clips_dir / "concat.txt"
    concat_list.write_text(
        "".join(f"file '{path.name}'\n" for path in clip_paths), encoding="utf-8"
    )
    concat_video = clips_dir / "concat_video.mp4"
    auto_cut._run(auto_cut.build_concat_command(concat_list, concat_video))
    auto_cut._run(auto_cut.build_mux_command(concat_video, voiceover, draft_out))

    source_probe = auto_cut._probe(source)
    expected_duration_sec = auto_cut._source_duration(job_dir)
    voice_source = voiceover_unify.get_voice_source(job_id, upload_root)
    tolerance = auto_cut._draft_validation_tolerance(
        expected_duration_sec, source_probe, voice_source
    )
    enforce_duration = voice_source != voiceover_unify.ALLOWED_MODES[1]
    ok, details = auto_cut._validate_draft(
        auto_cut._probe(draft_out), expected_duration_sec, tolerance,
        enforce_duration,
    )
    if not ok:
        logger.error("job %s: post-edit draft validation failed: %s", job_id, details)
        raise DraftValidationError(
            f"draft video validation failed after edit for job {job_id}: {details}"
        )

    logger.info("job %s: draft re-spliced after edit of serial %s", job_id, serial)
    return {
        "job_id": job_id,
        "serial": serial,
        "status": "ok",
        "source_start_sec": entry["source_start_sec"],
        "source_end_sec": entry["source_end_sec"],
        "pts_multiplier": entry["pts_multiplier"],
        "target_duration_sec": round(target_duration, 3),
        "flagged": flagged,
        "flag_reason": flag_reason,
        "re_cut_clip": str(clip_path),
        "draft_path": str(draft_out),
    }
