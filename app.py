"""Manhwa Video Dubber - FastAPI web app.

S1 scaffold + A1 Gemini key settings + A2 video upload.
Run locally with:
    uvicorn app:app --host 0.0.0.0 --port 5000
"""

import html
import json
import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from pipeline import (
    auto_cut,
    config,
    edit_guideline,
    error_bn,
    full_auto_chain,
    gemini_rotation,
    history_store,
    job_config,
    job_logging,
    job_status as job_status_store,
    key_store,
    lang_files,
    render_final,
    resume,
    review,
    segmentation,
    segmented_pipeline,
    stages,
    subtitle_builder,
    subtitle_extract,
    subtitle_qa,
    subtitle_verify,
    transcript_import,
    translator,
    ui,
    unresolved,
    video_ingest,
    voiceover_auto,
    voiceover_unify,
    voiceover_upload,
)

# Without this, the root logger has no handler and falls back to Python's
# bare "handler of last resort" — every WARNING+ from every module (Gemini
# rotation, ffmpeg, etc.) prints as a raw, undecorated message with no
# timestamp/level/source, one after another with nothing to separate them.
# That is what turns a handful of rotated-key failures into an unreadable
# wall of console text. A minimal formatter + level makes console output
# scannable; per-job detail still goes to uploads/<job_id>/logs/pipeline.log
# via pipeline.job_logging, unaffected by this.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

logger = logging.getLogger(__name__)

# F12b: shown in the job status when the user-transcript gap-fill could not
# re-extract every missing window from the video. Non-blocking — the rest of
# the pipeline still runs with the transcript as-is.
TRANSCRIPT_GAP_FILL_WARNING_BN = (
    "আপনার আপলোড করা ট্রান্সক্রিপ্টে কিছু সময়ের ব্যবধান ভিডিও থেকে "
    "পুনরায় নিষ্কাশন করা যায়নি; পাইপলাইনের বাকি অংশ স্বাভাবিকভাবে চলছে।"
)


def _friendly_error(exc: Exception) -> str:
    """Short, human-readable summary of a pipeline-stage failure.

    ``str(exc)`` on a Gemini rotation failure can be a multi-key attempt log
    where every entry embeds a full raw API error body — technically
    accurate but unreadable as a single line on a result page. Summarize the
    common cases instead of dumping the raw text; fall back to a truncated
    ``str(exc)`` for anything else so no error is ever silently swallowed.
    """
    if isinstance(exc, gemini_rotation.CallBudgetExceeded):
        return (
            f"This job hit its Gemini call budget ({exc.used}/{exc.max_calls} "
            "calls used) before finishing. Retry to continue with a fresh "
            "budget, or raise MAX_API_CALLS_PER_JOB in settings."
        )
    if isinstance(exc, gemini_rotation.AllKeysExhausted):
        n = len(exc.attempts)
        if n == 0:
            return "No active Gemini API keys are configured. Add one under Settings."
        last_reason = exc.attempts[-1][1]
        return (
            f"All {n} configured Gemini API key(s) failed — rate limit or "
            f"quota exhausted (latest: {last_reason}). Wait for the daily "
            "quota to reset and retry, or add more keys under Settings."
        )
    text = str(exc).strip()
    first_line = text.splitlines()[0] if text else "unknown error"
    return first_line[:280] + ("…" if len(first_line) > 280 else "")


# A job stuck in "running" with no status update for this long is treated as
# stale on the history page (F10.3): the resume button is shown for it.
STALE_RUNNING_SECONDS = 10 * 60


def _write_error_status(job_id, stage, exc):
    """Persist a stage failure with both English + Bengali detail (F11).

    Every error-status write in this module goes through this helper so the
    ``detail_bn`` mirror can never drift out of sync with ``detail``. The
    Bengali mapper is best-effort: if it raises (it should not), the English
    detail is reused so the banner still has something to show.
    """
    extra = {"detail": _friendly_error(exc)}
    try:
        extra["detail_bn"] = error_bn.explain_bn(exc, stage)
    except Exception:  # noqa: BLE001 - the Bengali mirror must never break a write
        extra["detail_bn"] = extra["detail"]
    job_status_store.write_status(job_id, stage, "error", extra=extra)

app = FastAPI(
    title="Manhwa Video Dubber",
    description="Auto Hindi-dub Chinese-subtitled manhwa explain videos.",
    version="0.1.0",
)

STATIC_ROOT = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_ROOT)), name="static")


def _extraction_error_summary(extraction):
    """Concise per-segment error summary for the /upload response (diagnostics)."""
    errors = extraction.get("errors") or {}
    items = []
    for idx in sorted(errors, key=lambda k: int(k)):
        info = errors[idx] or {}
        items.append(
            {
                "segment": int(idx),
                "type": info.get("type", "error"),
                "message": str(info.get("message", ""))[:300],
            }
        )
    return items[:5]


def _continue_from_voiceover(job_id):
    """Run D4 -> E1 -> E2 once the voiceover timing exists (G1 wiring).

    Gated on the voice source choice: without a valid choice there is nothing
    to unify, so the job stays where it is. F9: each stage records its own
    status entry (``D4_unify`` / ``E1_guideline`` / ``E2_draft``). Raises
    FileNotFoundError / RuntimeError / DraftValidationError when a stage fails.
    """
    if voiceover_unify.get_voice_source(job_id) not in voiceover_unify.ALLOWED_MODES:
        return
    job_status_store.run_stage(
        job_id, "D4_unify", voiceover_unify.unify_voiceover_timestamps, job_id
    )
    job_status_store.run_stage(
        job_id, "E1_guideline", edit_guideline.build_edit_guideline, job_id
    )
    job_status_store.run_stage(
        job_id, "E2_draft", auto_cut.build_draft_video, job_id
    )


def _process_auto_tts(job_id):
    """Run the auto-TTS backend chain D2 -> D4 -> E1 -> E2 (G1 wiring).

    Returns ``"pending"`` when the job has no ``subtitles_hi.json`` yet (the
    user must trigger the D2 page first); otherwise runs the whole chain down
    to the draft video so the review phase has data. Raises the underlying
    errors so the caller can surface them.
    """
    job_dir = video_ingest.UPLOAD_ROOT / job_id
    if not (
        job_dir
        / lang_files.subtitles_json(lang_files.target_lang(job_id))
    ).exists():
        return "pending"
    voiceover_auto.generate_auto_voiceover(job_id)
    _continue_from_voiceover(job_id)
    return "ok"


