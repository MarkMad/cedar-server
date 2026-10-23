"""The owner's own voice samples: read from the manifest, spoken by Pocket."""
from __future__ import annotations

import asyncio
import json

import pytest

from cedar import tts

# conftest.py swaps tts.list_voices for a stub once the API client starts, so
# the real one is kept here: this test is about what the picker is offered.
_real_list_voices = tts.list_voices


@pytest.fixture
def pocket_voices():
    """The Pocket voice table, restored after the test."""
    saved = dict(tts._POCKET_VOICES)
    yield tts._POCKET_VOICES
    tts._POCKET_VOICES.clear()
    tts._POCKET_VOICES.update(saved)


def _samples(tmp_path, entries, files=("narrator.wav",)):
    """A samples folder with a manifest and the files it names."""
    folder = tmp_path / "voices"
    folder.mkdir(exist_ok=True)
    (folder / "manifest.json").write_text(json.dumps(entries), encoding="utf-8")
    for name in files:
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF0000WAVE")
    return folder


def _register(monkeypatch, folder, base="http://voicefiles:8000"):
    monkeypatch.setattr(tts, "POCKET_VOICES_DIR", str(folder))
    monkeypatch.setattr(tts, "POCKET_VOICE_BASE", base)
    return tts._register_cloned_voices()


def test_cloned_voice_is_registered(monkeypatch, tmp_path, pocket_voices):
    folder = _samples(tmp_path, {
        "narrator": {"id": "af_pocket_narrator", "file": "narrator.wav"},
    })
    voices = _register(monkeypatch, folder)

    assert voices == {"af_pocket_narrator": "http://voicefiles:8000/narrator.wav"}
    assert "af_pocket_narrator" in tts._POCKET_VOICES
    # A cloned voice is a Pocket voice: same path, same stream, same cache.
    assert tts.is_pocket_voice("af_pocket_narrator")


def test_exported_voice_in_a_subfolder(monkeypatch, tmp_path, pocket_voices):
    folder = _samples(tmp_path, {
        "narrator": {"id": "af_pocket_narrator", "file": "exported/narrator.safetensors"},
    }, files=("exported/narrator.safetensors",))
    voices = _register(monkeypatch, folder)

    assert voices == {
        "af_pocket_narrator": "http://voicefiles:8000/exported/narrator.safetensors"}


@pytest.mark.parametrize("entry", [
    {"id": "af_heart", "file": "narrator.wav"},              # a Kokoro id: same cache key
    {"id": "af_pocket_Alba", "file": "narrator.wav"},        # not a voice id
    {"id": "af_pocket_narrator", "file": "../narrator.wav"},  # out of the folder
    {"id": "af_pocket_narrator", "file": "narrator.txt"},     # not a sample
    {"id": "af_pocket_narrator", "file": "missing.wav"},      # not there
    {"id": "af_pocket_alba", "file": "narrator.wav"},         # already a voice
    {"file": "narrator.wav"},                                # no id
    "narrator.wav",                                          # not an entry
])
def test_unusable_entries_are_skipped(monkeypatch, tmp_path, pocket_voices, entry):
    folder = _samples(tmp_path, {"narrator": entry})
    assert _register(monkeypatch, folder) == {}


def test_unreadable_manifest_is_not_fatal(monkeypatch, tmp_path, pocket_voices):
    folder = tmp_path / "voices"
    folder.mkdir()
    (folder / "manifest.json").write_text("{not json", encoding="utf-8")
    assert _register(monkeypatch, folder) == {}

    (folder / "manifest.json").write_text("[]", encoding="utf-8")
    assert _register(monkeypatch, folder) == {}


def test_without_a_base_url_there_are_no_cloned_voices(monkeypatch, tmp_path, pocket_voices):
    folder = _samples(tmp_path, {
        "narrator": {"id": "af_pocket_narrator", "file": "narrator.wav"},
    })
    assert _register(monkeypatch, folder, base="") == {}


def test_cloned_voice_reaches_pocket(monkeypatch, tmp_path, pocket_voices):
    folder = _samples(tmp_path, {
        "narrator": {"id": "af_pocket_narrator", "file": "narrator.wav"},
    })
    _register(monkeypatch, folder)

    posted = []

    class Response:
        content = b"RIFFtest-WAVE"

        def raise_for_status(self):
            pass

    async def _post(url, *, data):
        posted.append((url, data))
        return Response()

    monkeypatch.setattr(tts, "POCKET_URL", "http://pocket:8000")

    class Client:
        post = staticmethod(_post)

        async def aclose(self):
            pass

    monkeypatch.setattr(tts, "_new_synthesis_client", Client)

    asyncio.run(tts._ask_pocket("Hello there", "af_pocket_narrator"))

    assert posted == [("http://pocket:8000/tts", {
        "text": "Hello there",
        "voice_url": "http://voicefiles:8000/narrator.wav",
    })]


def test_cloned_voice_is_offered_by_the_picker(monkeypatch, tmp_path, pocket_voices):
    folder = _samples(tmp_path, {
        "narrator": {"id": "af_pocket_narrator", "file": "narrator.wav"},
    })
    _register(monkeypatch, folder)

    monkeypatch.setattr(tts, "POCKET_URL", "http://pocket:8000")

    async def _healthy():
        return True

    monkeypatch.setattr(tts, "pocket_health", _healthy)

    voices = asyncio.run(_real_list_voices())
    assert "af_pocket_narrator" in voices


def test_legacy_v0_voices_are_not_offered(monkeypatch):
    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"voices": ["af_heart", "af_bella", "af_v0", "af_v0bella", "am_v0adam"]}

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            pass

        async def get(self, url):
            return Resp()

    monkeypatch.setattr(tts.httpx, "AsyncClient", Client)
    monkeypatch.setattr(tts, "POCKET_URL", "")

    assert asyncio.run(_real_list_voices()) == ["af_bella", "af_heart"]
