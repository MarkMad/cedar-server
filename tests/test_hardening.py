"""Small robustness fixes: database calls off the event loop, input limits,
cleanup after a failed import, and markdown that must not eat identifiers."""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from conftest import AUTH

from cedar import chunker, dictionary, translate
from cedar.routes import documents

TEXT = "It was a bright cold day in April. The clocks were striking thirteen."


def _doc(client):
    return client.post("/api/documents/from_text", headers=AUTH,
                       json={"text": TEXT, "title": "T"}).json()["id"]


# ------------------------------------------------------- database off the loop

def test_dictionary_cache_runs_off_the_event_loop(monkeypatch):
    seen = {}

    def cached(word, lang):
        seen["read"] = threading.current_thread()
        return False, None, None

    def store(word, lang, provider, entry):
        seen["write"] = threading.current_thread()

    async def race(word, lang):
        return None, None

    monkeypatch.setattr(dictionary, "_cached", cached)
    monkeypatch.setattr(dictionary, "_store", store)
    monkeypatch.setattr(dictionary, "_race", race)

    assert asyncio.run(dictionary.define("wolves", "en")) is None
    assert seen["read"] is not threading.main_thread()
    assert seen["write"] is not threading.main_thread()


def test_translation_cache_runs_off_the_event_loop(monkeypatch):
    seen = {}

    def cached(word, src, dst):
        seen["read"] = threading.current_thread()
        return False, []

    def store(word, src, dst, terms):
        seen["write"] = threading.current_thread()

    async def fetch(word, dst):
        return ["loup"]

    monkeypatch.setattr(translate, "enabled", lambda: True)
    monkeypatch.setattr(translate, "_cached", cached)
    monkeypatch.setattr(translate, "_store", store)
    monkeypatch.setattr(translate, "_fetch", fetch)

    assert asyncio.run(translate.lookup("wolf", "en", "fr")) == {"lang": "fr", "terms": ["loup"]}
    assert seen["read"] is not threading.main_thread()
    assert seen["write"] is not threading.main_thread()


# ------------------------------------------------------------- input handling

@pytest.mark.parametrize("voice", ["af_heart; DROP", "a/../etc", "af heart", "a" * 41, "é_x"])
def test_voice_choice_rejects_ids_the_other_entry_points_reject(client, voice):
    r = client.post(f"/api/documents/{_doc(client)}/voice", headers=AUTH, json={"voice": voice})
    assert r.status_code == 400


def test_bookmark_note_is_capped(client):
    did = _doc(client)
    r = client.post(f"/api/documents/{did}/bookmarks", headers=AUTH,
                    json={"idx": 0, "note": "n" * (documents.MAX_NOTE_CHARS + 500)})
    assert r.status_code == 200
    assert len(r.json()["bookmark"]["note"]) == documents.MAX_NOTE_CHARS


def test_failed_import_leaves_no_orphaned_pdf(monkeypatch, tmp_path):
    result = SimpleNamespace(title="T", num_pages=1, pages=[], chunks=["One sentence."], toc=[])
    monkeypatch.setattr(documents, "UPLOAD_DIR", tmp_path)
    monkeypatch.setattr(documents, "extract_pdf", lambda path, title: result)

    def broken(**kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(documents.db, "create_document", broken)
    with pytest.raises(RuntimeError):
        documents._ingest_pdf_bytes(b"%PDF-1.4 stub", "T")
    assert list(tmp_path.iterdir()) == []


# ----------------------------------------------------------------- markdown

def test_underscores_inside_words_survive_markdown_stripping():
    assert chunker._strip_inline_md("call snake_case_name now") == "call snake_case_name now"
    assert chunker._strip_inline_md("see __init__.py and my_var_2") == "see init.py and my_var_2"


def test_underscore_emphasis_at_word_edges_is_still_stripped():
    assert chunker._strip_inline_md("an _emphasised_ word") == "an emphasised word"
    assert chunker._strip_inline_md("a __bold__ move, and *stars* too") == "a bold move, and stars too"


def test_every_line_of_a_blockquote_loses_its_marker():
    assert chunker._strip_inline_md("> first line\n> second line\n> third") == \
        "first line\nsecond line\nthird"