def _resume_pipeline_extra(job_id):
    """Derive the upload-pipeline summary from already-written files.

    Used on the idempotent resume path where the heavy B1/B2/C1 chain is
    skipped because ``subtitles_hi.json`` already exists. Best-effort: missing
    or unreadable files simply keep the defaults.
    """
    job_dir = video_ingest.UPLOAD_ROOT / job_id
    extra = {"extraction_status": "ok", "serials": 0}
    try:
        raw = json.loads(
            (job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        if isinstance(raw, dict):
            extra["extraction_status"] = raw.get("status", "ok")
    except (OSError, ValueError):
        pass
    try:
        hi = json.loads(
            (
                job_dir
                / lang_files.subtitles_json(lang_files.target_lang(job_id))
            ).read_text(encoding="utf-8")
        )
        if isinstance(hi, list):
            extra["serials"] = len(hi)
    except (OSError, ValueError):
        pass
    gap_stats = _load_gap_stats(job_dir)
    if gap_stats.get("detected"):
        extra["gap_fill_stats"] = gap_stats
        if gap_stats.get("failed"):
            extra["gap_fill_warning_bn"] = TRANSCRIPT_GAP_FILL_WARNING_BN
    extra.update(_unresolved_extra(job_id))
    return extra


def _unresolved_extra(job_id):
    """The upload-pipeline extra fields for unresolved segments (F12c Part B).

    Reads the persisted registry: active items carry a Bengali warning
    (``unresolved_warning_bn``) plus the structured ``unresolved_segments``
    list; fully-accepted jobs drop the warning and record ``unresolved_accepted``
    instead. Empty dict when there is nothing to report.
    """
    items = unresolved.load_unresolved(job_id)
    if not items:
        return {}
    active = [i for i in items if i.get("state") != "accepted"]
    if active:
        return {
            "unresolved_warning_bn": unresolved.build_warning_bn(active),
            "unresolved_segments": active,
        }
    return {"unresolved_segments": items, "unresolved_accepted": True}


def _refresh_upload_extra(job_id):
    """Rewrite the ``upload_pipeline`` done entry with fresh unresolved extras.

    Used after the F12c Part B retry/accept endpoints so the status/warning
    channel mirrors the updated registry. Best-effort: a missing/malformed
    stage entry is left untouched.
    """
    status = job_status_store.read_status(job_id)
    entry = (status.get("stages") or {}).get("upload_pipeline") or {}
    if not entry:
        return
    extra = {
        k: v for k, v in entry.items() if k not in ("stage", "state", "progress")
    }
    extra.pop("unresolved_warning_bn", None)
    extra.pop("unresolved_segments", None)
    extra.pop("unresolved_accepted", None)
    extra.update(_unresolved_extra(job_id))
    job_status_store.write_status(job_id, "upload_pipeline", "done", extra=extra)


def _default_gap_stats():
    """A zeroed gap-fill stats dict (F12b) — shared default for fresh runs."""
    return {
        "detected": 0,
        "attempted": 0,
        "filled": 0,
        "failed": 0,
        "added_entries": 0,
        "windows": [],
    }


def _load_gap_stats(job_dir):
    """Read the persisted gap-fill stats sidecar; zeroed default when missing.

    ``_run_upload_pipeline`` writes ``gap_fill_stats.json`` right after gap-fill
    runs so a resumed job can restore the F12b Part C warning even though the
    gap-fill sub-stage itself is never re-run. Never raises.
    """
    try:
        data = json.loads(
            (job_dir / "gap_fill_stats.json").read_text(encoding="utf-8")
        )
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return _default_gap_stats()


def _save_gap_stats(job_dir, gap_stats):
    """Best-effort persist of the gap-fill stats sidecar. Never raises."""
    try:
        (job_dir / "gap_fill_stats.json").write_text(
            json.dumps(gap_stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def _raw_summary(job_dir):
    """Best-effort F1 result summary from an existing ``subtitles_zh_raw.json``.

    Used on the resume path where F1 already finished (its artifact exists) so
    the completed extraction is never re-run; missing/malformed files keep the
    ``"ok"``/``{}`` defaults (a malformed raw is surfaced downstream instead).
    """
    summary = {"status": "ok", "errors": {}}
    try:
        data = json.loads(
            (job_dir / "subtitles_zh_raw.json").read_text(encoding="utf-8")
        )
        if isinstance(data, dict):
            summary["status"] = data.get("status", "ok")
            errors = data.get("errors")
            if isinstance(errors, dict):
                summary["errors"] = errors
    except (OSError, ValueError):
        pass
    return summary


def _load_whisper_check(job_dir):
    """Read an existing whisper cross-check result; ``skipped`` default.

    The whisper sub-stage always writes ``subtitle_qa_whisper.json`` when it
    runs, so its presence is the proof the stage finished. Never raises.
    """
    try:
        data = json.loads(
            (job_dir / "subtitle_qa_whisper.json").read_text(encoding="utf-8")
        )
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {"status": "skipped"}


def _stage_progress(job_id, stage):
    """Return a ``progress_cb(processed, total)`` writing stage progress (F9)."""

    def cb(processed, total):
        job_status_store.write_status(
            job_id, stage, "running",
            extra={"progress": {"processed": processed, "total": total}},
        )

    return cb


def _run_upload_pipeline(job_id):
    """Run the upload chain B1 -> B2 -> C1 on a background thread (G1 wiring).

    Persists status transitions via ``job_status`` so a client can poll
    ``GET /api/jobs/{job_id}/status`` until ``done``/``error``. Never lets an
    exception escape the thread — an uncaught exception in a bare thread
    silently kills it without a log, so the whole body is wrapped in
    try/except and failures are recorded as ``error`` status.

    F9: the two heavy Gemini stages get their own status entries — ``F1_extract``
    (per-chunk progress via ``progress_cb``) and ``C1_translate`` — under the
    umbrella ``upload_pipeline`` stage.

    Idempotent resume: if ``subtitles_hi.json`` already exists the chain is
    not re-run; ``done`` is recorded from the existing files instead. This is
    the basis for the future Retry button (U1c).

    F12c (Part A): resume works at sub-stage granularity too. Each sub-stage
    is skipped when its own artifact already exists (``subtitles_zh_raw.json``
    for F1, ``subtitles_zh.json`` for B2, ``subtitle_qa_whisper.json`` for the
    whisper cross-check, ``subtitles_hi.json`` for C1), so an interrupted
    upload pipeline resumes from its first missing sub-stage without re-running
    the completed ones. Gap-fill stats are persisted to a sidecar right after
    gap-fill so the F12b Part C warning survives a resume.
    """
    try:
        job_dir = video_ingest.UPLOAD_ROOT / job_id
        auto_continue = False
        if (
            job_dir
            / lang_files.subtitles_json(lang_files.target_lang(job_id))
        ).exists():
            extra = _resume_pipeline_extra(job_id)
        else:
            # U2b: the whole upload chain (B1 + C1) shares one per-job
            # CallBudget so a runaway Gemini rotation can never burn more than
            # config.MAX_API_CALLS_PER_JOB calls for a single job run.
            budget = gemini_rotation.CallBudget(config.MAX_API_CALLS_PER_JOB)
            cfg = job_config.read_config(job_id) or {}
            # F12c: on a resume the F1 artifact already exists (the extraction/
            # import ran before the interruption), so F1 and gap-fill are never
            # re-run — the summary is derived from disk and the persisted
            # gap-fill stats instead.
            gap_stats = _load_gap_stats(job_dir)
            if (job_dir / "subtitles_zh_raw.json").exists():
                extraction = _raw_summary(job_dir)
            elif cfg.get("subtitle_source") == "user_transcript":
                # F12a: the user uploaded their own transcript, so F1 (Gemini
                # extraction) is skipped entirely — the uploaded content is
                # imported into subtitles_zh_raw.json instead. Everything
                # downstream (B2, whisper cross-check, C1, D2-F3) is unchanged.
                extraction = job_status_store.run_stage(
                    job_id,
                    "F1_extract",
                    transcript_import.import_transcript,
                    job_id,
                )
                # F12b: best-effort gap-fill for uploaded transcripts. Missing
                # windows between consecutive timed entries are re-extracted
                # from the video with Gemini; failures are non-blocking and
                # only surface a Bengali warning in the job status below.
                try:
                    extraction, gap_stats = transcript_import.fill_gaps(
                        job_id,
                        extraction,
                        call_budget=budget,
                        logger_=job_logging.get_job_logger(job_id),
                    )
                except Exception as exc:  # noqa: BLE001 - gap-fill never breaks the chain
                    logger.warning(
                        "gap-fill raised for job %s (non-fatal): %s", job_id, exc
                    )
                _save_gap_stats(job_dir, gap_stats)
            else:
                extraction = job_status_store.run_stage(
                    job_id,
                    "F1_extract",
                    subtitle_extract.extract_subtitles,
                    job_id,
                    call_budget=budget,
                    progress_cb=_stage_progress(job_id, "F1_extract"),
                )
            # F13b: long videos are split at natural transcript gaps and the
            # whole downstream chain runs per segment, sequentially. Exactly
            # one segment keeps today's whole-video flow (short videos behave
            # byte-identically). user_transcript imports take the same route —
            # the plan reads subtitles_zh_raw.json regardless of its origin.
            # A plan can't be built for jobs without a transcript or a probeable
            # video (e.g. resumed jobs whose source was cleaned up), so any
            # failure here falls back to the existing whole-video flow.
            try:
                segment_plan = segmentation.build_segment_plan(job_id)
            except Exception as exc:  # noqa: BLE001 - segmentation is optional
                logger.warning(
                    "segment plan unavailable for job %s (falling back to "
                    "whole-video flow): %s", job_id, exc,
                )
                segment_plan = None
            if segment_plan and len(segment_plan["segments"]) > 1:
                job_status_store.init_segments(job_id, segment_plan)
                segmented_pipeline.run_segmented_pipeline(
                    job_id, call_budget=budget
                )
                extra = {
                    "extraction_status": extraction["status"],
                    "segmented": True,
                    "segments_count": len(segment_plan["segments"]),
                }
                if extraction["status"] != "ok":
                    extra["errors"] = _extraction_error_summary(extraction)
                if gap_stats.get("detected"):
                    extra["gap_fill_stats"] = gap_stats
                    if gap_stats.get("failed"):
                        extra["gap_fill_warning_bn"] = TRANSCRIPT_GAP_FILL_WARNING_BN
            else:
                if not (job_dir / "subtitles_zh.json").exists():
                    subtitle_builder.build_subtitle_list(job_id, call_budget=budget)
                if (job_dir / "subtitle_qa_whisper.json").exists():
                    whisper_check = _load_whisper_check(job_dir)
                else:
                    try:
                        whisper_check = subtitle_verify.whisper_cross_check(
                            job_id,
                            logger_=job_logging.get_job_logger(job_id),
                        )
                    except Exception as exc:  # noqa: BLE001 - best-effort, never break upload_pipeline
                        logger.warning(
                            "whisper cross-check failed for job %s (non-fatal): %s",
                            job_id, exc,
                        )
                        whisper_check = {"status": "skipped"}
                # C1 is always reached with subtitles_hi.json missing (the top-level
                # branch above already handled the fully-complete case), so the
                # translation stage always runs here.
                translation = job_status_store.run_stage(
                    job_id,
                    "C1_translate",
                    translator.translate_subtitles,
                    job_id,
                    call_budget=budget,
                )
                extra = {
                    "extraction_status": extraction["status"],
                    "serials": len(translation),
                    "whisper_check_status": whisper_check.get("status", "skipped"),
                }
                if extraction["status"] != "ok":
                    extra["errors"] = _extraction_error_summary(extraction)
                if gap_stats.get("detected"):
                    extra["gap_fill_stats"] = gap_stats
                    if gap_stats.get("failed"):
                        extra["gap_fill_warning_bn"] = TRANSCRIPT_GAP_FILL_WARNING_BN
                # F12c Part B: after the automatic repair/translation retries are
                # exhausted, any segments still flagged (repair gaps/clusters or
                # translation fallbacks) are surfaced — non-blocking — via the same
                # warning channel as gap-fill, and the user can ask for one more
                # retry or mark them acceptable.
                unresolved_items, _warning = unresolved.collect_unresolved(job_id)
                if unresolved_items:
                    unresolved.persist_unresolved(job_id, unresolved_items)
                    extra.update(_unresolved_extra(job_id))
                # FA-C1: for the auto_tts path the upload chain now continues,
                # on the SAME thread, straight through the full-auto chain down
                # to the final video (D2 -> D4 -> E1 -> E2 -> F3), so the user
                # gets a zero-click result. The user_upload path (or a job with
                # no choice yet) keeps the old behavior and stops here — group
                # D handles that path. F13b: a segmented job already ran its
                # per-segment D2 -> F3 chains inside run_segmented_pipeline, so
                # the whole-video chain is never re-run.
                auto_continue = (
                    voiceover_unify.get_voice_source(job_id) == "auto_tts"
                )
        job_status_store.write_status(
            job_id, "upload_pipeline", "done", extra=extra
        )

        if auto_continue:
            _run_auto_full_render(job_id)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.error("post-upload pipeline failed for job %s: %s", job_id, exc)
        _write_error_status(job_id, "upload_pipeline", exc)


def _start_stage(job_id, stage, target):
    """Start a background stage thread unless that stage is already running.

    Writes ``"running"`` status first so the polling page has a state to show;
    a concurrent request for the same stage does not spawn a second thread.
    """
    status = job_status_store.read_status(job_id)
    if status.get("stage") == stage and status.get("state") == "running":
        return
    job_status_store.write_status(job_id, stage, "running")
    threading.Thread(target=target, args=(job_id,), daemon=True).start()


def _run_voiceover_auto(job_id):
    """Run D2 -> D4 -> E1 -> E2 on a background thread (auto-TTS page, U1c).

    Never lets an exception escape the thread; failures are persisted as
    ``error`` status so the polling page can surface them.
    """
    try:
        # U2b: the auto-TTS chain shares the same per-job CallBudget pattern as
        # the upload chain (one cap across extraction/translation/TTS calls).
        budget = gemini_rotation.CallBudget(config.MAX_API_CALLS_PER_JOB)
        # F13b: a segmented job runs the whole D2 -> F3 chain per segment
        # instead of the whole-video chain (whose top-level artifacts do not
        # exist for it).
        if segmentation.is_segmented(job_id):
            result = segmented_pipeline.run_segmented_pipeline(
                job_id, call_budget=budget
            )
        else:
            result = job_status_store.run_stage(
                job_id,
                "D2_voiceover",
                voiceover_auto.generate_auto_voiceover,
                job_id,
                call_budget=budget,
            )
            _continue_from_voiceover(job_id)
        job_status_store.write_status(
            job_id,
            "voiceover_auto",
            "done",
            extra={"result": result},
        )
    except (
        FileNotFoundError,
        ValueError,
        RuntimeError,
        auto_cut.DraftValidationError,
        voiceover_unify.VoiceoverAlignmentError,
    ) as exc:
        logger.error("auto voiceover failed for job %s: %s", job_id, exc)
        _write_error_status(job_id, "voiceover_auto", exc)
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception("unexpected auto-voiceover failure for job %s", job_id)
        _write_error_status(job_id, "voiceover_auto", exc)


def _run_final_render(job_id):
    """Run the F3 final render on a background thread (final page, U1c).

    Never lets an exception escape the thread; failures are persisted as
    ``error`` status so the polling page can surface them.
    """
    try:
        result = render_final.finalize_video(job_id)
        job_status_store.write_status(
            job_id,
            "final_render",
            "done",
            extra={"result": result},
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.error("final render failed for job %s: %s", job_id, exc)
        _write_error_status(job_id, "final_render", exc)
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception("unexpected final-render failure for job %s", job_id)
        _write_error_status(job_id, "final_render", exc)


def _run_user_audio_pipeline(job_id):
    """Run D3 -> D4 -> E1 -> E2 -> F3 on a background thread (FA-D2).

    After the user uploads their own audio, this continues on its own daemon
    thread so the job reaches the final video with no further clicks. Never
    lets an exception escape the thread; failures are persisted as ``error``
    status so the polling page can surface them.
    """
    try:
        # F13b: a segmented job aligns the uploaded audio globally once, slices
        # it per segment and renders D4 -> E1 -> E2 -> F3 per segment.
        if segmentation.is_segmented(job_id):
            result = segmented_pipeline.run_segmented_user_audio_pipeline(job_id)
        else:
            result = full_auto_chain.run_user_upload_chain(job_id)
        job_status_store.write_status(
            job_id, "user_audio_pipeline", "done", extra={"result": result}
        )
    except (
        FileNotFoundError,
        ValueError,
        RuntimeError,
        auto_cut.DraftValidationError,
        voiceover_unify.VoiceoverAlignmentError,
    ) as exc:
        logger.error("user audio pipeline failed for job %s: %s", job_id, exc)
        _write_error_status(job_id, "user_audio_pipeline", exc)
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception("unexpected user-audio-pipeline failure for job %s", job_id)
        _write_error_status(job_id, "user_audio_pipeline", exc)


def _run_auto_full_render(job_id):
    """Run the auto_tts full-auto chain D2 -> D4 -> E1 -> E2 -> F3.

    Persists ``auto_full_render`` status (running/done/error). FA-C1 calls
    this from inside the upload thread (same thread, no new spawn); the
    ``/upload`` page also re-uses it via ``_start_stage`` to resume a job
    whose chain never started (a manual override to auto_tts via
    ``/voiceover/{job_id}/choose`` after upload). Never lets an exception
    escape the thread; failures are persisted as ``error`` status.
    """
    try:
        budget = gemini_rotation.CallBudget(config.MAX_API_CALLS_PER_JOB)
        result = full_auto_chain.run_auto_tts_chain(job_id, call_budget=budget)
        job_status_store.write_status(
            job_id, "auto_full_render", "done", extra={"result": result}
        )
    except (
        FileNotFoundError,
        ValueError,
        RuntimeError,
        auto_cut.DraftValidationError,
        voiceover_unify.VoiceoverAlignmentError,
    ) as exc:
        logger.error("auto full render failed for job %s: %s", job_id, exc)
        _write_error_status(job_id, "auto_full_render", exc)
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception(
            "unexpected auto full render failure for job %s", job_id
        )
        _write_error_status(job_id, "auto_full_render", exc)


def _polling_page(job_id, page_title, result_url, stage):
    """Intermediate HTML shown while a background stage runs (U1c/F10).

    No external framework/CDN: a small inline <script> polls
    ``/api/jobs/{job_id}/status`` every 2 seconds. On ``done`` it redirects to
    ``result_url`` (the same endpoint, which then renders its normal result
    page); on ``error`` it shows the error detail plus a "আবার চেষ্টা করুন"
    link back to the same endpoint — safe because every stage is idempotent
    and resumable.

    F10: replaces the lone spinner with an animated progress bar (width = done
    stages + in-stage fraction over ``stages.STAGE_SEQUENCE``) and one row per
    stage with a Bengali label, plus a docked live-log panel fed by
    ``/api/jobs/{job_id}/logs``. F11: the error banner shows ``detail_bn``
    first with the English ``detail`` behind a collapsed toggle.
    """
    body = f"""
  <h1>{page_title}</h1>
  <div id="job-processing" class="processing-banner">
    <span class="spinner" aria-hidden="true"></span>
    <span>Processing… this page updates automatically, no need to refresh.</span>
  </div>
  <div id="progress-panel" class="progress-panel" hidden>
    <div class="progress-track"
         role="progressbar" aria-valuemin="0" aria-valuemax="100"
         aria-valuenow="0" aria-label="Job progress">
      <div id="progress-fill" class="progress-fill" style="width: 0%"></div>
    </div>
    <p id="progress-pct" class="progress-pct">0%</p>
    <ol id="stage-list" class="stage-list"></ol>
  </div>
  <div id="job-error" class="error-banner" hidden></div>
  <div id="log-panel" class="log-panel" hidden>
    <div class="log-panel-head">
      <span class="log-panel-title">লাইভ লগ</span>
      <button type="button" id="log-toggle">Hide</button>
    </div>
    <pre id="log-output" class="log-output"></pre>
  </div>
  <script>
    var JOB_ID = {json.dumps(job_id)};
    var RESULT_URL = {json.dumps(result_url)};
    var STAGE = {json.dumps(stage)};
    var STAGE_SEQUENCE = {json.dumps(stages.STAGE_SEQUENCE)};
    var STAGE_LABELS_BN = {json.dumps(stages.STAGE_LABELS_BN)};
    var STAGE_KEY_GROUPS = {json.dumps(stages.STAGE_KEY_GROUPS)};
    var UMBRELLA_TO_SEQUENCE = {json.dumps(stages.UMBRELLA_TO_SEQUENCE)};
    var LOG_URL = '/api/jobs/' + encodeURIComponent(JOB_ID) + '/logs';
    var nextLogLine = 0;
    var logToggled = false;

    function stateRank(s) {{
      return s === 'done' ? 3 : s === 'running' ? 2 : s === 'error' ? 1 : 0;
    }}

    function stageEntryFor(status, seqStage) {{
      var all = status.stages || {{}};
      var entry = null;
      var keys = STAGE_KEY_GROUPS[seqStage] || [seqStage];
      for (var i = 0; i < keys.length; i++) {{
        var e = all[keys[i]];
        if (e && (!entry || stateRank(e.state) > stateRank(entry.state))) {{
          entry = e;
        }}
      }}
      if (!entry && status.stage && UMBRELLA_TO_SEQUENCE[status.stage] === seqStage) {{
        return {{state: 'running'}};
      }}
      return entry;
    }}

    function stageFraction(entry) {{
      var prog = entry && entry.progress;
      if (prog && typeof prog.total === 'number' && prog.total > 0 &&
          typeof prog.processed === 'number') {{
        var f = prog.processed / prog.total;
        return Math.max(0, Math.min(1, f));
      }}
      return 0.5;
    }}

    function computeProgress(status) {{
      var n = STAGE_SEQUENCE.length;
      var doneCount = 0, frac = 0;
      for (var i = 0; i < n; i++) {{
        var e = stageEntryFor(status, STAGE_SEQUENCE[i]);
        var st = e ? e.state : null;
        if (st === 'done') {{ doneCount = i + 1; frac = 0; }}
        else if (st === 'running') {{ doneCount = i; frac = stageFraction(e); break; }}
        else {{ doneCount = i; frac = 0; break; }}
      }}
      var width = Math.round(((doneCount + frac) / n) * 100);
      return Math.max(0, Math.min(100, width));
    }}

    function buildStageRows(status) {{
      var list = document.getElementById('stage-list');
      list.innerHTML = '';
      for (var i = 0; i < STAGE_SEQUENCE.length; i++) {{
        var seq = STAGE_SEQUENCE[i];
        var e = stageEntryFor(status, seq);
        var st = e ? e.state : 'not_started';
        var li = document.createElement('li');
        li.className = 'stage-row stage-' + st;
        var icon = document.createElement('span');
        icon.className = 'stage-icon';
        if (st === 'done') {{ icon.textContent = '✓'; }}
        else if (st === 'error') {{ icon.textContent = '✗'; }}
        else if (st === 'running') {{ icon.className += ' spinner'; }}
        else {{ icon.textContent = '○'; }}
        li.appendChild(icon);
        var label = document.createElement('span');
        label.className = 'stage-label';
        label.textContent = STAGE_LABELS_BN[seq] || seq;
        li.appendChild(label);
        if (st === 'running') {{
          var pct = document.createElement('span');
          pct.className = 'stage-pct';
          pct.textContent = Math.round(stageFraction(e) * 100) + '%';
          li.appendChild(pct);
        }}
        list.appendChild(li);
      }}
    }}

    function renderProgress(status) {{
      var width = computeProgress(status);
      document.getElementById('progress-fill').style.width = width + '%';
      var track = document.querySelector('.progress-track');
      if (track) {{ track.setAttribute('aria-valuenow', String(width)); }}
      document.getElementById('progress-pct').textContent = width + '%';
      buildStageRows(status);
    }}

    function showError(status) {{
      document.getElementById('job-processing').hidden = true;
      var el = document.getElementById('job-error');
      el.hidden = false;
      el.innerHTML = '';
      var stageInfo = (status.stages || {{}})[STAGE]
        || (status.stages || {{}})[status.stage];
      var detailBn = stageInfo && stageInfo.detail_bn;
      var detail = stageInfo && stageInfo.detail;
      var heading = document.createElement('p');
      heading.className = 'error-banner-title';
      heading.textContent = 'Something went wrong';
      el.appendChild(heading);
      var p = document.createElement('p');
      p.textContent = detailBn || detail || 'Unknown error.';
      el.appendChild(p);
      if (detail && detailBn && detail !== detailBn) {{
        var a = document.createElement('a');
        a.href = '#';
        a.className = 'error-detail-toggle';
        a.textContent = 'বিস্তারিত (English)';
        var pre = document.createElement('pre');
        pre.className = 'error-detail-en';
        pre.textContent = detail;
        pre.hidden = true;
        a.addEventListener('click', function (ev) {{
          ev.preventDefault();
          pre.hidden = !pre.hidden;
          a.textContent = pre.hidden ? 'বিস্তারিত (English)' : 'English detail লুকাও';
        }});
        el.appendChild(a);
        el.appendChild(pre);
      }}
      var retry = document.createElement('a');
      retry.className = 'error-banner-retry';
      retry.href = RESULT_URL;
      retry.textContent = 'আবার চেষ্টা করুন';
      el.appendChild(retry);
    }}

    function pollLogs() {{
      fetch(LOG_URL + '?since_line=' + nextLogLine)
        .then(function (r) {{ return r.json(); }})
        .then(function (data) {{
          var lines = data.lines || [];
          if (lines.length) {{
            document.getElementById('log-panel').hidden = false;
            var pre = document.getElementById('log-output');
            var atBottom = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 24;
            for (var i = 0; i < lines.length; i++) {{
              pre.textContent += String(lines[i]).replace(/\\n$/, '') + '\\n';
            }}
            if (atBottom) {{ pre.scrollTop = pre.scrollHeight; }}
          }}
          nextLogLine = typeof data.next_line === 'number' ? data.next_line : nextLogLine;
        }})
        .catch(function () {{ /* transient — retried on the next tick */ }});
    }}

    function poll() {{
      fetch('/api/jobs/' + encodeURIComponent(JOB_ID) + '/status')
        .then(function (r) {{ return r.json(); }})
        .then(function (status) {{
          if (status.state === 'done') {{
            window.location.href = RESULT_URL;
            return;
          }}
          if (status.state === 'error') {{
            showError(status);
            return;
          }}
          document.getElementById('progress-panel').hidden = false;
          renderProgress(status);
          setTimeout(poll, 2000);
        }})
        .catch(function () {{ setTimeout(poll, 2000); }});
    }}

    document.getElementById('log-toggle').addEventListener('click', function () {{
      var panel = document.getElementById('log-panel');
      var output = document.getElementById('log-output');
      var btn = document.getElementById('log-toggle');
      if (logToggled) {{
        panel.classList.remove('log-collapsed');
        btn.textContent = 'Hide';
        logToggled = false;
      }} else {{
        panel.classList.add('log-collapsed');
        btn.textContent = 'Show';
        logToggled = true;
      }}
      if (!output.hidden) {{
        output.scrollTop = output.scrollHeight;
      }}
    }});

    pollLogs();
    setInterval(pollLogs, 3000);
    poll();
  </script>
"""
    return HTMLResponse(ui.page(page_title + " — Manhwa Video Dubber", body))


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    default_engine = job_config.default_engine()
    engine_checked_primary = (
        'checked' if default_engine == "whisper_primary" else ''
    )
    engine_checked_gemini = (
        'checked' if default_engine == "gemini_only" else ''
    )
    target_lang_options = "".join(
        f'<option value="{lang}"'
        f'{" selected" if lang == job_config.DEFAULT_TARGET_LANG else ""}>'
        f'{job_config.TARGET_LANG_UI_LABELS[lang]}</option>'
        for lang in job_config.ALLOWED_TARGET_LANGS
    )
    body = f"""<section class="hero-panel">
    <h1>Manhwa Video Dubber</h1>
    <p>Upload a Chinese-subtitled manhwa explain video to start auto-dubbing.</p>
  </section>
  <form id="upload-form" enctype="multipart/form-data">
    <label for="file">Video (mp4/mkv/mov/avi/webm/flv/wmv/m4v)</label>
    <input type="file" id="file" name="file"
           accept=".mp4,.mkv,.mov,.avi,.webm,.flv,.wmv,.m4v" required>
    <fieldset>
      <legend>সাবটাইটেল উৎস</legend>
      <label for="subtitle-source">অরিজিনাল সাবটাইটেল কোথা থেকে আসবে?</label>
      <select id="subtitle-source" name="subtitle_source">
        <option value="gemini_extract" selected>
          Gemini ভিডিও থেকে নিজে বের করো (default)
        </option>
        <option value="user_transcript">
          আমি নিজের ট্রান্সক্রিপ্ট/সাবটাইটেল ফাইল দেব
        </option>
      </select>
    </fieldset>
    <fieldset>
      <legend>ডাবিং ভাষা</legend>
      <label for="target-lang">ভয়েসওভার কোন ভাষায় হবে?</label>
      <select id="target-lang" name="target_lang">
        {target_lang_options}
      </select>
    </fieldset>
    <label for="transcript">ট্রান্সক্রিপ্ট/সাবটাইটেল ফাইল (শুধু "আমি নিজের ট্রান্সক্রিপ্ট ফাইল দেব" বাছাই করলে প্রযোজ্য) — .srt, .vtt বা প্লেইন টেক্সট</label>
    <input type="file" id="transcript" name="transcript"
           accept=".srt,.vtt,.txt,text/plain">
    <fieldset>
      <legend>Voiceover source</legend>
      <label><input type="radio" name="voice_source" value="auto_tts" checked>
        সিস্টেম নিজেই ভয়েসওভার বানাক (Gemini TTS)</label>
      <label><input type="radio" name="voice_source" value="user_upload">
        আমি নিজের/অন্য AI দিয়ে বানানো অডিও দেব</label>
    </fieldset>
    <fieldset>
      <legend>Processing engine</legend>
      <label><input type="radio" name="engine" value="whisper_primary" {engine_checked_primary}>
        Whisper + Gemini (recommended — better timing)</label>
      <label><input type="radio" name="engine" value="gemini_only" {engine_checked_gemini}>
        Gemini only (skip local Whisper — lighter, no Whisper install needed)</label>
    </fieldset>
    <button type="submit" id="upload-submit">System Start</button>
  </form>
  <div id="upload-error" class="error-banner" hidden></div>
  <script>
    var form = document.getElementById('upload-form');
    var submitBtn = document.getElementById('upload-submit');
    var errorBox = document.getElementById('upload-error');
    function showError(message) {{
      submitBtn.disabled = false;
      submitBtn.textContent = 'System Start';
      var heading = document.createElement('p');
      heading.className = 'error-banner-title';
      heading.textContent = 'Upload failed';
      var msg = document.createElement('p');
      msg.textContent = message;
      errorBox.appendChild(heading);
      errorBox.appendChild(msg);
      errorBox.hidden = false;
    }}
    function startJob(jobId) {{
      window.location.href = '/upload/' + encodeURIComponent(jobId);
    }}
    form.addEventListener('submit', function (event) {{
      event.preventDefault();
      errorBox.hidden = true;
      errorBox.innerHTML = '';
      submitBtn.disabled = true;
      submitBtn.textContent = 'Uploading…';
      var data = new FormData(form);
      fetch('/upload', {{ method: 'POST', body: data }})
        .then(function (res) {{
          return res.json().then(function (body) {{
            if (!res.ok) throw {{ status: res.status, body: body }};
            return body;
          }});
        }})
        .then(function (body) {{
          startJob(body.job_id);
        }})
        .catch(function (err) {{
          var body = (err && err.body) || {{}};
          if (body && body.needs_confirm) {{
            var oldest = body.target_video_name || body.would_evict;
            var ok = window.confirm(
              'History-তে জায়গা নেই। সবচেয়ে পুরনো জব (' + oldest +
              ') মুছে ফেলতে হবে চালিয়ে যেতে হলে। ফাইলও ডিলিট করতে চান? ' +
              'OK = হ্যাঁ ফাইলসহ ডিলিট করো, Cancel = শুধু History লিস্ট থেকে ' +
              'সরাও, ফাইল থাকুক'
            );
            var deleteFiles = ok ? 'true' : 'false';
            return fetch(
              '/jobs/' + encodeURIComponent(body.job_id) + '/confirm-start' +
              '?evict_job_id=' + encodeURIComponent(body.would_evict) +
              '&delete_files=' + deleteFiles,
              {{ method: 'POST' }}
            ).then(function (r) {{
              return r.json().then(function (b) {{
                if (!r.ok) throw {{ status: r.status, body: b }};
                return b;
              }});
            }}).then(function (b) {{
              startJob(b.job_id);
            }}).catch(function (err2) {{
              var b2 = (err2 && err2.body) || {{}};
              showError(typeof b2.detail === 'string' ? b2.detail : 'Could not start the job.');
            }});
          }}
          var detail = typeof body.detail === 'string' ? body.detail : 'Upload failed.';
          showError(detail);
        }});
    }});
  </script>"""
    return HTMLResponse(ui.page("Manhwa Video Dubber", body, active="home"))


def _render_chain_final_result(job_id: str, stage: str) -> HTMLResponse:
    """Render the final-video page from a full-auto chain stage's result.

    Small adapter shared by the auto_tts (FA-C2) and user_audio (FA-D2)
    paths: each chain stage stores ``{"result": {"voiceover"/"alignment": ...,
    "final": <F3 result>}}``; this passes ``result.final`` into
    :func:`_render_final_result`.
    """
    chain_result = (
        job_status_store.read_status(job_id).get("stages", {}).get(stage, {}) or {}
    ).get("result") or {}
    final_result = (
        chain_result.get("final")
        if isinstance(chain_result, dict)
        else None
    )
    # Non-blocking warnings from the alignment (D3) and draft (E2) stages —
    # e.g. a serious alignment fallback or an extreme duration mismatch on the
    # user_upload path. They surface as a banner on the final page, never as a
    # block.
    warnings = []
    if isinstance(chain_result, dict):
        for key in ("alignment", "draft", "voiceover"):
            sub = chain_result.get(key)
            if not isinstance(sub, dict):
                continue
            for item in sub.get("warnings") or []:
                if item not in warnings:
                    warnings.append(item)
            duration_warning = sub.get("duration_warning")
            if duration_warning and duration_warning not in warnings:
                warnings.append(duration_warning)
    return _render_final_result(job_id, result=final_result, warnings=warnings)


@app.get("/upload/{job_id}", response_class=HTMLResponse)
def upload_status_page(job_id: str) -> HTMLResponse:
    """Status/result page for the upload_pipeline stage (fixes UI2: the home
    page's upload form used to POST directly to /upload and land the browser
    on that endpoint's raw JSON body — no styling, just Chrome's built-in
    JSON viewer. The form now submits via JS and redirects here instead, so
    the browser always shows a normal styled page: the polling page while
    B1/B2/C1 run in the background, then this result summary once done.
    """
    status = job_status_store.read_status(job_id)
    if status.get("stage") == "unknown":
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")

    stages = status.get("stages") or {}
    voice_source = voiceover_unify.get_voice_source(job_id)
    if voice_source == "auto_tts":
        # FA-C2: for the auto_tts path the upload thread continues (FA-C1)
        # straight into the full-auto chain, so this page stays on the
        # polling page until the auto_full_render stage is done and then shows
        # the final video player + download link directly — no extra click.
        auto_stage = stages.get("auto_full_render") or {}
        if auto_stage.get("state") == "done":
            return _render_chain_final_result(job_id, "auto_full_render")
        if not auto_stage and (stages.get("upload_pipeline") or {}).get("state") == "done":
            # FA-E1: the chain never started — the upload thread either was
            # still in B1/B2/C1 when this page loaded (FA-C1 starts it right
            # after) or stopped at upload_pipeline done (a manual override to
            # auto_tts via /voiceover/{job_id}/choose after upload). Resume
            # it so the page converges to the final video instead of polling a
            # stage that would otherwise never run.
            _start_stage(job_id, "auto_full_render", _run_auto_full_render)
        return _polling_page(
            job_id, "Uploading & Extracting", f"/upload/{job_id}",
            "auto_full_render",
        )

    # FA-D2: after the user uploads their own audio the job continues (same
    # page as the entry point): while user_audio_pipeline runs, stay on the
    # polling page; once done, show the final video directly.
    if stages.get("user_audio_pipeline"):
        if stages["user_audio_pipeline"].get("state") == "done":
            return _render_chain_final_result(job_id, "user_audio_pipeline")
        return _polling_page(
            job_id, "Processing your audio", f"/upload/{job_id}",
            "user_audio_pipeline",
        )

    # FA-D1: once the upload chain has completed (upload_pipeline done in the
    # stage history — the flat stage/state fields may have moved on to a later
    # stage, e.g. after a manual voice-source override), the user_upload path
    # drops straight into the audio-upload form — no extra "choose" click.
    if (stages.get("upload_pipeline") or {}).get("state") != "done":
        return _polling_page(
            job_id, "Uploading & Extracting", f"/upload/{job_id}", "upload_pipeline"
        )

    result = stages.get("upload_pipeline") or {}
    extraction_status = result.get("extraction_status", "ok")
    serials = result.get("serials")
    errors = result.get("errors") or []
    warning = (
        f'<div class="error-banner"><p class="error-banner-title">'
        f"Subtitle extraction had {len(errors)} issue(s)</p>"
        f"<p>Status: {extraction_status}. You can still continue — flagged "
        "lines get a translation fallback.</p></div>"
        if extraction_status != "ok"
        else ""
    )
    unresolved_card = _unresolved_card_html(job_id)
    # FA-D1: voice_source is already known (FA-A1), so the user_upload path
    # drops straight into the audio-upload form — no extra "choose" click.
    body = f"""
  <h1>Upload complete — job {job_id}</h1>
  <p>{serials if serials is not None else "?"} subtitle line(s) extracted and translated.</p>
  {warning}
  {unresolved_card}
  <h2>Upload your voiceover audio</h2>
  <p>Voice source set to <strong>user_upload</strong> for this job.</p>
  <p>Use these as reference while making your audio:
    <a href="/download/{job_id}/subtitles?format=srt">SRT</a> |
    <a href="/download/{job_id}/subtitles?format=txt">TXT</a>
  </p>
  <form method="post" action="/voiceover/{job_id}/upload" enctype="multipart/form-data">
    <label for="audio">Audio file (mp3/wav/m4a)</label>
    <input type="file" id="audio" name="audio" accept=".mp3,.wav,.m4a" required>
    <button type="submit">Upload</button>
  </form>"""
    return HTMLResponse(ui.page("Upload Complete — Manhwa Video Dubber", body))


@app.get("/settings", response_class=HTMLResponse)
def settings_page() -> HTMLResponse:
    keys = key_store.list_keys()
    rows = "\n".join(
        f"""
            <tr>
              <td>{k['id']}</td>
              <td>{k['label'] or ''}</td>
              <td><code>{k['key']}</code></td>
              <td><button onclick="delKey('{k['id']}')">Delete</button></td>
            </tr>
        """
        for k in keys
    )
    body = f"""<h1>Manhwa Video Dubber — Settings</h1>
  <h2>Add Gemini API key</h2>
  <form method="post" action="/settings/keys">
    <label for="key">Key</label>
    <input type="password" id="key" name="key" required>
    <label for="label">Label (optional)</label>
    <input type="text" id="label" name="label">
    <button type="submit">Add</button>
  </form>
  <h2>Add multiple keys at once</h2>
  <form method="post" action="/settings/keys/bulk">
    <label for="keys">Paste multiple keys here (one per line, or comma/space separated)</label>
    <textarea id="keys" name="keys" rows="8"
              placeholder="AIza...&#10;AIza...&#10;AIza..."></textarea>
    <button type="submit">Add all</button>
  </form>
  <h2>Stored keys</h2>
  <table class="keys-table">
    <thead>
      <tr><th>id</th><th>label</th><th>key</th><th></th></tr>
    </thead>
    <tbody>
{rows}
    </tbody>
  </table>
  <script>
    async function delKey(id) {{
      if (!confirm('Delete key ' + id + '?')) return;
      const res = await fetch('/settings/keys?key_id=' + encodeURIComponent(id), {{method: 'DELETE'}});
      if (res.ok) {{ location.reload(); }} else {{
        const data = await res.json();
        alert('Error: ' + (data.detail || res.status));
      }}
    }}
  </script>"""
    return HTMLResponse(
        ui.page("Manhwa Video Dubber — Settings", body, active="settings")
    )


@app.post("/settings/keys")
def add_key(key: str | None = Form(None), label: str | None = Form(None)) -> dict:
    try:
        entry = key_store.add_key(key, label)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"added": entry}


@app.post("/settings/keys/bulk")
def add_keys_bulk(keys: str | None = Form(None)) -> dict:
    try:
        entries = key_store.add_keys(keys)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"added": entries}


@app.delete("/settings/keys")
def delete_key(key_id: str) -> dict:
    try:
        entry = key_store.delete_key(key_id)
    except key_store.KeyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"deleted": entry}


@app.post("/upload")
async def upload_video(
    file: UploadFile = File(...),
    transcript: UploadFile | None = File(None),
    voice_source: str = Form("auto_tts"),
    engine: str | None = Form(None),
    target_lang: str | None = Form(None),
    subtitle_source: str | None = Form(None),
) -> dict:
    try:
        video_ingest.ensure_active_key(key_store.get_active_keys())
    except video_ingest.NoActiveKeyError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if voice_source not in voiceover_unify.ALLOWED_MODES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid voice source: {voice_source!r} "
                f"(allowed: {', '.join(voiceover_unify.ALLOWED_MODES)})"
            ),
        )
    if engine is not None and engine not in job_config.ALLOWED_ENGINES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid engine: {engine!r} "
                f"(allowed: {', '.join(job_config.ALLOWED_ENGINES)})"
            ),
        )
    if target_lang is not None and target_lang not in job_config.ALLOWED_TARGET_LANGS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid target lang: {target_lang!r} "
                f"(allowed: {', '.join(job_config.ALLOWED_TARGET_LANGS)})"
            ),
        )

    try:
        video_ingest.validate_file_type(file.filename)
    except video_ingest.UnsupportedFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # F12b (Part A): the subtitle source is an explicit user choice on the
    # upload form, no longer an implicit side-effect of whether a transcript
    # file happened to be attached. Default ``gemini_extract``.
    if subtitle_source is None:
        subtitle_source = job_config.DEFAULT_SUBTITLE_SOURCE
    if subtitle_source not in job_config.ALLOWED_SUBTITLE_SOURCES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid subtitle source: {subtitle_source!r} "
                f"(allowed: {', '.join(job_config.ALLOWED_SUBTITLE_SOURCES)})"
            ),
        )

    # F12a/F12b: when the user picked their own transcript, it is parsed and
    # validated HERE, before anything is persisted — a malformed/unparseable
    # transcript rejects the whole upload with no job dir, no video file and
    # no partial state saved anywhere. Picking "user_transcript" without
    # attaching a file is a hard rejection. Picking "gemini_extract" ignores
    # any attached transcript file and proceeds with normal Gemini extraction.
    transcript_bytes = None
    if subtitle_source == "user_transcript":
        if transcript is None or not transcript.filename:
            raise HTTPException(
                status_code=400,
                detail=(
                    "আপনি “আমি নিজের ট্রান্সক্রিপ্ট ফাইল দেব” বেছে নিয়েছেন, "
                    "কিন্তু কোনো ফাইল আপলোড করা হয়নি — .srt, .vtt বা প্লেইন "
                    "টেক্সট ফাইল যুক্ত করুন।"
                ),
            )
        try:
            transcript_bytes = await transcript.read()
        except OSError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"ট্রান্সক্রিপ্ট ফাইলটি পড়া যায়নি: {exc}",
            )
        content = transcript_bytes.decode("utf-8-sig", errors="replace")
        try:
            parsed, _kind = transcript_import.parse_transcript(
                content, transcript.filename
            )
        except transcript_import.TranscriptParseError as exc:
            raise HTTPException(
                status_code=400,
                detail=(
                    "ট্রান্সক্রিপ্ট ফাইলটি সঠিক ফরম্যাটে নেই "
                    f"({exc}). .srt, .vtt বা প্লেইন টেক্সট ফাইল আপলোড করুন।"
                ),
            )
        if not parsed:
            raise HTTPException(
                status_code=400,
                detail="ট্রান্সক্রিপ্ট ফাইলটি খালি — কোনো সাবটাইটেল পাওয়া যায়নি।",
            )
    else:
        transcript = None

    job_id = video_ingest.new_job_id()
    job_dir = video_ingest.UPLOAD_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    dest = job_dir / "source.mp4"
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                out.write(chunk)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to save upload: {exc}")

    try:
        job_meta = video_ingest.finalize_job(job_id, file.filename)
    except video_ingest.VideoProbeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # FA-A1: the voice-source choice is taken on the upload form (default
    # "auto_tts"); persist it immediately so the full-auto chain never needs a
    # separate /voiceover/{job_id}/choose click. set_voice_source validates the
    # mode, so a bad value would have already been rejected above.
    voiceover_unify.set_voice_source(job_id, voice_source)

    # F9: job_config.json is written once, at job creation, before any
    # Gemini/Whisper call runs (the pipeline runs on a background thread
    # below). Engine ("whisper_primary" | "gemini_only") and target_lang
    # (schema-only until F12) are recorded here; source_lang stays None until
    # auto-detection lands (F12). F12a records subtitle_source
    # ("gemini_extract" | "user_transcript").
    job_config.write_config(
        job_id,
        engine=engine,
        target_lang=target_lang,
        source_lang=None,
        voice_source=voice_source,
        subtitle_source=subtitle_source,
    )

    # F12a: the validated transcript is persisted for the background chain to
    # import (the pipeline thread only knows the job_id).
    if transcript_bytes is not None:
        suffix = Path(transcript.filename).suffix.lower()
        name = "transcript_upload" + (suffix if suffix in (".srt", ".vtt") else ".txt")
        (job_dir / name).write_bytes(transcript_bytes)

    # F9: history is capped at HISTORY_LIMIT (3) jobs and never evicts
    # silently. When the index is full the new job is NOT started; the client
    # gets a 409 and must confirm the eviction of the oldest job first
    # (two-step confirm flow, POST /jobs/{job_id}/confirm-start).
    registered = history_store.register_job(
        job_id, meta={"target_video_name": file.filename}
    )
    if not registered.get("added"):
        raise HTTPException(
            status_code=409,
            detail={
                "needs_confirm": True,
                "job_id": job_id,
                "would_evict": registered["would_evict"],
                "target_video_name": file.filename,
                "delete_files": True,
            },
        )

    # G1 wiring (U1b): the heavy B1 -> B2 -> C1 chain now runs on a daemon
    # background thread so the upload returns immediately with
    # {"status": "processing"}. Progress is persisted via job_status — poll
    # GET /api/jobs/{job_id}/status until "done" / "error".
    job_status_store.write_status(job_id, "upload_pipeline", "running")
    threading.Thread(
        target=_run_upload_pipeline, args=(job_id,), daemon=True
    ).start()

    return {"job_id": job_id, "meta": job_meta, "status": "processing"}


