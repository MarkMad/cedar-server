"""Private inference transports recover without restarting the application."""
import asyncio
import base64
import json
import struct
from contextlib import asynccontextmanager

import httpx
import pytest

from cedar import tts

_synthesize = tts.synthesize
VOICE = "af_pocket_alba"
PCM = b"\0\0" * 4096
WAV = (b"RIFF" + struct.pack("<I", 2_000_000_036) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16,
       1, 1, 24000, 48000, 2, 16) + b"data" + struct.pack("<I", 2_000_000_000) + PCM)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(tts, "TTS_CONCURRENCY", 1)
    monkeypatch.setattr(tts, "TTS_TIMEOUT", 2)
    monkeypatch.setattr(tts, "_read_cache", lambda key: None)
    monkeypatch.setattr(tts, "_probe", lambda: False)
    monkeypatch.setattr(tts, "_process_and_store", lambda *args: {"audio_b64": "audio", "duration": 1.0})
    for name in ("_inflight", "_active"):
        monkeypatch.setattr(tts, name, {})
    monkeypatch.setattr(tts, "_transport_closers", set())
    for name in ("_demand", "_speculative", "_windows"):
        monkeypatch.setattr(tts, name, tts.OrderedDict())


@asynccontextmanager
async def voice_server(monkeypatch, *, first="stream", trickle=False):
    """A real HTTP/1.1 peer: first response stalls, later requests finish."""
    started, disconnected = asyncio.Event(), asyncio.Event()
    calls, handlers = [], set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            body = await reader.readexactly(length)
            calls.append((headers.split(b"\r\n")[0], body))
            if len(calls) == 1:
                content = WAV if first == "stream" else b'{"audio":"'
                writer.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n")
                writer.write(f"{len(content):X}\r\n".encode() + content + b"\r\n")
                await writer.drain()
                started.set()
                if trickle:
                    while not reader.at_eof():
                        writer.write(b"1\r\nA\r\n")
                        await writer.drain()
                        await asyncio.sleep(0.005)
                else:
                    await reader.read(1)
                disconnected.set()
            else:
                content = json.dumps({"audio": base64.b64encode(b"test audio").decode(),
                                      "timestamps": []}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(content)).encode()
                             + b"\r\nConnection: close\r\n\r\n" + content)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            disconnected.set()
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            handlers.discard(task)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    url = f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
    monkeypatch.setattr(tts, "POCKET_URL", url)
    monkeypatch.setattr(tts, "KOKORO_URL", url)
    clients = []

    def client_factory():
        client = httpx.AsyncClient(timeout=httpx.Timeout(1, pool=0.05),
                                   limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
                                   trust_env=False)
        clients.append(client)
        return client

    monkeypatch.setattr(tts, "_new_synthesis_client", client_factory)
    try:
        yield started, disconnected, calls, clients
    finally:
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*list(handlers), return_exceptions=True)
        await asyncio.gather(*(client.aclose() for client in clients))


async def settle():
    async def wait():
        while tts._active or tts._transport_closers:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 2)


def test_real_stream_cancel_closes_connection_and_next_buffered_job_succeeds(monkeypatch):
    async def run():
        async with voice_server(monkeypatch) as (started, disconnected, calls, clients):
            stream = tts.stream_pocket("First sentence", VOICE)
            assert json.loads(await anext(stream))["type"] == "metadata"
            assert json.loads(await anext(stream))["type"] == "audio"
            assert started.is_set()
            await stream.aclose()
            await asyncio.wait_for(disconnected.wait(), 1)
            await settle()
            assert (await _synthesize("Next sentence", "af_heart"))["audio_b64"] == "audio"
            assert len(calls) == len(clients) == 2
            assert all(client.is_closed for client in clients)
            assert not tts._inflight
    asyncio.run(run())


def test_real_buffered_cancel_closes_connection_and_next_job_succeeds(monkeypatch):
    async def run():
        async with voice_server(monkeypatch, first="buffered") as (started, disconnected, calls, clients):
            request = asyncio.create_task(tts._ask_kokoro("First sentence", "af_heart", "mp3"))
            await asyncio.wait_for(started.wait(), 1)
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
            await asyncio.wait_for(disconnected.wait(), 1)
            await settle()
            audio, _ = await tts._ask_kokoro("Next sentence", "af_heart", "mp3")
            assert audio == b"test audio"
            assert len(calls) == len(clients) == 2
            assert all(client.is_closed for client in clients)
    asyncio.run(run())


