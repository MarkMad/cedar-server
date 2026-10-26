"""Everything the owner can change, in one small key/value table.

Two kinds of thing live here, deliberately in the same place:

* **preferences** the app syncs across devices — the voice chosen per
  language, reading speed, theme (``GET/PUT /api/settings``);
* **server knobs** — the book store's curated/full switch, dictionary and
  translation tuning (``GET/PUT /api/settings/server``).

Values are JSON. Unknown keys are refused on write, so a typo can't silently
create a setting nothing reads.
"""
from __future__ import annotations

import json
import math
import sqlite3
from typing import Any

from . import lang
from .db import connect

# Server knobs, with their defaults. This is also the allow-list for
# PUT /api/settings/server.
KNOBS: dict[str, Any] = {
    # Book store: serve every Gutenberg book on disk (True) or only the curated
    # set the ingest marked copyright-verifiable and free of the explicit /
    # racist-ideology lists (False). Read per request by catalog.py.
    "catalog_full": False,
    # Word look-up (dictionary.py): provider order, per-request timeout, and
    # how long the first provider gets before the rest are raced.
    "dict_providers": "wiktionary,datamuse,dictionaryapi",
    "dict_timeout_s": 3.0,
    "dict_hedge_ms": 700,
    # Translations under the definition (translate.py): kill switch + timeout.
    "translate_enabled": True,
    "translate_timeout_s": 4.0,
    # Word substitutions (substitute.py): what the voice says instead of what
    # the text says. {language: {word: replacement}}, with "*" for the rules
    # that hold in every language. The settings route validates the shape.
    "substitutions": {},
}

# Preferences the app syncs. `voices` is {lang: voice}; `voice` is the flat
# "last picked" that older clients read; speed/theme are account-wide.
PREF_KEYS = ("voice", "voices", "speed", "theme")

# How many times the settings have been written. A cache can watch it to
# know its copy is stale (see revision()).
_revision = 0


def init_tables() -> None:
    global _revision
    with connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    _revision += 1


def revision() -> int:
    """How many times the settings have been written.

    A cache of the settings can remember the number it read and know it is
    out of date as soon as this moves (substitute.py holds the rules this
    way, so reading them costs nothing on the request path).
    """
    return _revision


def get(key: str, default: Any = None) -> Any:
    with connect() as c:
        row = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if row is None:
        return KNOBS.get(key, default)
    try:
        return json.loads(row["value"])
    except ValueError:
        return default


def _bump() -> None:
    """Move the revision. Call it after the writing transaction has committed:
    a reader that sees the new number must also see the new data, or it would
    cache the old values under it."""
    global _revision
    _revision += 1


def put(key: str, value: Any) -> None:
    with connect() as c:
        _put(c, key, value)
    _bump()


def _put(c: sqlite3.Connection, key: str, value: Any) -> None:
    if value is None:
        c.execute("DELETE FROM settings WHERE key=?", (key,))
    else:
        c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, json.dumps(value)))


# ------------------------------------------------------------------- server knobs

def knobs() -> dict[str, Any]:
    return {k: get(k) for k in KNOBS}


def update_knobs(patch: dict[str, Any]) -> dict[str, Any]:
    """Apply a partial update. Unknown keys raise KeyError; a None resets a knob
    to its default."""
    for k in patch:
        if k not in KNOBS:
            raise KeyError(k)
    values = {k: None if v is None else _coerce(k, v) for k, v in patch.items()}
    with connect() as c:
        for k, v in values.items():
            _put(c, k, v)
    _bump()
    return knobs()


# Bounds for the numeric knobs: (smallest, largest), the smallest exclusive for
# the timeouts (0 would mean "give up before asking").
_BOUNDS: dict[str, tuple[float, float]] = {
    "dict_timeout_s": (0.0, 60.0),
    "translate_timeout_s": (0.0, 60.0),
    "dict_hedge_ms": (0, 60_000),
}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false", "1", "0"):
        return value.strip().lower() in ("true", "1")
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise ValueError("expected true or false")


def _coerce(key: str, value: Any) -> Any:
    default = KNOBS[key]
    if isinstance(default, bool):
        return _coerce_bool(value)
    if isinstance(default, (int, float)):
        if isinstance(value, bool):
            raise TypeError(f"{key} must be a number")
        try:
            number = int(value) if isinstance(default, int) else float(value)
        except OverflowError:
            raise ValueError(f"{key} is out of range") from None
        low, high = _BOUNDS.get(key, (float("-inf"), float("inf")))
        exclusive = isinstance(default, float) and key in _BOUNDS
        if not math.isfinite(number) or number > high or number < low or (exclusive and number == low):
            raise ValueError(f"{key} is out of range")
        return number
    if isinstance(default, dict):
        # A structured knob. The route that accepts it checks the shape; all
        # that is left here is to keep it a JSON object.
        if not isinstance(value, dict):
            raise TypeError(f"{key} must be an object")
        return value
    return str(value)


# -------------------------------------------------------------------- preferences

def prefs() -> dict[str, Any]:
    return {k: v for k in PREF_KEYS if (v := get(k)) is not None}


def update_prefs(patch: dict[str, Any]) -> dict[str, Any]:
    for k, v in patch.items():
        if k in PREF_KEYS:
            put(k, v)
    return prefs()


def chosen_voices() -> dict[str, str]:
    """The voice picked for each language — picks only, no defaults.

    A flat ``voice`` (older clients) counts as the pick for *that voice's*
    language and nothing wider: one Japanese voice, set once for Japanese,
    must never end up reading an English library.
    """
    voices = {k: v for k, v in (get("voices") or {}).items()
              if isinstance(k, str) and isinstance(v, str)}
    legacy = get("voice")
    if isinstance(legacy, str) and legacy:
        language = lang.language_of(legacy)
        if language and language not in voices:
            voices[language] = legacy
    return voices


def set_voice(voice: str, language: str | None = None) -> None:
    """Record ``voice`` as the voice for ``language`` (by default the language
    the voice itself speaks). Every document in that language follows.

    The flat ``voice`` key is written too for clients that predate per-language
    voices.
    """
    language = language or lang.language_of(voice)
    with connect() as c:
        # Reserve the writer before reading: another device must see this
        # update before it reads and modifies the same mapping.
        c.execute("BEGIN IMMEDIATE")
        row = c.execute("SELECT value FROM settings WHERE key='voices'").fetchone()
        voices = dict(json.loads(row["value"]) if row else {})
        if language:
            voices[language] = voice
        _put(c, "voices", voices)
        _put(c, "voice", voice)
    _bump()


def reading_defaults() -> tuple[str, float]:
    """(main voice, speed) — the fallbacks a document uses when nothing more
    specific applies."""
    from .config import DEFAULT_SPEED, DEFAULT_VOICE
    return (get("voice") or DEFAULT_VOICE), float(get("speed") or DEFAULT_SPEED)
