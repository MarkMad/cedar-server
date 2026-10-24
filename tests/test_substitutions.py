"""Word substitutions: the rules, the offsets they move, and the knob that holds them."""
from __future__ import annotations

import asyncio
import json
import struct

import httpx
import pytest

from cedar import settings, substitute, tts
from conftest import AUTH

# The session's API fixture replaces tts.synthesize for the rest of the run,
# so the real one is kept here.
_synthesize = tts.synthesize


@pytest.fixture(autouse=True)
def clean_rules():
    """The rules live in the one server database, so they are cleared either
    side of every test here."""
    settings.init_tables()
    settings.put("substitutions", {})
    yield
    settings.put("substitutions", {})


def _wav(pcm=b"\x01\x00" * 6000):
    fmt = struct.pack("<HHIIHH", 1, 1, 24000, 48000, 2, 16)
    size = len(pcm)
    return (b"RIFF" + struct.pack("<I", size + 36) + b"WAVEfmt " + struct.pack("<I", len(fmt))
            + fmt + b"data" + struct.pack("<I", size) + pcm)


# ------------------------------------------------------------------ validation

def test_validate_accepts_a_language_map_and_normalises_it():
    out = substitute.validate({"EN": {"Kokoro": "Koh-KOH-roh"}, "*": {"Mr.": "Mister"}})
    # The flat form means "every engine"; the canonical form says so.
    assert out == {"en": {"*": {"Kokoro": "Koh-KOH-roh"}}, "*": {"*": {"Mr.": "Mister"}}}


def test_validate_accepts_engine_scoped_rules_mixed_with_flat_ones():
    out = substitute.validate({"en": {"cat": "dog", "Pocket": {"Kokoro": "Ko-ko-ro"},
                                      "kokoro": {"Kokoro": "Koh-KOH-roh"}}})
    assert out == {"en": {"*": {"cat": "dog"}, "pocket": {"Kokoro": "Ko-ko-ro"},
                          "kokoro": {"Kokoro": "Koh-KOH-roh"}}}


@pytest.mark.parametrize("bad", [
    {"en": {"piper": {"a": "b"}}},          # not an engine
    {"en": {"pocket": {"a": ""}}},          # nothing to say instead
    {"en": {"pocket": {"...": "x"}}},       # no letters in the word
    {"en": {"pocket": {"a": 3}}},           # a replacement that is not a string
])
def test_validate_refuses_bad_engine_rules(bad):
    with pytest.raises(substitute.RulesError):
        substitute.validate(bad)


def test_validate_caps_count_the_rules_across_engines():
    with pytest.raises(substitute.RulesError):
        substitute.validate({"en": {"kokoro": {f"w{i}": "x" for i in range(120)},
                                    "pocket": {f"v{i}": "x" for i in range(120)}}})


@pytest.mark.parametrize("bad", [
    {"en": {"": "x"}},                 # an empty word
    {"en": {"a": ""}},                 # nothing to say instead
    {"en": {"a": "b", "c": 3}},        # a replacement that is not a string
    {"eng": {"a": "b"}},               # not a language code
    {"en": "not a table"},
    ["not a map"],
    {"en": {"...": "x"}},              # punctuation with no letters in it
])
def test_validate_refuses_a_map_it_cannot_use(bad):
    with pytest.raises(substitute.RulesError):
        substitute.validate(bad)


def test_validate_caps_a_runaway_map():
    with pytest.raises(substitute.RulesError):
        substitute.validate({"en": {f"word{i}": "x" for i in range(500)}})


# ------------------------------------------------------------------- matching

def test_rules_match_whole_words_only_and_ignore_case():
    spoken, edits = substitute.apply("The Cat sat. Cats and cat.", {"cat": "dog"})
    assert spoken == "The dog sat. Cats and dog."
    # "Cats" is left alone: the rule is for a whole word.
    assert [(e.orig_start, e.orig_end) for e in edits] == [(4, 7), (22, 25)]


def test_the_replacement_is_used_verbatim():
    spoken, _ = substitute.apply("KOKORO and Kokoro", {"kokoro": "Koh-KOH-roh"})
    assert spoken == "Koh-KOH-roh and Koh-KOH-roh"


def test_a_rule_is_not_applied_inside_a_longer_word():
    spoken, edits = substitute.apply("concatenate", {"cat": "dog"})
    assert spoken == "concatenate"
    assert edits == []


