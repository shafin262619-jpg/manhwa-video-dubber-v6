# Manhwa Video Dubber — Project Plan

## Goal

"Manhwa Video Dubber" is a system that takes a Chinese-subtitled manhwa explain
video, extracts the subtitles, translates them to Hindi, generates a Hindi
voiceover, matches scene timing to the voiceover, and produces a final
Hindi-dubbed video.

Pipeline stages:

1. Subtitle extract
2. Translation
3. Voiceover (auto + upload + unify)
4. Scene-speed matching
5. Final edit

The project is built as a GitHub-based chain. This document is chunk **S1**
(scaffolding only — no feature logic).

## Repo

`https://github.com/shafin262619-jpg/manhwa-video-dubber.git` (branch: `main`)

## Chunk S1 — Scaffolding (DONE)

Scope: structure only, no feature logic.

- `app.py` — FastAPI local web app shell on `localhost:5000`, home page route
  returning "Manhwa Video Dubber — coming soon". No upload/processing logic yet.
- `pipeline/__init__.py` (empty) — future chunks add modules here:
  `key_store.py`, `video_ingest.py`, `subtitle_extract.py`, `subtitle_builder.py`,
  `translator.py`, `voiceover_auto.py`, `voiceover_upload.py`, `voiceover_unify.py`,
  `edit_guideline.py`, `auto_cut.py`, `review.py`, `render_final.py`.
- `pipeline/tests/__init__.py` (empty).
- `requirements.txt` — `fastapi`, `uvicorn`, `python-multipart`, `google-genai`,
  `python-dotenv`. `python-multipart` is critical from the start: file-upload
  endpoints fail without it (previous project failed to boot for this reason).
- `docs/MANHWA_VIDEO_DUBBER_PLAN.md` — this file.
- `pipeline/config.py` — constants:
  - `SPEED_RATIO_MIN = 0.5`
  - `SPEED_RATIO_MAX = 2.0`
  - `TTS_VOICE_HINDI = "Aoede"` (placeholder, tune later)
  - `LONG_VIDEO_CHUNK_THRESHOLD_SEC = 600` (videos longer than 10 min are
    processed as B1 chunks)
- `gemini_keys_store.json.example` — placeholder format only. The real file is
  git-ignored and never committed.
- `.gitignore`.
- `docs/CHANGELOG.md`, `docs/HANDOFF_NEXT.md`.

### DoD (S1)

1. `python3 -m py_compile app.py` passes.
2. `python3 -m unittest discover -s pipeline/tests` shows "0 tests" with no error.
3. Commit `S1: project scaffolding`, push.
4. Tag `chunk-S1-done`, push tag.

## Chunk A1 — Gemini key settings UI

- UI to add/select Gemini API keys, persisted to `gemini_keys_store.json`
  (git-ignored). Never commit the real file.
- Full prompt for this chunk is tracked in the handoff docs.

## Conventions

- Use constants from `pipeline/config.py`; do not duplicate values.
- `.env` / `gemini_keys_store.json` are never committed.
- Gemini call failures must not raise uncaught exceptions.
- **G1 regressions require HTTP endpoint tests via `TestClient`** — calling
  pipeline modules directly is not sufficient.
