"""Word substitutions: say this instead of that, before the voice speaks.

Some words are mispronounced, misheard, or simply wrong for the reader: a name
the G2P mangles ("Kokoro" -> "ko-KOR-oh"), an abbreviation that wants spelling
out ("Ave." -> "Avenue"), a word the owner would rather not hear. Cedar had no
way to say any of that: the text went to the engine exactly as it had been
chunked.

The rules live in one server knob (``substitutions``) and are applied to the
text before it is sent to the engine. Where that happens matters more than it
looks:

* before ``_key()``, or the cache would be keyed on the original text while the
  audio held the substituted words, and a later change of the rules would keep
  serving the old audio;
* before the prefetch, or the warmed clip would not be the clip that gets asked
  for;
* not after the audio, because by then there is nothing left to change.

What the reader sees and what the voice says are now different texts, and the
word timings have to keep pointing at the text on the screen. So a substitution
records what it changed and where (:class:`Edit`), and the timings are mapped
back over those edits — see :func:`to_original`.

Matching is literal and whole-word: "cat" replaces "cat" and "Cat", but not
"cats" and not "concatenate". The replacement is used verbatim, so a rule that
says "Kokoro" -> "Koh-KOH-roh" produces that spelling whatever case the source
had.

A rule may instead match case: "US" -> "United States" replaces "US" but not
"us", compared character for character (no casefolding). It is stored as
``{"to": "United States", "case": true}`` where the plain form would put the
replacement string; a rule without the flag stays a plain string, so rules
written before the flag existed mean what they always did. Two rules that can
match the same word are told apart by layer first (below) and, within a layer,
a case-matching rule beats a case-insensitive one. A client that rewrites the
rules as plain strings drops the flags.

Rules are per language, with "*" for the ones that hold everywhere. The
language comes from the voice, which is chosen to speak the language the text
is in; "*" rules apply whatever the language turns out to be.

A respelling that suits one engine's G2P can sound wrong on another, so each
language's rules may also be scoped to an engine ("kokoro" or "pocket"; "*"
for both). The stored form is ``{language: {engine: {word: replacement}}}``.
The older flat ``{language: {word: replacement}}`` is still accepted and means
"every engine". Where several rules name the same word the most specific wins:
language + engine, then the language's own, then "*" + engine, then "*".
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from typing import Any, Iterable, List, NamedTuple, Optional, Sequence

from . import lang
from . import settings as appsettings

log = logging.getLogger("cedar.substitute")

# The server knob the rules live in.
KNOB = "substitutions"

# The bucket of rules that apply whatever the language turns out to be, and
# (inside a language) whatever the engine turns out to be.
ANY = "*"

# The engines a rule can be scoped to. The engine is the voice's: Pocket's
# voices (cloned ones included) run on Pocket, every other voice on Kokoro.
ENGINES = ("kokoro", "pocket")

# Bounds. The rules are owner-supplied text that runs over every sentence the
# server ever speaks, so they are capped like any other input: a runaway map
# would slow every request, and no map that big could be typed by hand anyway.
MAX_LANGUAGES = 16
MAX_RULES_PER_LANGUAGE = 200
MAX_RULES = 500
MAX_WORD_LENGTH = 64
MAX_REPLACEMENT_LENGTH = 200

# A rule needs a letter or a digit in it. A key made of nothing but
# punctuation would match in places nobody meant it to.
_WORDISH = re.compile(r"[^\W_]", re.UNICODE)


class RulesError(ValueError):
    """A substitution map that cannot be used as written."""


class Edit(NamedTuple):
    """One substituted word: where it was in the text, and where it went.

    ``orig_*`` are offsets in the text the reader is looking at, ``spoken_*``
    offsets in the text the engine was given.
    """
    orig_start: int
    orig_end: int
    spoken_start: int
    spoken_end: int


class Rule(NamedTuple):
    """What a word is replaced with, and whether the word has to match case.

    ``rank`` is how specific the layer the rule came from is (0 = "*" for every
    engine ... 3 = the language's own for this engine); :func:`rules_for` sets it.
    """
    to: str
    case: bool = False
    rank: int = 0


class _Matcher:
    """The compiled pattern for one set of rules, plus its lookup tables."""
    __slots__ = ("pattern", "exact", "folded")

    def __init__(self, rules: dict[str, Rule]):
        # Longest first, so "cats" is tried before "cat" and the engine does
        # not have to backtrack into it. Among rules of the same length (the
        # only ones that can match the same text) the more specific layer comes
        # first, then a rule that matches case, and the alternation takes the
        # first that fits.
        ordered = sorted(rules.items(),
                         key=lambda kv: (-len(kv[0]), -kv[1].rank, not kv[1].case))
        # Each alternative carries its own case flag: (?i:..) folds, (?-i:..) is exact.
        self.pattern = re.compile(
            r"(?<!\w)(?:" + "|".join(
                ("(?-i:" if rule.case else "(?i:") + re.escape(key) + ")"
                for key, rule in ordered) + r")(?!\w)")
        # The lookup has to agree with the alternation, or a word would be
        # replaced by the wrong rule. Case-matching rules are looked up as
        # written, the others casefolded (the text may say "KOKORO" where the
        # rule says "Kokoro"); where both fit, the one earlier in `ordered` wins.
        self.exact: dict[str, tuple[int, Rule]] = {}
        self.folded: dict[str, tuple[int, Rule]] = {}
        for n, (key, rule) in enumerate(ordered):
            if rule.case:
                self.exact.setdefault(key, (n, rule))
            else:
                self.folded.setdefault(key.casefold(), (n, rule))

    def find(self, found: str) -> Optional[Rule]:
        """The rule that replaces the matched text, if any."""
        hits = [h for h in (self.exact.get(found), self.folded.get(found.casefold())) if h]
        return min(hits)[1] if hits else None


# Compiling a pattern out of a few hundred rules is the expensive part of a
# substitution, and the rules change rarely, so the compiled form is kept.
# A handful of entries is enough: one per rule set the owner is running.
_matchers: dict[str, _Matcher] = {}
_MATCHER_CACHE_MAX = 8


# ------------------------------------------------------------------- validation

def _is_rule_object(value: Any) -> bool:
    """Whether a dict is one rule (``{"to": ..., "case": ...}``) rather than an
    engine's table of rules. A table only ever holds strings, so a "case" entry
    that is not one gives the rule away — even a malformed one, which then gets
    an error about the flag instead of about an engine."""
    return isinstance(value, dict) and "case" in value and not isinstance(value["case"], str)


def _check_rule(code: str, word: Any, replacement: Any) -> tuple[str, Any]:
    """One rule, trimmed and checked; raises :class:`RulesError` if unusable.

    Returns the word and the stored form: the replacement string, or
    ``{"to": replacement, "case": True}`` for a rule that matches case.
    """
    case = False
    if _is_rule_object(replacement):
        if not isinstance(replacement["case"], bool):
            raise RulesError(f"The case flag for {word!r} in the {code!r} rules must be true or false.")
        if set(replacement) - {"to", "case"}:
            raise RulesError(f"The rule for {word!r} in the {code!r} rules has unknown fields.")
        case, replacement = replacement["case"], replacement.get("to")
    if not isinstance(word, str) or not isinstance(replacement, str):
        raise RulesError(f"The rules for {code!r} must be word -> replacement strings.")
    word, replacement = word.strip(), replacement.strip()
    if not word:
        raise RulesError(f"The {code!r} rules have an empty word in them.")
    if len(word) > MAX_WORD_LENGTH:
        raise RulesError(f"A word in the {code!r} rules is too long.")
    if not replacement:
        raise RulesError(f"{word!r} has nothing to say instead.")
    if len(replacement) > MAX_REPLACEMENT_LENGTH:
        raise RulesError(f"The replacement for {word!r} is too long.")
    if not _WORDISH.search(word):
        raise RulesError(f"{word!r} has no letters or digits in it.")
    return word, ({"to": replacement, "case": True} if case else replacement)


def validate(value: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """Check a substitution map and return it in canonical form.

    Accepts ``{language: {engine: {word: replacement}}}`` and the older
    ``{language: {word: replacement}}`` (every engine); the two may be mixed
    inside one language. The result is always the nested form. A replacement
    is a string, or ``{"to": string, "case": true}`` for a rule that matches
    case (``"case": false`` is the plain form and is stored as a string).

    Raises :class:`RulesError` with a message the owner can act on; the
    settings route turns that into a 400.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise RulesError("Substitutions must be an object of language -> {word: replacement}.")
    if len(value) > MAX_LANGUAGES:
        raise RulesError(f"Too many languages ({len(value)}); at most {MAX_LANGUAGES}.")
    out: dict[str, dict[str, dict[str, Any]]] = {}
    for code, table in value.items():
        if not isinstance(code, str):
            raise RulesError("A language key must be a string.")
        key = code.strip().lower()
        if key != ANY and (len(key) != 2 or not key.isalpha()):
            raise RulesError(f"{code!r} is not a language code (two letters, or '*').")
        if not isinstance(table, dict):
            raise RulesError(f"The rules for {code!r} must be an object of word -> replacement.")
        buckets = out.setdefault(key, {})
        for word, replacement in table.items():
            if isinstance(replacement, dict) and not _is_rule_object(replacement):
                engine = word.strip().lower() if isinstance(word, str) else ""
                if engine != ANY and engine not in ENGINES:
                    raise RulesError(
                        f"{word!r} in the {code!r} rules is not an engine "
                        f"({', '.join(ENGINES)}, or '*'). A rule that matches case needs "
                        f'"to" and a "case" that is true or false.')
                bucket = buckets.setdefault(engine, {})
                for w, r in replacement.items():
                    w, r = _check_rule(code, w, r)
                    bucket[w] = r
            else:
                w, r = _check_rule(code, word, replacement)
                buckets.setdefault(ANY, {})[w] = r
    total = 0
    for key, buckets in out.items():
        count = sum(len(b) for b in buckets.values())
        if count > MAX_RULES_PER_LANGUAGE:
            raise RulesError(f"Too many rules for {key!r}; at most {MAX_RULES_PER_LANGUAGE}.")
        total += count
    if total > MAX_RULES:
        raise RulesError(f"Too many rules ({total}); at most {MAX_RULES}.")
    return out


# ----------------------------------------------------------------------- rules

def _read() -> Any:
    """The stored knob, or None if it cannot be read.

    The settings table is created at startup; if it is not there, the reading
    goes on without substitutions rather than failing every sentence.
    """
    try:
        return appsettings.get(KNOB)
    except sqlite3.Error as e:
        log.warning("substitution rules unavailable: %s", e)
        return None


# The rules as last read, and the settings revision they were read at. Reading
# the settings is blocking I/O and this runs for every sentence the server
# speaks, so the rules are held in memory and re-read only when the settings
# have actually been written since.
_cached: tuple[int, dict[str, dict[str, dict[str, Rule]]]] = (-1, {})


def _usable(table: Any) -> dict[str, Rule]:
    """The word -> rule pairs in ``table`` that can be used."""
    kept: dict[str, Rule] = {}
    if isinstance(table, dict):
        for word, replacement in table.items():
            case = False
            if _is_rule_object(replacement):
                if not isinstance(replacement["case"], bool):
                    continue
                case, replacement = replacement["case"], replacement.get("to")
            if (isinstance(word, str) and isinstance(replacement, str)
                    and word.strip() and replacement.strip()):
                kept[word.strip()] = Rule(replacement.strip(), case)
    return kept


def _stored() -> dict[str, dict[str, dict[str, Rule]]]:
    """The owner's rules as stored ({language: {engine: {word: rule}}}), with
    anything unusable dropped.

    The write path validates, but a rule map can also arrive from an older
    server (the flat form, before rules had engines) or a hand-edited database,
    and one rule that cannot be compiled must not take the reading down with it.
    """
    global _cached
    revision = appsettings.revision()
    at, rules = _cached
    if at == revision:
        return rules
    raw = _read()
    if raw is None:
        return {}   # a failed read is not cached: try again next time
    out: dict[str, dict[str, dict[str, Rule]]] = {}
    if isinstance(raw, dict):
        for code, table in raw.items():
            if not isinstance(code, str) or not isinstance(table, dict):
                continue
            buckets: dict[str, dict[str, Rule]] = {}
            flat = _usable({w: r for w, r in table.items()
                            if not isinstance(r, dict) or _is_rule_object(r)})
            if flat:
                buckets[ANY] = flat
            for engine, scoped in table.items():
                if (not isinstance(scoped, dict) or _is_rule_object(scoped)
                        or not isinstance(engine, str)):
                    continue
                engine = engine.strip().lower()
                if engine != ANY and engine not in ENGINES:
                    continue
                kept = _usable(scoped)
                if kept:
                    buckets.setdefault(engine, {}).update(kept)
            if buckets:
                out[code.strip().lower()] = buckets
    _cached = (revision, out)
    return out


def _language_of(language: Optional[str], voice: Optional[str]) -> str:
    """The language bucket for this text: its own if we know it, the voice's
    otherwise.

    The voice is chosen to speak the language the text is in (``lang.resolve``),
    so it is the answer almost every time; the explicit language wins where
    there is one.
    """
    for candidate in (language, lang.language_of(voice)):
        code = (candidate or "").strip().lower()
        if len(code) == 2 and code.isalpha():
            return code
    return ""


def rules_for(language: Optional[str] = None, voice: Optional[str] = None,
              engine: Optional[str] = None) -> dict[str, Rule]:
    """The rules that apply to this text: the ones for every language, plus
    the ones for its own, each narrowed to ``engine`` where one is given.

    Layered least to most specific, so the later layer wins a clash: "*" for
    every engine, "*" for this engine, the language's for every engine, the
    language's for this engine. Each rule carries its layer as ``rank``, for
    the rules whose words differ but can still match the same text.
    """
    stored = _stored()
    code = _language_of(language, voice)
    engine = (engine or "").strip().lower()
    rules: dict[str, Rule] = {}
    for base, lang_code in ((0, ANY), (2, code)):
        buckets = stored.get(lang_code) if lang_code else None
        if not buckets:
            continue
        layers = [(base, buckets.get(ANY))]
        if engine in ENGINES:
            layers.append((base + 1, buckets.get(engine)))
        for rank, table in layers:
            for word, rule in (table or {}).items():
                rules[word] = rule._replace(rank=rank)
    return rules


def _matcher(rules: dict[str, Any]) -> Optional[_Matcher]:
    if not rules:
        return None
    rules = {word: rule if isinstance(rule, Rule) else Rule(rule) for word, rule in rules.items()}
    key = json.dumps(rules, sort_keys=True, ensure_ascii=False)
    matcher = _matchers.get(key)
    if matcher is None:
        matcher = _Matcher(rules)
        if len(_matchers) >= _MATCHER_CACHE_MAX:
            _matchers.clear()
        _matchers[key] = matcher
    return matcher


# ---------------------------------------------------------------- application

def apply(text: str, rules: dict[str, Any]) -> tuple[str, list[Edit]]:
    """``text`` with ``rules`` applied, and the edits that got there.

    ``rules`` is a flat {word: Rule} map, as :func:`rules_for` returns it (a
    plain replacement string stands for a rule that ignores case). The edits say which part of the spoken text stands for which part of
    the original, so the word timings can be pointed back at the text the
    reader is looking at.
    """
    if not text:
        return text, []
    matcher = _matcher(rules)
    if matcher is None:
        return text, []
    parts: list[str] = []
    edits: list[Edit] = []
    orig_cursor = 0
    spoken = 0
    for m in matcher.pattern.finditer(text):
        rule = matcher.find(m.group(0))
        if rule is None:
            # A case the lookup does not know (the pattern folds more than
            # str.casefold does): leave the word alone rather than guess.
            continue
        replacement = rule.to
        gap = text[orig_cursor:m.start()]
        parts.append(gap)
        spoken += len(gap)
        start = spoken
        parts.append(replacement)
        spoken += len(replacement)
        edits.append(Edit(m.start(), m.end(), start, spoken))
        orig_cursor = m.end()
    if not edits:
        return text, []
    parts.append(text[orig_cursor:])
    return "".join(parts), edits


def plan(text: str, language: Optional[str] = None, voice: Optional[str] = None,
         engine: Optional[str] = None) -> tuple[str, list[Edit]]:
    """``text`` with the owner's rules for this language and engine applied."""
    return apply(text, rules_for(language, voice, engine))


# -------------------------------------------------------------------- mapping

def _point(edits: Sequence[Edit], offset: int) -> int:
    """The original offset one spoken offset corresponds to.

    An offset inside a substituted word maps to where that word started: the
    replacement is several characters long, and the text on the screen is
    still the original word.
    """
    delta = 0
    for e in edits:
        if offset < e.spoken_start:
            return offset + delta
        if offset < e.spoken_end:
            return e.orig_start
        delta += (e.orig_end - e.orig_start) - (e.spoken_end - e.spoken_start)
    return offset + delta


def to_original(edits: Sequence[Edit], start: int, end: int) -> tuple[int, int]:
    """Map a [start, end) span of the spoken text back onto the original.

    A span inside a substituted word maps to that word's own span, so the
    reader watches "Kokoro" light up while "Koh-KOH-roh" is spoken. A span
    that reaches past one maps to the stretch the edits cover.
    """
    if not edits:
        return start, end
    orig_start = _point(edits, start)
    orig_end = _point(edits, end)
    if orig_end <= orig_start and end > start:
        # The span sat inside a substituted word and collapsed to nothing:
        # light the word it stands for instead.
        for e in edits:
            if e.spoken_start <= start < e.spoken_end:
                return e.orig_start, e.orig_end
    return orig_start, orig_end


def map_words(words: Iterable[dict], edits: Sequence[Edit]) -> List[dict]:
    """Point a list of word timings back at the text the reader is looking at.

    ``words`` are the engine's, with ``cs``/``ce`` offsets into the spoken
    text; after this they are offsets into the original.
    """
    out = list(words)
    if not edits:
        return out
    for w in out:
        cs, ce = w.get("cs"), w.get("ce")
        if cs is None or ce is None or cs < 0 or ce < 0:
            continue
        w["cs"], w["ce"] = to_original(edits, cs, ce)
    return out
