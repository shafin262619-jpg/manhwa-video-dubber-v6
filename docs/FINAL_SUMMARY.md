# Manhwa Video Dubber — Final Summary (S1 → G2)

This is the end-of-project summary. All development chunks (S1 through G2) are
complete and the full automated regression suite passes. What remains is a
**real-media QA run that only the user (and their friend) can perform** — a
sandboxed AI agent cannot do it because it needs a real Gemini key, real
long-form videos, human eyes/ears, and creative/voice decisions. The checklist
is at the bottom of this document.

- Repo: `https://github.com/shafin262619-jpg/manhwa-video-dubber.git` (branch
  `main`, tag `manhwa-video-dubber-final`)
- Full prompt: `docs/MANHWA_VIDEO_DUBBER_PLAN.md`
- Per-chunk details: `docs/CHANGELOG.md`
- Previous handoff: `docs/HANDOFF_NEXT.md`

---

## 1. What the system does

Takes a Chinese-subtitled manhwa "explain" video, extracts the subtitles,
translates them to Hindi, produces a Hindi voiceover (either auto-TTS or a
user-uploaded track), speed-matches each scene to its voiceover line, lets the
user review/trim per-clip, and renders a final Hindi-dubbed MP4.

## 2. Stage-by-stage summary

| Chunk | Name | Status | Output artifact |
|---|---|---|---|
| S1 | Scaffolding (FastAPI app + package layout + constants) | DONE | `app.py`, `pipeline/`, `pipeline/config.py` |
| A1 | Gemini key settings UI | DONE | `gemini_keys_store.json` (git-ignored) |
| A2 | Video upload + System Start | DONE | `uploads/<job_id>/source.mp4`, `job_meta.json` |
| B1 | Subtitle extraction (Gemini video-understanding, chunked) | DONE | `uploads/<job_id>/subtitles_zh_raw.json` |
| B2 | Subtitle serialization + no-overlap clamp | DONE | `uploads/<job_id>/subtitles_zh.json` |
| C1 | Chinese→Hindi translation | DONE | `subtitles_hi.json`, `.srt`, `subtitles_hi_plain.txt` |
| D1 | Voiceover source choice (auto-TTS vs user-upload) | DONE | `voice_source_choice.json` |
| D2 | Auto Hindi TTS voiceover (Gemini TTS) | DONE | `voiceover_hi.wav`, `timestamps_hi_auto.json` |
| D3 | User-uploaded voiceover + alignment (Gemini/Whisper fallback) | DONE | `voiceover_hi.wav`, `timestamps_hi_upload.json` |
| D4 | Unify auto/upload timestamps + overlap validation | DONE | `timestamps_hi_final.json` |
| E1 | Speed-ratio edit guideline (soft-clamp + flag) | DONE | `edit_guideline.json` |
| E2 | Draft final video (cut + `setpts` + concat + mux) | DONE | `draft_final_video.mp4` |
| F1 | Per-clip review UI (flagged-serial highlight) | DONE | review page + on-the-fly clips |
| F2 | Apply per-clip review edits (partial re-render) | DONE | updated `edit_guideline.json` + re-spliced draft |
| F3 | Final render + download | DONE | `outputs/<job_id>/final_video.mp4` |
| G1 | HTTP-level end-to-end regression (permanent test) | DONE | `pipeline/tests/test_app_orchestration.py` |
| G2 | Final wrap-up + this summary | DONE | `docs/FINAL_SUMMARY.md` |

## 3. How data moves between stages (the data-flow chain)

All intermediate artifacts live under `uploads/<job_id>/` (git-ignored).
Each stage reads the previous stage's file and writes the next one; the G1
regression proves this chain works over HTTP end to end.