@app.post("/jobs/{job_id}/confirm-start")
def confirm_start(
    job_id: str,
    evict_job_id: str | None = Query(None),
    delete_files: bool = Query(True),
) -> dict:
    """Second step of the two-step confirm flow (F9).

    Called after the user accepts evicting the oldest job to make room for
    ``job_id`` (whose source.mp4 + job_config were already saved by /upload).
    Evicts ``evict_job_id`` (deleting its files when requested), registers the
    pending job in history and starts its pipeline.
    """
    if not (video_ingest.UPLOAD_ROOT / job_id).is_dir():
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")

    if evict_job_id:
        history_store.evict_job(evict_job_id, delete_files=delete_files)

    registered = history_store.register_job(job_id)
    if not registered.get("added"):
        raise HTTPException(
            status_code=409,
            detail="job history is still full after eviction — no job was started",
        )

    job_status_store.write_status(job_id, "upload_pipeline", "running")
    threading.Thread(
        target=_run_upload_pipeline, args=(job_id,), daemon=True
    ).start()
    return {"job_id": job_id, "status": "processing"}


@app.get("/api/jobs/{job_id}/logs")
def job_logs(job_id: str, since_line: int = Query(0)) -> dict:
    """Return new log lines for a job from ``since_line`` onward (F10).

    Feeds the polling page's docked log panel. Never raises: a missing or
    unreadable log file yields ``{"lines": [], "next_line": 0}``. ``since_line``
    is an index into the log file's lines; ``next_line`` is the index to pass
    back on the next poll. Negative or out-of-range indexes are clamped.
    """
    if since_line < 0:
        since_line = 0
    path = video_ingest.UPLOAD_ROOT / job_id / "logs" / "pipeline.log"
    try:
        with path.open("r", encoding="utf-8") as fh:
            all_lines = fh.readlines()
    except (OSError, ValueError):
        return {"lines": [], "next_line": 0}
    if since_line > len(all_lines):
        since_line = len(all_lines)
    lines = all_lines[since_line:]
    return {"lines": lines, "next_line": since_line + len(lines)}