def test_real_buffered_trickle_hits_total_deadline_then_next_job_succeeds(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "TTS_TIMEOUT", 0.08)
        async with voice_server(monkeypatch, first="buffered", trickle=True) as (_, disconnected, calls, clients):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(_synthesize("Trickling sentence", "af_heart"), 1)
            await asyncio.wait_for(disconnected.wait(), 1)
            await settle()
            assert (await _synthesize("Next sentence", "af_heart"))["audio_b64"] == "audio"
            assert len(calls) == len(clients) == 2
            assert all(client.is_closed for client in clients)
    asyncio.run(run())


def test_shutdown_closes_real_active_transport_before_return(monkeypatch):
    async def run():
        async with voice_server(monkeypatch) as (_, disconnected, calls, clients):
            stream = tts.stream_pocket("First sentence", VOICE)
            await anext(stream)
            await anext(stream)
            await tts.shutdown_synthesis()
            await asyncio.wait_for(disconnected.wait(), 1)
            assert len(calls) == 1 and clients[0].is_closed
            assert not tts._active and not tts._transport_closers
            with pytest.raises(asyncio.CancelledError):
                async for _ in stream:
                    pass
            await stream.aclose()
    asyncio.run(run())


def test_repeated_native_cancellation_cannot_interrupt_bounded_pool_cleanup(monkeypatch):
    async def run():
        entered, close_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class Client:
            closed = False

            async def aclose(self):
                close_started.set()
                await release.wait()
                self.closed = True

        client = Client()
        monkeypatch.setattr(tts, "_new_synthesis_client", lambda: client)

        async def request():
            async with tts._synthesis_client():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(request())
        await entered.wait()
        task.cancel()
        await close_started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not client.closed
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await settle()
        assert client.closed
    asyncio.run(run())


def test_stalled_cleanup_has_its_own_deadline(monkeypatch, caplog):
    async def run():
        monkeypatch.setattr(tts, "_TRANSPORT_CLOSE_TIMEOUT", 0.03)

        class Client:
            async def aclose(self):
                await asyncio.Event().wait()

        monkeypatch.setattr(tts, "_new_synthesis_client", Client)
        async with asyncio.timeout(1):
            async with tts._synthesis_client():
                pass
        await settle()
        assert "tts transport cleanup failed error=TimeoutError" in caplog.text
    asyncio.run(run())


@pytest.mark.parametrize("error", [httpx.PoolTimeout, httpx.ReadTimeout, httpx.ReadError])
def test_transport_failure_does_not_retry_with_a_second_format(monkeypatch, error):
    async def run():
        calls = []
        monkeypatch.setattr(tts, "_probe", lambda: True)

        async def ask(text, voice, fmt):
            calls.append(fmt)
            raise error("Secret engine URL")

        monkeypatch.setattr(tts, "_ask_kokoro", ask)
        with pytest.raises(error):
            await _synthesize("Hello", "af_heart")
        assert calls == ["wav"]
        assert not tts._active and not tts._inflight
    asyncio.run(run())


@pytest.mark.parametrize("stall_fallback", [False, True], ids=["status-fallback", "fallback-deadline"])
def test_status_format_fallback_is_preserved_within_one_total_deadline(monkeypatch, stall_fallback):
    async def run():
        calls, clients = [], []
        monkeypatch.setattr(tts, "_probe", lambda: True)
        if stall_fallback:
            monkeypatch.setattr(tts, "TTS_TIMEOUT", 0.15)

        async def handler(request):
            calls.append(json.loads(request.content)["response_format"])
            if len(calls) == 1:
                return httpx.Response(400, text="Unsupported format")
            if stall_fallback:
                await asyncio.Event().wait()
            return httpx.Response(200, json={"audio": base64.b64encode(b"test audio").decode()})

        def client_factory():
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
            clients.append(client)
            return client

        monkeypatch.setattr(tts, "_new_synthesis_client", client_factory)
        if stall_fallback:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(_synthesize("Hello", "af_heart"), 1)
        else:
            assert (await _synthesize("Hello", "af_heart"))["audio_b64"] == "audio"
        assert calls == ["wav", "mp3"]
        assert len(clients) == 2 and all(client.is_closed for client in clients)
        await settle()
    asyncio.run(run())
