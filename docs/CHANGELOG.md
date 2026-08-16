# Manhwa Video Dubber — Changelog

## [F10+F11] — 2026-08-16 — animated progress bar + live log panel + history tab UI + Bengali error text

F10 replaces the polling page's lone spinner with an animated progress bar and
per-stage Bengali rows, adds a docked live-log panel (new
`GET /api/jobs/{job_id}/logs`), turns `/history` into a real HTML page with
cards/badges/resume buttons (nav link "ইতিহাস"), and rewrites the history-full
`confirm()` dialog in Bengali with OK = delete files / Cancel = keep files.
F11 adds a Bengali `detail_bn` next to every error status `detail` and shows it
first in the error banner, with the English text behind a collapsed toggle.
Transcript upload/language-dropdown (F12) and retry-with-escalation UI (F13)
remain separate, not started.

- **`pipeline/stages.py` (নতুন)** — single source of truth for the progress
  bar: `STAGE_SEQUENCE` (F1_extract, C1_translate, D2_voiceover_or_D3_align,
  D4_unify, E1_guideline, E2_draft, F3_final), `STAGE_LABELS_BN` (Bengali
  labels), `STAGE_KEY_GROUPS` (status-file keys per slot — D2/D3 share one
  slot) and `UMBRELLA_TO_SEQUENCE` (`final_render` owns the F3 slot).
- **Progress bar + stage rows in `_polling_page` (F10.1)** — the spinner is
  replaced (the "Processing…" banner text stays) by a `.progress-track` /
  `.progress-fill` bar whose width = (done stages + in-stage fraction) /
  len(STAGE_SEQUENCE) * 100, fraction from `extra.progress.processed/total`
  else 0.5. Seven rows below it show ✓/spinner/✗/○ + the Bengali label per
  stage, rebuilt inside the existing 2s `poll()` loop. No framework/CDN — the
  existing inline vanilla-JS fetch pattern is extended.
- **Live log panel + `GET /api/jobs/{job_id}/logs` (F10.2)** — the endpoint
  never raises (missing file → `{"lines": [], "next_line": 0}`), clamps
  negative/past-end `since_line`, and returns new lines plus `next_line`. The
  polling page docks a fixed-bottom panel (max-height ~30vh, monospace dark)
  that polls the endpoint every 3s, appends new lines, and auto-scrolls only
  when the user is already at the bottom (`scrollTop` vs `scrollHeight`).
- **History page + nav (F10.3)** — `site_header` now carries a "ইতিহাস" nav
  link; `GET /history` returns an HTML page (cards with short job_id,
  created_at, target_lang, voice_source, colored done/running/error badge),
  "দেখুন" → `/review/{job_id}`, and "রিজিউম করুন" → POST
  `/jobs/{job_id}/resume` → redirect to the new `/resume/{job_id}` polling
  page → `/final/{job_id}`. Resume buttons are shown for error jobs and for
  stale-running jobs (no status update for 10+ min, via status-file mtime).
  The machine-readable JSON moved to `GET /api/history`.