@app.get("/history", response_class=HTMLResponse)
def history_page() -> HTMLResponse:
    """HTML history page (F10.3): one card per recent job with badges."""
    entries = history_store.list_history()
    cards = "\n".join(_history_card(e) for e in entries)
    empty = '<p class="meta">No jobs yet.</p>' if not entries else ""
    body = f"""<h1>Job history</h1>
  <p>Recent jobs (max {history_store.HISTORY_LIMIT}).</p>
  {empty}
  <div class="history-list">{cards}</div>
  <script>
    var forms = document.querySelectorAll('.resume-form');
    forms.forEach(function (form) {{
      form.addEventListener('submit', function (ev) {{
        ev.preventDefault();
        var jobId = form.getAttribute('data-job');
        fetch('/jobs/' + encodeURIComponent(jobId) + '/resume', {{ method: 'POST' }})
          .then(function (r) {{
            return r.json().then(function (b) {{
              if (!r.ok) throw {{ status: r.status, body: b }};
              return b;
            }});
          }})
          .then(function () {{
            window.location.href = '/resume/' + encodeURIComponent(jobId);
          }})
          .catch(function (err) {{
            var b = (err && err.body) || {{}};
            alert(typeof b.detail === 'string' ? b.detail : 'Could not resume the job.');
          }});
      }});
    }});
  </script>"""
    return HTMLResponse(ui.page("Job history — Manhwa Video Dubber", body, active="history"))


