"""Real incremental transport, shared scheduling, and stream completion checks."""
import asyncio
import base64
import json
import struct

import httpx
import pytest
from fastapi import FastAPI

from conftest import AUTH
from cedar import pocket_stream, tts
from cedar.routes import live

_synthesize = tts.synthesize
VOICE = "af_pocket_alba"


def wav(pcm=b"\x01\x00" * 6000, *, declared=None, fmt=None):
    size = len(pcm) if declared is None else declared
    fmt = fmt or struct.pack("<HHIIHH", 1, 1, 24000, 48000, 2, 16)
    riff_size = 0xFFFFFFFF if size == 0xFFFFFFFF else size + 36
    return (b"RIFF" + struct.pack("<I", riff_size) + b"WAVEfmt " + struct.pack("<I", len(fmt))
            + fmt + b"data" + struct.pack("<I", size) + pcm)


class Source(httpx.AsyncByteStream):
    def __init__(self, pieces, gate=None):
        self.pieces, self.gate, self.closed = pieces, gate, False

    async def __aiter__(self):
        for piece in self.pieces:
            yield piece
        if self.gate:
            await self.gate.wait()

    async def aclose(self):
        self.closed = True


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(tts, "TTS_CONCURRENCY", 1)
    monkeypatch.setattr(tts, "TTS_LOOKAHEAD", 3)
    monkeypatch.setattr(tts, "TTS_TIMEOUT", 2)
    monkeypatch.setattr(tts, "POCKET_URL", "http://pocket.invalid")
    monkeypatch.setattr(tts, "_read_cache", lambda key: None)
    for name in ("_inflight", "_active"):
        monkeypatch.setattr(tts, name, {})
    for name in ("_demand", "_speculative", "_windows"):
        monkeypatch.setattr(tts, name, tts.OrderedDict())


def backend(monkeypatch, source, status=200):
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(status, stream=source)

    monkeypatch.setattr(tts, "_new_synthesis_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return calls


async def collect():
    return [json.loads(frame) async for frame in tts.stream_pocket("Hello there", VOICE)]


async def until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 2)


@pytest.mark.parametrize("declared", [None, 2_000_000_000, 0xFFFFFFFF])
def test_valid_wav_and_end_totals(monkeypatch, declared):
    async def run():
        pcm = b"\x01\x00" * 6000
        source = Source([wav(pcm, declared=declared)])
        calls = backend(monkeypatch, source)
        frames = await collect()
        assert frames[0] == {"type": "metadata", "version": 1, "format": "pcm_s16le",
                             "sample_rate": 24000, "channels": 1, "voice": VOICE}
        audio = frames[1:-1]
        assert b"".join(base64.b64decode(f["audio_b64"]) for f in audio) == pcm
        offset = 0
        for seq, frame in enumerate(audio):
            chunk = base64.b64decode(frame["audio_b64"])
            assert frame["seq"] == seq and frame["sample_offset"] == offset // 2
            assert len(chunk) <= 8192 and len(chunk) % 2 == 0
            offset += len(chunk)
        assert frames[-1] == {"type": "end", "audio_frames": len(audio), "pcm_bytes": 12000,
                              "samples": 6000, "duration": 0.25}
        assert calls[0].url == "http://pocket.invalid/tts"
        assert calls[0].content == b"text=Hello+there&voice_url=alba"
        assert source.closed and not tts._active
    asyncio.run(run())


def test_audio_arrives_before_upstream_end(monkeypatch, caplog):
    async def run():
        gate = asyncio.Event()
        source = Source([wav(declared=2_000_000_000)], gate)
        backend(monkeypatch, source)
        stream = tts.stream_pocket("Hello", VOICE)
        with caplog.at_level("INFO", logger="cedar.tts"):
            assert json.loads(await anext(stream))["type"] == "metadata"
            frame = json.loads(await asyncio.wait_for(anext(stream), 1))
        assert frame["type"] == "audio" and not gate.is_set() and not source.closed
        assert "first_pcm_s=" in caplog.text
        gate.set()
        rest = [json.loads(frame) async for frame in stream]
        assert rest[-1]["type"] == "end"
    asyncio.run(run())