def test_the_language_bucket_and_the_any_bucket():
    settings.put("substitutions", {"*": {"a": "A"}, "es": {"b": "B"}})
    # The voice says which language this is; "af_heart" is English, "ef_dora"
    # is Spanish.
    assert substitute.rules_for(None, "ef_dora") == {"a": "A", "b": "B"}
    assert substitute.rules_for(None, "af_heart") == {"a": "A"}
    # An explicit language wins over the voice.
    assert substitute.rules_for("es", "af_heart") == {"a": "A", "b": "B"}


def test_a_new_rule_is_in_force_at_once():
    # The rules are held in memory between sentences, so a change has to be
    # seen the moment it is written rather than whenever the cache happens
    # to be refreshed.
    settings.put("substitutions", {"en": {"cat": "dog"}})
    assert substitute.rules_for(None, "af_heart") == {"cat": "dog"}
    settings.put("substitutions", {"en": {"cat": "dog", "bird": "plane"}})
    assert substitute.rules_for(None, "af_heart") == {"cat": "dog", "bird": "plane"}
    settings.put("substitutions", {})
    assert substitute.rules_for(None, "af_heart") == {}


def test_a_flat_rule_map_from_an_older_server_still_applies():
    # Stored before rules had engines: written straight to the knob, unvalidated.
    settings.put("substitutions", {"en": {"cat": "dog"}, "*": {"Mr.": "Mister"}})
    for engine in (None, "kokoro", "pocket"):
        assert substitute.rules_for(None, "af_heart", engine) == {"cat": "dog", "Mr.": "Mister"}


def test_an_engine_scoped_rule_applies_to_that_engine_only():
    settings.put("substitutions", {"en": {"pocket": {"Kokoro": "Ko-ko-ro"},
                                          "kokoro": {"Kokoro": "Koh-KOH-roh"}}})
    assert substitute.rules_for(None, "af_heart", "pocket") == {"Kokoro": "Ko-ko-ro"}
    assert substitute.rules_for(None, "af_heart", "kokoro") == {"Kokoro": "Koh-KOH-roh"}
    # No engine given: only the rules that hold for every engine.
    assert substitute.rules_for(None, "af_heart") == {}


def test_the_most_specific_rule_wins():
    settings.put("substitutions", {
        "*": {"*": {"w": "any-any"}, "pocket": {"w": "any-pocket", "x": "x-any-pocket"}},
        "en": {"*": {"w": "en-any", "y": "y-en-any"}, "pocket": {"w": "en-pocket"}},
    })
    assert substitute.rules_for(None, "af_heart", "pocket")["w"] == "en-pocket"
    # language-any-engine beats "*" + engine ...
    settings.put("substitutions", {
        "*": {"pocket": {"w": "any-pocket"}}, "en": {"*": {"w": "en-any"}}})
    assert substitute.rules_for(None, "af_heart", "pocket")["w"] == "en-any"
    # ... which beats "*" for every engine.
    settings.put("substitutions", {"*": {"*": {"w": "any-any"}, "pocket": {"w": "any-pocket"}}})
    assert substitute.rules_for(None, "af_heart", "pocket")["w"] == "any-pocket"
    assert substitute.rules_for(None, "af_heart", "kokoro")["w"] == "any-any"


def test_a_stored_bad_engine_bucket_is_dropped_not_fatal():
    settings.put("substitutions", {"en": {"piper": {"a": "b"}, "pocket": {"c": 3}, "ok": "fine"}})
    assert substitute.rules_for(None, "af_heart", "pocket") == {"ok": "fine"}


def test_the_engine_follows_the_voice():
    assert tts.engine_of("af_pocket_alba") == "pocket"
    assert tts.engine_of("af_heart") == "kokoro"
    assert tts.engine_of(None) == "kokoro"


# --------------------------------------------------------------------- offsets

def test_a_span_inside_a_substituted_word_lights_that_word():
    original = "Kokoro is here."
    spoken, edits = substitute.apply(original, {"Kokoro": "Koh-KOH-roh"})
    assert spoken == "Koh-KOH-roh is here."
    # The whole replacement stands for the one word on the screen.
    assert substitute.to_original(edits, 0, 11) == (0, 6)
    # A piece of it stands for the same word.
    assert substitute.to_original(edits, 4, 7) == (0, 6)
    # The words after it move back with it.
    assert substitute.to_original(edits, 12, 14) == (7, 9)
    assert substitute.to_original(edits, 15, 19) == (10, 14)


def test_a_span_with_no_edits_is_left_alone():
    assert substitute.to_original([], 3, 8) == (3, 8)


