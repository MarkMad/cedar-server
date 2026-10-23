"""The on-disk audio cache heals itself rather than failing a sentence forever."""
from __future__ import annotations

import json
import sys
import threading

import pytest

from cedar import tts


@pytest.fixture
def cache(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    return tmp_path


RAW = [{"word": "Hello", "start_time": 0.0, "end_time": 0.4},
       {"word": "there", "start_time": 0.4, "end_time": 0.9}]


def _stale(**extra):
    return {"format": tts.AUDIO_FORMAT, "words": [], "duration": 1.0, "text": "Hello there",
            "raw": RAW, "align": 0, "edits": [], **extra}


def _entry(cache, key, meta):
    (cache / f"{key}.{tts.AUDIO_FORMAT}").write_bytes(tts._SILENCE_MP3)
    (cache / f"{key}.json").write_text(meta if isinstance(meta, str) else json.dumps(meta))


def test_a_corrupt_meta_file_is_a_miss_and_is_discarded(cache):
    _entry(cache, "abc", '{"words": [{"start": 0.0, "en')  # cut off mid-write
    assert tts._read_cache("abc") is None
    assert list(cache.iterdir()) == []  # next request synthesizes it afresh


def test_a_stale_alignment_is_rewritten_without_leaving_temp_files(cache):
    _entry(cache, "old", _stale())
    got = tts._read_cache("old")
    assert got["cached"] is True and got["words"]
    assert json.loads((cache / "old.json").read_text())["align"] == tts._ALIGN_VERSION
    assert not list(cache.glob("*.tmp"))


@pytest.mark.skipif(sys.platform == "win32",
                    reason="Windows refuses to replace a file another thread has open")
def test_concurrent_realigns_of_one_entry_never_corrupt_it(cache):
    _entry(cache, "race", _stale())
    errors = []

    def read():
        try:
            for _ in range(20):
                assert tts._read_cache("race") is not None
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=read) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    json.loads((cache / "race.json").read_text())
    assert not list(cache.glob("*.tmp"))
