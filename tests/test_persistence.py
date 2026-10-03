"""Regression coverage for atomic updates, connection lifetime and rechunk anchors."""
import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from cedar import catalog, db, rechunk, settings
from cedar.chunker import Chunk, ExtractResult, MediaItem, TocItem


@pytest.fixture
def database(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "cedar.db")
    db.init_db()
    settings.init_tables()


def test_connections_close_after_commit_and_rollback(database):
    with db.connect() as c:
        c.execute("INSERT INTO settings VALUES ('committed', 'true')")
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")
    with pytest.raises(RuntimeError), db.connect() as failed:
        failed.execute("INSERT INTO settings VALUES ('rolled_back', 'true')")
        raise RuntimeError("abort")
    with pytest.raises(sqlite3.ProgrammingError):
        failed.execute("SELECT 1")
    assert settings.get("committed") is True
    assert settings.get("rolled_back") is None


def test_invalid_knob_patch_changes_nothing(database):
    before = settings.knobs()
    with pytest.raises(ValueError):
        settings.update_knobs({"catalog_full": True, "dict_timeout_s": "invalid"})
    assert settings.knobs() == before


def test_catalog_connection_closes(database, monkeypatch):
    monkeypatch.setattr(catalog, "CATALOG_DB", db.DB_PATH)
    with catalog._connect() as c:
        assert c.execute("SELECT 1").fetchone()[0] == 1
    with pytest.raises(sqlite3.ProgrammingError):
        c.execute("SELECT 1")


def test_knob_write_failure_rolls_back(database, monkeypatch):
    original = settings._put

    def fail_second(c, key, value):
        if key == "dict_timeout_s":
            raise sqlite3.OperationalError("simulated write failure")
        original(c, key, value)

    monkeypatch.setattr(settings, "_put", fail_second)
    with pytest.raises(sqlite3.OperationalError):
        settings.update_knobs({"catalog_full": True, "dict_timeout_s": 5})
    assert settings.get("catalog_full") is False


def test_concurrent_language_voice_updates_survive(database):
    languages = [f"language_{i}" for i in range(12)]
    barrier = Barrier(len(languages))

    def choose(language):
        barrier.wait(timeout=10)
        settings.set_voice("af_heart", language)

    with ThreadPoolExecutor(max_workers=len(languages)) as pool:
        list(pool.map(choose, languages))
    assert settings.get("voices") == dict.fromkeys(languages, "af_heart")
    assert settings.get("voice") == "af_heart"


def test_voice_mapping_and_legacy_value_roll_back_together(database, monkeypatch):
    settings.set_voice("af_heart", "en")
    original = settings._put

    def fail_legacy(c, key, value):
        if key == "voice":
            raise sqlite3.OperationalError("simulated write failure")
        original(c, key, value)

    monkeypatch.setattr(settings, "_put", fail_legacy)
    with pytest.raises(sqlite3.OperationalError):
        settings.set_voice("ef_dora", "es")
    assert settings.get("voices") == {"en": "af_heart"}
    assert settings.get("voice") == "af_heart"


def _result(texts):
    return ExtractResult("Test", 1, chunks=[Chunk(i, 1, 0, text) for i, text in enumerate(texts)],
                         toc=[TocItem(1, "Chapter", 1, 0)],
                         pages=[{"page": 1, "start": 0, "count": len(texts)}])


def _document(texts):
    result = _result(texts)
    return db.create_document("Test", "", 1, result.pages, result.chunks, result.toc,
                              kind="url", source_url="https://example.com/article",
                              media=[MediaItem(0, 1, "https://example.com/image.jpg")])


def _rewrite(c, did, result, monkeypatch):
    async def refetch(kind, url):
        return "readable source " * 20

    monkeypatch.setattr(rechunk, "_refetch_text", refetch)
    monkeypatch.setattr(rechunk, "chunk_plain_text", lambda *args, **kwargs: result)
    row = c.execute("SELECT * FROM documents WHERE id=?", (did,)).fetchone()
    asyncio.run(rechunk._rechunk_doc(c, row, True))


