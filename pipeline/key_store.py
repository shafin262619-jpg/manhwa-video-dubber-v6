"""Gemini API key storage.

Keys are persisted to ``gemini_keys_store.json`` at the repo root. That file
is git-ignored and must never be committed.

Security: functions used by the API never expose the full key. They return a
masked suffix (e.g. ``"...ab12"``) plus id/label. ``get_active_keys()`` returns
raw key strings and is intended only for internal pipeline use (rotation), not
for API responses.
"""

import json
import re
from pathlib import Path

KEY_STORE_PATH = Path(__file__).resolve().parent.parent / "gemini_keys_store.json"


class KeyNotFoundError(Exception):
    """Raised when deleting a key id that does not exist."""


def _load(store_path=None):
    path = Path(store_path) if store_path else KEY_STORE_PATH
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    keys = data.get("keys", [])
    return keys if isinstance(keys, list) else []


def _save(keys, store_path=None):
    path = Path(store_path) if store_path else KEY_STORE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"keys": keys}, indent=2), encoding="utf-8")


def _next_id(keys):
    max_num = 0
    for entry in keys:
        match = re.fullmatch(r"k(\d+)", str(entry.get("id", "")))
        if match:
            max_num = max(max_num, int(match.group(1)))
    return f"k{max_num + 1}"


def _mask(key):
    key = str(key or "")
    return "..." + key[-4:]


def _public(entry):
    return {
        "id": entry.get("id"),
        "label": entry.get("label"),
        "key": _mask(entry.get("key")),
    }


def list_keys(store_path=None):
    """Return all keys as masked entries: id, label, masked key."""
    return [_public(entry) for entry in _load(store_path)]


def add_key(key_str, label=None, store_path=None):
    """Add a key. Returns the masked public entry."""
    key_str = (key_str or "").strip()
    if not key_str:
        raise ValueError("key must not be empty")
    keys = _load(store_path)
    entry = {
        "id": _next_id(keys),
        "key": key_str,
        "label": (label or "").strip() or None,
    }
    keys.append(entry)
    _save(keys, store_path)
    return _public(entry)


def _parse_key_text(text):
    """Split a paste blob into individual keys.

    Handles newline-separated, comma-separated and whitespace-separated input;
    empty tokens are dropped.
    """
    tokens = []
    for line in re.split(r"[\r\n,]+", str(text or "")):
        for token in re.split(r"\s+", line.strip()):
            if token:
                tokens.append(token)
    return tokens


def add_keys(key_text, store_path=None):
    """Add multiple keys at once from a paste blob.

    Returns the list of masked public entries. Raises ValueError when the
    blob contains no keys. Keys already present in the store (exact value)
    are skipped.
    """
    tokens = _parse_key_text(key_text)
    if not tokens:
        raise ValueError("no keys found to add")
    keys = _load(store_path)
    existing = {str(entry.get("key", "")) for entry in keys}
    added = []
    for token in tokens:
        if token in existing:
            continue
        entry = {
            "id": _next_id(keys),
            "key": token,
            "label": None,
        }
        keys.append(entry)
        existing.add(token)
        added.append(_public(entry))
    if not added:
        raise ValueError("all keys already stored")
    _save(keys, store_path)
    return added


def delete_key(key_id, store_path=None):
    """Delete a key by id. Raises KeyNotFoundError if id is absent."""
    keys = _load(store_path)
    for entry in keys:
        if entry.get("id") == key_id:
            keys.remove(entry)
            _save(keys, store_path)
            return _public(entry)
    raise KeyNotFoundError(f"key id not found: {key_id}")


def get_active_keys(store_path=None):
    """Return raw key strings only. For internal pipeline use (rotation)."""
    return [entry.get("key") for entry in _load(store_path)]
