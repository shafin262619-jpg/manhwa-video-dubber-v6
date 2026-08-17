"""target_lang-driven output filename generation (F12b, Part B).

Every on-disk artifact whose name embeds the voiceover language (``_hi``
today) is generated through this module, so a future non-Hindi target
language only needs its ``target_lang`` code written into the job config.
For ``target_lang == "hi"`` every generated name is byte-identical to the
pre-F12b hardcoded value (regression-tested in test_lang_files.py).

Only FILENAMES live here. The JSON field names on disk (``text_hi``,
``serial``, ...) are part of the persisted schema and are intentionally NOT
generalized — changing them would break every downstream reader.
"""

from pathlib import Path

from pipeline import job_config, video_ingest


def target_lang(job_id, upload_root=None):
    """The job's target voiceover language code.

    Reads the persisted per-job config and falls back to the F9 default
    (``"hi"``) when the config is missing/unreadable, so jobs created before
    F12b and test fixtures without a config still resolve to the exact
    pre-F12b filenames.
    """
    cfg = job_config.read_config(job_id, upload_root)
    if not isinstance(cfg, dict):
        return job_config.DEFAULT_TARGET_LANG
    return cfg.get("target_lang") or job_config.DEFAULT_TARGET_LANG


def subtitles_json(lang):
    """``subtitles_<lang>.json`` — translated subtitle list (C1 output)."""
    return f"subtitles_{lang}.json"


def subtitles_srt(lang):
    """``subtitles_<lang>.srt`` — downloadable reference SRT (C1 output)."""
    return f"subtitles_{lang}.srt"


def subtitles_plain(lang):
    """``subtitles_<lang>_plain.txt`` — plain reference text (C1 output)."""
    return f"subtitles_{lang}_plain.txt"


def timestamps_auto(lang):
    """``timestamps_<lang>_auto.json`` — auto-TTS timestamps (D2 output)."""
    return f"timestamps_{lang}_auto.json"


def timestamps_upload(lang):
    """``timestamps_<lang>_upload.json`` — uploaded-voiceover timestamps (D3)."""
    return f"timestamps_{lang}_upload.json"


def timestamps_final(lang):
    """``timestamps_<lang>_final.json`` — unified timestamps (D4 output)."""
    return f"timestamps_{lang}_final.json"


def voiceover_audio(lang):
    """``voiceover_<lang>.wav`` — the normalized voiceover audio file."""
    return f"voiceover_{lang}.wav"


def job_dir(job_id, upload_root=None):
    """The job directory under the upload root (shared Path helper)."""
    root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    return root / job_id