def _job_is_stale_running(job_id, status):
    """Whether a job is stuck "running" with no update for 10+ minutes (F10.3).

    The status file's mtime is a cheap proxy for "last progress write": a
    genuinely running job updates it as its stages transition. Jobs that
    silently died (process crash, stale thread) keep an old mtime and get the
    resume button on the history page.
    """
    if status.get("state") != "running":
        return False
    path = job_status_store.status_path(job_id)
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age > STALE_RUNNING_SECONDS


def _unresolved_card_html(job_id):
    """F12c Part B: non-blocking "ask the user" card for unresolved segments.

    Rendered on the upload result page (and the review page) whenever the
    automatic repair/translation retries were exhausted and flagged segments
    are still unresolved. Shows the Bengali warning with the exact regions
    plus two actions — "একবার আবার চেষ্টা করুন" (one more retry) and
    "মেনে নিন / বাদ দিন" (accept/skip). The card does NOT gate the rest of
    the job: it stays fully usable even if the user ignores it entirely.
    """
    status = job_status_store.read_status(job_id)
    extra = (status.get("stages") or {}).get("upload_pipeline") or {}
    items = extra.get("unresolved_segments") or []
    if not items:
        return ""
    active = [i for i in items if i.get("state") != "accepted"]
    if not active:
        return (
            '<div class="ok-banner">উল্লেখিত সেগমেন্টগুলো মেনে নেওয়া হয়েছে।</div>'
        )
    warning_bn = extra.get("unresolved_warning_bn") or (
        unresolved.build_warning_bn(active)
    )
    safe_warning = html.escape(warning_bn)
    retry_url = f"/jobs/{job_id}/unresolved/retry"
    accept_url = f"/jobs/{job_id}/unresolved/accept"
    return f"""
  <div class="unresolved-card" id="unresolved-card">
    <h3>সমস্যাযুক্ত সেগমেন্ট</h3>
    <pre>{safe_warning}</pre>
    <form method="post" action="{retry_url}">
      <button type="submit">একবার আবার চেষ্টা করুন</button>
    </form>
    <form method="post" action="{accept_url}">
      <button type="submit">মেনে নিন / বাদ দিন</button>
    </form>
  </div>"""