```
User uploads video (A2)
  -> uploads/<job_id>/source.mp4  (+ job_meta.json: duration/resolution)

B1  source.mp4                     -> subtitles_zh_raw.json
    (short videos: one Gemini call; >10 min: ffmpeg split into overlapping
     segments, per-segment extraction, timestamps rebased + de-duplicated)

B2  subtitles_zh_raw.json          -> subtitles_zh.json
    (serials 1..N assigned, consecutive ranges clamped to never overlap,
     extraction_failed parts kept as flagged placeholders)

C1  subtitles_zh.json              -> subtitles_hi.json + .srt + _plain.txt
    (serial count/order preserved; per-line translation_fallback flag)

D1  (user chooses voice source)    -> voice_source_choice.json
     mode = auto_tts | user_upload

D2  subtitles_hi.json (auto path)  -> voiceover_hi.wav + timestamps_hi_auto.json
    (one Gemini TTS call per line, ffprobe-measured durations, cumulative
     timestamps; tts_failed -> silence placeholder + flag)

D3  subtitles_hi.json (upload path)-> voiceover_hi.wav + timestamps_hi_upload.json
    (user's mp3/wav/m4a normalized to voiceover_hi.wav; Gemini audio
     alignment -> Whisper fallback -> equal-split fallback; alignment_fallback
     + alignment_source flags per serial)

D4  voice_source_choice.json       -> timestamps_hi_final.json
    (picks D2 or D3 file; maps tts_failed / alignment_fallback onto
     flagged + flag_reason; deterministic overlap clamp; verifies shared
     voiceover_hi.wav exists — later stages are mode-agnostic)

E1  subtitles_zh.json  +           -> edit_guideline.json
    timestamps_hi_final.json
    (pts_multiplier = target_duration / source_duration per serial;
     outside 0.5..2.0 -> flagged extreme_speed_ratio; zero/negative duration
     -> pts_multiplier 1.0 + invalid_duration; pure Python, no ffmpeg/network)

E2  edit_guideline.json            -> draft_final_video.mp4
    + source.mp4 + voiceover_hi.wav
    (per serial: cut [source_start, source_end] video-only, setpts stretch,
     concat in serial order, mux voiceover as audio; ffprobe-validated against
     voiceover duration within a few frames)

F1  draft_final_video.mp4 +        -> review page per serial
    edit_guideline.json + subtitles_hi.json
    (clip player streams each serial's target segment on the fly; trim inputs;
     flagged serials highlighted)

F2  review edits (trim form)       -> updated edit_guideline.json + re-spliced
    draft_final_video.mp4
    (only the edited serial changes; target timing fixed, pts_multiplier
     recomputed, extreme_speed_ratio re-evaluated; partial re-render of just
     that clip + concat/mux, then E2-style validation)

F3  draft_final_video.mp4          -> outputs/<job_id>/final_video.mp4
    (normalize to H.264/AAC + faststart; download served at
     /download/<job_id>)
```

Shared helpers (no duplication): `subtitle_extract.call_with_rotation`
multi-key round-robin is reused by C1/D2; key masking everywhere so raw keys
never leak in API responses.

## 4. Automated coverage (what is already proven)

- `python3 -m unittest discover -s pipeline/tests` — **184 tests OK**
  (module-level tests per stage + `TestClient` HTTP endpoint tests).
- `pipeline/tests/test_app_orchestration.py` (G1, permanent) drives the app
  purely through HTTP endpoints over the full chain: add key → upload
  (B1+B2+C1) → choose auto-TTS (D2+D4+E1+E2) → review loads per-clip data →
  review edit (F2 partial re-render) → final render (F3) → download serves the
  final file. Gemini mocked, auto-cut/render ffmpeg mocked, D2 clips are real
  ffmpeg silence (deterministic, no network).
- Conventions enforced throughout: no uncaught exceptions on Gemini/ffmpeg
  failures (fall back + flag), constants from `pipeline/config.py`, secrets
  never committed.

---

## 5. These steps a sandboxed AI agent CANNOT do — the user must do them

The automated suite proves the *machinery* works with mocked Gemini and
synthetic media. It cannot prove the *output quality* of the real pipeline.
The following require a real Gemini key, real videos, and human eyes/ears.
They are intentionally not part of any sandboxed-agent run.

### (a) Full end-to-end run on a real video with a real Gemini key

Use one of your friend's real manhwa "explain" videos and a real Gemini API
key (add it under `/settings`). Verify each of these with eyes/ears:

1. **Subtitle extraction accuracy** (B1) — do the extracted Chinese subtitles
   match what is actually on screen? Any missing/merged/mis-timed lines?
   Download the SRT at `/download/{job_id}/subtitles?format=srt` and spot-check
   against the source video.
2. **Translation naturalness** (C1) — is the Hindi natural and correct for a
   manhwa explain video? Any `translation_fallback` lines (kept as Chinese
   because Gemini failed) that need manual fixing?
3. **Voiceover quality** (D2/D3) — listen to `voiceover_hi.wav`. If auto-TTS:
   does the Hindi voice sound good? Any `tts_failed` silences? If
   user-upload: is the recorded audio clean, and is the alignment accurate?
   (Check `timestamps_hi_auto.json` / `timestamps_hi_upload.json`.)
