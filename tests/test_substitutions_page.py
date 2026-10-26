"""The substitution editor page: open to fetch, and nothing else is opened with it."""
from __future__ import annotations

import base64
import hashlib
import re

from conftest import AUTH


def test_the_editor_page_needs_no_key(client):
    r = client.get("/substitutions")
    assert r.status_code == 200
    assert "Word substitutions" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_the_page_holds_no_data_and_is_not_cached(client):
    r = client.get("/substitutions")
    assert r.headers["cache-control"] == "no-store"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["x-content-type-options"] == "nosniff"
    # No rules, no key: the page is the same for everyone.
    assert AUTH["Authorization"].split()[-1] not in r.text


def test_the_csp_allows_only_its_own_script_and_same_origin_calls(client):
    r = client.get("/substitutions")
    csp = r.headers["content-security-policy"]
    script = re.search(r"<script>(.*?)</script>", r.text, re.DOTALL).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    assert f"script-src 'sha256-{digest}'" in csp
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    # Nothing external is loaded by the page itself.
    assert not re.search(r"""(src|href)=["']https?:""", r.text)


def test_the_page_offers_match_case_and_keeps_the_flag_through_save_and_export(client):
    text = client.get("/substitutions").text
    # The column, the shape a flagged rule is saved in, and the shape import reads back.
    assert "<th" in text and "Match case" in text
    assert "{ to: repl, case: true }" in text
    assert "matchCase" in text and "isRule" in text
    assert "must be true or false" in text


def test_only_the_exact_path_is_open(client):
    for path in ("/substitutions/", "/substitutions/x", "/substitutions.html",
                 "/static/substitutions.html", "/Substitutions"):
        assert client.get(path).status_code == 401, path


def test_a_forged_host_does_not_open_other_paths(client):
    r = client.get("/api/settings/server", headers={"Host": "x/substitutions?"})
    assert r.status_code == 401


def test_the_api_behind_the_page_is_still_gated(client):
    assert client.get("/api/settings/server").status_code == 401
    assert client.put("/api/settings/server", json={"substitutions": {}}).status_code == 401
    wrong = client.put("/api/settings/server", json={"substitutions": {}},
                       headers={"Authorization": "Bearer nope"})
    assert wrong.status_code == 401
    assert client.get("/api/settings/server", headers=AUTH).status_code == 200


# ------------------------------------------------------------------- the preview

def _wav(pcm=b"\x01\x00" * 6000):
    import struct
    fmt = struct.pack("<HHIIHH", 1, 1, 24000, 48000, 2, 16)
    return (b"RIFF" + struct.pack("<I", len(pcm) + 36) + b"WAVEfmt " + struct.pack("<I", len(fmt))
            + fmt + b"data" + struct.pack("<I", len(pcm)) + pcm)


def test_the_preview_needs_the_key(client):
    r = client.post("/api/substitutions/preview", json={"text": "cat"})
    assert r.status_code == 401


def test_the_preview_speaks_the_word_as_written_without_any_rule(client, monkeypatch):
    from cedar import settings, tts
    settings.init_tables()
    settings.put("substitutions", {"*": {"*": {"cat": "dog"}}})
    heard: list[str] = []

    async def ask(text, voice, fmt):
        heard.append(text)
        return _wav(), []

    monkeypatch.setattr(tts, "_ask_kokoro", ask)
    try:
        r = client.post("/api/substitutions/preview", headers=AUTH,
                        json={"text": "cat", "lang": "en", "engine": "kokoro"})
        assert r.status_code == 200
        assert r.headers["content-type"] == "audio/wav"
        assert r.content.startswith(b"RIFF")
        # The original word is heard as it is, even though a rule would change it ...
        assert heard == ["cat"]
        # ... and a replacement is heard literally too, not run through the rules.
        client.post("/api/substitutions/preview", headers=AUTH, json={"text": "dog cat"})
        assert heard == ["cat", "dog cat"]
    finally:
        settings.put("substitutions", {})


def test_the_preview_refuses_nothing_to_say_and_too_much(client):
    assert client.post("/api/substitutions/preview", headers=AUTH,
                       json={"text": "  "}).status_code == 400
    assert client.post("/api/substitutions/preview", headers=AUTH,
                       json={"text": "x" * 201}).status_code == 413


def test_the_preview_voice_follows_the_rules_engine():
    from cedar import tts
    from cedar.routes import system
    assert tts.is_pocket_voice(system._preview_voice("en", "pocket"))
    assert not tts.is_pocket_voice(system._preview_voice("en", "kokoro"))
    # "Both" and an unknown language fall back to the owner's own voice.
    assert system._preview_voice("*", "*") == system._preview_voice(None, None)


def test_a_failed_preview_does_not_leak_the_engine(client, monkeypatch):
    from cedar import tts

    async def boom(text, voice, **kw):
        raise RuntimeError("http://kokoro:8880 refused")

    monkeypatch.setattr(tts, "synthesize_raw", boom)
    r = client.post("/api/substitutions/preview", headers=AUTH, json={"text": "cat"})
    assert r.status_code == 502
    assert "kokoro" not in r.text