def _history_card(entry):
    """Render one history card (F10.3): meta + badge + দেখুন / রিজিউম করুন."""
    job_id = entry.get("job_id", "")
    state = entry.get("state") or "unknown"
    badge_class = {
        "done": "badge-done",
        "error": "badge-error",
        "running": "badge-running",
        "not_started": "badge-idle",
    }.get(state, "badge-idle")
    created = (entry.get("created_at") or "")[:19].replace("T", " ")
    name = html.escape(entry.get("target_video_name") or "—")
    target_lang = html.escape(entry.get("target_lang") or "—")
    voice_source = html.escape(entry.get("voice_source") or "—")
    status = {"state": state, "stage": entry.get("stage")}
    resume_form = ""
    if state == "error" or _job_is_stale_running(job_id, status):
        resume_form = (
            f'<form class="resume-form" method="post" data-job="{job_id}">'
            '<button type="submit">রিজিউম করুন</button></form>'
        )
    # F10.5: the primary card link is state-aware — a running job points at
    # the live progress page (the review page would only 404/redirect anyway),
    # a done job opens the review page, error/idle jobs have no view link.
    if state == "running":
        view_link = (
            f'<a class="history-view" href="/upload/{job_id}">চলমান — দেখুন</a>'
        )
    elif state == "done":
        view_link = f'<a class="history-view" href="/review/{job_id}">দেখুন</a>'
    else:
        view_link = ""
    return f"""
    <div class="history-card">
      <div class="history-card-top">
        <span class="history-id"><code>{html.escape(job_id)}</code></span>
        <span class="history-badge {badge_class}">{state}</span>
      </div>
      <p class="history-meta">{created} · {name}</p>
      <p class="history-meta">target_lang: {target_lang} ·
        voice_source: {voice_source}</p>
      <div class="history-actions">
        {view_link}
        {resume_form}
      </div>
    </div>"""


@app.get("/api/history")
def history_api() -> dict:
    """JSON history feed (machine-readable sibling of the HTML page)."""
    return {"history": history_store.list_history(), "limit": history_store.HISTORY_LIMIT}


@app.get("/resume/{job_id}", response_class=HTMLResponse)
def resume_polling_page(job_id: str) -> HTMLResponse:
    """Polling page shown after clicking "রিজিউম করুন" on the history page.

    Redirects to the final-video page once the resume chain completes.
    F12c (Part A): a job whose upload pipeline is mid-flight polls the upload
    pipeline instead and lands on the ``/upload`` result page.
    """
    status = job_status_store.read_status(job_id)
    if status.get("stage") == "unknown":
        raise HTTPException(status_code=404, detail=f"unknown job {job_id}")
    upload_point = resume.find_upload_resume_point(job_id)
    if upload_point not in (None, "upload_pipeline"):
        return _polling_page(
            job_id, "Resuming upload pipeline", f"/upload/{job_id}",
            "upload_pipeline",
        )
    return _polling_page(
        job_id, "Resuming job", f"/final/{job_id}", "resume"
    )