4. **Speed-adjusted scenes** (E2) — the biggest thing an agent cannot judge:
   do the stretched/squeezed scenes look natural? Pay special attention to the
   serials flagged `flagged: true, flag_reason: "extreme_speed_ratio"` on the
   `/review/{job_id}` page — these are outside the 0.5×–2.0× safe band and are
   the most likely to look wrong. Use the review page's trim controls to fix
   any that look bad, then re-render.
5. **Final lip-sync / scene-sync** (F3) — in `outputs/<job_id>/final_video.mp4`
   (or `/download/{job_id}`), does the Hindi speech actually line up with the
   scenes in a watchable way? Watch a couple of clips with sound on.

### (b) Long-video (>10 min) chunking test (B1)

B1's `LONG_VIDEO_CHUNK_THRESHOLD_SEC` (600 s) path — ffmpeg split into
overlapping segments, per-segment Gemini extraction, timestamp rebasing and
dedup — is unit-tested but has not run against a real long video. Upload a
video longer than 10 minutes and confirm:

- subtitles across the whole video are extracted continuously (no gap or jump
  at a segment boundary),
- no duplicated subtitle lines appear around the segment seams.

### (c) TTS voice / persona selection (D2) — creative decision

`TTS_VOICE_HINDI = "Aoede"` in `pipeline/config.py` is a placeholder. Picking
the actual Hindi voice/persona for the explain-video style is a creative
decision you must make by listening to real output — an agent cannot decide
the tone/branding of the voice for you. Change it in `config.py` and re-run
D2 to try candidates.

### (d) BGM preservation — confirm with your friend, then request separately

The original Chinese explain video's background music is **not** preserved:
E2 deliberately discards the source audio (`-an` per clip) and muxes only the
Hindi voiceover, so the final video has no BGM track. This was a deliberate
scope decision (see the Assumptions note in the plan). **Before treating the
project as done, confirm with your friend whether BGM preservation is wanted.**
If yes, ask for it as a separate follow-up feature plan (e.g. duck the original
BGM under the voiceover, or keep a low-volume music bed) — it is not part of
this plan's scope and should not be silently bolted on.

### (e) Run the pre-flight dry-run check before/after every real-media run

`pipeline/dry_run_check.py` এখন আছে — offline JSON-সংগতি যাচাই (কোনো
network/ffmpeg/Gemini কল ছাড়া, সেকেন্ডেই সেরে যায়)। প্রতিটা real-media
রান-এর **আগে ও পরে** চালিয়ে দেখুন (খরচবহুল TTS/রেন্ডার শুরু করার আগে বড়
ভুল ধরার জন্য):

```bash
python3 -m pipeline.dry_run_check --job-id <job_id>
```

Exit 0 = কোনো ব্লকিং error নেই; 1 = serial mismatch/gap/duplicate বা
invalid/overlapping duration — রেন্ডারের আগে ফিক্স করা আবশ্যক।

---

## Full-Auto Pipeline (FA1-F2)

### Short summary

The Full-Auto update removes the manual clicks from the PRD's core flow.
Uploading a video now needs no further interaction:

- **`auto_tts` choice** (the default) — after upload the job runs the whole
  backend chain **zero-click** straight to the final video
  (B1→B2→C1 → D2→D4→E1→E2→F3). The user watches the status page and gets the
  final video player + download link.
- **`user_upload` choice** — the job stops at **exactly one** pause: the audio
  upload. The upload page drops straight into the audio form (no separate
  "choose voice source" click — the choice is taken on the upload form since
  FA-A1). After the audio is posted, it auto-continues zero-click to the final
  video (D3→D4→E1→E2→F3).

The old manual routes (`/voiceover/{job_id}/choose`,
`/voiceover/{job_id}/upload`, `/voiceover/{job_id}/align_uploaded`,
`/voiceover/{job_id}/auto_tts`, `/review/{job_id}`, `/final/{job_id}`) are all
kept and still work by direct URL (backward-compat; verified by the FA-E1
audit), so the pre-FA manual flow remains available as an override.

### What was added, and where

- `pipeline/full_auto_chain.py` — **new**. Two pure-Python orchestration
  wrappers:
  - `run_auto_tts_chain(job_id, call_budget=None)` — D2→D4→E1→E2→F3 for the
    `auto_tts` path.
  - `run_user_upload_chain(job_id)` — D3→D4→E1→E2→F3 for the `user_upload`
    path (precondition: `voiceover_hi.wav` already saved).
  Both raise on failure (no silent swallow); mid-chain failure stops the
  following steps.