@pytest.mark.parametrize("bad", [
    b"not a wave", wav()[:30], wav()[:-2], wav(b""),
    wav(b"\0", declared=2_000_000_000),
    wav(fmt=struct.pack("<HHIIHH", 1, 2, 24000, 96000, 4, 16)),
    wav(fmt=struct.pack("<HHIIHH", 3, 1, 24000, 96000, 4, 32)),
    wav() + b"extra", wav(declared=1),
], ids=["not-wave", "short-header", "short-data", "empty-data", "odd-stream", "stereo",
        "float-format", "extra-bytes", "odd-declared"])
def test_malformed_and_truncated_wav_never_end(monkeypatch, bad):
    async def run():
        source = Source([bad])
        backend(monkeypatch, source)
        frames = await collect()
        assert frames[-1] == {"type": "error", "code": "voice_service_failed"}
        assert all(f["type"] != "end" for f in frames)
        assert source.closed and not tts._active
    asyncio.run(run())


def test_upstream_http_and_transport_failures_are_sanitized(monkeypatch):
    class Failed(Source):
        async def __aiter__(self):
            yield wav(declared=2_000_000_000)
            raise httpx.ReadError("Secret upstream URL and text")

    async def run():
        for source, status in [(Source([b"Secret upstream body"]), 500), (Failed([]), 200)]:
            backend(monkeypatch, source, status)
            frames = await collect()
            assert frames[-1] == {"type": "error", "code": "voice_service_failed"}
            assert all(f["type"] != "end" for f in frames)
            assert "Secret" not in json.dumps(frames)
            assert source.closed and not tts._active
    asyncio.run(run())


def test_output_limit_and_absolute_deadline(monkeypatch):
    async def run():
        monkeypatch.setattr(pocket_stream, "MAX_PCM_BYTES", 8)
        backend(monkeypatch, Source([wav()]))
        assert (await collect())[-1]["type"] == "error"
        monkeypatch.setattr(pocket_stream, "MAX_PCM_BYTES", 14_400_000)
        monkeypatch.setattr(tts, "TTS_TIMEOUT", 0.05)
        source = Source([wav(declared=2_000_000_000)], asyncio.Event())
        backend(monkeypatch, source)
        frames = await collect()
        assert frames[-1]["type"] == "error"
        assert source.closed and not tts._active
    asyncio.run(run())


def test_slow_consumer_backpressure_expires_and_releases_slot(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "TTS_TIMEOUT", 0.05)
        source = Source([wav(b"\0\0" * 20000, declared=2_000_000_000)])
        backend(monkeypatch, source)
        stream = tts.stream_pocket("Hello", VOICE)
        assert json.loads(await anext(stream))["type"] == "metadata"
        job = next(iter(tts._active.values()))
        await asyncio.wait_for(asyncio.shield(job), 1)
        assert source.closed and not tts._active
        rest = [json.loads(frame) async for frame in stream]
        assert len(rest) <= 3  # At most two audio frames plus the terminal error.
        assert rest[-1]["type"] == "error"
    asyncio.run(run())


def test_queue_deadline_expires_without_starting_upstream(monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def synth(key, text, voice):
            started.set()
            await release.wait()
            return {}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        calls = backend(monkeypatch, Source([wav()]))
        normal = asyncio.create_task(_synthesize("Normal", "af_heart"))
        await started.wait()
        monkeypatch.setattr(tts, "TTS_TIMEOUT", 0.05)
        frames = await collect()
        assert frames == [{"type": "error", "code": "voice_service_failed"}]
        assert not calls and not tts._demand and len(tts._active) == 1
        release.set()
        await normal
        assert not tts._active
    asyncio.run(run())


def test_wav_parser_accepts_fragmented_headers_and_padded_metadata():
    async def run():
        pcm = b"\x01\0" * 7
        original = wav(pcm)
        extra = b"JUNK\x03\0\0\0abc\0"
        data = original[:36] + extra + original[36:]
        data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]

        async def pieces():
            for index in range(0, len(data), 3):
                yield data[index:index + 3]

        frames = [frame async for frame in pocket_stream.wav_pcm(pieces())]
        assert frames[0] is None
        assert b"".join(frames[1:]) == pcm
        assert all(len(frame) % 2 == 0 for frame in frames[1:])
    asyncio.run(run())