@app.post("/jobs/{job_id}/resume")
def resume_job_endpoint(job_id: str) -> dict:
    """Resume a job interrupted mid-chain (F9).

    The resume point is derived from which artifacts exist (see
    ``resume.find_resume_point``); the chain is re-run from that point on a
    background thread, with completed stages skipped (never re-run). Returns a
    409 when there is nothing to resume yet.

    F12c (Part A): a job interrupted inside the upload pipeline (before
    ``subtitles_hi.json``) is resumable too — ``find_upload_resume_point``
    picks the first missing upload sub-stage and the existing upload thread
    continues from there (skipping completed sub-stages). Only a job with no
    upload work done at all keeps returning 409.
    """
    try:
        point = resume.find_resume_point(job_id)
    except Exception as exc:  # noqa: BLE001 - guard; find_resume_point never raises normally
        raise HTTPException(status_code=500, detail=str(exc))
    if point is None:
        raise HTTPException(
            status_code=409,
            detail=f"job {job_id} is already complete — nothing to resume",
        )
    if point == "upload_pipeline":
        upload_point = resume.find_upload_resume_point(job_id)
        if upload_point is None:
            raise HTTPException(
                status_code=409,
                detail=f"job {job_id} is already complete — nothing to resume",
            )
        if upload_point == "upload_pipeline":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"job {job_id} has not finished subtitle extraction yet — "
                    "nothing to resume"
                ),
            )
        _start_stage(job_id, "upload_pipeline", _run_upload_pipeline)
        return {
            "job_id": job_id, "resume_point": upload_point, "status": "processing",
        }
    _start_stage(job_id, "resume", _run_resume)
    return {"job_id": job_id, "resume_point": point, "status": "processing"}


def _run_resume(job_id):
    """Run the resume chain on a background thread (F9).

    Persists ``resume`` status (running/done/error) so the polling page can
    surface the outcome. Never lets an exception escape the thread.
    """
    try:
        result = resume.resume_job(job_id)
        job_status_store.write_status(job_id, "resume", "done", extra={"result": result})
    except (
        FileNotFoundError,
        ValueError,
        RuntimeError,
        auto_cut.DraftValidationError,
        voiceover_unify.VoiceoverAlignmentError,
    ) as exc:
        logger.error("resume failed for job %s: %s", job_id, exc)
        _write_error_status(job_id, "resume", exc)
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception("unexpected resume failure for job %s", job_id)
        _write_error_status(job_id, "resume", exc)


@app.post("/jobs/{job_id}/unresolved/retry")
def unresolved_retry(job_id: str) -> dict:
    """F12c Part B: one more *user-initiated* retry for unresolved segments.

    Explicitly requested by the user (never automatic) — each unresolved repair
    region/line gets a fresh attempt on top of the exhausted automatic retries
    (``config.SUBTITLE_MAX_REPAIR_ATTEMPTS`` stays unchanged). Runs synchronously
    like the review edit endpoints; ``translate_subtitles`` is re-run so
    ``subtitles_hi.json`` stays in sync. The status/warning channel
    (``upload_pipeline`` extra) is refreshed from the updated registry.
    """
    try:
        items, _warning = unresolved.apply_retry(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    _refresh_upload_extra(job_id)
    return {
        "job_id": job_id,
        "unresolved": items,
        "warning_bn": unresolved.build_warning_bn(items),
    }


@app.post("/jobs/{job_id}/unresolved/accept")
def unresolved_accept(job_id: str) -> dict:
    """F12c Part B: mark the unresolved segments as acceptable/skip.

    The flagged segments keep their last imperfect on-disk state but are no
    longer reported as actionable; the ``upload_pipeline`` warning is cleared.
    """
    try:
        items, _warning = unresolved.apply_accept(job_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    _refresh_upload_extra(job_id)
    return {"job_id": job_id, "unresolved": items, "accepted": True}


@app.get("/api/jobs/{job_id}/status")
def job_status(job_id: str) -> dict:
    return job_status_store.read_status(job_id)


@app.get("/download/{job_id}/subtitles")
def download_subtitles(job_id: str, format: str = Query("srt")) -> FileResponse:
    fmt = format.lower()
    lang = lang_files.target_lang(job_id)
    files = {
        "srt": (lang_files.subtitles_srt(lang), "text/plain"),
        "txt": (lang_files.subtitles_plain(lang), "text/plain"),
        "json": (lang_files.subtitles_json(lang), "application/json"),
    }
    if fmt not in files:
        raise HTTPException(status_code=400, detail=f"unsupported format: {format}")
    name, media_type = files[fmt]
    path = video_ingest.UPLOAD_ROOT / job_id / name
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no {name} for job {job_id} (translation not done yet?)",
        )
    return FileResponse(path, media_type=media_type, filename=name)


@app.get("/download/{job_id}/voiceover")
def download_voiceover(job_id: str, format: str = Query("wav")) -> FileResponse:
    fmt = format.lower()
    lang = lang_files.target_lang(job_id)
    files = {
        "wav": (lang_files.voiceover_audio(lang), "audio/wav"),
        "timestamps": (lang_files.timestamps_auto(lang), "application/json"),
    }
    if fmt not in files:
        raise HTTPException(status_code=400, detail=f"unsupported format: {format}")
    name, media_type = files[fmt]
    path = video_ingest.UPLOAD_ROOT / job_id / name
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no {name} for job {job_id} (voiceover not generated yet?)",
        )
    return FileResponse(path, media_type=media_type, filename=name)


@app.get("/download/{job_id}/voiceover_upload")
def download_voiceover_upload(job_id: str, format: str = Query("timestamps")) -> FileResponse:
    fmt = format.lower()
    lang = lang_files.target_lang(job_id)
    files = {
        "timestamps": (lang_files.timestamps_upload(lang), "application/json"),
        "wav": (lang_files.voiceover_audio(lang), "audio/wav"),
    }
    if fmt not in files:
        raise HTTPException(status_code=400, detail=f"unsupported format: {format}")
    name, media_type = files[fmt]
    path = video_ingest.UPLOAD_ROOT / job_id / name
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no {name} for job {job_id} (voiceover not uploaded/aligned yet?)",
        )
    return FileResponse(path, media_type=media_type, filename=name)


@app.get("/download/{job_id}/subtitle_qa")
def download_subtitle_qa(job_id: str) -> FileResponse:
    path = video_ingest.UPLOAD_ROOT / job_id / "subtitle_qa.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no subtitle_qa.json for job {job_id}",
        )
    return FileResponse(
        path, media_type="application/json", filename="subtitle_qa.json"
    )


