"""Pocket's WAV-only API still satisfies Cedar's MP3 and word-map contract."""
from __future__ import annotations

import asyncio

from cedar import tts


def test_pocket_audio_is_encoded_and_highlighted(monkeypatch, tmp_path):
    posted = []

    class Response:
        content = b"RIFFtest-WAVE"

        def raise_for_status(self):
            pass

    async def post(url, *, data):
        posted.append((url, data))
        return Response()

    monkeypatch.setattr(tts, "POCKET_URL", "http://pocket:8000")
    monkeypatch.setattr(tts._client, "post", post)
    monkeypatch.setattr(tts, "_encode", lambda source: tts._SILENCE_MP3 if source == Response.content else None)
    monkeypatch.setattr(tts, "_CACHE", tmp_path)

    voice = "af_pocket_alba"
    text = "Hello there"
    result = asyncio.run(tts._synth_uncached(tts._key(text, voice), text, voice))

    assert posted == [("http://pocket:8000/tts", {"text": text, "voice_url": "alba"})]
    assert result["format"] == "mp3"
    assert result["duration"] > 0
    assert [(word["cs"], word["ce"]) for word in result["words"]] == [(0, 5), (6, 11)]
    assert abs(result["words"][-1]["end"] - result["duration"]) < 0.001
    assert tts._read_cache(tts._key(text, voice))["cached"] is True


def test_pocket_cache_does_not_reuse_kokoro_audio():
    assert tts._key("Hello there", "af_pocket_alba") != tts._key("Hello there", "af_heart")