- **Bengali history-full confirm (F10.4)** — on HTTP 409 `needs_confirm` the
  upload form shows the Bengali `confirm()` dialog ("OK = হ্যাঁ ফাইলসহ ডিলিট
  করো, Cancel = শুধু History লিস্ট থেকে সরাও, ফাইল থাকুক"); OK posts
  `confirm-start?delete_files=true`, Cancel `delete_files=false`, then proceeds
  to the polling page either way.
- **`pipeline/error_bn.py` (নতুন) + `detail_bn` wiring (F11)** —
  `error_bn.explain_bn(exc, stage)` mirrors `_friendly_error`: CallBudgetExceeded,
  AllKeysExhausted (empty + populated), ffmpeg/ffprobe/subprocess failures,
  whisper import/runtime errors, malformed-transcript placeholder (F12), and
  timeout/network; falls back to a generic message naming the stage (Bengali
  label) with the truncated English text. It never raises. All 11 app error
  sites now write via `_write_error_status` so every error status carries both
  `detail` and `detail_bn`; the polling error banner shows `detail_bn` first
  with "বিস্তারিত (English)" collapsing the English `detail`.
- **Tests**: full suite **৫০২ টেস্ট OK** (was ৪৭১; +৩১) — new
  `test_error_bn.py` (one case per mapped exception + never-raises),
  `test_f10_endpoints.py` (logs incremental `since_line` + clamping, /history
  HTML for 0/1/3 jobs, badges + resume buttons incl. stale-running, keep-files
  confirm, error-path `detail_bn`); extended `test_f9_endpoints.py` (history
  JSON assertions retargeted to `/api/history`) and `test_job_status.py`
  (`detail`/`detail_bn` persisted together on error entries).

## [F9] — 2026-08-16 — per-job engine/language config, 3-job history with confirm-based eviction, resume-from-interruption

Every job now carries a per-job config written once at creation, the app keeps
the last 3 jobs in a history index that never evicts silently, and an
interrupted job can be resumed from the exact stage it stopped at. F10
(progress bar / log panel / history-tab UI) and F11 (Bengali error text) are
**not** part of this chunk — they are separate, not-started.

- **New module `pipeline/job_config.py`** — `uploads/<job_id>/job_config.json`
  written once at job creation, before any Gemini/Whisper stage runs. Records
  the processing engine (`whisper_primary` vs `gemini_only`), the target
  language (`target_lang`, `"hi"` today; `source_lang` is schema-only until
  F12 auto-detection), and the voice source. `write_config` validates the
  engine / voice source (ValueError), `read_config` never raises and returns
  the F9 defaults for pre-F9 jobs (dir exists, no config file) or `None` for a
  missing job dir. `default_engine()` = `whisper_primary` when local Whisper is
  importable, else `gemini_only`; `whisper_importable()` is the probe.
- **Engine-gated Whisper (F9 §2)** — `whisper_align.engine_allows_whisper()`
  reads the per-job config: a `gemini_only` job skips Whisper even when it IS
  installed, for phone/Termux users who avoid the heavy torch/whisper install.
  Pre-F9 jobs with no `job_config.json` keep F8 behavior (Whisper allowed).
  Gated call sites: `subtitle_extract._whisper_merge` and
  `voiceover_upload.align_uploaded_voiceover`. The engine radio on the upload
  form (`GET /`) is pre-selected from `default_engine()`.
- **Per-stage status + progress** — `job_status.run_stage()` writes
  `running` → runs the stage → writes `done` (preserving a `progress` dict the
  stage wrote) or `error` + re-raise (status writes are best-effort). Every
  stage of `full_auto_chain.run_auto_tts_chain()` /
  `run_user_upload_chain()` is wrapped, so each records its own status entry
  (D2_voiceover / D3_align / D4_unify / E1_guideline / E2_draft / F3_final) in
  order. `subtitle_extract.extract_subtitles` and `auto_cut.build_draft_video`
  take an optional `progress_cb(processed, total)`. The app's `_run_upload_pipeline`
  wraps F1_extract and C1_translate with `run_stage` (F1 gets per-chunk progress).
- **Resume-from-interruption (F9 §5)** — new module `pipeline/resume.py`:
  `find_resume_point(job_id)` derives the next stage from which artifacts
  exist (`"upload_pipeline"` when `subtitles_hi.json` is missing, then the
  first missing of timestamps → edit_guideline → draft_final_video.mp4 →
  `outputs/<job_id>/final_video.mp4`; `None` when complete). `resume_job` runs
  the chain from that point via `start_from` on
  `run_auto_tts_chain` / `run_user_upload_chain` (earlier stages skipped, their
  result keys `None` — a completed stage is never re-run), and raises
  `RuntimeError` for complete / not-yet-uploaded jobs. New endpoint
  `POST /jobs/{job_id}/resume` returns `{"resume_point", "status":
  "processing"}` and runs `_run_resume` on a background thread persisting
  `resume` stage status; 409 when there is nothing to resume.
- **3-job history with confirm-based eviction (F9 §4)** — new module
  `pipeline/history_store.py`: index at `uploads/_history_index.json`, capped
  at `HISTORY_LIMIT` (3). `register_job` adds newest-first; when the index is
  already full it does **not** evict — it returns `{"added": False,
  "would_evict": <oldest>, "needs_confirm": True}`. `evict_job(job_id,
  delete_files=...)` drops the job, optionally deleting its files — only ever
  called after the user confirms. `list_history` returns the jobs newest-first
  with live metadata (job_config, voice source, status, target video name),
  skipping missing dirs. Endpoint `GET /history` returns
  `{"history": [...], "limit": 3}`.
- **Browser `confirm()` eviction flow on history-full** — on the upload form,
  when `POST /upload` returns 409 with `needs_confirm`, the page shows a
  two-step `window.confirm()` dialog naming the oldest job to be evicted;
  accepting calls `POST /jobs/{job_id}/confirm-start?evict_job_id=<oldest>&
  delete_files=true`, which evicts, registers the pending job and starts its
  pipeline; declining cancels without evicting anything. `POST /upload` also
  accepts the engine and target_lang form fields (validated) and writes
  `job_config.json` before any Gemini/Whisper work.
- **Tests**: full suite **৪৭১ টেস্ট OK** (was ৪৩০; +৪১) — new
  `test_job_config.py`, `test_history_store.py`, `test_resume.py`,
  `test_f9_endpoints.py` (HTTP: 409 confirm flow, confirm-start, /history,
  resume endpoint); extended `test_full_auto_chain.py` (per-stage status
  order), `test_subtitle_extract.py` + `test_voiceover_upload.py` (engine
  gating: `gemini_only` never transcribes). The resume acceptance test proves
  a stage that wrote its artifact then crashed is not re-run.

## [F8] — 2026-08-16 — Whisper becomes the primary timing authority (F1-F3, D3) + target-collapse render fix (F7 item 4)

Every source-video subtitle line has real spoken audio, so local Whisper is now
the timing authority for Chinese subtitle extraction (F1-F3) and user-uploaded
Hindi voiceover alignment (D3); Gemini is demoted to a bounded advisory
text-cleanup / secondary-pass role. Gemini can never set timing outside what
Whisper detected. Replaces F7 items 2-3; item 1 (shared `whisper_align.py`) is
extended, not replaced; item 4 (render fix) is implemented here.

- **New shared module `pipeline/whisper_align.py`** (F7 item 1, extended):
  `transcribe_segments` (segment-level, for F1-F3), `transcribe_words`
  (word-level, for D3), `last_speech_end`, `overlap_ratio`, and
  `match_words_to_entries` (migrated from `voiceover_upload.py`, which keeps
  the private `_transcribe_words` / `_match_words_to_entries` names as aliases
  so its call site and mocks are unchanged). All helpers never raise — `None`
  on import/transcribe failure, `[]` when Whisper hears no speech.
- **F1-F3 — `subtitle_extract.py`**: after each chunk's Gemini extraction, the
  chunk audio is extracted (`-vn -ar 16000 -ac 1`; whole source ->
  `source_audio.wav`, chunk -> `segments/seg_XXX.wav`) and transcribed with
  Whisper (per-chunk merge, before the existing `_dedup_merge`, which is
  structurally unchanged). Every Whisper segment becomes a subtitle entry:
  timing from Whisper; text = Gemini's when an unused Gemini line overlaps the
  segment by `overlap_ratio >= SUBTITLE_OVERLAP_MATCH_MIN (0.5)` AND their
  normalized texts are at least 0.3 similar (`SequenceMatcher`) ->
  `text_source="gemini_cleaned"`, otherwise `text_source="whisper_raw"`. Gemini
  lines that overlap no Whisper segment are dropped and counted in
  `gemini_hallucinated_dropped`. When Whisper is unavailable / hears no speech
  / audio extraction fails, the pure-Gemini output is returned unchanged (no
  `text_source` keys). F7's `timing_source` is dropped in favour of
  `text_source`.
- **D3 — `voiceover_upload.py`**: Whisper-primary flow — transcribe the
  voiceover unconditionally (`language="hi"`, model
  `WHISPER_MODEL_HI or WHISPER_MODEL`), sequential fuzzy-match serials
  (matched -> `alignment_source="whisper"`, `alignment_fallback=False`).
  Unmatched serials go to a **bounded Gemini secondary pass**: Gemini only sees
  those serials and an item is accepted (`alignment_source="gemini_assisted"`)
  iff its `end_sec <= last_speech_end + WHISPER_TAIL_TOLERANCE_SEC (1.0)`;
  everything else keeps the equal-split placeholder. New status
  `"gemini_assisted"` (some serials resolved via the bounded secondary);
  `"ok"` = every serial Whisper-matched (or, in the pure-Gemini fallback, every
  serial Gemini-matched); `"whisper"` keeps its meaning (some lines never got
  matched / Gemini did not help); `"equal_split"` unchanged. When Whisper is
  unavailable/fails entirely, today's pure-Gemini flow is used unchanged
  (Gemini for all serials, then equal-split) — this keeps the existing
  `_gemini_align` mocks and behavior intact. The E9 `_clamp_timestamps_to_audio`
  guard still runs after every path.
- **Config**: `WHISPER_MODEL` "base" -> **"small"** (coarse `base` boundaries
  are not reliable enough for primary timing), new `WHISPER_MODEL_ZH` /
  `WHISPER_MODEL_HI` (None), `SUBTITLE_OVERLAP_MATCH_MIN` (0.5),
  `WHISPER_TAIL_TOLERANCE_SEC` (1.0).
- **Target-collapse render fix (F7 item 4)**: a near-zero target must render
  near-zero, not the full untouched source clip.
  - `edit_guideline._build_entry`: a collapsed target (`<= 0`) with a healthy
    source now sets `pts_multiplier = RENDER_MIN_SEGMENT_DURATION_SEC /
    source_duration` (was `1.0`), still flagged `invalid_duration`.
  - `auto_cut.py` render loop: the degenerate-segment guard now also triggers
    when `target_duration <= RENDER_MIN_SEGMENT_DURATION_SEC` with a
    normal-length source — a minimal real window is cut (instead of rendering
    the full source clip) and the guideline multiplier / target sizes it.
    `extreme_speed_ratio` stays soft/informational.
- **Regression tests** (+25, full suite 430 passed, 42 subtests):
  - `test_whisper_align.py` (new): `overlap_ratio`, `last_speech_end`,
    `transcribe_segments` / `transcribe_words` (fake whisper via
    `sys.modules`), `match_words_to_entries` sequential matching.
  - `test_subtitle_extract.py` `WhisperPrimaryMergeTest`: whisper timing +
    `text_source` schema, zero-overlap Gemini lines dropped +
    `gemini_hallucinated_dropped`, whisper_raw when texts differ, falsy /
    unavailable Whisper keeps pure-Gemini output, per-chunk merge before dedup.
  - `test_voiceover_upload.py`: whisper-primary matches everything -> `"ok"`;
    `"gemini_assisted"` accepted within the speech tail / rejected past it;
    `max(end_sec) <= total_sec + epsilon` and the E9 clamp still protecting the
    whisper-primary path.
  - `test_edit_guideline.py` zero/negative-target multiplier assertions updated
    to the MIN/source value; `test_auto_cut.py` `_entry` now builds healthy
    (non-collapsed) targets, plus the existing degenerate-source tests still
    green.
- **Files**: `pipeline/whisper_align.py` (new), `pipeline/subtitle_extract.py`,
  `pipeline/voiceover_upload.py`, `pipeline/edit_guideline.py`,
  `pipeline/auto_cut.py`, `pipeline/config.py`, tests above,
  `docs/CHANGELOG.md`, `docs/HANDOFF_NEXT.md`.

## [E10] — 2026-08-16 — test-isolation fix: drain the FA-C1 auto-render thread so `/upload` tests cannot leak into later suites

Found while running the full suite as part of the E9 duration-drift verification.
Not a product bug — a flaky cross-test leak.

- **Problem**: `test_video_ingest.test_upload_success_creates_job` posts a real
  upload, and `/upload` defaults the voice source to `auto_tts` (FA-A1), so
  `_run_upload_pipeline` continues into `_run_auto_full_render` **on the same
  background daemon thread** (FA-C1). The test's `_wait_for_upload_done` only
  polls until the `upload_pipeline` stage reaches `"done"`, so the thread is
  still running the full-auto chain when the test ends and the mocks are
  unpatched. That thread then leaks into later tests: when `test_voiceover_auto`
  patches `voiceover_auto._call_tts`, the leaked thread calls it with the
  invalid test key, so `test_call_budget_cap_uses_silence_without_raising` fails
  with `Expected '_call_tts' to not have been called` / a `MagicMock` written to
  a clip file. Order- and timing-dependent (only manifests in the full-suite run).
- **Fix**: in `test_video_ingest`, mock `full_auto_chain.run_auto_tts_chain`
  (no network) and add a `_wait_for_stage_done` helper so the test waits for the
  `auto_full_render` stage to settle **inside** the mock `with` block — the
  daemon thread fully exits before any mock is restored.
- **Regression**: full suite is deterministic green (403 passed, 42 subtests).
- **Files**: `pipeline/tests/test_video_ingest.py`, `docs/CHANGELOG.md`,
  `docs/HANDOFF_NEXT.md`.

## [E9] — 2026-08-15 — duration-drift fix for user_upload: alignment targets are clamped to the real voiceover length so the final video can never be longer than the audio

Real-media QA job `6b2c0929-607f-4f79-a99a-76e0ed0dd5f1` rendered a **797.8s
final video from a 522s voiceover** (~53% longer, 111 of ~226 segments flagged
`extreme_speed_ratio`). The design invariant is: each serial's source clip is
pts-stretched to exactly its voiceover segment target duration — **with no cap
on the multiplier** — so the concatenated final video must equal the voiceover
audio duration. The bug was not a cap: tracing the multiplier path showed
`edit_guideline.py` keeps the real value (`target_duration / source_duration`)
and `auto_cut.py` passes it straight to `setpts=<multiplier>*PTS`;
`SPEED_RATIO_MIN/MAX` are flag-only QA thresholds. The drift came from the
**targets themselves**: D3 alignment (Gemini/Whisper) reported per-serial end
times that ran past the actual audio length, so the target durations summed to
more than the voiceover and E2 faithfully stretched every clip to an inflated
target.

- **Problem**: alignment target durations could sum to more than the uploaded
  voiceover's real duration → the draft video (sum of stretched clips) was
  longer than the audio, breaking the invariant.
- **Fix 1 — clamp at alignment time (D3)**:
  `pipeline/voiceover_upload.align_uploaded_voiceover` now runs
  `_clamp_timestamps_to_audio` over whatever alignment source was used
  (Gemini / Whisper / equal-split): every `start_sec`/`end_sec` is clamped into
  `[0, total_sec]`, consecutive starts are pulled up to the previous end
  (deterministic), so the target durations always tile inside the audio. The
  result now reports `target_total_sec` (sum of target durations, `==`
  voiceover duration after clamping) and `clamped_serials`; a non-blocking
  warning is added when clamping happened.
- **Fix 2 — defense-in-depth at unify time (D4)**:
  `pipeline/voiceover_unify.unify_voiceover_timestamps` applies the same clamp
  to the `user_upload` path against the probed voiceover duration (protects a
  stale or edited `timestamps_hi_upload.json`); skipped gracefully when the
  audio cannot be probed and never applied to the precise `auto_tts` path.
- **Shared helper**: `_clamp_timestamps_to_audio` lives in
  `pipeline/voiceover_unify.py` (reused by D3 via import, mirroring the
  existing cross-module helper pattern).
- **Verified no cap exists**: `SPEED_RATIO_MIN/MAX` (0.5/2.0) remain
  flag-only in `edit_guideline.py` / `review.py`; E7's
  `_redistribute_collision_cluster` + `SUBTITLE_MIN_SERIAL_DURATION_SEC` and
  the `RENDER_MIN_SEGMENT_DURATION_SEC` min-window fallback are source-side
  only and untouched.
- **Regression tests** (+8, full suite 403 OK):
  - `test_voiceover_upload.py` — `DurationDriftRegressionTest`: replicates the
    real-media pattern (522s audio, Gemini timestamps covering 0..797.8s) and
    asserts every timestamp is clamped to ≤ 522s, the last end lands on 522s,
    `sum(target durations) == 522s`, and clamping surfaces a warning;
    within-audio alignments are not clamped.
  - `test_voiceover_unify.py` — `VoiceoverDurationClampTest`: user_upload
    timestamps past the audio length are clamped in D4; unprobeable audio
    skips the clamp gracefully.
  - `test_edit_guideline.py` — a 20x speed-up keeps its real multiplier (no
    cap) and is only flagged `extreme_speed_ratio`.
  - `test_auto_cut.py` — `DurationDriftRegressionTest`: a 20x multiplier flows
    through `build_clip_command` uncapped (`setpts=20.000000*PTS`); the draft
    reports exactly the voiceover duration (`final video duration == voiceover
    audio duration`).
- **Files**: `pipeline/voiceover_upload.py`, `pipeline/voiceover_unify.py`,
  `pipeline/tests/test_voiceover_upload.py`, `pipeline/tests/test_voiceover_unify.py`,
  `pipeline/tests/test_edit_guideline.py`, `pipeline/tests/test_auto_cut.py`,
  `docs/CHANGELOG.md`, `docs/HANDOFF_NEXT.md`.

## [E8] — 2026-08-15 — duration-check removed for user_upload: total-length differences are normal translation drift, never a block

Design correction of the E6 fix. E6 made the draft video validation compare the
rendered draft's **total duration** against the **source video's** duration
with a loose `user_upload` tolerance (3s / 5%), blocking the whole job with
`duration_ok: False` on a large mismatch. The premise was wrong: the pipeline's
purpose is to stretch/squeeze each source scene-clip to the length of the
corresponding segment of the user's uploaded voiceover (`pts_multiplier` in
`auto_cut.py`). Because of translation, a dialogue (and therefore the whole
video) being substantially shorter or longer than the original is **normal and
desirable** — the real-media test was 523s of audio on a 303s video (~73%
longer) and that is valid, not an error condition.