@app.post("/voiceover/{job_id}/upload", response_class=HTMLResponse)
async def upload_voiceover(job_id: str, audio: UploadFile = File(...)) -> HTMLResponse:
    audio_bytes = await audio.read()
    try:
        voiceover_upload.save_uploaded_voiceover(
            job_id, audio_bytes, audio.filename
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except (voiceover_upload.UnsupportedAudioError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # FA-D2: the job auto-continues (D3 -> D4 -> E1 -> E2 -> F3) on a
    # background thread, so the user just watches /upload/{job_id} land on the
    # final video — no extra click. The /voiceover/{job_id}/align_uploaded
    # route is kept for manual re-alignment (backward-compat).
    _start_stage(job_id, "user_audio_pipeline", _run_user_audio_pipeline)
    return _polling_page(
        job_id, "Processing your audio", f"/upload/{job_id}", "user_audio_pipeline"
    )


@app.get("/voiceover/{job_id}/align_uploaded", response_class=HTMLResponse)
def align_uploaded_page(job_id: str) -> HTMLResponse:
    try:
        result = voiceover_upload.align_uploaded_voiceover(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    status = result["status"]
    fallback_serials = result["fallback_serials"] or []
    lang = lang_files.target_lang(job_id)
    ts_name = lang_files.timestamps_upload(lang)
    wav_name = lang_files.voiceover_audio(lang)
    warning = ""
    if status != "ok":
        warning = (
            f"<p><strong>Alignment fallback used</strong> "
            f"(source: {result['alignment_source']}). "
            f"{len(fallback_serials)} line(s) flagged: {fallback_serials}</p>"
        )
    body = f"""
  <h1>Uploaded voiceover alignment — job {job_id}</h1>
  <p>Status: <strong>{status}</strong></p>
  <p>{result.get("entries_count")} line(s) aligned.</p>
  <p>Audio duration: {result.get("total_sec")} sec.</p>
  {warning}
  <p>Timestamps:
    <a href="/download/{job_id}/voiceover_upload?format=timestamps">{ts_name}</a></p>
  <p>Audio:
    <a href="/download/{job_id}/voiceover_upload?format=wav">{wav_name}</a></p>
  <p><a href="/voiceover/{job_id}/choose">Change voice source</a></p>
"""
    return HTMLResponse(ui.page(f"Alignment — Manhwa Video Dubber", body))


@app.get("/voiceover/{job_id}/choose", response_class=HTMLResponse)
def voiceover_choose_page(job_id: str) -> HTMLResponse:
    current = voiceover_unify.get_voice_source(job_id)
    current_html = (
        f"<p>Current choice: <strong>{current}</strong></p>" if current else ""
    )
    qa_banner = ""
    try:
        qa = subtitle_qa.build_qa_summary(job_id)
        if qa.get("qa_status") == "flagged":
            items = "".join(
                f"<li>{html.escape(w)}</li>" for w in qa.get("warnings", [])
            )
            qa_banner = (
                '<div class="flagged-banner">'
                "<p><strong>এই ভিডিওর সাবটাইটেল এক্সট্রাকশনে কিছু সমস্যা "
                "পাওয়া গেছে</strong></p>"
                f"<ul>{items}</ul>"
                f'<p><a href="/download/{job_id}/subtitle_qa">subtitle_qa.json '
                "ডাউনলোড করুন</a></p>"
                "<p>এটা শুধু তথ্যের জন্য — তবুও এগিয়ে যেতে পারেন, কিন্তু "
                "ভয়েসওভার রেকর্ড করার আগে চাইলে সাবটাইটেল দেখে নিতে পারেন।</p>"
                "</div>"
            )
    except Exception as exc:  # noqa: BLE001 - banner is non-blocking
        logger.warning(
            "QA summary banner failed for job %s (non-fatal): %s", job_id, exc
        )
    body = f"""
  <h1>Voiceover source — job {job_id}</h1>
  {current_html}
  {qa_banner}
  <p>Choose how the Hindi voiceover will be created:</p>
  <form method="post" action="/voiceover/{job_id}/choose">
    <button type="submit" name="mode" value="auto_tts">
      সিস্টেম নিজেই ভয়েসওভার বানাক (Gemini TTS)
    </button>
  </form>
  <form method="post" action="/voiceover/{job_id}/choose">
    <button type="submit" name="mode" value="user_upload">
      আমি নিজে/অন্য AI দিয়ে বানানো অডিও ফাইল আপলোড করব
    </button>
  </form>"""
    return HTMLResponse(ui.page("Voiceover Source — Manhwa Video Dubber", body))


@app.post("/voiceover/{job_id}/choose", response_class=HTMLResponse)
def voiceover_choose(job_id: str, mode: str = Form(...)) -> HTMLResponse:
    try:
        voiceover_unify.set_voice_source(job_id, mode)
    except voiceover_unify.InvalidVoiceSourceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if mode == "auto_tts":
        # G1 wiring: choosing auto-TTS runs the backend chain down to the
        # draft (D2 -> D4 -> E1 -> E2) so the review phase has data.
        try:
            _process_auto_tts(job_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc))
        except (RuntimeError, auto_cut.DraftValidationError) as exc:
            raise HTTPException(status_code=500, detail=str(exc))
        body = f"""<h1>Auto TTS selected</h1>
  <p>Voice source set to <strong>auto_tts</strong> for job {job_id}.</p>
  <p><a href="/voiceover/{job_id}/auto_tts">Continue to automatic voiceover (D2)</a></p>
  <p><a href="/voiceover/{job_id}/choose">Change choice</a></p>"""
        return HTMLResponse(ui.page("Auto TTS — Manhwa Video Dubber", body))
    else:
        body = f"""<h1>Upload your voiceover audio</h1>
  <p>Voice source set to <strong>user_upload</strong> for job {job_id}.</p>
  <p>Use these as reference while making your audio:
    <a href="/download/{job_id}/subtitles?format=srt">SRT</a> |
    <a href="/download/{job_id}/subtitles?format=txt">TXT</a>
  </p>
  <form method="post" action="/voiceover/{job_id}/upload" enctype="multipart/form-data">
    <label for="audio">Audio file (mp3/wav/m4a)</label>
    <input type="file" id="audio" name="audio" accept=".mp3,.wav,.m4a" required>
    <button type="submit">Upload</button>
  </form>
  <p><a href="/voiceover/{job_id}/choose">Change choice</a></p>"""
        return HTMLResponse(ui.page("Upload Voiceover — Manhwa Video Dubber", body))


@app.get("/voiceover/{job_id}/auto_tts", response_class=HTMLResponse)
def voiceover_auto_page(job_id: str) -> HTMLResponse:
    # U1c: if the auto-TTS stage already finished, render its result page
    # synchronously (identical markup). Otherwise run D2 -> E2 on a background
    # thread and show an intermediate polling page that redirects here.
    status = job_status_store.read_status(job_id)
    if not (
        status.get("stage") == "voiceover_auto" and status.get("state") == "done"
    ):
        job_dir = video_ingest.UPLOAD_ROOT / job_id
        sub_name = lang_files.subtitles_json(lang_files.target_lang(job_id))
        if not (job_dir / sub_name).exists():
            raise HTTPException(
                status_code=404,
                detail=f"no {sub_name} for job {job_id}",
            )
        _start_stage(
            job_id, "voiceover_auto", _run_voiceover_auto
        )
        return _polling_page(
            job_id,
            "Automatic Voiceover",
            f"/voiceover/{job_id}/auto_tts",
            "voiceover_auto",
        )
    return _render_auto_tts_result(job_id)


def _render_auto_tts_result(job_id: str) -> HTMLResponse:
    stage = job_status_store.read_status(job_id).get("stages", {}).get(
        "voiceover_auto"
    )
    result = (stage or {}).get("result")
    if not isinstance(result, dict):
        # Stage reports done but its payload is missing (e.g. status file was
        # re-seeded). Re-run on the spot so the page still renders.
        result = voiceover_auto.generate_auto_voiceover(job_id)
        _continue_from_voiceover(job_id)

    status = result["status"]
    failed = result["failed_serials"] or []
    if status == "tts_unavailable":
        body = (
            f"<h1>Auto voiceover — job {job_id}</h1>"
            "<p>No active Gemini key — add one in Settings first.</p>"
        )
    else:
        lang = lang_files.target_lang(job_id)
        links = []
        if result.get("voiceover_path"):
            links.append(
                f'<p>Audio: <a href="/download/{job_id}/voiceover?format=wav">'
                f'{lang_files.voiceover_audio(lang)}</a> '
                f'({result.get("total_sec")} sec)</p>'
            )
        links.append(
            f'<p>Timestamps: '
            f'<a href="/download/{job_id}/voiceover?format=timestamps">'
            f'{lang_files.timestamps_auto(lang)}</a></p>'
        )
        warning = (
            f"<p>TTS failed for {len(failed)} line(s): {failed}. "
            "Silence used for those.</p>" if failed else ""
        )
        body = f"""
  <h1>Auto voiceover — job {job_id}</h1>
  <p>Status: <strong>{status}</strong></p>
  <p>{result.get("entries_count")} line(s) rendered.</p>
  {warning}
  {"".join(links)}
  <p><a href="/voiceover/{job_id}/choose">Change voice source</a></p>
"""
    return HTMLResponse(ui.page("Auto Voiceover — Manhwa Video Dubber", body))


@app.get("/review/{job_id}", response_class=HTMLResponse)
def review_page(job_id: str) -> HTMLResponse:
    """Per-clip review page (F1) with F10.5 state handling.

    A job still in ``running`` state has nothing to review yet — instead of
    raising a raw-JSON 404 the browser lands on the live progress page
    (``/upload/{job_id}``). Only once the job is done/error does the page get
    built; a missing artifact (e.g. ``edit_guideline.json``) yields a friendly
    HTML 404 in Bengali (F11 mirror), never a JSON body.
    """
    status = job_status_store.read_status(job_id)
    if status.get("state") == "running":
        return RedirectResponse(url=f"/upload/{job_id}", status_code=302)
    try:
        page = review.build_review_page(job_id)
    except FileNotFoundError as exc:
        detail_bn = error_bn.explain_bn(exc, "review")
        body = (
            "<h1>রিভিউ পাওয়া যায়নি</h1>"
            f"<p>Job <code>{html.escape(job_id)}</code> এর রিভিউ পেজ লোড করা "
            "যায়নি — ক্লিপ ডেটা (edit_guideline.json) পাওয়া যায় না।</p>"
            '<div class="error-banner">'
            '<p class="error-banner-title">কী হয়েছে</p>'
            f"<p>{html.escape(detail_bn)}</p>"
            "</div>"
            '<p><a href="/history">ইতিহাসে ফিরে যান</a></p>'
        )
        return HTMLResponse(
            ui.page("রিভিউ পাওয়া যায়নি — Manhwa Video Dubber", body),
            status_code=404,
        )
    unresolved_card = _unresolved_card_html(job_id)
    if unresolved_card:
        page = page.replace(
            "</h1>", "</h1>" + unresolved_card, 1
        ) if "</h1>" in page else page
    return HTMLResponse(page)


@app.get("/review/{job_id}/clip/{serial}", response_class=FileResponse)
def review_clip(job_id: str, serial: int) -> FileResponse:
    try:
        path = review.extract_clip(job_id, serial)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(path, media_type="video/mp4", filename=Path(path).name)


@app.post("/review/{job_id}/edit", response_class=HTMLResponse)
def review_edit(
    job_id: str,
    serial: int = Form(...),
    new_source_start: str | None = Form(None),
    new_source_end: str | None = Form(None),
) -> HTMLResponse:
    try:
        result = review.apply_clip_edit(
            job_id, serial, new_source_start, new_source_end
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except review.DraftValidationError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    body = f"""<h1>Edit applied — job {job_id}</h1>
  <p>Serial {result["serial"]}: source range now
    [{result["source_start_sec"]}, {result["source_end_sec"]}], pts
    x{result["pts_multiplier"]}, flagged {result["flagged"]}
    ({result["flag_reason"] or "none"}).</p>
  <p><a href="/review/{job_id}">Back to review</a></p>"""
    return HTMLResponse(ui.page("Edit applied — Manhwa Video Dubber", body))


@app.get("/final/{job_id}", response_class=HTMLResponse)
def final_page(job_id: str) -> HTMLResponse:
    # U1c: if the final-render stage already finished, render its result page
    # synchronously (identical markup). Otherwise run the render on a
    # background thread and show an intermediate polling page that redirects
    # here.
    status = job_status_store.read_status(job_id)
    if not (
        status.get("stage") == "final_render" and status.get("state") == "done"
    ):
        job_dir = video_ingest.UPLOAD_ROOT / job_id
        if not (job_dir / "draft_final_video.mp4").exists():
            raise HTTPException(
                status_code=404,
                detail=f"no draft_final_video.mp4 for job {job_id}",
            )
        _start_stage(job_id, "final_render", _run_final_render)
        return _polling_page(
            job_id,
            "Final Video",
            f"/final/{job_id}",
            "final_render",
        )
    return _render_final_result(job_id)


def _render_final_result(job_id: str, result=None, warnings=None) -> HTMLResponse:
    if not isinstance(result, dict):
        stage = job_status_store.read_status(job_id).get("stages", {}).get(
            "final_render"
        )
        result = (stage or {}).get("result")
    if not isinstance(result, dict):
        result = render_final.finalize_video(job_id)

    warning_html = ""
    warning_items = [w for w in (warnings or []) if w]
    if warning_items:
        items = "".join(f"<li>{html.escape(w)}</li>" for w in warning_items)
        warning_html = (
            '<div class="error-banner">'
            '<p class="error-banner-title">Note</p>'
            f"<ul>{items}</ul>"
            "</div>"
        )

    duration = (
        f"{result['duration_sec']:.3f} sec." if result["duration_sec"] is not None
        else "duration unknown"
    )
    body = f"""<h1>Final video — job {job_id}</h1>
  <p>Status: <strong>{result["status"]}</strong>. Duration: {duration}</p>
  {warning_html}
  <video controls src="/download/{job_id}"></video>
  <p><a href="/download/{job_id}">Download final video</a></p>
  <p><a href="/review/{job_id}">Back to Review</a></p>"""
    return HTMLResponse(ui.page(f"Final Video — job {job_id} — Manhwa Video Dubber", body))


@app.get("/download/{job_id}", response_class=FileResponse)
def download_final(job_id: str) -> FileResponse:
    path = render_final.final_video_path(job_id)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"no final_video.mp4 for job {job_id} (final render not done yet?)",
        )
    return FileResponse(path, media_type="video/mp4", filename="final_video.mp4")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=5000)
