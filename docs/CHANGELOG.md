# Manhwa Video Dubber — Changelog

## [FA-B3] — 2026-08-13 — Error-handling polish + complete failure-case suite for full_auto_chain (Full-Auto Pipeline)

Group B's last chunk: robustness + tests only, no new feature. The two chain
functions already contained no try/except, so every stage's
`FileNotFoundError` / `ValueError` / `RuntimeError` /
`auto_cut.DraftValidationError` propagates straight to the caller (the group
C/D wiring catches and persists them via job_status) — verified explicitly.

- `pipeline/full_auto_chain.py`: confirmed no bare `except` / `pass` /
  TODO / placeholder; a mid-chain failure stops the following steps
  immediately (exception propagates, no partial/silent state).
- `pipeline/tests/test_full_auto_chain.py`: now the complete final test
  suite (6 tests):
  - happy path for both chains (existing FA-B1/B2 tests);
  - TTS complete failure (auto_tts chain) -> `RuntimeError` propagates, no
    ffmpeg work runs, no `final_video.mp4`;
  - draft validation failure -> `auto_cut.DraftValidationError` propagates,
    `render_final.finalize_video` never called;
  - final render (F3) failure -> propagates, no `final_video.mp4`;
  - user_upload chain D3 align failure -> propagates, F3 never runs.
- Full suite: **279 tests OK** (275 prior + 4).

## [FA-B2] — 2026-08-13 — run_user_upload_chain() — D3 to F3 in one function (Full-Auto Pipeline)

The own-audio path used to require several manual clicks after the audio was
saved (align, then final). This chunk puts the whole post-save chain into one
function (HTTP wiring comes in group D).

- `pipeline/full_auto_chain.py`: new `run_user_upload_chain(job_id)` runs D3
  (`voiceover_upload.align_uploaded_voiceover`) -> D4
  (`voiceover_unify.unify_voiceover_timestamps`) -> E1
  (`edit_guideline.build_edit_guideline`) -> E2 (`auto_cut.build_draft_video`)
  -> F3 (`render_final.finalize_video`) and returns
  `{"alignment": <D3 result>, "final": <F3 result>}`.