- **Problem** (`pipeline/auto_cut.py` `_validate_draft` + `build_draft_video`):
  the total-duration tolerance check blocked `user_upload` jobs whose
  translated voiceover was a different length from the source video.
- **Fix 1 — remove the total-duration block for `user_upload`**:
  `_validate_draft` gained an `enforce_duration` flag. It is now `False` when
  `voice_source == "user_upload"`, so a duration mismatch never fails the draft
  (structural checks — video + audio streams — still always apply). The
  auto-TTS / unset path keeps the strict frames tolerance unchanged (TTS clip
  durations are measured/precise). The duration numbers are still computed and
  reported (`duration_ok`, `duration_enforced`).
- **Fix 2 — the real correctness check is per-segment alignment** (the log
  line `unified N voiceover timestamps from timestamps_hi_upload.json`):
  - `voiceover_unify.unify_voiceover_timestamps` (D4) now validates per-segment
    completeness: every serial in `subtitles_hi.json` must have a matching
    voiceover timestamp. A subtitle segment with no aligned audio raises the
    new `VoiceoverAlignmentError` (blocking) — the only legitimate user_upload
    block, alongside an audio file with no measurable content.
  - `voiceover_upload.align_uploaded_voiceover` (D3) raises
    `VoiceoverAlignmentError` when the uploaded audio has `total_sec <= 0`
    (no measurable content → no segment can be aligned to real audio). Its
    result now carries a `warnings` list; a serious `equal_split` fallback
    (no segment matched real audio) is surfaced as a non-blocking warning.
  - `full_auto_chain` results now include the `draft` (E2) result, and the
    final-video page renders a non-blocking banner when the alignment or draft
    stage reported warnings.
- **Fix 3 — optional non-blocking warning for extreme mismatches**:
  `build_draft_video` reports `duration_warning` when the user_upload draft
  length differs from the source by ≥ `USER_UPLOAD_DURATION_WARNING_RATIO`
  (5x, new config; 1/5x in the short direction) — e.g. "এই অডিও আসল ভিডিওর
  চেয়ে উল্লেখযোগ্য ভিন্ন দৈর্ঘ্যের, ঠিক ফাইল আপলোড হয়েছে তো?" It never
  blocks.
- **Files**: `pipeline/config.py` (new
  `USER_UPLOAD_DURATION_WARNING_RATIO`; tolerance constants are now
  informational-only), `pipeline/auto_cut.py`, `pipeline/voiceover_unify.py`
  (`VoiceoverAlignmentError` + completeness check), `pipeline/voiceover_upload.py`,
  `pipeline/review.py` (`apply_clip_edit` re-splice uses the same
  `enforce_duration` logic), `pipeline/full_auto_chain.py`, `app.py` (catches
  `VoiceoverAlignmentError`, renders the warning banner).
- **Regression tests** (+13):
  - `test_auto_cut.py`: user_upload accepts a large mismatch (523s vs 303s,
    the real-media case) and 50%+ longer / 50%+ shorter without blocking;
    extreme ~5x mismatch proceeds with a non-blocking warning; structural
    validation (missing audio stream) still blocks; auto-TTS keeps the strict
    tolerance.
  - `test_voiceover_unify.py`: a subtitle serial with no voiceover timestamp
    raises `VoiceoverAlignmentError`; complete coverage passes; the check is
    skipped when `subtitles_hi.json` is absent (backward compat).
  - `test_voiceover_upload.py`: zero-duration audio raises
    `VoiceoverAlignmentError`; the equal_split serious fallback is surfaced as
    a non-blocking warning.
- Full suite: **395 tests OK** (was 382; +13).
- Tag: `manhwa-video-dubber-v6-duration-check-removed`.

## [E7] — 2026-08-15 — cascade-crash fix: subtitle collision clusters are redistributed (no more zero-length ffmpeg cuts)

Real-media QA whole-job crash, reported from job
`97a9b90e-71f4-4d64-931d-b1b5cd194ce2`:

```
subtitle serial 44 overlap: start 60.000s clamped to previous end 100.000s
subtitle serial 44 zero/negative duration after clamp (start 100.000s, original end 61.000s)
... (serial 45-70 the same)
edit_guideline: serial 44 invalid duration (source 0.000s, target X.Xs); using pts_multiplier 1.0
auto_cut: cutting serial 44 [100.000..100.000] pts x1.0000
ffmpeg: -to value smaller than -ss; aborting
ERROR app: user audio pipeline failed for job ...
```

A long raw subtitle entry ending at 100s followed by 27+ entries whose raw
starts (60..86s) all collided with it. `subtitle_builder._serialize` clamped
each one to `prev_end` one at a time, so every entry collapsed to the same
zero-length `[100.000..100.000]` timestamp. That leaked through to
`edit_guideline.json` as `source_duration = 0` and to `auto_cut`, where ffmpeg
aborted on the zero/negative `-to` window and the whole job died (the user only
saw "Something went wrong" plus the ffmpeg version banner).

Three fixes:

- **Fix B (root cause) — `pipeline/subtitle_builder.py`**: runs of
  `SUBTITLE_COLLISION_CLUSTER_MIN_COUNT`+ (default 3) consecutive raw entries
  that collide with the running end cursor are now detected during
  serialization and redistributed as one cluster via
  `_redistribute_collision_cluster` — each entry gets a **non-zero** duration,
  text-length-weighted, inside `[window_start, next_anchor]` when there is
  room, otherwise a per-entry fallback minimum
  (`SUBTITLE_MIN_SERIAL_DURATION_SEC = 0.8s`). Small 1-2 entry overlaps keep
  the legacy clamp. New `detect_collision_clusters()` flags 3+ collision runs
  as a distinct QA diagnostic (`reason: "collision_cluster"`) in
  `subtitle_qa.json` (and surfaced in `subtitle_verify.py`'s whisper
  cross-check result), separate from `duplicate_clusters`.
