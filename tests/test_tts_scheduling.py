"""Exercise real scheduling without the session's API synthesis stub."""
import asyncio

import pytest

from cedar import tts

_synthesize = tts.synthesize


def test_prefetch_is_serial_and_cached_audio_bypasses_queue(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "_synth_slots", asyncio.Semaphore(1))
        monkeypatch.setattr(tts, "_inflight", {})
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        hit = tts._key("Cached sentence", "af_heart")
        monkeypatch.setattr(tts, "_read_cache",
                            lambda key: {"cached": True} if key == hit else None)

        async def synth(key, text, voice):
            calls.append(text)
            started.set()
            await release.wait()
            return {"cached": False}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        first = asyncio.create_task(_synthesize("First sentence", "af_heart"))
        await asyncio.wait_for(started.wait(), 2)
        second = asyncio.create_task(_synthesize("Second sentence", "af_heart"))
        cached = await asyncio.wait_for(_synthesize("Cached sentence", "af_heart"), 2)
        assert cached["cached"] is True
        assert calls == ["First sentence"]
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        assert calls == ["First sentence", "Second sentence"]

    asyncio.run(run())


def test_disconnected_caller_does_not_cancel_shared_synthesis(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "_synth_slots", asyncio.Semaphore(1))
        monkeypatch.setattr(tts, "_inflight", {})
        cache = {}
        monkeypatch.setattr(tts, "_read_cache", lambda key: cache.get(key))
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def synth(key, text, voice):
            calls.append(text)
            started.set()
            await release.wait()
            cache[key] = {"audio_b64": "audio"}
            return cache[key]

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        first = asyncio.create_task(_synthesize("Shared sentence", "af_heart"))
        await asyncio.wait_for(started.wait(), 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not next(iter(tts._inflight.values())).cancelled()
        second = asyncio.create_task(_synthesize("Shared sentence", "af_heart"))
        release.set()
        assert (await asyncio.wait_for(second, 2))["audio_b64"] == "audio"
        assert calls == ["Shared sentence"]
        assert not tts._inflight

    asyncio.run(run())
