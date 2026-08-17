"""Chinese-to-Hindi subtitle translation (C1).

Consumes ``subtitles_zh.json`` (B2 output) and produces
``uploads/<job_id>/subtitles_hi.json`` plus downloadable reference files
``subtitles_hi.srt`` and ``subtitles_hi_plain.txt``.

Hard constraint: the output serial count/order must match the input exactly.
Gemini occasionally merges/splits lines; on a count mismatch the batch is
retried once with a strict prompt. If the retry still mismatches (or every
key failed), a batch-split repair kicks in (U3a): the batch is split into two
roughly equal halves, each half is translated separately (strict prompt) and a
half that still mismatches is split again recursively until every line matches
or a single-line chunk keeps failing. Only those genuinely failing lines keep
the original Chinese text in ``text_hi`` with ``translation_fallback: true`` —
silent mismatches are never allowed, and a whole batch is no longer thrown
away just because one line is problematic. The recursion is bounded by
``max_split_rounds`` so pathological input falls back gracefully instead of
looping; an exhausted shared ``CallBudget`` keeps what already matched and
falls the rest back without raising.

``start_sec`` / ``end_sec`` keep the ORIGINAL Chinese video timing (reference
for D-group speed-ratio calculation).
"""

import json
import logging
import re
from pathlib import Path

from google import genai
from google.genai import types as genai_types

from pipeline import config, job_logging, key_store, lang_files, subtitle_extract, video_ingest

logger = logging.getLogger(__name__)

TRANSLATION_PROMPT = (
    "Translate each of the following {count} Chinese subtitle line(s) into "
    "natural Hindi. Reply with EXACTLY {count} lines, one translation per "
    "line, in the same order. No numbering, no commentary, no empty lines.\n"
    "Chinese lines:\n{lines}"
)

RETRY_PROMPT = (
    "STRICT instruction: translate these {count} Chinese subtitle line(s) "
    "into natural Hindi. You MUST reply with EXACTLY {count} non-empty "
    "lines, one translation per line, in the same order. Do NOT merge or "
    "split any line. No numbering, no commentary.\n"
    "Chinese lines:\n{lines}"
)


def _call_gemini_text(key, prompt):
    client = genai.Client(api_key=key)
    response = client.models.generate_content(
        model=config.GEMINI_MODEL,
        contents=[prompt],
        config=genai_types.GenerateContentConfig(
            temperature=config.GEMINI_TEMPERATURE
        ),
    )
    return response.text


def _extract_lines(text):
    """Parse a Gemini text response into a list of non-empty lines."""
    text = (text or "").strip()
    try:
        data = json.loads(text)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except json.JSONDecodeError:
        pass
    fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
    if fence:
        try:
            data = json.loads(fence.group(1))
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [line.strip() for line in text.splitlines() if line.strip()]


def _translate_lines(key, lines, expected_count, emphasis):
    prompt = (RETRY_PROMPT if emphasis else TRANSLATION_PROMPT).format(
        count=expected_count, lines="\n".join(lines)
    )
    text = _call_gemini_text(key, prompt)
    return _extract_lines(text)


def _translate_chunk(keys, rotation, lines, depth, max_depth, call_budget=None, logger_=None):
    """Attempt a strict translation of ``lines``; on count mismatch, split.

    Returns ``(translations, rotation)`` where ``translations`` is aligned
    1:1 with ``lines`` and a ``None`` entry marks a line that could not be
    translated and must fall back to the original Chinese.

    ``call_with_rotation`` never raises on a Gemini failure: it returns
    ``(None, rotation, error_dict)``. When that happens (every key failed, a
    non-rotatable error, or an exhausted shared ``CallBudget``) the whole
    chunk falls back as a unit — smaller chunks cannot help, and the budget
    rule is to keep what already matched and fall the rest back, never raise.

    ``logger_`` (optional) is the per-job logger threaded from the entry
    function so Gemini failures land in the job's ``pipeline.log``.
    """
    n = len(lines)
    result, rotation, error = subtitle_extract.call_with_rotation(
        keys, rotation, _translate_lines, lines, n, True,
        call_budget=call_budget, logger_=logger_,
    )
    if error is not None:
        return [None] * n, rotation
    if len(result) == n:
        return result, rotation
    return _repair_split(
        keys, rotation, lines, depth + 1, max_depth,
        call_budget=call_budget, logger_=logger_,
    )


def _repair_split(keys, rotation, lines, depth, max_depth, call_budget=None, logger_=None):
    """Split ``lines`` into two roughly equal halves and repair each one.

    ``depth`` counts how many times this chunk has already been split. A chunk
    is split again only while ``depth < max_depth``; reaching ``max_depth``
    (or a single-line chunk) falls the chunk back as a unit, so pathological
    input is bounded by ``max_split_rounds`` instead of looping forever.
    """
    n = len(lines)
    if n <= 1 or depth >= max_depth:
        return [None] * n, rotation
    mid = (n + 1) // 2
    left, rotation = _translate_chunk(
        keys, rotation, lines[:mid], depth, max_depth,
        call_budget=call_budget, logger_=logger_,
    )
    right, rotation = _translate_chunk(
        keys, rotation, lines[mid:], depth, max_depth,
        call_budget=call_budget, logger_=logger_,
    )
    return left + right, rotation