- **Fix A (crash guard) — `pipeline/auto_cut.py`**: before invoking ffmpeg,
  a source segment whose duration is `<= RENDER_MIN_SEGMENT_DURATION_SEC`
  (0.05s) is replaced by a minimal real window clamped inside the source and
  stretched (`setpts`) to the target duration, so `-to value smaller than -ss`
  can never happen and the draft timeline stays intact.
- **Fix C (readable errors) — `pipeline/auto_cut.py`**: `_extract_ffmpeg_error`
  pulls the actual error line out of the ffmpeg stderr dump (skipping the
  `ffmpeg version`/`configuration` banner) so the user-facing message is e.g.
  `ffmpeg error: -to value smaller than -ss; aborting` instead of the banner.
- **Files**: `pipeline/subtitle_builder.py`, `pipeline/auto_cut.py`,
  `pipeline/subtitle_verify.py`, `pipeline/config.py` (new
  `RENDER_MIN_SEGMENT_DURATION_SEC`, `SUBTITLE_COLLISION_CLUSTER_MIN_COUNT`,
  `SUBTITLE_MIN_SERIAL_DURATION_SEC`).
- **Regression tests** (+16):
  - `pipeline/tests/test_cascade_crash_regression.py` drives the exact
    reported pattern (a 100s-spanning entry + 27 colliding entries) through
    subtitle_builder → edit_guideline → auto_cut (ffmpeg mocked) and asserts
    (ক) no zero/negative-duration entry in subtitle_builder output, (খ)
    auto_cut never throws, (গ) the job completes `status: "ok"` with every
    clip cut using a strictly positive `[start, end)` window.
  - `pipeline/tests/test_subtitle_builder.py`
    (`CollisionClusterRedistributionTest`): 27-entry run redistributed over
    `[100, 121.6]` with the 0.8s minimum each; text-length-weighted
    redistribution into an anchor window; 2-entry overlaps keep the legacy
    clamp; `DetectCollisionClustersTest` (flag/floor/config);
    `CollisionClusterQaTest` (QA artifact carries `collision_clusters`).
  - `pipeline/tests/test_auto_cut.py`: `FfmpegErrorExtractionTest`
    (banner-free error line); `DegenerateSegmentTest` (zero-window segment is
    cut as a minimal real window stretched to the target, healthy segments
    unaffected).
- Full suite: **382 tests OK** (was 366; +16).
- Tag: `manhwa-video-dubber-v6-cascade-crash-fix`.

## [E6] — 2026-08-15 — draft duration-validation fix: validate against the source video (single source of truth) + loose user_upload tolerance

Real-media QA audio-upload validation bug, reported from job
`705fea53-129e-4cfe-bc75-0e49ef356305` (voice source `user_upload`):

```
draft video validation failed for job 705fea53-129e-4cfe-bc75-0e49ef356305:
{'has_video': True, 'has_audio': True, 'duration_sec': 526.228451,
 'expected_duration_sec': 504.517, 'tolerance_sec': 0.1, 'duration_ok': False}
```

The same job's `subtitle_qa.json` reported `total_duration_sec: 303.021` — a
different number for the same source video (also confirmed by the user: ~5min
3s). Two distinct problems:

- **Problem 1 — `expected_duration_sec` from the wrong source**
  (`pipeline/auto_cut.py` + duplicated in `pipeline/review.py`): the draft
  validation compared the rendered draft against the **voiceover audio's**
  ffprobe duration (`_probe_duration(voiceover_hi.wav)`), which for a
  human-recorded voiceover can be a completely different number from the
  source video's real length. So the same job produced two unrelated
  durations (303s vs 504s). Fix: `expected_duration_sec` now comes from the
  **source video** via `auto_cut._source_duration` — it reads
  `job_meta.json` (written by `video_ingest` from an ffprobe of
  `source.mp4`) with a direct ffprobe fallback. This is the exact same
  origin `subtitle_builder` uses for `subtitle_qa.json`'s
  `total_duration_sec`, so the validation and the subtitle diagnostics now
  share one source of truth and always agree.
- **Problem 2 — tolerance unrealistically strict for user uploads**: the
  tolerance was `RENDER_TOLERANCE_FRAMES` (3 source frames ≈ 0.1s) — fine for
  auto-TTS (clip durations are measured/precise) but impossible for a
  human-recorded voiceover with natural pacing variance. Fix: a new
  configurable loose tolerance is applied only when
  `voice_source == "user_upload"` — the larger of
  `config.USER_UPLOAD_DURATION_TOLERANCE_SEC` (3.0s) and
  `config.USER_UPLOAD_DURATION_TOLERANCE_RATIO` (0.05 × source duration). The
  auto-TTS / unset path keeps the strict frames tolerance. A large mismatch
  (e.g. 20+s on a ~5min video) still fails — a strong wrong-file signal.
- **Files**: `pipeline/auto_cut.py` (`_source_duration`,
  `_draft_validation_tolerance`, `build_draft_video` now reports
  `expected_duration_sec` + `voice_source`), `pipeline/review.py`
  (`apply_clip_edit` re-splice validation uses the same helpers),
  `pipeline/config.py` (new `USER_UPLOAD_DURATION_TOLERANCE_SEC` /
  `USER_UPLOAD_DURATION_TOLERANCE_RATIO`).
