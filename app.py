"""Manhwa Video Dubber - FastAPI web app.

S1 scaffold + A1 Gemini key settings + A2 video upload.
Run locally with:
    uvicorn app:app --host 0.0.0.0 --port 5000
"""

import html
import json
import logging
import threading
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from pipeline import (
    auto_cut,
    config,
    edit_guideline,
    full_auto_chain,
    gemini_rotation,
    job_logging,
    job_status as job_status_store,
    key_store,
    render_final,
    review,
    subtitle_builder,
    subtitle_extract,
    subtitle_qa,
    subtitle_verify,
    translator,
    ui,
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
    to unify, so the job stays where it is. Raises FileNotFoundError /
    RuntimeError / DraftValidationError when a stage fails.
    """
    if voiceover_unify.get_voice_source(job_id) not in voiceover_unify.ALLOWED_MODES:
        return
    voiceover_unify.unify_voiceover_timestamps(job_id)
    edit_guideline.build_edit_guideline(job_id)
    auto_cut.build_draft_video(job_id)


def _process_auto_tts(job_id):
    """Run the auto-TTS backend chain D2 -> D4 -> E1 -> E2 (G1 wiring).

    Returns ``"pending"`` when the job has no ``subtitles_hi.json`` yet (the
    user must trigger the D2 page first); otherwise runs the whole chain down
    to the draft video so the review phase has data. Raises the underlying
    errors so the caller can surface them.
    """
    job_dir = video_ingest.UPLOAD_ROOT / job_id
    if not (job_dir / "subtitles_hi.json").exists():
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
            (job_dir / "subtitles_hi.json").read_text(encoding="utf-8")
        )
        if isinstance(hi, list):
            extra["serials"] = len(hi)
    except (OSError, ValueError):
        pass
    return extra


def _run_upload_pipeline(job_id):
    """Run the upload chain B1 -> B2 -> C1 on a background thread (G1 wiring).

    Persists status transitions via ``job_status`` so a client can poll
    ``GET /api/jobs/{job_id}/status`` until ``done``/``error``. Never lets an
    exception escape the thread — an uncaught exception in a bare thread
    silently kills it without a log, so the whole body is wrapped in
    try/except and failures are recorded as ``error`` status.

    Idempotent resume: if ``subtitles_hi.json`` already exists the chain is
    not re-run; ``done`` is recorded from the existing files instead. This is
    the basis for the future Retry button (U1c).
    """
    try:
        job_dir = video_ingest.UPLOAD_ROOT / job_id
        if (job_dir / "subtitles_hi.json").exists():
            extra = _resume_pipeline_extra(job_id)
        else:
            # U2b: the whole upload chain (B1 + C1) shares one per-job
            # CallBudget so a runaway Gemini rotation can never burn more than
            # config.MAX_API_CALLS_PER_JOB calls for a single job run.
            budget = gemini_rotation.CallBudget(config.MAX_API_CALLS_PER_JOB)
            extraction = subtitle_extract.extract_subtitles(
                job_id, call_budget=budget
            )
            subtitle_builder.build_subtitle_list(job_id, call_budget=budget)
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
            translation = translator.translate_subtitles(
                job_id, call_budget=budget
            )
            extra = {
                "extraction_status": extraction["status"],
                "serials": len(translation),
                "whisper_check_status": whisper_check.get("status", "skipped"),
            }
            if extraction["status"] != "ok":
                extra["errors"] = _extraction_error_summary(extraction)
        job_status_store.write_status(
            job_id, "upload_pipeline", "done", extra=extra
        )

        # FA-C1: for the auto_tts path the upload chain now continues, on the
        # SAME thread, straight through the full-auto chain down to the final
        # video (D2 -> D4 -> E1 -> E2 -> F3), so the user gets a zero-click
        # result. The user_upload path (or a job with no choice yet) keeps the
        # old behavior and stops here — group D handles that path.
        voice_source = voiceover_unify.get_voice_source(job_id)
        if voice_source == "auto_tts":
            _run_auto_full_render(job_id)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        logger.error("post-upload pipeline failed for job %s: %s", job_id, exc)
        job_status_store.write_status(
            job_id, "upload_pipeline", "error", extra={"detail": _friendly_error(exc)}
        )


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
        result = voiceover_auto.generate_auto_voiceover(
            job_id, call_budget=budget
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
    ) as exc:
        logger.error("auto voiceover failed for job %s: %s", job_id, exc)
        job_status_store.write_status(
            job_id, "voiceover_auto", "error", extra={"detail": _friendly_error(exc)}
        )
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception("unexpected auto-voiceover failure for job %s", job_id)
        job_status_store.write_status(
            job_id, "voiceover_auto", "error", extra={"detail": _friendly_error(exc)}
        )


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
        job_status_store.write_status(
            job_id, "final_render", "error", extra={"detail": _friendly_error(exc)}
        )
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception("unexpected final-render failure for job %s", job_id)
        job_status_store.write_status(
            job_id, "final_render", "error", extra={"detail": _friendly_error(exc)}
        )


def _run_user_audio_pipeline(job_id):
    """Run D3 -> D4 -> E1 -> E2 -> F3 on a background thread (FA-D2).

    After the user uploads their own audio, this continues on its own daemon
    thread so the job reaches the final video with no further clicks. Never
    lets an exception escape the thread; failures are persisted as ``error``
    status so the polling page can surface them.
    """
    try:
        result = full_auto_chain.run_user_upload_chain(job_id)
        job_status_store.write_status(
            job_id, "user_audio_pipeline", "done", extra={"result": result}
        )
    except (
        FileNotFoundError,
        ValueError,
        RuntimeError,
        auto_cut.DraftValidationError,
    ) as exc:
        logger.error("user audio pipeline failed for job %s: %s", job_id, exc)
        job_status_store.write_status(
            job_id,
            "user_audio_pipeline",
            "error",
            extra={"detail": _friendly_error(exc)},
        )
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception("unexpected user-audio-pipeline failure for job %s", job_id)
        job_status_store.write_status(
            job_id,
            "user_audio_pipeline",
            "error",
            extra={"detail": _friendly_error(exc)},
        )


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
    ) as exc:
        logger.error("auto full render failed for job %s: %s", job_id, exc)
        job_status_store.write_status(
            job_id, "auto_full_render", "error",
            extra={"detail": _friendly_error(exc)},
        )
    except Exception as exc:  # noqa: BLE001 — daemon thread must never die
        logger.exception(
            "unexpected auto full render failure for job %s", job_id
        )
        job_status_store.write_status(
            job_id, "auto_full_render", "error",
            extra={"detail": _friendly_error(exc)},
        )


def _polling_page(job_id, page_title, result_url, stage):
    """Intermediate HTML shown while a background stage runs (U1c).

    No external framework/CDN: a small inline <script> polls
    ``/api/jobs/{job_id}/status`` every 2 seconds. On ``done`` it redirects to
    ``result_url`` (the same endpoint, which then renders its normal result
    page); on ``error`` it shows the error detail plus a "আবার চেষ্টা করুন"
    link back to the same endpoint — safe because every stage is idempotent
    and resumable.
    """
    body = f"""
  <h1>{page_title}</h1>
  <div id="job-processing" class="processing-banner">
    <span class="spinner" aria-hidden="true"></span>
    <span>Processing… this page updates automatically, no need to refresh.</span>
  </div>
  <div id="job-error" class="error-banner" hidden></div>
  <script>
    var JOB_ID = {json.dumps(job_id)};
    var RESULT_URL = {json.dumps(result_url)};
    var STAGE = {json.dumps(stage)};
    function poll() {{
      fetch('/api/jobs/' + encodeURIComponent(JOB_ID) + '/status')
        .then(function (r) {{ return r.json(); }})
        .then(function (status) {{
          if (status.state === 'done') {{
            window.location.href = RESULT_URL;
            return;
          }}
          if (status.state === 'error') {{
            document.getElementById('job-processing').hidden = true;
            var el = document.getElementById('job-error');
            el.hidden = false;
            var stageInfo = (status.stages || {{}})[STAGE]
              || (status.stages || {{}})[status.stage];
            var detail = stageInfo && stageInfo.detail
              ? stageInfo.detail : 'Unknown error.';
            var heading = document.createElement('p');
            heading.className = 'error-banner-title';
            heading.textContent = 'Something went wrong';
            var p = document.createElement('p');
            p.textContent = detail;
            var a = document.createElement('a');
            a.className = 'error-banner-retry';
            a.href = RESULT_URL;
            a.textContent = 'আবার চেষ্টা করুন';
            el.appendChild(heading);
            el.appendChild(p);
            el.appendChild(a);
            return;
          }}
          setTimeout(poll, 2000);
        }});
    }}
    poll();
  </script>
  <p><a href="/">Back to home</a></p>"""
    return HTMLResponse(ui.page(page_title + " — Manhwa Video Dubber", body))


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    body = """<h1>Manhwa Video Dubber</h1>
  <p>Upload a Chinese-subtitled manhwa explain video to start auto-dubbing.</p>
  <form id="upload-form" enctype="multipart/form-data">
    <label for="file">Video (mp4/mkv/mov/avi/webm/flv/wmv/m4v)</label>
    <input type="file" id="file" name="file"
           accept=".mp4,.mkv,.mov,.avi,.webm,.flv,.wmv,.m4v" required>
    <fieldset>
      <legend>Voiceover source</legend>
      <label><input type="radio" name="voice_source" value="auto_tts" checked>
        সিস্টেম নিজেই ভয়েসওভার বানাক (Gemini TTS)</label>
      <label><input type="radio" name="voice_source" value="user_upload">
        আমি নিজের/অন্য AI দিয়ে বানানো অডিও দেব</label>
    </fieldset>
    <button type="submit" id="upload-submit">System Start</button>
  </form>
  <div id="upload-error" class="error-banner" hidden></div>
  <p><a href="/settings">Gemini API key settings</a></p>
  <script>
    var form = document.getElementById('upload-form');
    var submitBtn = document.getElementById('upload-submit');
    var errorBox = document.getElementById('upload-error');
    form.addEventListener('submit', function (event) {
      event.preventDefault();
      errorBox.hidden = true;
      errorBox.innerHTML = '';
      submitBtn.disabled = true;
      submitBtn.textContent = 'Uploading…';
      var data = new FormData(form);
      fetch('/upload', { method: 'POST', body: data })
        .then(function (res) {
          return res.json().then(function (body) {
            if (!res.ok) {
              throw new Error(body.detail || 'Upload failed.');
            }
            return body;
          });
        })
        .then(function (body) {
          window.location.href = '/upload/' + encodeURIComponent(body.job_id);
        })
        .catch(function (err) {
          submitBtn.disabled = false;
          submitBtn.textContent = 'System Start';
          var heading = document.createElement('p');
          heading.className = 'error-banner-title';
          heading.textContent = 'Upload failed';
          var msg = document.createElement('p');
          msg.textContent = err.message;
          errorBox.appendChild(heading);
          errorBox.appendChild(msg);
          errorBox.hidden = false;
        });
    });
  </script>"""
    return HTMLResponse(ui.page("Manhwa Video Dubber", body))


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
    return _render_final_result(job_id, result=final_result)


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
    # FA-D1: voice_source is already known (FA-A1), so the user_upload path
    # drops straight into the audio-upload form — no extra "choose" click.
    body = f"""
  <h1>Upload complete — job {job_id}</h1>
  <p>{serials if serials is not None else "?"} subtitle line(s) extracted and translated.</p>
  {warning}
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
  </form>
  <p><a href="/">Back to home</a></p>"""
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
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Manhwa Video Dubber — Settings</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="{ui.FONTS_HREF}" rel="stylesheet">
  <link rel="stylesheet" href="/static/style.css">
</head>
<body>
<div class="grain"></div>
  <header class="site-header">
    <a class="brand" href="/">Manhwa Video Dubber</a>
    <span class="page-title">Settings</span>
  </header>
  <main class="site-content">
  <h1>Manhwa Video Dubber — Settings</h1>
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
  </script>
  <p><a href="/">Back to home</a></p>
  </main>
</body>
</html>"""
    return HTMLResponse(html)


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
    voice_source: str = Form("auto_tts"),
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

    try:
        video_ingest.validate_file_type(file.filename)
    except video_ingest.UnsupportedFileError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

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

    # G1 wiring (U1b): the heavy B1 -> B2 -> C1 chain now runs on a daemon
    # background thread so the upload returns immediately with
    # {"status": "processing"}. Progress is persisted via job_status — poll
    # GET /api/jobs/{job_id}/status until "done" / "error".
    job_status_store.write_status(job_id, "upload_pipeline", "running")
    threading.Thread(
        target=_run_upload_pipeline, args=(job_id,), daemon=True
    ).start()

    return {"job_id": job_id, "meta": job_meta, "status": "processing"}


@app.get("/api/jobs/{job_id}/status")
def job_status(job_id: str) -> dict:
    return job_status_store.read_status(job_id)


@app.get("/download/{job_id}/subtitles")
def download_subtitles(job_id: str, format: str = Query("srt")) -> FileResponse:
    fmt = format.lower()
    files = {
        "srt": ("subtitles_hi.srt", "text/plain"),
        "txt": ("subtitles_hi_plain.txt", "text/plain"),
        "json": ("subtitles_hi.json", "application/json"),
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
    files = {
        "wav": ("voiceover_hi.wav", "audio/wav"),
        "timestamps": ("timestamps_hi_auto.json", "application/json"),
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
    files = {
        "timestamps": ("timestamps_hi_upload.json", "application/json"),
        "wav": ("voiceover_hi.wav", "audio/wav"),
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
    <a href="/download/{job_id}/voiceover_upload?format=timestamps">timestamps_hi_upload.json</a></p>
  <p>Audio:
    <a href="/download/{job_id}/voiceover_upload?format=wav">voiceover_hi.wav</a></p>
  <p><a href="/voiceover/{job_id}/choose">Change voice source</a></p>
  <p><a href="/">Back to home</a></p>
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
  </form>
  <p><a href="/">Back to home</a></p>"""
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
        if not (job_dir / "subtitles_hi.json").exists():
            raise HTTPException(
                status_code=404,
                detail=f"no subtitles_hi.json for job {job_id}",
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
        links = []
        if result.get("voiceover_path"):
            links.append(
                f'<p>Audio: <a href="/download/{job_id}/voiceover?format=wav">voiceover_hi.wav</a> '
                f'({result.get("total_sec")} sec)</p>'
            )
        links.append(
            f'<p>Timestamps: '
            f'<a href="/download/{job_id}/voiceover?format=timestamps">timestamps_hi_auto.json</a></p>'
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
  <p><a href="/">Back to home</a></p>
"""
    return HTMLResponse(ui.page("Auto Voiceover — Manhwa Video Dubber", body))


@app.get("/review/{job_id}", response_class=HTMLResponse)
def review_page(job_id: str) -> HTMLResponse:
    try:
        page = review.build_review_page(job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
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
  <p><a href="/review/{job_id}">Back to review</a></p>
  <p><a href="/">Back to home</a></p>"""
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


def _render_final_result(job_id: str, result=None) -> HTMLResponse:
    if not isinstance(result, dict):
        stage = job_status_store.read_status(job_id).get("stages", {}).get(
            "final_render"
        )
        result = (stage or {}).get("result")
    if not isinstance(result, dict):
        result = render_final.finalize_video(job_id)

    duration = (
        f"{result['duration_sec']:.3f} sec." if result["duration_sec"] is not None
        else "duration unknown"
    )
    body = f"""<h1>Final video — job {job_id}</h1>
  <p>Status: <strong>{result["status"]}</strong>. Duration: {duration}</p>
  <video controls src="/download/{job_id}"></video>
  <p><a href="/download/{job_id}">Download final video</a></p>
  <p><a href="/review/{job_id}">Back to Review</a></p>
  <p><a href="/">Back to home</a></p>"""
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