def _srt_timestamp(sec):
    sec = max(0.0, float(sec or 0.0))
    total_ms = int(round(sec * 1000))
    hours, rem = divmod(total_ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def build_srt(entries):
    """Build a standard SRT file (reference timing = original Chinese)."""
    blocks = []
    for entry in entries:
        text = entry.get("text_hi") or entry.get("text_zh") or ""
        blocks.append(
            f"{entry['serial']}\n"
            f"{_srt_timestamp(entry.get('start_sec'))} --> "
            f"{_srt_timestamp(entry.get('end_sec'))}\n"
            f"{text}"
        )
    return "\n\n".join(blocks) + "\n"


def build_plain(entries):
    """Build a plain text file: serial + Hindi text, one per line."""
    return "\n".join(
        f"{entry['serial']}\t{entry.get('text_hi') or ''}" for entry in entries
    ) + "\n"


def _build_output(entries, translations, fallback):
    """Build the output list, preserving input serial count/order exactly.

    ``translations`` is aligned one-to-one with the non-empty ``text_zh``
    lines: a ``None`` entry means that specific line could not be translated
    (U3a per-line repair) and keeps the original Chinese with
    ``translation_fallback: true``, while its neighbours still get their
    translations.
    """
    t = 0
    output = []
    for entry in entries:
        text_zh = entry.get("text_zh", "")
        serial = entry.get("serial")
        if not text_zh:
            text_hi, is_fallback = "", False
        elif fallback or translations is None or t >= len(translations):
            text_hi, is_fallback = text_zh, True
        else:
            candidate = translations[t]
            t += 1
            if candidate is None:
                text_hi, is_fallback = text_zh, True
            else:
                text_hi, is_fallback = candidate, False
        output.append(
            {
                "serial": serial,
                "text_zh": text_zh,
                "text_hi": text_hi,
                "start_sec": entry.get("start_sec"),
                "end_sec": entry.get("end_sec"),
                "translation_fallback": is_fallback,
            }
        )
    return output


def translate_subtitles(
    job_id, upload_root=None, call_budget=None, max_split_rounds=4
):
    """Translate all subtitle entries. Returns the output list.

    ``call_budget`` (optional) is a shared ``gemini_rotation.CallBudget`` the
    whole translation — including every recursive split-repair call — draws
    from; when it is exhausted the already-matched lines are kept and the rest
    fall back, never raising. ``max_split_rounds`` bounds the batch-split
    repair recursion (default 4).
    """
    upload_root = Path(upload_root) if upload_root else video_ingest.UPLOAD_ROOT
    job_dir = upload_root / job_id
    in_path = job_dir / "subtitles_zh.json"
    if not in_path.exists():
        raise FileNotFoundError(f"no subtitles_zh.json for job {job_id}")

    job_logger = job_logging.get_job_logger(job_id, upload_root)
    entries = json.loads(in_path.read_text(encoding="utf-8"))
    if not isinstance(entries, list):
        raise ValueError(f"malformed subtitles_zh.json for job {job_id}")

    lines = [e.get("text_zh", "") for e in entries if e.get("text_zh")]
    expected = len(lines)
    fallback = False
    translations = None

    if expected:
        keys = key_store.get_active_keys()
        if not keys:
            fallback = True
            job_logger.error(
                "translation fallback for job %s: no active Gemini keys", job_id
            )
        else:
            rotation = 0
            translations, rotation, error = subtitle_extract.call_with_rotation(
                keys, rotation, _translate_lines, lines, expected, False,
                call_budget=call_budget, logger_=job_logger,
            )
            if translations is not None and len(translations) != expected:
                job_logger.warning(
                    "translation count mismatch (%d != %d); retrying strict for job %s",
                    len(translations), expected, job_id,
                )
                translations, rotation, error = (
                    subtitle_extract.call_with_rotation(
                        keys, rotation, _translate_lines, lines, expected, True,
                        call_budget=call_budget, logger_=job_logger,
                    )
                )
            if translations is None or len(translations) != expected:
                if error is not None and error.get("type") == "call_budget_exceeded":
                    # Budget is gone: nothing more can succeed. Keep whatever
                    # already matched (nothing here) and fall back the rest.
                    fallback = True
                    job_logger.error(
                        "translation call budget exceeded for job %s; "
                        "keeping original Chinese (translation_fallback)",
                        job_id,
                    )
                else:
                    job_logger.warning(
                        "translation failed/mismatched after strict retry for job %s; "
                        "running batch-split repair",
                        job_id,
                    )
                    translations, rotation = _repair_split(
                        keys, rotation, lines, 0, max_split_rounds,
                        call_budget=call_budget, logger_=job_logger,
                    )

    output = _build_output(entries, translations, fallback)

    lang = lang_files.target_lang(job_id, upload_root)
    (job_dir / lang_files.subtitles_json(lang)).write_text(
        json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (job_dir / lang_files.subtitles_srt(lang)).write_text(
        build_srt(output), encoding="utf-8"
    )
    (job_dir / lang_files.subtitles_plain(lang)).write_text(
        build_plain(output), encoding="utf-8"
    )
    return output