- Hard constraints honored: the function does **not** save the audio (that
  stays `voiceover_upload.save_uploaded_voiceover`'s job, wired in group D);
  it only assumes `voiceover_hi.wav` is already on disk. `app.py` untouched.
- `pipeline/tests/test_full_auto_chain.py`: +1 test — a fake
  `voiceover_hi.wav` is saved first, the Gemini align call is mocked, then
  `run_user_upload_chain(job_id)` is called directly and
  `outputs/<job_id>/final_video.mp4` is asserted.
- Full suite: **275 tests OK** (274 prior + 1).

## [FA-B1] — 2026-08-13 — run_auto_tts_chain() — D2 to F3 in one function (Full-Auto Pipeline)

The F3 (final render) used to run only when the user manually visited
`/final/{job_id}` (biggest gap in the old flow). This chunk introduces a
pure-Python orchestration wrapper that runs the entire auto-TTS chain **F3
included** in one place — HTTP wiring still comes later (group C).

- `pipeline/full_auto_chain.py` (new): `run_auto_tts_chain(job_id,
  call_budget=None)` runs D2 (`voiceover_auto.generate_auto_voiceover`) ->
  D4 (`voiceover_unify.unify_voiceover_timestamps`) -> E1
  (`edit_guideline.build_edit_guideline`) -> E2 (`auto_cut.build_draft_video`)
  -> **F3 (`render_final.finalize_video`)** in sequence and returns
  `{"voiceover": <D2 result>, "final": <F3 result>}` so the caller (group C)
  can persist both in the job status. Failures propagate (no swallow).
- Hard constraint honored: `app.py` untouched — `_process_auto_tts` /
  `_continue_from_voiceover` unchanged (needed for backward-compat, FA-E1);
  the new function is standalone and not called from any route yet.
- `pipeline/tests/test_full_auto_chain.py` (new, 1 test): mocks Gemini TTS
  with real ffmpeg silence placeholders + `auto_cut._run`, calls
  `run_auto_tts_chain(job_id)` directly (no HTTP) and asserts
  `outputs/<job_id>/final_video.mp4` is produced.
- Full suite: **274 tests OK** (273 prior + 1).

## [FA-A1] — 2026-08-13 — Upfront voice-source input on /upload (Full-Auto Pipeline)

The voice-source question (auto TTS vs own audio) is now taken on the upload
form itself, so the choice is persisted the moment the upload succeeds and the
full-auto chain never depends on a later `/voiceover/{job_id}/choose` click.

- `app.py` `home()`: the upload form now has a radio-group:
  - "সিস্টেম নিজেই ভয়েসওভার বানাক (Gemini TTS)" — `value="auto_tts"`,
    **default checked**.
  - "আমি নিজের/অন্য AI দিয়ে বানানো অডিও দেব" — `value="user_upload"`.
  - The existing inline `<script>` already builds `FormData(form)`, so the
    field rides along with the file automatically — no JS change needed.
- `app.py` `upload_video()`: new optional `voice_source: str = Form("auto_tts")`
  param. Values outside `voiceover_unify.ALLOWED_MODES` are rejected with a
  400 (same pattern as the existing `InvalidVoiceSourceError`). A valid value
  is persisted via `voiceover_unify.set_voice_source(job_id, voice_source)`
  synchronously, right before the B1→B2→C1 background thread starts — the
  choice is always known from the upload moment onward.
- Hard constraints honored: `_run_upload_pipeline`, `voiceover_choose` and the
  `/voiceover/{job_id}/choose` route behavior are unchanged; the old manual
  choose page still works (backward-compat, verified again in FA-E1).
- `pipeline/tests/test_full_auto_upload.py` (new, 3 tests): voice_source=
  "user_upload" is persisted to `voice_source_choice.json` immediately (before
  the background thread finishes), the default is "auto_tts" when omitted, and
  an invalid value returns 400.
- Full suite: **273 tests OK** (270 prior + 3 new).

## [UI2] — 2026-08-13 — Visual redesign, unified with BlueprintTube (CSS/head only)

UI1 (below) intentionally kept `static/style.css` minimal — "light and
readable," no external CDN/framework. That was a deliberate scope choice at
the time, not a bug, but it meant this sibling pipeline looked nothing like
BlueprintTube's dark, custom-typeset design. This chunk ports BlueprintTube's
design tokens over so both projects share one visual identity.

- `static/style.css` (full rewrite, same selector set as UI1 — no class
  renamed or removed): BlueprintTube's exact token set — dark charcoal
  background (`--bg #131110`), amber tally-light accent (`--amber #e8a33d`),
  teal (`--teal #3fa796`), red (`--red #e5484d`), `Baloo Da 2` (display) /
  `Hind Siliguri` (body) / `IBM Plex Mono` (data/code) type stack, pill
  buttons, hairline borders, radial-gradient + film-grain background texture.
  `.review-box`, `.flagged-banner`/`.flag-note`/`section.flagged`,
  `.keys-table`, `.processing-banner`/`.spinner`, `.error-banner*`,
  `.trim-form` all re-themed onto the dark palette; added a generic bare
  `video` rule for the final-page player (previously only `.review-box
  video` was styled).
- `pipeline/ui.py` `page_head()`: now links the same Google Fonts
  (`FONTS_HREF`, new) BlueprintTube uses, plus a `<div class="grain">`
  overlay right after `<body>`. `site_header()`'s markup is unchanged — the
  amber "sprocket" dot next to the brand name is pure CSS
  (`.site-header .brand::before`), so no HTML structure or test-checked
  string moved.
- `app.py` `settings_page()` builds its own standalone `<head>` (doesn't go
  through `ui.page()`) — brought into line by hand with the same font link +
  grain div, reusing `ui.FONTS_HREF`.
- No route body text, class name, id, form field, or JSON contract changed —
  every string `pipeline/tests/*.py` asserts on (`href="/static/style.css"`,
  `site-header`, `review-box flagged`, `id="serial-{n}"`, button/link labels,
  etc.) is untouched. This is a CSS + `<head>` change only; full suite
  expected to still be **270 tests OK** (not re-run in the sandbox that made
  this change — no network access to install `fastapi`/`uvicorn` there. Run
  `python3 -m unittest discover -s pipeline/tests -v` locally to confirm
  before relying on it).

## [FIX3] — 2026-08-13 — Upload no longer lands on raw JSON

The screen the user photographed (`0.0.0.0:5000/upload` showing Chrome's
built-in JSON pretty-printer) was not a bug in FIX2's error-banner styling —
it was the home page's `<form method="post" action="/upload">` submitting
as a classic full-page navigation straight to an endpoint that returns a
plain JSON `dict`. FastAPI/Starlette never rendered any of `ui.py`'s HTML
chrome for that response because it isn't an HTML response — the browser
was just displaying the JSON body Chrome always shows for a JSON
`Content-Type`, with no app styling involved at any point.

- `app.py` `home()`: the upload form no longer does a native POST
  navigation. It now submits via `fetch()`, disables the button and shows
  "Uploading…" while in flight, and on success redirects the browser to a
  new `/upload/{job_id}` page; on failure it shows the same `.error-banner`
  card used elsewhere instead of a raw JSON error body or a browser alert.
- `app.py`: new `GET /upload/{job_id}` route — shows the existing
  `_polling_page` while `upload_pipeline` is running/erroring (identical
  pattern to `/voiceover/{job_id}/auto_tts` and `/final/{job_id}`), then a
  styled result summary (lines extracted, any extraction warnings, link to
  `/voiceover/{job_id}/choose`) once done. Unknown job id -> 404.
- `POST /upload` itself is unchanged (still returns
  `{"job_id", "meta", "status"}` as JSON) — that contract is relied on by
  `test_video_ingest.py` and is the correct shape for an API consumer; only
  the *browser* path was wrong, so only the browser-facing form changed.
- Full suite: **270 tests OK** — zero regressions. Manually verified via
  TestClient: home page has no `action="/upload"` left, `GET
  /upload/{job_id}` returns `text/html` (not JSON) both immediately after
  upload (polling state) and for an unknown job id (404).

## [FIX2] — 2026-08-13 — TTS model correction + console/UI error readability

Root cause of production `429 RESOURCE_EXHAUSTED` on every rotated Gemini
key: U0 had pinned `TTS_MODEL` to `gemini-3.1-flash-tts-preview`, a
deviation from the original robustness plan (which specified
`gemini-2.5-flash-preview-tts`, matching BlueprintTube exactly) introduced
earlier in FIX1 on the theory that Google's deprecation page listed it as
the "recommended replacement." That model's free-tier daily quota per key
turned out to be far smaller (observed `limit: 10` requests/day) than
`gemini-2.5-flash-preview-tts`'s, so even 25 rotated keys exhausted almost
immediately. The risk was flagged as unverified in U0's own comment but
never confirmed before shipping.

- `pipeline/config.py`: `TTS_MODEL` reverted to `gemini-2.5-flash-preview-tts`
  (BlueprintTube parity, field-verified) with an updated comment explaining
  why `gemini-3.1-flash-tts-preview` was rejected.
- `pipeline/tests/test_config_pins.py`: pin test updated to match.
- `pipeline/gemini_rotation.py`: added `_short()` — per-key rotation
  warnings and `AllKeysExhausted.attempts` now keep only the first line of
  a failed call's error text (capped ~160 chars) instead of the full raw
  Google API error body (nested dict with help links + per-violation quota
  metadata) repeated once per rotated key. This was the direct cause of the
  unreadable multi-thousand-character console output when every key hit the
  same quota error back to back.
- `app.py`: added `logging.basicConfig(...)` — previously the root logger
  had no handler configured, so every `WARNING+` fell back to Python's bare
  "handler of last resort" (no timestamp/level/source). Console output is
  now `HH:MM:SS LEVEL logger.name: message`.
- `app.py`: new `_friendly_error(exc)` replaces raw `str(exc)` in all three
  background-stage error paths (`upload_pipeline`, `voiceover_auto`,
  `final_render`) that write `job_status`'s `detail` field — summarizes
  `AllKeysExhausted`/`CallBudgetExceeded` into one readable sentence instead
  of dumping the full per-key attempt log to the result page.
- `static/style.css` / `_polling_page` (app.py): the polling page's error
  state now renders as a styled `.error-banner` card (reusing the existing
  flagged-color tokens) with a heading, the short message, and a real
  "আবার চেষ্টা করুন" button, instead of a plain unstyled `<p>` in a hidden
  `<div>`. Added a `.processing-banner` + spinner for the in-progress state.
- Full suite: **270 tests OK** — zero regressions.

## [U5] — 2026-08-12 — Pre-flight dry_run_check.py (final robustness chunk)

Chunk U5, the last chunk of the robustness update plan (U0–U5): an offline
sanity gate that validates a job's existing JSON artifacts *before* starting
expensive stages (D2 Gemini TTS, F3 final render) — no network, no ffmpeg,
no Gemini calls, completes in seconds.

- `pipeline/dry_run_check.py` (new, standalone CLI):
  `python3 -m pipeline.dry_run_check --job-id <job_id> [--upload-root uploads]`
  (default `video_ingest.UPLOAD_ROOT`). Missing files are informational
  "stage not done yet" skips, never errors; only present files are checked:
  1. `subtitles_zh.json` (B2): every entry has `serial`/`text_zh`/
     `start_sec`/`end_sec`, and serials are `1..N` consecutive (no gap /
     duplicate).
  2. `subtitles_hi.json` (C1): serial count/order matches
     `subtitles_zh.json` exactly (translator hard constraint) and prints the
     `translation_fallback: true` share as a percentage.
  3. `timestamps_hi_final.json` (D4): entry count matches
     `subtitles_hi.json`, every entry has `end_sec > start_sec` (no
     invalid/zero duration), no overlap (next `start_sec >=` previous
     `end_sec`).
  4. `edit_guideline.json` (E1): informational — flagged count and
     `flag_reason` distribution (`extreme_speed_ratio` vs
     `invalid_duration`); the F1 review page is where the user fixes these,
     so it never blocks.
- Exit code: 0 = no blocking error (missing files are not blocking),
  1 = blocking error(s) — serial mismatch / gap / duplicate / invalid or
  overlapping duration — so the check can gate a CI/script chain.
- No new app.py endpoint in this chunk (scope = standalone CLI only; wiring
  it into the UI is a possible separate follow-up).
- Tests: `pipeline/tests/test_dry_run_check.py` (new, 13 tests) — one test
  per blocking-error class (serial gap, duplicate, missing required key, hi
  count/order mismatch, timestamps count mismatch, invalid duration,
  overlap, malformed JSON, unknown job) plus a clean happy path (exit 0),
  a missing-file non-blocking case, and CLI exit-code checks. Fixtures are
  temp-dir JSON files; no real job needed.
- Full suite: **270 tests OK** (was 257) — +13 tests, zero regression.
- Docs: `docs/HANDOFF_NEXT.md` rewritten to mark the whole U0–U5 plan
  complete; `docs/FINAL_SUMMARY.md` Section 5 gains item (e) telling the user
  to run the dry-run check before/after every real-media run.
- Tags: `chunk-U5-done` + `manhwa-video-dubber-robustness-final`.

## [U4] — 2026-08-12 — Per-job persistent pipeline.log

Chunk U4 of the robustness update plan (U0–U5): every pipeline stage for a
job now appends its progress to the same per-job log file
`uploads/<job_id>/logs/pipeline.log`, so the whole job lifecycle (extraction
→ translation → voiceover → alignment → final render) can be replayed later.

- `pipeline/job_logging.py` (new, standalone): `get_job_logger(job_id,
  upload_root=None)` returns a `logging.Logger` named `manhwa.job.<job_id>`
  whose `FileHandler` appends to `uploads/<job_id>/logs/pipeline.log`
  (parents created on demand; `upload_root` defaults to
  `video_ingest.UPLOAD_ROOT`). Repeated calls for the same job reuse the
  existing file handler instead of stacking new handlers, so a job's lines
  never get duplicated. Format:
  `%(asctime)s [%(levelname)s] %(name)s: %(message)s`.
- The five pipeline entry functions now route their progress through the
  per-job logger (created *after* each function's existence/validation
  checks, so `get_job_logger` never masks a missing-job
  `FileNotFoundError`/`ValueError`):
  - `pipeline/subtitle_extract.py` `extract_subtitles()` — "no active Gemini
    keys" error; the Gemini-call error logs inside `call_with_rotation()` /
    `_generate_with_rotation()` also land in the job file via a new optional
    `logger_` kwarg (default `None` → module logger, so existing callers are
    unaffected).
  - `pipeline/translator.py` `translate_subtitles()` — fallback / mismatch /
    budget-exceeded / split-repair warnings; the same `logger_` kwarg is
    threaded through `_translate_chunk` / `_repair_split` and the
    `call_with_rotation()` calls.
  - `pipeline/voiceover_auto.py` `generate_auto_voiceover()` — "no keys",
    clip-probe errors, second-pass progress; `logger_` threaded into both
    TTS `call_with_rotation()` calls.
  - `pipeline/voiceover_upload.py` `save_uploaded_voiceover()` and
    `align_uploaded_voiceover()` — saved-path, probe-fail, whisper
    fallback, alignment-finished lines; `logger_` threaded into
    `_gemini_align` / `_transcribe_words`.
  - `pipeline/render_final.py` `finalize_video()` — final video written.
- **Key-leak guard retained:** the per-key failure log only ever records
  `(key_index, reason)` pairs (see `gemini_rotation.py` `.attempts`) and
  masked keys — never raw API key values.
- Tests: `pipeline/tests/test_job_logging.py` (new, 5 tests) — file write +
  `[INFO]` format, repeated-call handler dedup (no duplicated lines),
  separate files per job, default-root behavior, and a multi-stage test where
  three real entry functions (keys mocked empty) append one entry each into a
  single `pipeline.log`.
- Full suite: **257 tests OK** (was 252) — +5 tests, zero regression.

## [U3b] — 2026-08-12 — Auto-TTS second bounded pass over failed_serials

Chunk U3b of the robustness update plan (U0–U5), completing the U3 auto-repair
group: like the U3a translation repair, the auto-TTS path now gives its failed
lines one bounded retry instead of leaving them at silence placeholders.

- `pipeline/voiceover_auto.py` `generate_auto_voiceover()`: after the main
  per-entry loop, if `failed_serials` is non-empty a single bounded second
  pass runs over just those serials. It carries the key `rotation` state on
  from where the main loop left it (never restarting at 0, so a key that was
  rate-limited in pass one gets a fresh chance if its quota has refreshed) and
  reuses the same shared `call_budget` from U2b (not a fresh one). Each failed
  serial is tried exactly once more; a success overwrites its silence
  placeholder with the real audio and drops it from `failed_serials`, a
  second failure keeps it failed for good. Serial entries whose `text_hi` is
  empty are skipped in the second pass (empty text cannot be TTS'd) and stay
  failed.
- **Timestamps recalculated after repair:** the prompt assumed E1
  (edit_guideline) derives its speed ratio from ffprobe and that timestamps
  could stay untouched. Verified against `pipeline/edit_guideline.py`: E1
  computes `pts_multiplier = target_duration / source_duration` from
  `timestamps_hi_final.json` (D4 output, derived from
  `timestamps_hi_auto.json`), not from ffprobe. Per the prompt's own
  conditional, the cumulative `start_sec`/`end_sec` are therefore recomputed
  from the final clip durations (first-pass durations plus the second-pass
  ffprobe durations of repaired clips) before `timestamps_hi_auto.json` is
  written, so the timestamps match the re-concatenated `voiceover_hi.wav` and
  E1 gets correct target durations even when a repaired clip's real duration
  differs from the silence placeholder.
- Tests (`pipeline/tests/test_voiceover_auto.py`): two new module tests —
  `test_second_pass_repairs_first_pass_failure` (a serial that fails every key
  in the first pass but succeeds on the second → final `tts_failed: false`,
  real audio replaces the silence, `failed_serials` empty, status `ok`) and
  `test_second_pass_persistent_failure_keeps_silence` (a serial failing both
  passes → `tts_failed: true` and the `TTS_FAIL_SILENCE_SEC` placeholder
  remain, status `partial`).
- Full suite: **252 tests OK** (was 250) — 2 new tests, zero regressions; the
  U1c resume/reuse and U2b budget-cap tests still pass (the second pass also
  draws from the budget but never raises).

## [U3a] — 2026-08-12 — Translation batch-split auto-repair (per-line fallback)

Chunk U3a of the robustness update plan (U0–U5), first half of the U3
auto-repair group: `translate_subtitles()` no longer throws away a whole batch
when a strict retry still mismatches. It now recursively splits the batch and
only the genuinely failing lines fall back.

- `pipeline/translator.py`:
  - New private helpers `_translate_chunk()` and `_repair_split()`. The
    repair step runs after the existing normal → strict (RETRY_PROMPT) whole-
    batch attempts both fail: the line list is split into two roughly equal
    halves, each half is translated separately with the strict prompt, and a
    half that still mismatches is split again recursively. The recursion stops
    when a chunk matches, when a single-line chunk keeps failing, or when the
    split depth reaches `max_split_rounds` — so pathological input falls back
    gracefully instead of looping forever.
  - Per-line fallback: repair results are aligned 1:1 with the non-empty
    `text_zh` lines, with `None` marking a line that could not be translated.
    `_build_output()` treats a `None` entry as `translation_fallback: true`
    with `text_hi = text_zh` for that line only; neighbouring lines still get
    their translations. Output serial order and the entry dict schema are
    unchanged.
  - `translate_subtitles(job_id, upload_root=None, call_budget=None,
    max_split_rounds=4)` gains `max_split_rounds` (default 4, backward
    compatible; `app.py` call site untouched). When the whole-batch attempt
    already failed with a `call_budget_exceeded` error the repair is skipped
    (nothing more can succeed) and the batch falls back directly. Every
    recursive split call draws from the same shared `call_budget`; if the
    budget runs out mid-repair, `_translate_chunk` gets the error dict from
    `call_with_rotation` (which never raises) and falls that chunk back —
    already-matched lines are kept and the whole job never raises.
- Tests (`pipeline/tests/test_translator.py`): the old
  `test_mismatch_retry_then_fallback_with_original_text` was updated — under
  U3a each single-line chunk must also fail for both lines to fall back (4
  calls). New `BatchSplitRepairTest` with 3 tests:
  `test_split_repair_recovers_when_halves_succeed` (whole-batch mismatches,
  split halves succeed → all `translation_fallback: false`),
  `test_only_persistently_failing_line_falls_back` (one line always fails,
  the other translates → only that one is flagged), and
  `test_max_split_rounds_falls_back_gracefully` (4-line batch,
  `max_split_rounds=1` → leftover half-chunks fall back whole, no raise).
- Full suite: **250 tests OK** (was 247) — 3 new tests, zero regressions; the
  U2b `CallBudget` cap test still passes (skips repair on budget exhaustion).

## [U2b] — 2026-08-12 — Wire gemini_rotation error-classification + CallBudget into call sites

Chunk U2b of the robustness update plan (U0–U5), completing the U2 group: the
standalone `gemini_rotation` core from U2a is now wired into all three Gemini
call sites (extraction / translation / auto-TTS) as a thin wrapper, and the
background job threads share a per-job `CallBudget`.

- `pipeline/config.py`: new `MAX_API_CALLS_PER_JOB = None` (None = unlimited,
  the default). `GEMINI_RETRY_BACKOFF_SEC` / `GEMINI_TRANSIENT_HTTP_CODES` are
  no longer used by `call_with_rotation` (superseded by
  `gemini_rotation.classify_error`); comments updated to mark that.
- `pipeline/subtitle_extract.py`: `call_with_rotation()` is now a thin wrapper
  over `gemini_rotation.call_with_rotation_v2()`. Public contract unchanged
  (`(result, next_rotation, None)` on success, `(None, rotation, error_dict)`
  on failure, never raises) so callers in `translator` / `voiceover_auto` /
  `voiceover_upload` keep working untouched. New optional `call_budget=None`
  kwarg (None = unlimited, so no existing caller changes behavior).
  `AllKeysExhausted` / `CallBudgetExceeded` / re-raised non-rotatable errors
  are caught and returned as `(None, rotation, error_dict)`; the full per-key
  `attempts` log and non-rotatable failures are now logged explicitly (v1
  swallowed them). Error-dict `type` keeps the old taxonomy
  (`rate_limit`/`transient`/`permanent`/`content_blocked` plus new
  `non_rotatable`/`call_budget_exceeded`) and gains an `attempts` list.
  Dead helpers (`_error_http_code`/`_is_rate_limit`/`_is_transient`) and the
  now-unused `import time` were removed; `_block_reason_from_response` and
  `_is_content_blocked` are unchanged. `extract_subtitles` gained
  `call_budget=None` and passes it through both the chunked and non-chunked
  paths (`_generate_with_rotation` → `call_with_rotation`).
- `pipeline/translator.py`: `translate_subtitles(job_id, upload_root=None,
  call_budget=None)` — passed through to both `call_with_rotation` calls.
- `pipeline/voiceover_auto.py`: `generate_auto_voiceover(job_id,
  upload_root=None, call_budget=None)` — passed through to the per-line TTS
  rotation call.
- `app.py`: `_run_upload_pipeline` (U1b thread) builds one shared
  `CallBudget(config.MAX_API_CALLS_PER_JOB)` and passes it to
  `extract_subtitles` + `translate_subtitles`; `_run_voiceover_auto` (U1c
  thread) does the same for `generate_auto_voiceover` — a single global cap
  across the whole job run. Default `None` preserves the old unlimited
  behavior for synchronous paths (`_process_auto_tts`, result re-renders).
- **Intentional behavior change (U2 design):** the same-key 429 backoff
  (10s/30s/60s) is gone — 429 is now classified `rotatable` and rotates to the
  next key immediately; 400/`invalid`/`content`/`safety`/`blocked` errors are
  classified `non_rotatable` and stop on the first key. Four old
  `test_subtitle_extract.py` tests that asserted the backoff behavior were
  updated to the new contract (rotate-immediately / stop-on-non-rotatable,
  `sleep` assertions removed).
- Tests: `test_subtitle_extract.py` gains `test_call_budget_cap_fails_
  gracefully` (exhausted cap → `extraction_failed` + error type
  `call_budget_exceeded`, never raises) and `test_upload_cached_per_key_and_
  path` (`_get_or_upload` cache: same key+path uploads once, a different key
  uploads separately — the remaining purpose of `_UPLOAD_CACHE` now that
  same-key retries are gone); `test_translator.py` gains
  `test_call_budget_cap_falls_back_without_raising` (exhausted cap → original
  Chinese fallback, never raises); `test_voiceover_auto.py` gains
  `test_call_budget_cap_uses_silence_without_raising` (exhausted cap →
  silence placeholders + `tts_failed`, status `partial`, never raises).
- Full suite: **247 tests OK** (was 244) — 3 new tests, zero regressions.

## [U2a] — 2026-08-12 — Gemini key-rotation error classification + CallBudget core (standalone)

Chunk U2a of the robustness update plan (U0–U5), first sub-chunk of the U2
group (key-rotation error-classification + call-budget). New standalone module
only — no existing pipeline code changed and no call site wired yet (that is
U2b).

- `pipeline/gemini_rotation.py` (new): exception hierarchy —
  `GeminiRotationError` (base) → `NonRotatableError` (base for "rotating
  keys can't help") → `ContentSafetyBlocked`, plus `AllKeysExhausted`
  (carries `.attempts` = `list[(key_index: int, reason: str)]`) and
  `CallBudgetExceeded` (carries `.used` / `.max_calls`).
- `classify_error(exc)` → `"rotatable"` / `"non_rotatable"`: on the
  lowercased `str(exc)`, checks rotatable markers first (`429`, `quota`,
  `rate limit`, `timeout`, `connection`, `503`, `502`, `500`), then
  non-rotatable markers (`400`, `invalid`, `content`, `safety`, `blocked`),
  then defaults to `"rotatable"` (safe default — an unrecognized error costs
  only one extra key attempt, while aborting a recoverable call loses the
  job). `NonRotatableError` instances are always `"non_rotatable"` regardless
  of message text.
- `CallBudget(max_calls: int | None)`: `None` = unlimited; `.consume()`
  raises `CallBudgetExceeded(used, max_calls)` when the budget is exhausted,
  else increments an internal counter. Deliberately not thread-safe (each
  rotation call runs sequentially inside a single job thread).
- `call_with_rotation_v2(keys, rotation, callable_, *args, call_budget=None)`:
  same round-robin order as `subtitle_extract.call_with_rotation` and the
  same success signature `(result, next_rotation)` (first two elements
  identical, so U2b can swap call sites easily). Every attempt consumes the
  budget first (`CallBudgetExceeded` propagates, never caught); each key is
  invoked as `callable_(key, *args)`; on failure the error is classified —
  `"non_rotatable"` re-raises the original exception immediately (no further
  keys tried), `"rotatable"` appends `(key_index, str(exc))` to the attempt
  log and tries the next key; all keys exhausted raises
  `AllKeysExhausted(attempts)`.
- `pipeline/tests/test_gemini_rotation.py` (new, 23 tests): classify_error
  (rotatable/non-rotatable markers, default, instance override, rotatable
  wins when both marker groups present); CallBudget (consume until limit,
  zero max-calls raises immediately, `None` unlimited, negative rejected);
  call_with_rotation_v2 (success + next-rotation, rotation advance from given
  index, rotatable failure retries next key, all-keys-fail →
  `AllKeysExhausted` with correct rotated-index attempts, non-rotatable stops
  without touching other keys via call-count, `ContentSafetyBlocked`
  propagates the original instance, budget exceeded propagates counting
  failed and successful calls, empty keys, args forwarding, no budget =
  unlimited).
- Full suite: **244 tests OK** (was 221) — 23 new tests, zero regressions;
  no behavior change anywhere (module is unwired).

## [U1c] — 2026-08-12 — Background voiceover/render + resumable clip reuse + polling UI

Chunk U1c of the robustness update plan (U0–U5), completing the U1 group:
the auto-TTS and final-render endpoints now run on background threads with a
no-CDN polling page, and the auto-TTS generation reuses existing clips so a
retry never re-pays for successful TTS lines.

- `pipeline/voiceover_auto.py` `generate_auto_voiceover()`: per-clip resume —
  if `serial_N.wav` already exists with probed duration ≥ `TTS_FAIL_SILENCE_SEC`
  + 0.2 (2.2s), it is reused (`tts_failed=False`, no TTS call); missing /
  silence-sized / unprobeable clips are regenerated. Basis for the "আবার চেষ্টা
  করুন" retry path.
- `app.py` `GET /voiceover/{job_id}/auto_tts`: when the `voiceover_auto` stage
  is already `done`, the pre-U1c result HTML is rendered synchronously
  (byte-identical markup); otherwise `_start_stage()` writes `running` and
  `_run_voiceover_auto()` (D2 → D4 → E1 → E2) runs on a daemon thread, storing
  the result dict in status, and `_polling_page()` is shown. 404 fail-fast when
  `subtitles_hi.json` is missing.
- `app.py` `GET /final/{job_id}`: same pattern for the `final_render` stage via
  `_run_final_render()`; 404 fail-fast when `draft_final_video.mp4` is missing.
- `app.py` new helpers: `_start_stage()` (single-flight: no second thread while
  `running`), `_run_voiceover_auto()` / `_run_final_render()` (both fully
  try/except-wrapped including a final `except Exception` fallback so no
  exception escapes a bare daemon thread), and `_polling_page()` (inline
  `<script>` polling `/api/jobs/{job_id}/status` every 2s; `done` → redirect to
  the same URL which now renders the result, `error` → detail + retry link).
- Tests: `test_voiceover_auto.py` gains `test_resume_reuses_existing_clips`
  (1.0s clip regenerated, 2.5s clip reused, no re-TTS for it) and
  `test_auto_page_done_short_circuits_no_rerun` (second GET with stage done
  does not invoke `_call_tts`); endpoint tests poll via a new
  `_wait_for_stage()` while keeping mocks active. `test_app_orchestration.py`
  G1 final step polls `final_render` then re-GETs the done page (identical
  assertions); `_wait_for_upload_done` became a wrapper over generic
  `_wait_for_stage_done`. `test_render_final.py` endpoint test follows the same
  poll pattern. `test_static_assets.py` `test_final_page_links_stylesheet`
  seeds a done status for a deterministic stylesheet-link render.
- Manual resume check (temp upload root, mocked TTS): 2nd auto_tts run
  re-invokes `_call_tts` only for the short clip (`["पहला"]`), reusing the 2.5s
  clip — no re-TTS.
- Full suite: **221 tests OK** (was 219) — two new tests, zero regressions.

## [U1b] — 2026-08-12 — Background upload pipeline (B1+B2+C1) + idempotent resume

Chunk U1b of the robustness update plan (U0–U5): the heavy post-upload chain
now runs in a background thread with persisted job status, and is idempotent
so a re-run with existing outputs is a no-op (basis for the future Retry
button, U1c).

- `app.py` `POST /upload`: file save + `finalize_job()` stay synchronous; the
  response is now `{"job_id", "meta", "status": "processing"}` (no `pipeline`
  key anymore). `job_status.write_status(job_id, "upload_pipeline",
  "running")` is written, then B1/B2/C1 runs on a daemon `threading.Thread`.
- `app.py` new `_run_upload_pipeline(job_id)`: runs extract → build → translate,
  then writes status `done` with `extra={"extraction_status", "serials"}` (plus
  `errors` on partial extraction). Failures (`FileNotFoundError`/`ValueError`/
  `RuntimeError`) are caught and recorded as status `error` + `{"detail": ...}`
  — never an uncaught exception in a bare thread.
- **Idempotent resume:** if `uploads/<job_id>/subtitles_hi.json` already
  exists, the chain is skipped and `done` is recorded from the existing files
  via `_resume_pipeline_extra()`.
- `pipeline/tests/test_app_orchestration.py`: G1 upload step now asserts the
  immediate `processing` response, polls `GET /api/jobs/{job_id}/status` until
  `done` (10s/0.1s loop), and verifies `extraction_status`/`serials` from the
  status file. `test_review_before_voiceover_404` also waits for `done`.
- `pipeline/tests/test_video_ingest.py`: `test_upload_success_creates_job`
  updated to the new async contract (mocks kept active until the job reaches
  `done`, summary read from status instead of the response body).
- Scripted resume check (manual DoD): second `_run_upload_pipeline` call with
  `subtitles_hi.json` present does not re-run B1/B2/C1 and records `done` with
  the correct summary.
- Full suite: **219 tests OK** (was 219) — zero regressions; no behavior
  change to the voiceover/render endpoints (still synchronous, U1c).

## [U1a] — 2026-08-12 — Job status infrastructure + read-only polling endpoint

Chunk U1a of the robustness update plan (U0–U5): infrastructure only, no
behavior change. The pipeline does not write status yet — wiring lands in
U1b/U1c.

- `pipeline/job_status.py` (new): per-job `threading.Lock` store for
  thread-safe read-modify-write; `status_path()` → `uploads/<job_id>/
  job_status.json` (based on `video_ingest.UPLOAD_ROOT`); `read_status()`
  returns `{"stage": "unknown", "state": "not_started"}` when the file is
  missing (never raises); `write_status()` merges the existing JSON under a
  `{"stages": {stage: {...}}}` map so older stage history is never dropped,
  then writes atomically (temp file + `os.replace`). `state` is validated as
  `running | done | error`.
- `app.py`: new pure read-only endpoint `GET /api/jobs/{job_id}/status`
  returning the job status dict. Imported as `job_status as job_status_store`
  so the endpoint function can share the name without shadowing the module.
- `pipeline/tests/test_job_status.py` (new, 10 tests): read_status default on
  missing file, write_status new write + merge/history preservation, invalid
  state rejection, concurrent writes from 8 threads (barrier-synchronized)
  with no race/corruption/lost updates, and TestClient coverage of the
  endpoint (default for unknown job + written status round-trip).
- No existing tests were modified and no existing behavior changed.
- Full suite: **219 tests OK** (was 209) — zero regressions.

## [U0] — 2026-08-12 — Pin TTS_MODEL to gemini-3.1-flash-tts-preview

Chunk U0 of the robustness update plan (U0–U5). The original plan targeted
the older `gemini-2.5-flash-preview-tts`, but the repo's current model is the
intentional decision (FIX1 switched to it as the recommended replacement for
the deprecated 2.5), so the current model is the one pinned.

- `pipeline/config.py`: protective comment above `TTS_MODEL` — model is
  deliberately kept as default (Google-recommended replacement, no shutdown
  date); warns future agents not to switch to a pro/paid TTS variant for
  perceived quality gains. The "pro is paid-only (HTTP 429) / flash keeps a
  free tier" claim (BlueprintTube parity) is NOT verifiable from this repo, so
  the comment marks it conditional and it must be confirmed by the project
  owner before being relied on.
- `pipeline/tests/test_config_pins.py` (new): pin test asserting
  `config.TTS_MODEL == "gemini-3.1-flash-tts-preview"`, so a future agent
  changing the model breaks the suite immediately.
- Full suite: **209 tests OK** (was 208) — zero regressions.

## [FIX1] — 2026-08-11 — Gemini resilience & deprecated-model fix

Root cause of `extraction_status: "extraction_failed"` on real videos:
`GEMINI_MODEL`/`ALIGNMENT_MODEL` were the deprecated `gemini-2.0-flash`
(shutdown 2026-06-01), so every Gemini call returned 404 and every segment
failed. Verified against the Gemini deprecations page before choosing
replacement models.

- `pipeline/config.py`: `GEMINI_MODEL` → `gemini-3.6-flash` (Google's
  recommended replacement for `gemini-2.0-flash`, no shutdown date);
  `ALIGNMENT_MODEL` → `gemini-3.6-flash`; `TTS_MODEL` →
  `gemini-3.1-flash-tts-preview` (recommended replacement for
  `gemini-2.5-flash-preview-tts`). Reminder comments to re-check
  https://ai.google.dev/gemini-api/docs/deprecations added above each.
- `pipeline/config.py`: new `GEMINI_RETRY_BACKOFF_SEC = (10, 30, 60)` and
  `GEMINI_TRANSIENT_HTTP_CODES = (408, 429, 500, 502, 503, 504)`.
- `pipeline/subtitle_extract.py`: `call_with_rotation` now returns
  `(result, next_rotation, error)`. HTTP 429 (and other transient statuses)
  retry the SAME key with the 10s→30s→60s backoff before rotating;
  non-transient errors still rotate immediately. Content-blocked responses
  (`prompt_feedback.block_reason` / `FinishReason.SAFETY` family) are logged
  distinctly as `content_blocked` and never retried/rotated.
- `pipeline/subtitle_extract.py`: `_call_gemini` uploads each video/segment
  once (`_get_or_upload` cache); a rate-limit retry reuses the already-uploaded
  file instead of re-uploading it.
- `pipeline/subtitle_extract.py`: `subtitles_zh_raw.json` now carries an
  `errors` map (`{"<segment>": {"type": ..., "message": ...}}`) so the saved
  JSON alone is enough to diagnose a failed segment.
- `app.py`: `/upload` response includes a concise `pipeline.errors` summary
  when `extraction_status != "ok"`.
- Callers updated for the 3-tuple return: `translator.py`, `voiceover_auto.py`,
  `voiceover_upload.py`.
- `pipeline/tests/test_subtitle_extract.py`: +10 tests (same-key 429 backoff,
  exhaustion→rotate, permanent-rotate-without-retry, content-block no retry/rot
  ate, error persistence in saved JSON, upload reuse across a 429 retry,
  block-reason detection via prompt_feedback / finish_reason).
- Full suite: **208 tests OK** (was 198) — zero regressions.

## [UI2] — 2026-08-11 — Bulk Gemini key add

- `pipeline/key_store.py`: new `add_keys(key_text)` — accepts a paste blob
  (newline / comma / whitespace separated) and adds every key at once;
  exact duplicates already in the store are skipped; masks all returned
  entries. Raises `ValueError` when the blob is empty or all duplicates.
- `app.py`: new `POST /settings/keys/bulk` endpoint (form field `keys`);
  raw keys never leak (masked entries only).
- Settings page: new "Add multiple keys at once" section with a `<textarea
  name="keys">` posting to the bulk endpoint; the existing single-key add
  form is unchanged.
- `static/style.css`: textarea styling (monospace, focus ring).
- `pipeline/tests/test_key_store.py`: +9 tests (module: parse/mask/dupes/
  empty; endpoint: multi-add, empty 400, dup 400, no raw-key leak).
- Full suite: **198 tests OK** (was 189) — zero regressions.

## [UI1] — 2026-08-11 — CSS/layout polish (no logic changes)

- `static/style.css` (new): shared pure-CSS stylesheet — system font stack,
  comfortable padding/spacing, light contrast on white, button hover states,
  input border-radius. No external CDN/framework. Mounted in `app.py` via
  `StaticFiles` at `/static`.
- `pipeline/ui.py` (new): shared `page_head`/`site_header`/`page` helpers so
  every page renders the same stylesheet `<link>` and a consistent
  header/nav (home link + page title). Used by both `app.py` and
  `pipeline/review.py`.
- All HTML pages (`/`, `/settings`, `/voiceover/*`, `/review/*`, `/final/*`)
  now link the shared stylesheet and show the shared header/nav.
- `/review/{job_id}` focus: per-serial blocks restyled as cards
  (border/shadow/spacing); `flagged` serials get a distinct red/orange border
  + a `FLAGGED: <reason>` badge so they are easy to spot during manual review.
- Settings "Stored keys" table restyled as a readable `keys-table`
  (no raw `border="1"` table).
- Strict rule respected: no HTML element `id`/`name`/text content checked by
  the existing tests was changed (all tested substrings unchanged); no new
  feature/endpoint/behavior added.
- `pipeline/tests/test_static_assets.py` (new, 5 tests): `/static/style.css`
  is served (200, `text/css`) and every page (`/`, `/settings`,
  `/review/{job_id}`, `/final/{job_id}`) references it via the `<link>` tag.
- Full suite: **189 tests OK** (was 184) — zero regressions.

## [G2] — 2026-08-11 — Final wrap-up (all chunks S1–G2 complete)

- `docs/FINAL_SUMMARY.md` (new): end-of-project summary — stage-by-stage table
  (S1→G2), the artifact-to-artifact data-flow chain (A2 `source.mp4` → B1 → B2
  → C1 → D1/D2/D3 → D4 → E1 → E2 → F1/F2 → F3 `final_video.mp4`), the
  automated coverage summary (184 tests OK), and the **"steps a sandboxed AI
  agent cannot do — the user must do them"** checklist (real Gemini key +
  real-video end-to-end QA run with the five verification points, the >10-min
  B1 chunking test, TTS voice/persona selection as a creative decision, and
  BGM-preservation confirmation with a follow-up feature-plan note).
- `docs/HANDOFF_NEXT.md`: status set to **all chunks (S1–G2) complete** —
  remaining work is only the user's own real-media QA run.
- No pipeline/app code changes in this chunk — the G1 regression suite still
  passes unchanged (**184 tests OK**).

## [G1] — 2026-08-11 — HTTP-level end-to-end wiring regression

- `pipeline/tests/test_app_orchestration.py`: permanent HTTP-level regression
  that drives the app purely through `fastapi.testclient.TestClient` endpoints
  (never calling pipeline modules directly) over the full chain: add Gemini key
  → upload (B1+B2+C1) → choose auto-TTS (D2+D4+E1+E2 draft video) → review page
  loads per-clip data → review edit (F2 partial re-render + guideline update) →
  final render (F3) → download serves the final file. Gemini calls are mocked,
  `auto_cut`/`render_final` ffmpeg is mocked, and the D2 TTS clips are real
  ffmpeg silence placeholders (deterministic, no network).
- `app.py` wiring gaps fixed (found by the chain test): `POST /upload` only
  saved + probed the video (now chains B1 extract → B2 serialize → C1
  translate and reports `pipeline.extraction_status`/`serials`);
  `POST /voiceover/{job_id}/choose` (auto_tts) only wrote the choice file (now
  runs D2+D4+E1+E2 down to `draft_final_video.mp4`); `GET
  /voiceover/{job_id}/auto_tts` only ran D2 (now continues through
  D4+E1+E2). New `_continue_from_voiceover`/`_process_auto_tts` helpers.
- `pipeline/tests/test_video_ingest.py`: `test_upload_success_creates_job`
  updated with mocked Gemini + asserts for `pipeline.extraction_status`,
  `serials`, and the B2/C1 output files.
- Full suite: **184 tests OK** (was 181). Plus 3 new orchestration chain tests.

## [F3] — 2026-08-11 — Final render + download + back-to-review

- `pipeline/render_final.py`: `finalize_video(job_id)` copies/normalizes the
  approved `draft_final_video.mp4` (E2 / F2) into
  `outputs/<job_id>/final_video.mp4` — H.264 + AAC with a `+faststart` moov
  atom so any container/codec quirk in the draft is fixed and the file streams
  instantly (Auto Manhwa Maker `_ffmpeg_normalize` deliverable pattern);
  `final_video_path(job_id)` returns the expected path without rendering.
- `app.py`: `GET /final/{job_id}` runs the finalize and shows the final video
  with a download link and a "Back to Review" link back to
  `/review/{job_id}`; `GET /download/{job_id}` serves
  `outputs/<job_id>/final_video.mp4` as `video/mp4`. The review page gained a
  "Final Render →" link to `/final/{job_id}`.
- `.gitignore`: `outputs/` (final deliverables are processing artifacts).
- `pipeline/tests/test_render_final.py`: 9 tests — finalize writes the
  normalized output with the exact ffmpeg command (codecs, `+faststart`, in/out
  paths), missing draft/job raise, ffmpeg failure and empty-output raise, and
  endpoint tests: final page renders video/download/back-to-review links,
  download serves the file bytes, unknown job → 404.

## [F2] — 2026-08-11 — Apply per-clip review edits (partial re-render)

- `pipeline/review.py`: `apply_clip_edit(job_id, serial, new_source_start,
  new_source_end)`.
- Only the edited serial's `edit_guideline.json` entry changes — the target
  timing stays fixed and `pts_multiplier` is recomputed as
  `target_duration / new_source_duration`, then the `extreme_speed_ratio`
  flag is re-evaluated (e.g. an over-aggressive trim re-flags the serial).
- Partial re-render (Auto Manhwa Maker pattern, no full re-encode): the one
  source segment is re-cut with the new range (`setpts` updated) into its
  existing clip slot, then the concat demuxer + mux steps run over the
  untouched clips to re-splice `draft_final_video.mp4`; the re-spliced draft
  is ffprobe-validated against the voiceover duration like E2
  (`DraftValidationError` on failure).
- Validation: both start/end unset, `end <= start` or negative times → 400;
  missing job / guideline / serial / draft clip → 404.
- `app.py`: `POST /review/{job_id}/edit` applies the trim form and links back
  to the review page.
- `pipeline/tests/test_review.py`: +14 tests — only the edited entry changes
  (others byte-identical fields), target timing preserved, extreme flag
  recomputation, start-only edit, re-cut uses the new range on the correct
  index clip (`serial_00000.mp4` vs `serial_00001.mp4`), concat/mux sequence,
  invalid-range / no-edit / unknown-serial / missing-clip errors,
  post-edit validation failure, and endpoint tests (apply + back link, 404/400).

## [F1] — 2026-08-11 — Per-clip review UI (flagged-serial highlighting)

- `pipeline/review.py`: `get_review_items(job_id)` joins each E1
  `edit_guideline.json` entry with the B2 Hindi subtitle text
  (`subtitles_hi.json`, optional) into review items carrying
  source/target ranges + durations, `pts_multiplier`, `flagged` and
  `flag_reason`.
- `build_review_page(job_id)`: one block per serial with a clip player
  (`<video src="/review/{job_id}/clip/{serial}">`), a trim timeline
  (source start/end number inputs that F2 submits to
  `POST /review/{job_id}/edit`), the `text_hi` line and its target duration.
  Serials flagged `extreme_speed_ratio` / `invalid_duration` are shown in a
  highlighted banner on top and in a distinct `review-box flagged` style.
- `extract_clip(job_id, serial)`: on-the-fly ffmpeg cut of the serial's
  `[target_start_sec, target_end_sec]` segment from
  `draft_final_video.mp4` (video + audio, `+faststart`) into
  `uploads/<job_id>/review_clips/serial_<serial>.mp4`, so the browser player
  never downloads the whole draft. Always re-extracted on demand (fresh after
  F2 edits).
- `app.py`: `GET /review/{job_id}` (page), `GET /review/{job_id}/clip/{serial}`
  (extract + serve as `video/mp4`); missing job/guideline/serial/draft → 404.
- `pipeline/tests/test_review.py`: 15 tests — items join + defaults, page
  renders player URLs / subtitle text / durations, flagged highlight (banner +
  class) and clean pages, trim-form controls, clip extract uses the exact
  target time range, endpoint tests for the page and clip route (404s and the
  served bytes), all ffmpeg mocked.

## [E2] — 2026-08-11 — FFmpeg auto-cut (speed-adjust + concat + mux)

- `pipeline/auto_cut.py`: `build_draft_video(job_id)`.
- Per serial in `edit_guideline.json`: cuts `[source_start_sec, source_end_sec]`
  from the source video (video stream only, `-an`; the original audio is
  discarded), speed-adjusts it with FFmpeg's `setpts=<pts_multiplier>*PTS`
  filter so it lands exactly on the target duration, concatenates the clips in
  serial order (concat demuxer + `-c copy`), then muxes `voiceover_hi.wav`
  (D4) in as the audio track (`-c:a aac`, `-map 0:v -map 1:a`).
- Output `uploads/<job_id>/draft_final_video.mp4`; resolution / aspect ratio
  are inherited from the source (no forced values).
- Verification (Auto Manhwa Maker `render.py` pattern): ffprobe must show both
  a video and an audio stream and a duration within a few frames of the
  voiceover duration — the tolerance is derived from the source frame rate
  (`RENDER_TOLERANCE_FRAMES`); failure raises `DraftValidationError`.
- `pipeline/config.py`: `RENDER_VIDEO_CODEC`, `RENDER_VIDEO_PRESET`,
  `RENDER_PIX_FMT`, `RENDER_AUDIO_CODEC`, `RENDER_DEFAULT_FPS`,
  `RENDER_TOLERANCE_FRAMES`, `RENDER_TIMEOUT_SEC`.
- `pipeline/tests/test_auto_cut.py`: 16 tests — exact clip/concat/mux command
  sequences with mocked subprocess (order, `-an`, setpts values, map args,
  codec args), empty guideline no-op, ffmpeg failure raises, validation
  pass/fail (missing streams, out-of-tolerance, missing duration) both as a
  pure function and as a build-level `DraftValidationError`.

## [E1] — 2026-08-11 — Speed-ratio edit guideline builder (soft-clamp + flagging)

- `pipeline/edit_guideline.py`: `build_edit_guideline(job_id)` — pure Python,
  no network, no ffmpeg.
- Per serial, reads the original Chinese timing (`subtitles_zh.json`, B2) and
  the unified Hindi voiceover timing (`timestamps_hi_final.json`, D4) and
  computes `pts_multiplier = target_duration / source_duration` — the value
  that later feeds FFmpeg's `setpts` (e.g. 8s dialog on 6s source ->
  `1.333`, a slow-down).
- Soft clamp, never blocks: multipliers outside `SPEED_RATIO_MIN`/`MAX`
  (0.5–2.0) keep their real value but are flagged
  `flag_reason: "extreme_speed_ratio"` for the F-group review UI.
- Zero/negative durations never crash: safe `pts_multiplier: 1.0` +
  `flag_reason: "invalid_duration"`. Serials present in only one input are
  logged and skipped.
- Output: `uploads/<job_id>/edit_guideline.json`
  `[{"serial", "source_start_sec", "source_end_sec", "target_start_sec",
  "target_end_sec", "pts_multiplier", "flagged", "flag_reason"}]`.
- `pipeline/tests/test_edit_guideline.py`: 16 tests (normal ratios flagged-
  free, boundary 0.5, extreme ratios flagged but kept, edge-case safe defaults,
  missing/malformed inputs, partial serial coverage).

## [D4] — 2026-08-11 — Unify auto-TTS / user-upload timestamps + overlap validation

- `pipeline/voiceover_unify.py`: `unify_voiceover_timestamps(job_id)` —
  reads `voice_source_choice.json` and picks the D2 (`timestamps_hi_auto.json`)
  or D3 (`timestamps_hi_upload.json`) output, then writes one common
  `uploads/<job_id>/timestamps_hi_final.json` so later chunks are
  mode-agnostic.
- Common schema `[{"serial", "start_sec", "end_sec", "flagged", "flag_reason"}]`;
  D2's `tts_failed` and D3's `alignment_fallback` map onto `flagged` +
  `flag_reason` (`"tts_failed"` / `"alignment_fallback"` / `null`).
- Hard constraint: consecutive entries never overlap — overlaps are
  deterministically clamped to the previous end (B2-style) and logged; the
  clamped serials are reported in the result.
- `voiceover_hi.wav` (shared path by D2/D3) presence is verified so the E group
  can use it directly.
- `pipeline/tests/test_voiceover_unify.py` extended (11 new tests): auto_tts
  unify, user_upload unify, overlap clamping, collapsed-range clamp, flag
  precedence, serial ordering, and missing choice/source/audio/job errors.

## [D3] — 2026-08-11 — User-uploaded voiceover alignment + Gemini/Whisper fallback

- `pipeline/voiceover_upload.py`: `save_uploaded_voiceover(job_id, audio_bytes,
  filename)` — user's complete Hindi voiceover (mp3/wav/m4a) is ffmpeg-normalized
  to mono PCM and saved as `uploads/<job_id>/voiceover_hi.wav`.
- `align_uploaded_voiceover(job_id)` — finds where every `subtitles_hi.json`
  `text_hi` line is spoken and writes `uploads/<job_id>/timestamps_hi_upload.json`
  in D2's schema with an `alignment_fallback` flag per serial:
  `[{"serial", "start_sec", "end_sec", "alignment_fallback", "alignment_source"}]`.
- Primary path: Gemini audio-understanding (audio + subtitle list) with
  multi-key rotation (`call_with_rotation`); every serial must be present/numeric
  or the whole pass is treated as failed.
- Resilience ladder (mirrors the "Gemini fail -> Whisper fallback" pattern):
  Gemini fail/malformed → local Whisper transcription with word timestamps
  (lazy import, unavailable/failing Whisper never crashes the job) → sequential
  fuzzy matching via difflib to the subtitle lines (`WHISPER_MATCH_MIN_RATIO`),
  unmatched lines equal-split between matched neighbours; both paths fail →
  deterministic equal-split of total audio. Never raises.
- `pipeline/config.py`: added `ALIGNMENT_MODEL`, `WHISPER_MODEL`,
  `WHISPER_MATCH_MIN_RATIO`.
- `app.py`: `POST /voiceover/{job_id}/upload` (save + link to alignment),
  `GET /voiceover/{job_id}/align_uploaded` (runs alignment, shows status/fallback
  flags) and `GET /download/{job_id}/voiceover_upload?format=timestamps|wav`.
- `pipeline/tests/test_voiceover_upload.py`: 25 tests — Gemini happy path,
  Gemini fail → Whisper fallback + flags, malformed Gemini triggers fallback,
  partial Whisper match gap-fill, both-fail → equal-split + clear flag,
  no-active-key still proceeds, upload save/validation (module + TestClient),
  align page + download endpoints.

## [D2] — 2026-08-10 — Automatic Hindi voiceover via Gemini TTS

- `pipeline/voiceover_auto.py`: `generate_auto_voiceover(job_id)`.
- Each `text_hi` line is sent to Gemini TTS in its own request via the shared
  `call_with_rotation` key-rotation helper (no duplication).
- Deterministic timing: every clip's real duration is measured with ffprobe
  and timestamps are built by cumulative sum — no estimation, no
  after-the-fact guesswork.
- Clips concatenated in serial order into `uploads/<job_id>/voiceover_hi.wav`;
  `uploads/<job_id>/timestamps_hi_auto.json` carries
  `[{"serial", "start_sec", "end_sec", "tts_failed"}]`.
- Resilience: TTS failure (all keys) → `TTS_FAIL_SILENCE_SEC` silence
  placeholder + `tts_failed: true`, job continues, never raises. No active
  key → `tts_unavailable` status without audio. Unreadable clip → same
  silence fallback.
- `pipeline/config.py`: added `TTS_MODEL`, `TTS_SAMPLE_RATE`,
  `TTS_FAIL_SILENCE_SEC`.
- `app.py`: `GET /voiceover/{job_id}/auto_tts` (runs generation, shows status
  + download links) and `GET /download/{job_id}/voiceover?format=wav|timestamps`.
- `pipeline/tests/test_voiceover_auto.py`: 12 tests (deterministic happy path,
  full-failure silence+flag, partial-failure continues, no-key, missing input,
  empty input, TestClient generation + download endpoints).

## [D1] — 2026-08-10 — Voiceover source choice UI

- `pipeline/voiceover_unify.py`: `set_voice_source(job_id, mode)` and
  `get_voice_source(job_id)`; mode must be `auto_tts` or `user_upload`, saved
  to `uploads/<job_id>/voice_source_choice.json`.
- `app.py`: `GET /voiceover/{job_id}/choose` (page with both options) and
  `POST /voiceover/{job_id}/choose` (saves choice; `auto_tts` shows the D2
  endpoint link, `user_upload` shows the D3 upload form).
- `pipeline/tests/test_voiceover_unify.py`: 11 tests (both modes save the
  file, overwrite, invalid mode errors, unknown job errors, TestClient page +
  POST flow).

## [C1] — 2026-08-10 — Chinese-to-Hindi translation + downloadable subtitle files

- `pipeline/translator.py`: `translate_subtitles(job_id)`.
- Translates each `text_zh` to Hindi via Gemini; reuses B1's shared
  `call_with_rotation` key-rotation helper (no duplication).
- Hard constraint: output serial count/order identical to input. Count
  mismatch triggers one strict retry; if it still mismatches (or all keys
  fail), original Chinese text is kept with `translation_fallback: true`.
- `start_sec`/`end_sec` keep the original Chinese video timing (reference for
  D-group speed-ratio).
- Outputs: `subtitles_hi.json`, `subtitles_hi.srt` (standard SRT, reference
  timing), `subtitles_hi_plain.txt` (serial + Hindi text per line).
- `app.py`: `GET /download/{job_id}/subtitles?format=srt|txt|json`.
- `pipeline/subtitle_extract.py`: extracted shared `call_with_rotation`
  helper (B1 behavior unchanged).
- `pipeline/tests/test_translator.py`: 12 tests (happy path, retry+fallback,
  retry-fixes, no-key fallback, SRT/plain files, download endpoint).

## [B2] — 2026-08-10 — Subtitle serialization + no-overlap validation

- `pipeline/subtitle_builder.py`: `build_subtitle_list(job_id)`.
- Reads `subtitles_zh_raw.json` (B1), assigns serial numbers (1..N) in
  chronological order.
- Hard constraint: consecutive subtitle ranges never overlap — overlaps are
  deterministically clamped (next start = previous end) and the fixed serial
  is logged.
- `extraction_failed` parts are kept as flagged placeholder entries
  (`status: "extraction_failed"`, empty text) — never dropped.
- Output: `uploads/<job_id>/subtitles_zh.json` with schema
  `{"serial", "text_zh", "start_sec", "end_sec", "status"}`.
- `pipeline/tests/test_subtitle_builder.py`: 7 tests (clean input unchanged,
  overlap clamp + log, gap preserved, failed placeholders kept).

## [B1] — 2026-08-10 — Subtitle extraction (Gemini video understanding)

- `pipeline/subtitle_extract.py`: `extract_subtitles(job_id)`.
- Videos under `LONG_VIDEO_CHUNK_THRESHOLD_SEC` (default 600s) sent whole;
  longer videos split by ffmpeg into overlapping segments
  (`SUBTITLE_OVERLAP_SEC`, default 30s), each segment processed separately and
  timestamps rebased to absolute with the segment offset.
- Overlap duplicates (same normalized text, close start) de-duplicated into
  one continuous list.
- Gemini multi-key round-robin rotation from `key_store.get_active_keys()`;
  failure falls back to the next key.
- Resilience: all keys failing / malformed JSON never raises — emits
  `ok`/`partial`/`extraction_failed` status, logs, flags `failed_segments`,
  continues with what succeeded.
- Output: `uploads/<job_id>/subtitles_zh_raw.json`.
- `pipeline/config.py`: added `SUBTITLE_OVERLAP_SEC`,
  `SUBTITLE_DEDUP_TOLERANCE_SEC`, `GEMINI_MODEL`, `GEMINI_TEMPERATURE`.
- `pipeline/tests/test_subtitle_extract.py`: 10 tests (happy path, chunk
  merge + dedup, round-robin rotation, all-keys-fail signal, malformed JSON).

## [A2] — 2026-08-10 — Chinese video upload + System Start

- `pipeline/video_ingest.py`: upload validation, `job_id` (UUID) per upload,
  saved to `uploads/<job_id>/source.mp4`, ffprobe-based `job_meta.json`
  (duration / resolution).
- Hard constraint: upload blocked with a clear error when no active Gemini key
  exists (from A1 store).
- Unsupported file types / unreadable videos return clear 400 errors.
- `app.py`: home page upload form + "System Start" button, `POST /upload`
  endpoint.
- `pipeline/tests/test_video_ingest.py`: module + `TestClient` HTTP tests
  (11 tests) using real generated sample videos; ffmpeg/ffprobe required.

## [A1] — 2026-08-10 — Gemini API key settings

- `pipeline/key_store.py`: `list_keys()`, `add_key()`, `delete_key()`,
  `get_active_keys()`; persisted to git-ignored `gemini_keys_store.json`.
- API responses expose only masked keys (`...ab12`) + id + label — never the
  raw key.
- `delete_key()` on a missing id raises a clear `KeyNotFoundError` (no silent
  no-op).
- `app.py`: `/settings` page (list + add/delete) and `/settings/keys`
  POST (add) / DELETE (delete) endpoints.
- `pipeline/tests/test_key_store.py`: module + `TestClient` HTTP tests
  (12 tests), including raw-key leak checks.

## [S1] — 2026-08-10 — Project scaffolding

- `app.py`: FastAPI shell on `localhost:5000`, home route "coming soon".
- `pipeline/` package + empty `__init__.py`; `pipeline/tests/` package added.
- `pipeline/config.py`: speed ratio bounds, Hindi TTS voice placeholder,
  long-video chunk threshold.
- `requirements.txt` including `python-multipart`.
- `gemini_keys_store.json.example` placeholder (real store is git-ignored).
- `.gitignore` covering env, secrets, python, OS/editor and media artifacts.
- Docs: plan, changelog, handoff.
