"""Per-job configuration (F9).

Every job gets a ``uploads/<job_id>/job_config.json`` written once, at job
creation, BEFORE any Gemini/Whisper stage runs. It records the processing
engine (``whisper_primary`` vs ``gemini_only``), the target language the
voiceover is produced in (``target_lang``, ``"hi"`` today), the auto-detected
source language (``source_lang``, optional — schema only until F12), the
voice source (``auto_tts`` / ``user_upload`` / ``transcript_upload``) and the
subtitle source (``gemini_extract`` / ``user_transcript``, F12a).

Engine semantics:

- ``whisper_primary`` (default): local Whisper is the primary timing
  authority (F8 behavior, unchanged). When Whisper is not installed it fails
  gracefully into the pure-Gemini path exactly as before.
- ``gemini_only``: Whisper is never called, even when it IS installed —
  for phone / Termux users who avoid the heavy torch/whisper install, or who
  prefer speed/cost over local timing accuracy.

Pre-F9 jobs (a job dir that exists but has no ``job_config.json`` yet) read
back sensible defaults so every downstream reader keeps working:
``engine`` = ``whisper_primary`` if whisper is importable else
``gemini_only``, ``target_lang`` = ``"hi"``, ``voice_source`` =
``"auto_tts"``. ``read_config`` never raises.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pipeline import config, video_ingest

ALLOWED_ENGINES = ("whisper_primary", "gemini_only")

# Schema-only for F9 (built in F12): the value can be stored and read back,
# but nothing downstream consumes it yet.
ALLOWED_VOICE_SOURCES = ("auto_tts", "user_upload", "transcript_upload")

# F12a: where the original-language subtitles came from. ``gemini_extract`` is
# the normal F1 (Gemini video-understanding) path; ``user_transcript`` means
# the user uploaded their own transcript file and F1 was skipped.
ALLOWED_SUBTITLE_SOURCES = ("gemini_extract", "user_transcript")

# F12f: the target language the voiceover is produced in. ``"hi"`` is the
# long-standing default; ``bn`` / ``en`` are the additional supported dubbing
# targets. Every downstream consumer (translation prompt, TTS voice, Whisper
# alignment language, on-disk filenames via ``lang_files``) keys off this one
# catalog, so adding a 4th language is a single new entry here plus a voice in
# ``config.TTS_VOICES``.
ALLOWED_TARGET_LANGS = ("hi", "bn", "en")

# English name used in translation/alignment prompts and logs.
TARGET_LANG_NAMES = {
    "hi": "Hindi",
    "bn": "Bangla",
    "en": "English",
}

# Bangla UI labels for the upload-form dropdown (the UI language is Bangla).
TARGET_LANG_UI_LABELS = {
    "hi": "হিন্দি",
    "bn": "বাংলা",
    "en": "ইংরেজি",
}

DEFAULT_TARGET_LANG = "hi"
DEFAULT_SUBTITLE_SOURCE = "gemini_extract"


def config_path(job_id, upload_root=None):
    """Return the ``job_config.json`` path for a job under the upload root."""
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    return root / job_id / "job_config.json"


def whisper_importable():
    """True when the optional local-whisper package can be imported."""
    try:
        import whisper  # noqa: PLC0415 - lazy: heavy optional dependency
        return True
    except ImportError:
        return False


def default_engine():
    """The engine a job falls back to when no choice was recorded."""
    return "whisper_primary" if whisper_importable() else "gemini_only"


def _defaults():
    return {
        "job_id": None,
        "created_at": None,
        "engine": default_engine(),
        "source_lang": None,
        "target_lang": DEFAULT_TARGET_LANG,
        "voice_source": "auto_tts",
        "subtitle_source": DEFAULT_SUBTITLE_SOURCE,
    }


def write_config(job_id, engine=None, target_lang=None, source_lang=None,
                 voice_source="auto_tts", subtitle_source=None, upload_root=None):
    """Write the per-job config. Returns the saved dict.

    ``engine`` / ``target_lang`` / ``voice_source`` / ``subtitle_source`` may
    be ``None`` to keep the defaults. Raises ValueError on an invalid
    ``engine``, ``target_lang``, ``voice_source`` or ``subtitle_source``.
    Written atomically (temp file + ``os.replace``).
    """
    if engine is None:
        engine = default_engine()
    if engine not in ALLOWED_ENGINES:
        raise ValueError(
            f"invalid engine: {engine!r} (allowed: {', '.join(ALLOWED_ENGINES)})"
        )
    if voice_source not in ALLOWED_VOICE_SOURCES:
        raise ValueError(
            f"invalid voice source: {voice_source!r} "
            f"(allowed: {', '.join(ALLOWED_VOICE_SOURCES)})"
        )
    if subtitle_source is None:
        subtitle_source = DEFAULT_SUBTITLE_SOURCE
    if subtitle_source not in ALLOWED_SUBTITLE_SOURCES:
        raise ValueError(
            f"invalid subtitle source: {subtitle_source!r} "
            f"(allowed: {', '.join(ALLOWED_SUBTITLE_SOURCES)})"
        )
    if target_lang is None:
        target_lang = DEFAULT_TARGET_LANG
    if target_lang not in ALLOWED_TARGET_LANGS:
        raise ValueError(
            f"invalid target lang: {target_lang!r} "
            f"(allowed: {', '.join(ALLOWED_TARGET_LANGS)})"
        )
    data = {
        "job_id": job_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "engine": engine,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "voice_source": voice_source,
        "subtitle_source": subtitle_source,
    }
    path = config_path(job_id, upload_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    return data


def read_config(job_id, upload_root=None):
    """Read the persisted config for a job, never raising.

    Returns the saved dict, the F9 defaults for a job dir that exists but has
    no config file yet (pre-F9 jobs), or ``None`` when the job dir does not
    exist at all.
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    path = config_path(job_id, upload_root)
    if not path.parent.is_dir():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _defaults()
    if not isinstance(data, dict):
        return _defaults()
    defaults = _defaults()
    defaults.update({k: v for k, v in data.items() if v is not None})
    defaults["engine"] = (
        defaults["engine"] if defaults["engine"] in ALLOWED_ENGINES else default_engine()
    )
    defaults["voice_source"] = (
        defaults["voice_source"]
        if defaults["voice_source"] in ALLOWED_VOICE_SOURCES
        else "auto_tts"
    )
    defaults["subtitle_source"] = (
        defaults["subtitle_source"]
        if defaults["subtitle_source"] in ALLOWED_SUBTITLE_SOURCES
        else DEFAULT_SUBTITLE_SOURCE
    )
    defaults["target_lang"] = (
        defaults["target_lang"]
        if defaults["target_lang"] in ALLOWED_TARGET_LANGS
        else DEFAULT_TARGET_LANG
    )
    return defaults