def test_rechunk_remaps_marks_and_media(database, monkeypatch):
    did = _document(["First sentence.", "Second sentence."])
    db.add_bookmark(did, 0, "first")
    db.add_bookmark(did, 1, "second")
    hl = db.add_highlight(did, 1, 0, 6, "Second")
    db.save_progress(did, 1)
    db.set_generated(did, "af_heart")
    result = _result(["New introduction.", "First sentence.", "Second sentence."])
    with db.connect() as c:
        _rewrite(c, did, result, monkeypatch)
        assert c.execute("SELECT anchor_idx FROM media WHERE doc_id=?", (did,)).fetchone()[0] == 2
    assert [(bm["idx"], bm["note"]) for bm in db.list_bookmarks(did)] == [(1, "first"), (2, "second")]
    moved = db.list_highlights(did)[0]
    assert (moved["id"], moved["idx"], moved["text"]) == (hl["id"], 2, "Second")
    doc = db.get_document(did)
    assert doc["content_rev"] == 2 and doc["generated_voice"] is None


@pytest.mark.parametrize("new_texts", [
    ["First sentence.", "Second sentence."],
    ["Introduction.", "First sentence.", "Second sentence."],
    ["Replacement sentence."],
])
def test_rechunk_preserves_trailing_images(database, monkeypatch, new_texts):
    did = _document(["First sentence.", "Second sentence."])
    with db.connect() as c:
        c.execute("UPDATE media SET anchor_idx=2 WHERE doc_id=?", (did,))
    with db.connect() as c:
        _rewrite(c, did, _result(new_texts), monkeypatch)
        anchor = c.execute("SELECT anchor_idx FROM media WHERE doc_id=?", (did,)).fetchone()[0]
    assert anchor == len(new_texts)
    assert db.get_document(did)["content_rev"] == 2


def test_end_of_document_bookmark_is_not_treated_as_trailing_image(database, monkeypatch):
    did = _document(["First sentence.", "Second sentence."])
    db.add_bookmark(did, 2, "invalid sentence anchor")
    with db.connect() as c:
        c.execute("UPDATE media SET anchor_idx=2 WHERE doc_id=?", (did,))
    before = db.get_document(did)
    with db.connect() as c:
        _rewrite(c, did, _result(["Replacement sentence."]), monkeypatch)
        assert c.execute("SELECT anchor_idx FROM media WHERE doc_id=?", (did,)).fetchone()[0] == 2
    assert db.get_document(did) == before


@pytest.mark.parametrize("new_texts", [
    ["First sentence.", "Changed second sentence."],
    ["First sentence.", "Second sentence.", "Second sentence."],
])
def test_rechunk_skips_changed_or_ambiguous_anchors(database, monkeypatch, new_texts):
    did = _document(["First sentence.", "Second sentence."])
    db.add_bookmark(did, 1, "keep me")
    before = db.get_document(did)
    with db.connect() as c:
        _rewrite(c, did, _result(new_texts), monkeypatch)
    assert db.get_document(did) == before
    assert db.get_sentence_text(did, 1) == "Second sentence."
    assert db.list_bookmarks(did)[0]["note"] == "keep me"


def test_failed_rechunk_cannot_be_committed_by_next_document(database, monkeypatch):
    first = _document(["First sentence.", "Second sentence."])
    second = _document(["First sentence.", "Second sentence."])
    db.add_bookmark(first, 1, "keep me")
    with db.connect() as c:
        c.execute(f"""CREATE TRIGGER fail_toc BEFORE INSERT ON toc
                     WHEN NEW.doc_id = {first}
                     BEGIN SELECT RAISE(ABORT, 'simulated rewrite failure'); END""")
        c.commit()
        result = _result(["Introduction.", "First sentence.", "Second sentence."])
        with pytest.raises(sqlite3.IntegrityError):
            _rewrite(c, first, result, monkeypatch)
        assert not c.in_transaction
        _rewrite(c, second, result, monkeypatch)
    assert db.get_sentence_text(first, 0) == "First sentence."
    assert db.get_document(first)["content_rev"] == 1
    assert db.list_bookmarks(first)[0]["idx"] == 1
    assert db.get_document(second)["content_rev"] == 2