- `app.py` — new status **stage names** `auto_full_render` and
  `user_audio_pipeline` (persisted in `uploads/<job_id>/job_status.json` and
  polled via `GET /api/jobs/{job_id}/status`):
  - `_run_upload_pipeline()` (FA-C1): after `upload_pipeline`/`done`, an
    `auto_tts` job continues on the **same** thread through
    `run_auto_tts_chain` (the inline logic is factored into
    `_run_auto_full_render()`, which the `/upload` page also re-uses to resume
    a chain that never started after a manual override — FA-E1 fix).
  - `upload_voiceover()` (FA-D2): after the audio is saved, starts a daemon
    thread (`_start_stage(..., "user_audio_pipeline", _run_user_audio_pipeline)`)
    that runs `run_user_upload_chain` to the final video.
  - `upload_status_page()` (FA-C2/D1/D2/E1): renders the final video directly
    when `auto_full_render`/`user_audio_pipeline` is `done`, shows the audio
    form for `user_upload`, and is gated on stage-history so manual overrides
    never cause a redirect loop.
- `app.py` home page (FA-A1): the upload form takes the `voice_source` choice
  upfront (`auto_tts` default), persisted the moment the upload succeeds.
- Tests: `pipeline/tests/test_full_auto_chain.py` (FA-B1/B2/B3), plus
  `test_full_auto_upload.py`, `test_backward_compat_audit.py` (FA-E1) and
  `test_full_auto_orchestration.py` (FA-E2) — permanent HTTP regression tests
  for both paths.

### These steps a sandboxed AI agent CANNOT do — the user must do them

The automated suite (290 tests) proves the *wiring* end-to-end with mocked
Gemini and synthetic media. The real-media confirmation below requires a real
Gemini key, a real video, and human eyes/ears — it is intentionally not part
of any sandboxed-agent run:

(a) **auto_tts zero-click on real media** — with a real Gemini key and a real
    video, upload with `auto_tts` and confirm in the browser that the final
    video appears with **no extra clicks** (no `/choose`, `/align_uploaded` or
    `/final` navigation).
(b) **user_upload single-pause on real media** — upload with `user_upload`,
    confirm the page stops at **only the audio upload**, then after posting
    the audio confirm the final video appears with no further clicks.