- **Regression tests** (`pipeline/tests/test_auto_cut.py`,
  `DurationValidationRegressionTest`, +6):
  - auto-TTS and user_upload paths: `expected_duration_sec` equals the
    draft's real ffprobe duration (a correct draft lands on the source
    duration).
  - `expected_duration_sec` reads `job_meta.json` (single source of truth,
    matching `subtitle_qa.json`'s `total_duration_sec`) even when a direct
    source probe would disagree.
  - user_upload accepts a few seconds of variation (2.5s on a 60s video).
  - user_upload rejects a large mismatch (25s) and the raised error reports
    the **source** duration as `expected_duration_sec`.
  - auto-TTS keeps the strict tolerance (a 3s drift is rejected).
  - Existing orchestration/chain/review fixtures updated so their mocked
    draft duration matches the mocked/real source duration (the validation
    base changed from voiceover to source).
- Full suite: **366 tests OK** (was 360; +6).
- Tag: `manhwa-video-dubber-v6-duration-validation-fix`.

## [E5] — 2026-08-15 — real-media QA-writeback fix: single zero-duration subtitles now repaired instead of leaking into the final SRT

Root-cause fix + regression tests. Real job
`edb1b1ef-5041-491e-bb3f-c8aa3617794a` reported `subtitle_qa.json` with
`"repair": {"attempted": 2, "succeeded": 2}` yet the delivered
`subtitles_hi.srt` still contained zero-duration blocks (serial 89 at
`00:01:59,000`, serial 154 at `00:03:30,000` sharing its start with serial 155).

- **Root cause** (`pipeline/subtitle_builder.py`): `detect_duplicate_clusters`
  only flagged runs of ≥ `SUBTITLE_DUP_CLUSTER_MIN_COUNT` (default 3)
  degenerate entries. A *single* zero-duration entry (or a pair) was never
  flagged, so `build_subtitle_list`'s auto-repair never re-extracted those
  windows; the zero-duration timestamps flowed unchanged through
  `subtitles_zh.json` → `translator.build_srt` → `subtitles_hi.srt`, while the
  post-repair diagnostics (also ≥3-gated) misleadingly reported
  `duplicate_clusters: []`. The reported repair success referred to *other*
  flagged gaps/clusters, not the leaked zero-duration entries — so the QA
  artifact claimed repaired entries the SRT never showed.
- **Fix**: zero-duration runs are now flagged even as a single entry
  (`if zero_duration or count >= min_count`). Same-start-timestamp runs keep
  the ≥3 floor. `pipeline/config.py` comment updated to document the new
  semantics.
- **Regression tests** (`pipeline/tests/test_subtitle_builder.py`, +4):
  - `DetectDuplicateClustersTest`: single zero-duration flagged (count 1);
    pair flagged (count 2); zero-duration followed by same-start non-zero
    flags the zero-duration entry.
  - `RepairSrtWritebackRegressionTest.test_single_zero_duration_leaks_nothing_into_final_srt`:
    end-to-end — synthetic gap-free raw input with a single zero-duration +
    same-start pair, repair mocked to succeed, then the real
    `translator.translate_subtitles` writes `subtitles_hi.srt`; asserts repair
    reported success, the intermediate `subtitles_zh.json` has no zero-duration
    entries, and the final `.srt` file has no zero/negative-duration and no
    duplicate start-times. Verified it FAILS against the pre-fix code (repair
    never triggered).
- Full suite: **360 tests OK** (was 356; +4).
- Tag: `manhwa-video-dubber-v6-qa-repair-writeback-fix`.

## [E4] — 2026-08-15 — final wrap-up + `FINAL_SUMMARY.md` subtitle-QA-fix section

Final chunk of group E / of the whole subtitle-QA-fix plan (A1→E4,
`docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). Docs-only; no code changes.

- `docs/FINAL_SUMMARY.md`: new "Subtitle QA Fixes (A1-E4)" section covering the
  6 original bugs → group mapping (A/B/C/D/E), the new files
  (`pipeline/subtitle_verify.py`, `pipeline/subtitle_qa.py`), the new config
  constants (`SUBTITLE_GAP_FLAG_THRESHOLD_SEC`, `SUBTITLE_DUP_CLUSTER_MIN_COUNT`,
  `SUBTITLE_MAX_REPAIR_ATTEMPTS`, `SUBTITLE_COVERAGE_MISMATCH_RATIO`,
  `LONG_VIDEO_CHUNK_THRESHOLD_SEC` 600→90), the new artifacts
  (`subtitle_qa.json`, `subtitle_qa_whisper.json`), the new route
  `GET /download/{job_id}/subtitle_qa`, the known limitations (single-pass
  repair; non-gap/cluster misplaced-content like hallucination-suspect sections
  still need manual review; whisper optional), and the explicit "user must do
  this" real-media QA run (real Gemini key + real ~5-6 min dialogue-dense
  video, verifying the 90s chunking, the repair mechanism, and whisper
  false-positives).
- `docs/HANDOFF_NEXT.md`: groups A-E complete; only the user's own real-media
  QA run remains.
- Final suite: **356 tests OK** (no new tests — docs-only chunk).
- Final tag: `manhwa-video-dubber-v6-qa-final`.

## [E3] — 2026-08-15 — full regression pass across groups A-E + fixes

Third chunk of group E (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`).
Verification-only — no new features, no production code changes.

- **Full suite** (`python3 -m unittest discover -s pipeline/tests`): **356
  tests OK** across groups A→E (A1/B1/B2/B3/B4/C1/C2/D1/D2/D3/E1/E2 plus the
  whole pre-existing S1–F3 suite). Ran the full suite 3× total in this chunk's
  verification window — every run 100% pass, no order-dependency/flakiness.
  No regressions were found, so no bugfix was required.
- `python3 -m py_compile` passed for every touched file: `app.py`,
  `pipeline/config.py`, `pipeline/subtitle_builder.py`,
  `pipeline/subtitle_extract.py`, `pipeline/subtitle_verify.py`,
  `pipeline/subtitle_qa.py`.
- **End-to-end sanity** (mocked Gemini/Whisper/ffmpeg, existing
  `test_full_auto_orchestration.py` style) confirmed both headline user paths
  still work: `test_auto_tts_zero_click_end_to_end` (upload → auto TTS → draft
  → final, no clicks) and `test_user_upload_single_pause_end_to_end` (upload →
  one pause at voice-source choice → user upload → final) both pass unchanged.
- Final test count: **356 tests OK** (was 290 before groups A–E began).

## [E2] — 2026-08-15 — wire QA summary banner into `/voiceover/{id}/choose` (non-blocking) + `subtitle_qa.json` download route

Second chunk of group E (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`).

- `app.py` `voiceover_choose_page()` (GET `/voiceover/{job_id}/choose`) now
  calls `subtitle_qa.build_qa_summary(job_id)` before rendering. When
  `qa_status == "flagged"` a `.flagged-banner` (same style as `review.py`) is
  shown with: a short headline, each `qa["warnings"]` item as an `<li>`, a
  link to download `subtitle_qa.json`, and an explicit note that it is
  informational only — the auto_tts / user_upload choice buttons still render
  unchanged, no confirmation step is added. `qa_status == "ok"` renders the
  page exactly as before. The whole banner block is wrapped in a defensive
  `try/except` so a `build_qa_summary` failure still serves the page
  (non-blocking; E1's never-raise contract is guarded regardless).
- New route `GET /download/{job_id}/subtitle_qa` → serves
  `uploads/<job_id>/subtitle_qa.json` (404 when missing), in the same pattern
  as the existing `download_voiceover_upload` route.
- New `SubtitleQaBannerTest` in `pipeline/tests/test_app_orchestration.py`
  (6 tests): `ok` → no banner; `flagged` → banner + warnings text + download
  link + both choice forms intact (informational only); `build_qa_summary`
  raising → page still 200 without banner; download route 200 with
  `application/json` when the file exists and 404 when missing.
- Existing orchestration tests (incl. the auto_tts zero-click path in
  `test_full_auto_orchestration.py`, which never calls `/choose`) pass
  unchanged. `python3 -m py_compile app.py` passes.
- Full suite: **356 tests OK** (350 prior + 6 new).

## [E1] — 2026-08-15 — `pipeline/subtitle_qa.py` combined human-readable QA summary

First chunk of group E (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). New
standalone module only — no wiring yet (that is E2).

- New `pipeline/subtitle_qa.py` `build_qa_summary(job_id, upload_root)`:
  pure aggregation (no Gemini/Whisper calls) that merges the mechanical
  diagnostics from `subtitle_qa.json` (A3/B3: `gaps`,
  `duplicate_clusters`, `repair` summary) with the independent cross-check
  from `subtitle_qa_whisper.json` (D1). Returns `{job_id, qa_status
  ("ok"|"flagged"), warnings[], gaps_remaining, duplicate_clusters_remaining,
  repair_attempted, repair_succeeded, whisper_check_status}`. `flagged` when
  gaps/clusters remain or the whisper check is a mismatch; each flag gets one
  short Bengali, non-technical warning line. Never raises — a missing or
  malformed file contributes no flags (whisper absent → `"skipped"`), and the
  behaviour is documented in the module docstring.
- New `pipeline/tests/test_subtitle_qa.py` (7 tests): clean files → `ok` with
  empty warnings; gaps+clusters → `flagged` with the right warning lines and
  repair counts; whisper mismatch → `flagged` and mentioned in warnings; both
  files missing / one malformed / whisper missing with gaps present — no raise
  and the most reasonable status.
- Full suite: **350 tests OK** (343 prior + 7 new).

## [D3] — 2026-08-15 — whisper cross-check regression pass + edge-case coverage

Final chunk of group D (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`).
Verification-only; no production code changed.

- **Real-environment smoke test (passed)**: installed `openai-whisper` with
  CPU-only torch (`--index-url https://download.pytorch.org/whl/cpu`) — the
  default CUDA torch pulls several GB of `nvidia-*` wheels and was too slow to
  finish, so the CPU build was used instead. Ran the real
  `whisper_cross_check()` against a synthetic 5s audio-only `source.mp4`
  (ffmpeg-generated sine tone) with a `subtitle_qa.json` fixture: it did not
  raise, returned a reasonable dict (`status: "ok"`, `coverage_ratio: null`
  because the tone contains no speech segments, `mismatch: false`), downloaded
  the `base` model into `~/.cache/whisper/base.pt`, and wrote
  `uploads/smoke-job/subtitle_qa_whisper.json`. `openai-whisper` is **not**
  added to `requirements.txt` — it stays an optional lazy dependency by design
  (D1); environments without it take the `skipped` path.
- **Full regression pass, no flakiness**: the whole suite (groups A, B, C, D)
  ran 4 times — 3× with whisper absent, 1× with whisper installed — every run
  **343 tests OK** with stable timings; the mocked tests use
  `sys.modules["whisper"] = None` to force the ImportError path, so they are
  independent of whether whisper is really present.
- `python3 -m py_compile` passed for all group-D touched files: `app.py`,
  `pipeline/subtitle_builder.py`, `pipeline/subtitle_extract.py`,
  `pipeline/subtitle_verify.py`, `pipeline/config.py`.
- Final test count: **343 tests OK** (no new tests this chunk — verification only).

## [D2] — 2026-08-15 — wire `whisper_cross_check` into `app.py` upload pipeline (non-blocking, status-tracked)

Second chunk of group D (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`).

- `app.py` `_run_upload_pipeline()` now calls
  `subtitle_verify.whisper_cross_check(job_id, logger_=job_logging.get_job_logger(job_id))`
  right after `build_subtitle_list(...)` and before translation, wrapped in a
  defensive `try/except Exception` so the best-effort cross-check can never
  break the upload pipeline (bare `Exception` is intentional — the pipeline
  must survive any whisper/librosa-related failure). The job's per-job logger
  (via new `job_logging` import) captures the cross-check diagnostics.
- The `upload_pipeline` "done" status `extra` now carries
  `whisper_check_status` = the returned dict's `"status"` field, or
  `"skipped"` when the call raised.
- New tests in `pipeline/tests/test_app_orchestration.py`:
  - `whisper_cross_check` succeeds → status extra shows
    `whisper_check_status: "ok"`.
  - `whisper_cross_check` raises (`side_effect=RuntimeError`) → the pipeline
    still reaches `"done"` with `whisper_check_status: "skipped"`, no crash.
- Existing upload-pipeline orchestration tests (incl. `test_full_auto_orchestration.py`
  end-to-end) pass unchanged — with whisper not installed they naturally get
  the `skipped` path. `python3 -m py_compile app.py` passes.
- Full suite: **343 tests OK** (341 prior + 2 new).

## [D1] — 2026-08-15 — `pipeline/subtitle_verify.py` standalone local-Whisper coverage cross-check

First chunk of group D (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). New
standalone module only — no wiring yet (that is D2).

- New `pipeline/subtitle_verify.py` `whisper_cross_check(job_id, upload_root,
  logger_)`: extracts mono wav from `source.mp4` via ffmpeg (reuses
  `voiceover_upload._convert_to_wav`, same `TTS_SAMPLE_RATE`/mono PCM pattern),
  transcribes with local Whisper (`config.WHISPER_MODEL`, segment-level, no
  word timestamps, language auto-detect) and compares Whisper's measured
  spoken duration against the Gemini-extracted `covered_duration_sec` from
  `subtitle_qa.json` (via `subtitle_builder.load_subtitle_qa`). Returns a dict
  with `status` (`ok`/`skipped`/`mismatch`), `reason`
  (`whisper_not_installed`/`transcription_failed`), `whisper_spoken_sec`,
  `extracted_covered_sec`, `coverage_ratio` (None when spoken is 0), and
  `mismatch` bool. Never raises — Whisper missing / transcription / ffmpeg
  failure all return `skipped`. Result persisted to
  `uploads/<job_id>/subtitle_qa_whisper.json`.
- `pipeline/config.py` `SUBTITLE_COVERAGE_MISMATCH_RATIO = 0.75`: a mismatch
  is flagged when Gemini-extracted coverage is below this fraction of
  Whisper's independently-measured spoken duration.
- New `pipeline/tests/test_subtitle_verify.py` (6 tests): Whisper not
  installed (`sys.modules["whisper"] = None` → ImportError), transcription
  runtime failure, ffmpeg audio-extraction failure (all `skipped`, no raise),
  coverage ratio above threshold → `ok`, below threshold → `mismatch`, and
  `subtitle_qa_whisper.json` written with the full result dict.
- Full suite: **341 tests OK** (335 prior + 6 new).

## [C2] — 2026-08-15 — regression-verify the 90s chunking threshold + explicit `chunked=True` coverage test

Second and final chunk of group C (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`).
Verification + test-level coverage only; no production code changed (C1 made
the actual config change).

- Audited every real-ffmpeg fixture in `test_video_ingest.py`,
  `test_subtitle_extract.py`, `test_app_orchestration.py`, and
  `test_full_auto_orchestration.py`: all synthetic clips are 1–5 seconds,
  far under the new 90s threshold, so none silently fall onto the chunked
  (ffmpeg segment-cutting) path. The full suite passes unchanged.
- New test in `pipeline/tests/test_subtitle_extract.py`
  (`SubtitleExtractChunkTest.test_default_threshold_90s_chunks_150s_video`):
  with the production default `LONG_VIDEO_CHUNK_THRESHOLD_SEC=90` (no
  threshold mock), a 150s video (duration read directly from `job_meta.json`)
  extracts with `chunked=True` and `segments_count=2` — the exact case that
  used to be `chunked=False` under the old 600s default. Gemini and
  `_segment_video` are mocked.
- Full suite: **335 tests OK** (334 prior + 1 new).

## [C1] — 2026-08-15 — `LONG_VIDEO_CHUNK_THRESHOLD_SEC` 600s → 90s (always sub-chunk dialogue-dense short videos)

First chunk of group C (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). A
single config change, no production logic touched.

- `pipeline/config.py` `LONG_VIDEO_CHUNK_THRESHOLD_SEC` lowered from `600`
  to `90`, with a `DELIBERATE` comment (same style as `TTS_MODEL`) explaining
  the real-world failure that drove it: a ~5-6 minute dialogue-dense
  manhwa-dub video fell under the old 600s threshold, was sent to Gemini in
  a single call, and the model dropped an entire ~50-second/37-line
  dialogue-heavy block while mis-timing others into duplicate-timestamp
  clusters. Lowering to 90s forces even short videos through B1
  sub-chunking (with `SUBTITLE_OVERLAP_SEC` overlap + dedup), improving
  per-segment timestamp accuracy and reducing missed dialogue at the cost of
  more Gemini calls (still capped by `MAX_API_CALLS_PER_JOB`) and more
  ffmpeg segment-cutting time.
- Only the constant's value changed — `_segment_video()`,
  `_segment_ranges()`, and the chunked-decision logic are untouched; they
  read the constant at runtime. Confirmed via
  `grep -rn LONG_VIDEO_CHUNK_THRESHOLD_SEC pipeline/` (read-sites only).
- Full suite: **334 tests OK** (tests that exercise chunking already mock
  the constant to `2.0`; none depend on the old default).

## [B4] — 2026-08-15 — repair edge-case coverage (budget exhaustion, total failure, single-pass limitation)

Fourth and final chunk of group B (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`).
Verification + edge-case coverage; no production code changes.

- New tests in `pipeline/tests/test_subtitle_builder.py` (`RepairBudgetEdgeCaseTest`):
  - Budget exhausted mid-repair: a `CallBudget(1)` with two flagged ranges —
    the first repair consumes the only budget slot, the second
    `extract_window()` returns `None`, repair continues gracefully (no
    raise), and the summary reports `succeeded=1 / failed=1`.
  - All repair calls fail: every `extract_window()` returns `None` →
    `succeeded=0 / failed=N`, entries untouched, orchestration keeps running.
- New tests in `BuildSubtitleListAutoRepairTest`:
  - All-repair-failed end-to-end: `subtitle_qa.json` keeps the un-repaired
    diagnostics plus `repair.failed`, `subtitles_zh.json` keeps the original
    entries, and the pipeline never crashes.
  - **Single-pass limitation**: when the repair response itself contains a
    new duplicate timestamp cluster, the re-diagnose step surfaces it in
    `subtitle_qa.json["duplicate_clusters"]` but repair is **not** re-run
    (`extract_window` called exactly once). Recursive repair would be a
    future chunk.
- New test in `pipeline/tests/test_app_orchestration.py`: when
  `subtitles_hi.json` already exists, `_run_upload_pipeline()` takes the
  idempotent resume path and **does not** call `build_subtitle_list()`,
  `extract_subtitles()`, or `translate_subtitles()` (B3 wiring confirmed
  off the resume path).
- Known limitation (noted for the future): repair is single-pass — a repair
  that re-introduces flags is reported, not re-repaired.
- Full suite: **334 tests OK** (329 prior + 5 new).

## [B3] — 2026-08-15 — auto-repair wired into `build_subtitle_list()` + `app.py` upload pipeline (end-to-end, Subtitle QA Fix)

Third chunk of group B (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). Wires
the B2 repair orchestration into the normal build path so every job
automatically self-heals flagged gaps/clusters.

- `pipeline/subtitle_builder.py` `build_subtitle_list()` signature extended
  to `build_subtitle_list(job_id, upload_root=None, call_budget=None,
  auto_repair=True)` (backward-compatible defaults). When `auto_repair` is
  true and A3 diagnostics flag any gaps/duplicate clusters, it calls
  `repair_flagged_regions()`, rebuilds the entries, and **re-diagnoses** the
  repaired list; the `"repair"` summary is added to the `subtitle_qa.json`
  artifact. Diagnostics always reflect the final list. Return value stays the
  serialized entries list (backward compat).
- `call_budget` is forwarded through to `repair_flagged_regions()` →
  `extract_window()`, so the repair shares the job's per-job CallBudget.
- New internal helper `_entries_from_serialized()` converts serialized
  entries back to raw dicts for re-serialization.
- `pipeline/subtitle_builder.py` `_build_repair_ranges()` clamps negative
  duplicate-cluster window starts to `0.0` (defensive).
- `app.py` `_run_upload_pipeline()` now calls
  `build_subtitle_list(job_id, call_budget=budget)`, sharing the same
  per-job CallBudget already passed to `extract_subtitles()` and
  `translate_subtitles()`.
- New tests in `pipeline/tests/test_subtitle_builder.py`
  (`BuildSubtitleListAutoRepairTest`, 3 tests): flagged input auto-repairs
  and re-diagnoses (repair summary present, fixed entry inserted);
  `auto_repair=False` skips repair and keeps raw diagnostics; clean input
  never calls `extract_window`.
- Full suite: **329 tests OK** (326 prior + 3 new).

## [B2] — 2026-08-15 — `subtitle_builder.repair_flagged_regions()` — bounded targeted-repair orchestration (Subtitle QA Fix)

Second chunk of group B (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). Adds
the bounded repair orchestration that consumes A3 diagnostics and drives
B1's `extract_window`.

- New `pipeline/subtitle_builder.py` function `repair_flagged_regions(job_id,
  entries, diagnostics, upload_root=None, call_budget=None, logger_=None,
  max_attempts=None)` — builds a merged, weight-ordered list of time ranges
  from `diagnostics["gaps"]` and `["duplicate_clusters"]` (gaps use their
  exact window; clusters span first/last entry ± half
  `SUBTITLE_OVERLAP_SEC` padding; overlapping ranges merged), calls
  `subtitle_extract.extract_window()` for at most `max_attempts` ranges
  (largest `gap_sec`/`count` first, default
  `config.SUBTITLE_MAX_REPAIR_ATTEMPTS = 3`), replaces raw entries
  overlapping each repaired window with the fresh absolute-timed subtitles,
  and re-serializes. Returns `(repaired_entries, repair_summary)` with
  `{"attempted", "succeeded", "failed", "skipped_budget": [ranges]}`. Never
  raises — a failing range is skipped, remaining ranges still run, and
  beyond-budget ranges land in `skipped_budget`.
- `call_budget` is forwarded to `extract_window()` so repair shares the
  job's per-job CallBudget (no unlimited calls).
- New `pipeline/config.py` constant `SUBTITLE_MAX_REPAIR_ATTEMPTS = 3`.
- New tests in `pipeline/tests/test_subtitle_builder.py`
  (`RepairFlaggedRegionsTest`, 8 tests): gap repaired inserts new entries;
  duplicate cluster replaced; `extract_window` `None` → range untouched and
  `failed == 1` without raising; >`max_attempts` flags run largest-first and
  rest land in `skipped_budget`; overlapping ranges merged into one call;
  no flags → entries unchanged and `extract_window` never called;
  `call_budget` forwarded; default `max_attempts` reads config.
- Full suite: **326 tests OK** (318 prior + 8 new).

## [B1] — 2026-08-15 — `subtitle_extract.extract_window()` — targeted time-range re-extraction (standalone, Subtitle QA Fix)

First chunk of group B (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). Adds
the windowed re-extraction primitive the repair pipeline will consume.

- New `pipeline/subtitle_extract.py` function `extract_window(job_id,
  start_sec, end_sec, upload_root=None, call_budget=None, logger_=None)` —
  cuts `[start_sec, end_sec)` from `source.mp4` via ffmpeg with the exact
  `_segment_video()` pattern (`-ss` / `-to` / `-c copy`, no re-encode),
  writes the clip under `job_dir/repair_segments/`, sends it to Gemini
  through `call_with_rotation` (key-rotation / content-block resilience
  preserved), and returns a subtitle list with absolute timing (offset
  `start_sec` added back). Never raises: returns `None` on missing source,
  no active keys, ffmpeg failure, rotated-Gemini failure, or malformed
  response. Standalone — not wired into `build_subtitle_list()` or app.py
  yet (that is B2/B3).
- New tests in `pipeline/tests/test_subtitle_extract.py`
  (`ExtractWindowTest`, 6 tests): success applies absolute offset (relative
  Gemini times become `start_sec`-shifted absolute times); all-keys-fail →
  `None` without raising; malformed JSON → `None` without raising; ffmpeg
  call uses `-ss`/`-to`/`-c copy` and writes under `repair_segments/`;
  ffmpeg failure → `None` (Gemini never called); missing source → `None`.
- Full suite: **318 tests OK** (312 prior + 6 new).

## [A3] — 2026-08-15 — Wire gap/duplicate-cluster diagnostics into `build_subtitle_list()` → `subtitle_qa.json` (Subtitle QA Fix)

Third and final chunk of group A (plan: `docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`).
Wires the A1/A2 standalone diagnostics into the builder and writes a
per-job QA artifact.

- `pipeline/subtitle_builder.py` `build_subtitle_list()`: after serializing,
  calls `detect_gaps(result)` and `detect_duplicate_clusters(result)`,
  computes `covered_duration_sec = max(0.0, duration - sum(gap_sec))`, and
  writes a diagnostics dict to `job_dir/subtitle_qa.json` (same
  `json.dumps(..., ensure_ascii=False, indent=2)` style as
  `subtitles_zh.json`). Return value unchanged — still the serialized entries
  list, so app.py and existing tests need no changes (backward compat).
- New helper `load_subtitle_qa(job_id, upload_root=None)` — reads
  `subtitle_qa.json`; missing/malformed/non-dict input returns a default dict
  (`gaps: []`, `duplicate_clusters: []`, ...), never raises. Intended for
  groups B and E.
- New tests in `pipeline/tests/test_subtitle_builder.py`
  (`SubtitleQaArtifactTest`, 3 tests; `LoadSubtitleQaTest`, 4 tests):
  gap+cluster fixture writes correct `subtitle_qa.json` while the return
  value stays the same; clean fixture writes empty lists; `load_subtitle_qa`
  reads existing files and returns defaults for missing / malformed /
  non-dict JSON without raising. Existing builder tests unchanged
  (regression).
- Full suite: **312 tests OK** (305 prior + 7 new).

## [A2] — 2026-08-15 — Duplicate/degenerate-timestamp cluster detection + `_serialize()` zero-duration logging (Subtitle QA Fix)

Second chunk of the subtitle-QA-fix chain (plan:
`docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`). Adds degenerate-timestamp cluster
diagnostics and zero-duration logging to the serializer.

- New `pipeline/subtitle_builder.py` function `detect_duplicate_clusters(
  serialized_entries, min_count=None)` — flags consecutive post-`_serialize`
  runs sharing the same rounded `start_sec` or being zero-duration
  (`start_sec == end_sec`), returning serial-ordered
  `{"start_serial", "end_serial", "start_sec", "count",
  "reason": "same_start_timestamp" | "zero_duration"}` dicts. A run matching
  both reasons is reported as `"zero_duration"` (more severe takes
  precedence). Pure Python, no network, no side effects. Not yet wired into
  `build_subtitle_list()` (that is chunk A3).
- `_serialize()`: two new distinct `logger.warning` lines — one for
  clamp-induced zero/negative duration (`"zero/negative duration after
  clamp"`), one for raw zero-duration input entries. Existing overlap-clamp
  warning message/behavior unchanged (backward-compat).
- New `pipeline/config.py` constant `SUBTITLE_DUP_CLUSTER_MIN_COUNT = 3`.
- New tests in `pipeline/tests/test_subtitle_builder.py`
  (`DetectDuplicateClustersTest`, 8 tests; `SerializeZeroDurationLoggingTest`,
  2 tests): no-cluster case; same-start cluster; zero-duration cluster with
  distinct reason; below-min-count run not flagged; multiple clusters in
  serial order; custom `min_count` override; config-default min_count;
  zero-duration precedence in mixed runs; `assertLogs` verifies both new
  zero-duration warnings fire; existing overlap-clamp tests still pass
  (regression).
- Full suite: **305 tests OK** (295 prior + 10 new).

## [A1] — 2026-08-15 — Coverage-gap diagnostics (Subtitle QA Fix)

Introduces standalone coverage-gap diagnostics to detect consecutive subtitle timing gaps exceeding a threshold.

- `pipeline/config.py`: Added `SUBTITLE_GAP_FLAG_THRESHOLD_SEC` config constant (default: `6.0`).
- `pipeline/subtitle_builder.py`: Implemented `detect_gaps(serialized_entries, threshold_sec=None)` to chronologically list timing gaps between consecutive entries.
- `pipeline/tests/test_subtitle_builder.py`: Added 5 unit tests verifying gap detection correctness, boundary conditions, custom threshold overrides, and chronological order under `SubtitleGapDetectionTest`.
- Test suite: Expanded from 290 to 295 tests, 100% passing.

## [FA-F2] — 2026-08-13 — Final wrap-up: Full-Auto Pipeline complete (Full-Auto Pipeline)

The final Full-Auto chunk — all 12 chunks (FA-A1 … FA-F2) are done,
committed, pushed and tagged (`chunk-FA-<id>-done`, plus the final
`manhwa-video-dubber-v6-full-auto-final`).

- `docs/FINAL_SUMMARY.md`: added the "## Full-Auto Pipeline (FA1-F2)" section
  (not a replacement) — the zero-click / single-pause summary, what was added
  and where (`pipeline/full_auto_chain.py` new; `app.py` stage names
  `auto_full_render` / `user_audio_pipeline`), and the clear note that the
  real-media QA can only be done by the user: (a) real-Gemini zero-click
  auto_tts run, (b) real single-pause user_upload run, (c) output-quality
  spot-check against the pre-FA U-series output.
- `docs/HANDOFF_NEXT.md`: "all FA chunks (A1-F2) complete — the PRD's core
  requirement (upload → zero-click final video, and stop only at the audio
  upload when the user supplies their own audio) is implemented and
  regression-tested; only the user's own real-media QA run remains."
- Final state: **290 tests, 100% pass**.

## [FA-F1] — 2026-08-13 — Full regression pass (Full-Auto Pipeline)

Verification-only chunk: the entire suite (U0-U5 plus everything from FA-A1
through FA-E2) passes with no regressions and no fixes required.

- `python3 -m unittest discover -s pipeline/tests -v` — **290 tests, 100%
  pass (OK)**. No regression, no bugfix needed, no new feature added.
- `python3 -m py_compile` on the touched files — OK: `app.py`,
  `pipeline/full_auto_chain.py`, `pipeline/voiceover_unify.py`.
- `python3 -m pipeline.dry_run_check --job-id <fixture>` (U5 tool) on a job
  carrying the new-flow artifacts (FA-D4/E1) — exit 0, "RESULT: OK — no
  blocking errors". The tool is compatible with the new flow because artifact
  names/locations did not change across the FA chunks.
- Final test count documented: 290.

## [FA-E2] — 2026-08-13 — Permanent E2E regression tests for both full-auto paths (Full-Auto Pipeline)

Adds two permanent end-to-end regression tests that lock in the PRD's core
claim (zero-click auto path, single-pause upload path) and prove the old
manual routes are never needed on either path.

- New `pipeline/tests/test_full_auto_orchestration.py` — HTTP-only
  (`TestClient`), mocked Gemini/ffmpeg, D2 real-ffmpeg-silence pattern (same
  deterministic setup as the G1 orchestration test):
  - `test_auto_tts_zero_click_end_to_end` — POST /upload (auto_tts) → only
    `GET /api/jobs/{id}/status` is polled (no other endpoint) → asserts the
    stage-through `upload_pipeline → auto_full_render` → `GET /download/{job_id}`
    serves the final video. A recording client proves `/choose`,
    `/align_uploaded` and `/final` are never requested.
  - `test_user_upload_single_pause_end_to_end` — POST /upload (user_upload)
    → stops at `upload_pipeline`/`done` (no auto-continue, no final video) →
    POST `/voiceover/{job_id}/upload` (fake wav) → `user_audio_pipeline`
    done → final video downloadable. The same recording client proves
    `/choose`, `/align_uploaded` and `/final` are never requested — the only
    pause is the audio upload.
- Hard constraint honored: the old `test_app_orchestration.py` (G1) is not
  modified at all — it still passes on its own alongside the new E2E tests as
  the backward-compat proof.
- Full suite: **290 tests OK** (288 prior + 2 new).

## [FA-E1] — 2026-08-13 — Backward-compat audit + fixes (Full-Auto Pipeline)

Verification chunk: confirms the old manual routes still coexist with the new
full-auto pipeline, and fixes the one real bug the audit found.

- Audit result (documented in `docs/HANDOFF_NEXT.md`, checklist item by item):
  1. Manual voice-source override is resumable/idempotent — **fixed a bug**.
     `upload_status_page()` used to gate on the flat `stage`/`state` fields,
     which caused an infinite redirect loop after an override in two cases:
     (a) auto_tts chain already ran (`auto_full_render`/`done`), then the user
     overrode to `user_upload` — the page polled `upload_pipeline` forever;
     (b) a job that stopped at `upload_pipeline`/`done` was overridden to
     `auto_tts` — the page polled `auto_full_render`, a stage that never
     starts. The polling JS redirects whenever the *overall* state is `done`,
     so both looped.
  2. All old manual routes still work by direct URL — passed (covered by the
     existing G1 orchestration test + `test_voiceover_upload` +
     `test_voiceover_auto` + `test_render_final` + `test_review`).
  3. Entire existing test suite (U0-U5) passes unchanged — passed.
  4. `pipeline/dry_run_check.py` (U5) works with the new flow — passed
     (artifact names/locations unchanged; a fixture job exits 0).
- `app.py` `upload_status_page()` fixes:
  - Branch decisions now use the per-stage history (`stages.<stage>.state`)
    instead of the flat `stage`/`state` fields, so the page never polls a
    stage that is already done or was superseded by a later one.
  - For `voice_source == "auto_tts"`: when `auto_full_render` never started
    but `upload_pipeline` finished (a manual override to auto_tts after
    upload), the page resumes the chain itself via
    `_start_stage(job_id, "auto_full_render", _run_auto_full_render)` so it
    converges to the final video instead of looping.
- `app.py` refactor: the FA-C1 same-thread chain block is factored into
  `_run_auto_full_render(job_id)` (still runs on the upload thread — no new
  spawn, the FA-C1 hard constraint is preserved) so the upload path and the
  resume path share one implementation.
- New `pipeline/tests/test_backward_compat_audit.py`: +4 HTTP tests locking in
  the audit findings — override-to-user_upload after auto render shows the
  audio form directly and re-renders on audio upload; re-override to auto_tts
  is idempotent (no extra F3 render, final video preserved); override from
  user_upload to auto_tts resumes to a downloadable final video; the old
  manual routes still work end-to-end by direct URL.
- Full suite: **288 tests OK** (284 prior + 4 new).

## [FA-D2] — 2026-08-13 — Audio upload auto-continues to final video (single pause point complete, Full-Auto Pipeline)

Group D done — the PRD's core requirement is now fully implemented: the
user_upload path has exactly one stop (the audio upload), after which the job
auto-continues to the final video.

- `app.py` `upload_voiceover()`: after `save_uploaded_voiceover(...)` succeeds,
  instead of the old "Voiceover saved — Align subtitles" page it now writes
  `user_audio_pipeline`/`running` and starts a daemon thread
  (`_start_stage(job_id, "user_audio_pipeline", _run_user_audio_pipeline)`)
  that runs `full_auto_chain.run_user_upload_chain(job_id)` (D3 → D4 → E1 →
  E2 → F3) and persists `done` (with the result) or `error` (friendly detail).
  It returns the existing `_polling_page(..., "user_audio_pipeline")`.
- `app.py` `upload_status_page()`: for the user_upload path, when
  `user_audio_pipeline` is `done` it renders the final video player +
  download link via the shared `_render_chain_final_result()` adapter (the
  same one FA-C2 uses for `auto_full_render`); while `running` it shows the
  polling page.
- Hard constraints honored: `/voiceover/{job_id}/align_uploaded` route is NOT
  deleted (manual re-align still works, verified by the existing
  `test_align_page_*` tests); the thread never dies on uncaught exceptions
  (try/except + `except Exception` per daemon-thread convention); the existing
  `UnsupportedAudioError` format/size validation is unchanged.
- Tests: +1 HTTP end-to-end in `test_full_auto_upload.py` — POST /upload
  (user_upload) → poll B1/B2/C1 → POST /voiceover/{id}/upload (fake wav) →
  poll `user_audio_pipeline` → `GET /download/{job_id}` returns content. Only
  those endpoints are used. The old `test_voiceover_upload.py` upload-page
  test now asserts the auto-continue polling page.
- Full suite: **284 tests OK** (283 prior + 1 new; 1 updated).

## [FA-D1] — 2026-08-13 — Post-upload page shows audio-upload form directly for user_upload (Full-Auto Pipeline)

Removes the redundant "choose voiceover source" click on the user_upload path
(the choice was already persisted at upload time, FA-A1).

- `app.py` `upload_status_page()` — `user_upload` branch: the "Continue:
  choose voiceover source" link is replaced by the audio-upload form directly
  (SRT/TXT reference links + `<form action="/voiceover/{job_id}/upload">`),
  reusing the markup from `voiceover_choose()`'s user_upload branch.
- Hard constraint honored: `/voiceover/{job_id}/choose` route is NOT deleted
  (manual override / backward-compat still works via direct URL); the
  `/voiceover/{job_id}/upload` POST handler is untouched (that's FA-D2).
- `pipeline/tests/test_full_auto_upload.py`: the FA-C2 user_upload page test
  is updated to the FA-D1 behavior — `GET /upload/{job_id}` shows the audio
  `<form>` directly (action + multipart), with NO "choose voiceover source"
  link and no `<video>`.
- Full suite: **283 tests OK** (unchanged count — test updated, not added).

## [FA-C2] — 2026-08-13 — Upload status page shows final video directly for auto_tts (Full-Auto Pipeline)

Group C done: an auto_tts upload now ends on the final video page with zero
clicks.

- `app.py` `upload_status_page()`: branches on
  `voiceover_unify.get_voice_source(job_id)` —
  - `auto_tts`: stays on the existing `_polling_page(...)` (only the target
    stage changes to `auto_full_render`), then once that stage is `done`
    renders the final video player + download link directly. Reuses
    `_render_final_result()` (which now accepts an optional `result` dict —
    the chain's `auto_full_render.result.final` payload is passed in as a
    small adapter).
  - `user_upload` / `None` (legacy): untouched — the old "Continue: choose
    voiceover source" behavior stays intact (group D replaces it later).
- `_polling_page()`: its error branch now falls back to the *current* stage's
  detail when the polled stage is missing (e.g. an early B1/B2/C1 failure on
  the auto_tts polling page shows the real error instead of "Unknown error.").
- `/voiceover/{job_id}/choose` and `/final/{job_id}` routes are NOT deleted —
  direct URL access still works (manual override / backward-compat; verified
  in group E).
- `pipeline/tests/test_full_auto_upload.py`: +2 HTTP tests —
  auto_tts `GET /upload/{job_id}` eventually contains `<video>` + download
  link and NO "choose voiceover source" link; user_upload keeps the old
  continue link and no `<video>`.
- Full suite: **283 tests OK** (281 prior + 2).

## [FA-C1] — 2026-08-13 — Wire auto_tts path end-to-end in app.py (Full-Auto Pipeline)

Group C's first (riskiest) chunk: the existing upload thread now *continues*
straight into the full-auto chain for auto_tts jobs, so a zero-click upload
produces the final video with no other endpoint needed.

- `app.py`: `_run_upload_pipeline()` — after writing `upload_pipeline`/`done`
  (the existing B1→B2→C1 chain), if `voice_source == "auto_tts"` the SAME
  thread keeps going: `auto_full_render`/`running` →
  `full_auto_chain.run_auto_tts_chain(job_id, call_budget)` →
  `auto_full_render`/`done` (with the result attached), or `/error` with a
  friendly message on FileNotFoundError / ValueError / RuntimeError /
  `auto_cut.DraftValidationError` (plus a final `except Exception` so the
  daemon thread never dies on unexpected errors). No new thread is spawned.
- The `user_upload` path (and legacy jobs with no choice) is untouched — it
  still stops at `upload_pipeline`/`done`; group D wires that path.
- `pipeline/tests/test_full_auto_upload.py`: new `AutoFullRenderWireTest`
  (2 HTTP tests using TestClient):
  - POST /upload (auto_tts) → only the status endpoint is polled →
    `auto_full_render` reaches `done`, `outputs/<job_id>/final_video.mp4`
    exists, result carries voiceover+final;
  - POST /upload (user_upload) → stays at `upload_pipeline`/`done`, no
    `auto_full_render` stage, no `final_video.mp4`.
- Full suite: **281 tests OK** (279 prior + 2).

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