def test_stream_waits_for_normal_job_and_precedes_speculation(monkeypatch):
    async def run():
        gate, started = asyncio.Event(), asyncio.Event()
        order = []

        async def synth(key, text, voice):
            order.append(text)
            if text == "Normal":
                started.set()
                await gate.wait()
            return {}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        calls = backend(monkeypatch, Source([wav()]))
        normal = asyncio.create_task(_synthesize("Normal", "af_heart"))
        await started.wait()
        token = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", token, ["Speculative"])
        streaming = asyncio.create_task(collect())
        await until(lambda: len(tts._demand) == 1)
        assert not calls and len(tts._active) == 1
        gate.set()
        await normal
        assert (await streaming)[-1]["type"] == "end"
        await until(lambda: not tts._active)
        assert calls and order == ["Normal", "Speculative"]
    asyncio.run(run())


def test_normal_waits_for_stream_and_cancel_releases_slot(monkeypatch):
    async def run():
        source = Source([wav(declared=2_000_000_000)], asyncio.Event())
        backend(monkeypatch, source)
        stream = tts.stream_pocket("Hello", VOICE)
        await anext(stream)
        await anext(stream)
        calls = []

        async def synth(key, text, voice):
            calls.append(text)
            return {}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        normal = asyncio.create_task(_synthesize("Normal", "af_heart"))
        await until(lambda: bool(tts._demand))
        assert not calls and len(tts._active) == 1
        await stream.aclose()
        await normal
        assert source.closed and calls == ["Normal"] and not tts._active
    asyncio.run(run())


def test_cancel_queued_stream_does_not_start_upstream(monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def synth(key, text, voice):
            started.set()
            await release.wait()
            return {}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        calls = backend(monkeypatch, Source([wav()]))
        normal = asyncio.create_task(_synthesize("Normal", "af_heart"))
        await started.wait()
        streaming = asyncio.create_task(collect())
        await until(lambda: bool(tts._demand))
        streaming.cancel()
        with pytest.raises(asyncio.CancelledError):
            await streaming
        assert not tts._demand and not calls and len(tts._active) == 1
        release.set()
        await normal
        assert not tts._active
    asyncio.run(run())


def test_asgi_disconnect_closes_upstream_and_releases_slot(monkeypatch):
    async def run():
        source = Source([wav(declared=2_000_000_000)], asyncio.Event())
        backend(monkeypatch, source)
        app = FastAPI()
        app.include_router(live.router)
        disconnect = asyncio.Event()
        request_sent = False
        frames = []

        async def receive():
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": json.dumps({"text": "Hello", "voice": VOICE}).encode(),
                        "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body" and message.get("body"):
                frame = json.loads(message["body"])
                frames.append(frame)
                if frame["type"] == "audio":
                    disconnect.set()

        scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
                 "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/live/tts/stream",
                 "raw_path": b"/api/live/tts/stream", "query_string": b"", "root_path": "",
                 "headers": [(b"content-type", b"application/json")], "server": ("test", 80),
                 "client": ("test", 1)}
        await asyncio.wait_for(app(scope, receive, send), 1)
        await until(lambda: not tts._active)
        assert source.closed and any(f["type"] == "audio" for f in frames)
        assert all(f["type"] != "end" for f in frames)
        assert not tts._demand
    asyncio.run(run())


def test_route_auth_validation_and_opt_in(client, monkeypatch):
    path = "/api/live/tts/stream"
    assert client.post(path, json={"text": "Hello", "voice": VOICE}).status_code == 401
    for body, status in [({"text": " "}, 400), ({"text": "Hello"}, 400),
                         ({"text": "Hello", "voice": "af_heart"}, 400),
                         ({"text": "Hello", "voice": "http://evil"}, 400),
                         ({"text": "x" * 2001, "voice": VOICE}, 413)]:
        assert client.post(path, headers=AUTH, json=body).status_code == status
    monkeypatch.setattr(tts, "POCKET_URL", "")
    assert client.post(path, headers=AUTH, json={"text": "Hello", "voice": VOICE}).status_code == 503
    assert client.post("/api/live/tts", headers=AUTH, json={"text": "Hello"}).json()["format"] == "mp3"