def test_a_rule_for_another_language_does_not_apply():
    settings.put("substitutions", {"es": {"gato": "perro"}})
    spoken, edits = substitute.apply("The gato sat.", substitute.rules_for(None, "af_heart"))
    assert spoken == "The gato sat."
    assert edits == []
    # The Spanish voice is the one that gets the rule.
    spoken, edits = substitute.apply("The gato sat.", substitute.rules_for(None, "ef_dora"))
    assert spoken == "The perro sat."
    assert len(edits) == 1


def test_the_any_bucket_applies_to_every_language():
    settings.put("substitutions", {"*": {"gato": "perro"}})
    spoken, _ = substitute.apply("The gato sat.", substitute.rules_for(None, "af_heart"))
    assert spoken == "The perro sat."


# ----------------------------------------------------------------- the knob

def test_the_server_knob_takes_the_rules(client):
    body = {"substitutions": {"en": {"Kokoro": "Koh-KOH-roh"}}}
    response = client.put("/api/settings/server", headers=AUTH, json=body)
    assert response.status_code == 200
    assert response.json()["substitutions"] == {"en": {"*": {"Kokoro": "Koh-KOH-roh"}}}

    bad = client.put("/api/settings/server", headers=AUTH,
                     json={"substitutions": {"en": {"Kokoro": ""}}})
    assert bad.status_code == 400


# -------------------------------------------------------------- the pipeline

def test_the_cache_key_follows_the_words_that_are_spoken():
    plain = tts._key("Kokoro is here.", "af_heart")
    # No rules: the key is what it always was, so every cached clip survives.
    assert plain == tts._key("Kokoro is here.", "af_heart", "Kokoro is here.")
    # A rule that changes what is said is a different clip.
    assert plain != tts._key("Kokoro is here.", "af_heart", "Koh-KOH-roh is here.")


def test_synthesis_speaks_the_rule_and_lights_the_original_word(monkeypatch, tmp_path):
    settings.put("substitutions", {"en": {"Kokoro": "Koh-KOH-roh"}})
    asked = []

    async def ask(text, voice, fmt):
        asked.append(text)
        return b"\xff\xfb\x90\x00" + b"\0" * 64, [
            {"word": "Koh", "start": 0.0, "end": 0.3},
            {"word": "KOH", "start": 0.3, "end": 0.6},
            {"word": "roh", "start": 0.6, "end": 0.9},
            {"word": "is", "start": 0.9, "end": 1.1},
            {"word": "here", "start": 1.1, "end": 1.5},
        ]

    monkeypatch.setattr(tts, "_ask_kokoro", ask)
    monkeypatch.setattr(tts, "_probe", lambda: False)
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    monkeypatch.setattr(tts, "_read_cache", lambda key: None)
    for name in ("_inflight", "_active"):
        monkeypatch.setattr(tts, name, {})
    for name in ("_demand", "_speculative", "_windows"):
        monkeypatch.setattr(tts, name, tts.OrderedDict())

    original = "Kokoro is here."
    result = asyncio.run(_synthesize(original, "af_heart"))

    # The engine was given the substituted words...
    assert asked == ["Koh-KOH-roh is here."]
    # ...and the highlight points at the words on the screen: the three
    # syllables of the replacement all stand for "Kokoro", and the words
    # after it keep their own spans.
    assert [(w["cs"], w["ce"]) for w in result["words"]] == [
        (0, 6), (0, 6), (0, 6), (7, 9), (10, 14)]
    assert "edits" not in result


def test_the_pocket_stream_says_the_substitution(monkeypatch):
    settings.put("substitutions", {"en": {"Kokoro": "Koh-KOH-roh"}})
    calls = []

    async def handler(request):
        calls.append(request.content)
        return httpx.Response(200, content=_wav())

    monkeypatch.setattr(tts, "TTS_CONCURRENCY", 1)
    monkeypatch.setattr(tts, "TTS_TIMEOUT", 2)
    monkeypatch.setattr(tts, "POCKET_URL", "http://pocket.invalid")
    monkeypatch.setattr(tts, "_new_synthesis_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    for name in ("_inflight", "_active"):
        monkeypatch.setattr(tts, name, {})
    for name in ("_demand", "_speculative", "_windows"):
        monkeypatch.setattr(tts, name, tts.OrderedDict())

    async def run():
        return [json.loads(frame) async for frame in
                tts.stream_pocket("Kokoro is here.", "af_pocket_alba")]

    frames = asyncio.run(run())
    assert frames[-1]["type"] == "end"
    assert calls == [b"text=Koh-KOH-roh+is+here.&voice_url=alba"]
