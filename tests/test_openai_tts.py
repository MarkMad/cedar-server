"""The OpenAI-compatible /v1 API: its own key, formats, voices and errors."""
from __future__ import annotations

import asyncio
import io
import wave

import pytest

from cedar import auth, tts
from conftest import AUTH, TTS_AUTH


def _wav(frames: int = 2400, rate: int = 24_000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(b"\x01\x00" * frames)
    return buf.getvalue()


@pytest.fixture
def raw_calls(monkeypatch):
    calls: list[tuple[str, str]] = []

    async def fake(text: str, voice: str) -> bytes:
        calls.append((text, voice))
        return _wav()

    monkeypatch.setattr(tts, "synthesize_raw", fake)
    return calls


def _speech(client, headers=TTS_AUTH, **body):
    body.setdefault("input", "Hello there.")
    return client.post("/v1/audio/speech", headers=headers, json=body)


# --- the key ---------------------------------------------------------------

def test_tts_key_opens_v1(client):
    assert _speech(client).status_code == 200


def test_v1_refuses_missing_wrong_and_owner_keys(client):
    assert _speech(client, headers={}).status_code == 401
    assert _speech(client, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert _speech(client, headers=AUTH).status_code == 401


def test_tts_key_does_not_open_the_api(client):
    assert client.get("/api/voices", headers=TTS_AUTH).status_code == 401
    assert client.get("/api/voices", headers=AUTH).status_code == 200


def test_v1_is_closed_without_a_tts_key(client, monkeypatch):
    monkeypatch.setattr(auth, "_tts_key", "")
    assert _speech(client).status_code == 401
    assert _speech(client, headers={"Authorization": "Bearer "}).status_code == 401


def test_tts_key_must_differ_from_owner_key(monkeypatch):
    monkeypatch.setattr(auth, "CEDAR_TTS_KEY", auth.key())
    with pytest.raises(auth.WeakKeyError):
        auth.load_tts_key()
    monkeypatch.undo()
    auth.load_tts_key()


# --- mp3 ---------------------------------------------------------------------

def test_mp3_by_default(client):
    r = _speech(client)
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content.startswith(b"\xff\xfb")


def test_long_input_is_chunked_and_joined(client):
    sentence = "This is one sentence of reasonable length. "
    r = _speech(client, input=sentence * 40)
    assert r.status_code == 200
    # The stub clip is 68 bytes; many chunks make a longer body.
    assert len(r.content) > 68 * 3


def test_alias_maps_to_a_kokoro_voice(client, monkeypatch):
    seen: list[str] = []
    original = tts.synthesize

    async def spy(text, voice, speed=1.0):
        seen.append(voice)
        return await original(text, voice, speed)

    monkeypatch.setattr(tts, "synthesize", spy)
    assert _speech(client, voice="alloy").status_code == 200
    assert _speech(client, voice="af_bella").status_code == 200
    assert seen == ["af_alloy", "af_bella"]


# --- wav / pcm ---------------------------------------------------------------

def test_wav_has_one_header_and_all_chunks(client, raw_calls):
    r = _speech(client, input="One sentence here. " * 40, response_format="wav")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/wav"
    with wave.open(io.BytesIO(r.content), "rb") as clip:
        assert (clip.getnchannels(), clip.getsampwidth(), clip.getframerate()) == (1, 2, 24_000)
        assert clip.getnframes() == 2400 * len(raw_calls) > 2400


def test_pcm_is_headerless(client, raw_calls):
    r = _speech(client, response_format="pcm")
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/pcm"
    assert r.content == b"\x01\x00" * 2400 * len(raw_calls)


def test_mismatched_engine_format_is_a_502(client, monkeypatch):
    rates = iter([24_000, 16_000, 24_000, 16_000])

    async def fake(text, voice):
        return _wav(rate=next(rates))

    monkeypatch.setattr(tts, "synthesize_raw", fake)
    r = _speech(client, input="First sentence here. " * 40, response_format="wav")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "voice_service_failed"


# --- validation and shape ------------------------------------------------------

@pytest.mark.parametrize("body,status,code", [
    ({"input": "  "}, 400, "empty_input"),
    ({"input": "x" * 4097}, 400, "string_above_max_length"),
    ({"input": "hi", "voice": "x y"}, 400, "invalid_voice"),
    ({"input": "hi", "response_format": "opus"}, 400, "unsupported_format"),
])
def test_bad_requests_get_openai_errors(client, body, status, code):
    r = client.post("/v1/audio/speech", headers=TTS_AUTH, json=body)
    assert r.status_code == status
    assert r.json()["error"]["code"] == code
    assert r.json()["error"]["type"] == "invalid_request_error"


def test_missing_input_is_an_openai_error(client):
    r = client.post("/v1/audio/speech", headers=TTS_AUTH, json={})
    assert r.status_code == 400
    assert "error" in r.json()


def test_engine_failure_is_generic(client, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("http://kokoro.invalid:8880 said secret text")

    monkeypatch.setattr(tts, "synthesize", boom)
    r = _speech(client)
    assert r.status_code == 502
    assert "kokoro" not in r.text


def test_models_and_voices(client):
    models = client.get("/v1/models", headers=TTS_AUTH).json()
    assert models["object"] == "list" and {"id": "tts-1"}.items() <= models["data"][0].items()
    ids = [v["id"] for v in client.get("/v1/audio/voices", headers=TTS_AUTH).json()["voices"]]
    assert "alloy" in ids and "af_heart" in ids


# --- the engine path -----------------------------------------------------------

def test_synthesize_raw_uses_the_bounded_pool_and_substitutions(monkeypatch):
    """Two raw requests never run at once when TTS_CONCURRENCY is 1, and the
    engine is given the substituted words."""
    monkeypatch.setattr(tts, "TTS_CONCURRENCY", 1)
    running = peak = 0
    texts: list[str] = []

    async def ask(text, voice, fmt):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        texts.append(text)
        await asyncio.sleep(0.02)
        running -= 1
        return _wav(), []

    monkeypatch.setattr(tts, "_ask_kokoro", ask)
    monkeypatch.setattr(tts.substitute, "plan", lambda text, lang, voice: (text.replace("Dr", "Doctor"), []))

    async def run():
        return await asyncio.gather(tts.synthesize_raw("Dr Who one", "af_heart"),
                                    tts.synthesize_raw("Dr Who two", "af_heart"))

    out = asyncio.run(run())
    assert all(o.startswith(b"RIFF") for o in out)
    assert peak == 1
    assert sorted(texts) == ["Doctor Who one", "Doctor Who two"]


def test_synthesize_raw_says_nothing_for_text_without_words():
    out = asyncio.run(tts.synthesize_raw("* * * *", "af_heart"))
    with wave.open(io.BytesIO(out), "rb") as clip:
        assert clip.getnframes() > 0