(c) **Output-quality spot-check** — confirm the final video's quality is the
    same as the pre-FA U-series output (this update changed the UX/wiring
    only; the pipeline's output quality is unchanged).

---

## 6. How to run

```bash
# install deps (ffmpeg must be on PATH)
pip install -r requirements.txt

# start the web app (local preview)
uvicorn app:app --host 0.0.0.0 --port 5000
```

1. Open the app, add a real Gemini key under **Settings**.
2. **System Start**: upload the Chinese-subtitled video (B1+B2+C1 run
   automatically).
3. **/voiceover/{job_id}/choose**: pick auto-TTS or upload your own audio
   (D2/D3 → D4).
4. **/review/{job_id}**: inspect every serial, fix flagged clips via the trim
   form (F2).
5. **Final Render**: `/final/{job_id}` → normalize + download
   `final_video.mp4` (F3).

## 7. Known follow-ups (not in this plan's scope)

- BGM preservation (background music bed / ducking) — pending your friend's
  confirmation, request as a separate feature plan.
- Final Hindi TTS voice/persona selection (creative decision — see 5(c)).
- Production concerns if this ever leaves local use: job queues/background
  tasks for long videos, storage cleanup policy, concurrency — none were in
  scope.

---

## Subtitle QA Fixes (A1-E4)

The subtitle-QA-fix update (chunks A1→E4, plan:
`docs/SUBTITLE_QA_FIX_HANDOFF_PLAN.md`) fixed the six root-cause bugs found by
comparing `subtitles_hi.srt` against a Turboscribe transcript in an earlier
conversation. Everything below is automated and covered by the permanent test
suite; the one thing it cannot do is the real-media QA run (see "The user must
do this" at the end of this section).

### The 6 bugs → which group fixed them

| # | Bug | Fixed by |
|---|---|---|
| 1 | Zero-duration / duplicate-timestamp entry clusters (81 lines stuck at 3.000s, 48 at 4.000s, 43 at 5.000s) — no coverage-gap or duplicate-cluster detection existed | **A** (A1 gap detection, A2 duplicate-cluster detection, A3 QA diagnostics artifact) |
| 2 | A ~50s / 37-line dialogue-dense block dropped entirely, plus mis-timed lines — the whole video was sent to Gemini in one call (`LONG_VIDEO_CHUNK_THRESHOLD_SEC = 600`s for a ~5-6 min video) | **C** (C1 lowered the threshold to 90s so even short videos are sub-chunked with overlap + dedup) |
| 3 | No targeted re-extraction/repair mechanism — flagged regions sailed through to SRT/render untouched | **B** (B1–B4 bounded, budget-aware repair over flagged ranges, wiring into `build_subtitle_list`) |
| 4 | `_serialize()` only logged forward-overlap clamps; zero-duration and large backward jumps were silent | **A** (A1/A3 surface gaps + clusters in `subtitle_qa.json` diagnostics) |
| 5 | No independent cross-check of extraction (no audio-based Whisper verification pass) | **D** (D1–D3 `subtitle_verify.whisper_cross_check`, wired non-blocking into the upload pipeline) |
| 6 | No coverage/QA report shown to the user before recording/uploading the voiceover — problems surfaced only after the whole voiceover was done | **E** (E1 combined `build_qa_summary`, E2 informational QA banner on `/voiceover/{job_id}/choose`) |

### What was added

- **New files**
  - `pipeline/subtitle_verify.py` — `whisper_cross_check()`: extracts mono wav
    from `source.mp4` (ffmpeg), transcribes with local Whisper, and compares
    Whisper's measured spoken duration against Gemini-extracted coverage. Never
    raises; missing whisper/ffmpeg/failure → `skipped`.
  - `pipeline/subtitle_qa.py` — `build_qa_summary()`: pure aggregation merging
    `subtitle_qa.json` (A3/B3) with `subtitle_qa_whisper.json` (D1) into one
    human-readable `{qa_status, warnings[], ...}` summary. Never raises.
- **New config constants** (`pipeline/config.py`)
  - `SUBTITLE_GAP_FLAG_THRESHOLD_SEC = 6.0` (A1)
  - `SUBTITLE_DUP_CLUSTER_MIN_COUNT = 3` (A2)
  - `SUBTITLE_MAX_REPAIR_ATTEMPTS = 3` (B2)
  - `SUBTITLE_COVERAGE_MISMATCH_RATIO = 0.75` (D1)
  - `LONG_VIDEO_CHUNK_THRESHOLD_SEC` changed **600 → 90** (C1)
- **New artifact files** (both under `uploads/<job_id>/`)
  - `subtitle_qa.json` — coverage-gap + duplicate-cluster diagnostics (A3), with
    the post-repair `repair` summary (B3).
  - `subtitle_qa_whisper.json` — the whisper cross-check result dict (D1).
- **New route**
  - `GET /download/{job_id}/subtitle_qa` — serves `subtitle_qa.json` (E2).
- **Other wiring**
  - `build_subtitle_list()` auto-repair over flagged ranges (B3), sharing the
    per-job `CallBudget`.
  - `whisper_cross_check()` called non-blocking in `_run_upload_pipeline()`
    (D2), with `whisper_check_status` recorded in the job-status extra.
  - Informational `.flagged-banner` on `/voiceover/{job_id}/choose` when the QA
    summary is `flagged` — never blocks the auto_tts / user_upload choice (E2).

### Known limitations

- The repair pass is **single-pass** (targeted ranges, capped at
  `SUBTITLE_MAX_REPAIR_ATTEMPTS`) — it is not recursive, and it only acts on
  ranges flagged by gap / duplicate-cluster diagnostics.
- Misplaced-content problems other than gaps/duplicate-clusters — e.g. the
  "hallucination-suspect" sections found in the earlier conversation (lines
  present but mis-timed/relocated) — are **not** caught automatically. They
  still need a human reading the SRT/QA output; the QA banner is a signal, not
  a substitute for manual review.
- Whisper is an optional, lazy dependency (not in `requirements.txt`): CI /
  environments without it take the `skipped` cross-check path.

### The user must do this (a sandboxed AI agent cannot)

A real **Gemini API key** plus a real **~5–6 minute dialogue-dense video** are
needed for a full real-media run that no sandboxed AI agent can perform. Do a
complete run: upload → `subtitle_qa.json` / QA banner → (if flagged) manual
review → voiceover upload, and verify specifically:

1. **(ক)** the new 90-second chunking actually prevents the old "~50s of
   dialogue dropped" failure on a real dialogue-dense video;
2. **(খ)** the repair mechanism actually works on a real duplicate-timestamp
   cluster (check the `repair` summary in `subtitle_qa.json`);
3. **(গ)** the whisper cross-check does not false-positive on real audio
   (compare its status against the actual `subtitle_qa.json` coverage on a
   known-good video).

See section 5 above for the broader real-media checklist.
