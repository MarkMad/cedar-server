"""Exercise real scheduling without the session's API synthesis stub."""
import asyncio

import pytest

from cedar import tts
from cedar.routes import documents

_synthesize = tts.synthesize


@pytest.fixture(autouse=True)
def isolate_scheduler(monkeypatch):
    monkeypatch.setattr(tts, "TTS_CONCURRENCY", 1)
    monkeypatch.setattr(tts, "TTS_LOOKAHEAD", 3)
    for name in ("_inflight", "_active"):
        monkeypatch.setattr(tts, name, {})
    for name in ("_demand", "_speculative", "_windows"):
        monkeypatch.setattr(tts, name, tts.OrderedDict())


def test_prefetch_is_serial_and_cached_audio_bypasses_queue(monkeypatch):
    async def run():
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


def test_live_page_starts_next_clips_before_playback_requests_them(monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        cache = {}
        monkeypatch.setattr(tts, "_read_cache", lambda key: cache.get(key))

        async def synth(key, text, voice):
            calls.append(text)
            started.set()
            await release.wait()
            result = {"audio_b64": text, "duration": 1.0}
            cache[key] = result
            return result

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        tts.prefetch_live_page("af_pocket_alba", ["First line", "Second line", "Third line"])
        await asyncio.wait_for(started.wait(), 2)
        assert calls == ["First line"]
        assert len(tts._speculative) == 2

        release.set()
        await _wait_until(lambda: not tts._inflight)
        assert calls == ["First line", "Second line", "Third line"]
        assert (await _synthesize("First line", "af_pocket_alba"))["audio_b64"] == "First line"
        assert calls == ["First line", "Second line", "Third line"]

    asyncio.run(run())


def test_disconnected_caller_does_not_cancel_shared_synthesis(monkeypatch):
    async def run():
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


async def _wait_until(predicate):
    async def wait():
        while not predicate():
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), 2)


def test_foreground_promotes_prefetch_and_takes_next_slot(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "_read_cache", lambda key: None)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def synth(key, text, voice):
            calls.append(text)
            started.set()
            if text == "Active sentence":
                await release.wait()
            return {"audio_b64": text}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        token = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", token,
                              ["Active sentence", "Pending sentence", "Wanted sentence"])
        await asyncio.wait_for(started.wait(), 2)
        wanted = asyncio.create_task(_synthesize("Wanted sentence", "af_heart"))
        await _wait_until(lambda: bool(tts._demand))
        assert len(tts._active) == 1
        release.set()
        assert (await wanted)["audio_b64"] == "Wanted sentence"
        await _wait_until(lambda: not tts._inflight)
        assert calls == ["Active sentence", "Wanted sentence", "Pending sentence"]

    asyncio.run(run())


def test_seek_drops_pending_but_keeps_running_inference_and_cache(monkeypatch):
    async def run():
        cache, calls = {}, []
        started, release = asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(tts, "_read_cache", lambda key: cache.get(key))

        async def synth(key, text, voice):
            calls.append(text)
            if text == "Active sentence":
                started.set()
                await release.wait()
            cache[key] = {"audio_b64": text, "cached": False}
            return cache[key]

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        old_token = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", old_token, ["Active sentence", "Stale sentence"])
        await started.wait()
        current_token = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", current_token, ["New sentence"])
        tts.prefetch_document(1, "af_heart", old_token, ["Older request sentence"])
        foreground = asyncio.create_task(_synthesize("Active sentence", "af_heart"))
        release.set()
        assert (await foreground)["audio_b64"] == "Active sentence"
        await _wait_until(lambda: not tts._inflight)
        assert (await _synthesize("Active sentence", "af_heart"))["audio_b64"] == "Active sentence"
        assert calls == ["Active sentence", "New sentence"]

    asyncio.run(run())


def test_lookahead_backlog_and_window_state_are_globally_bounded(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "_read_cache", lambda key: None)
        started, release = asyncio.Event(), asyncio.Event()

        async def synth(key, text, voice):
            started.set()
            await release.wait()
            return {}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        first = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", first, ["Active sentence"])
        await started.wait()
        for doc_id in range(2, 100):
            token = tts.begin_document_request(doc_id, "af_heart")
            tts.prefetch_document(doc_id, "af_heart", token,
                                  [f"Document {doc_id} sentence {idx}" for idx in range(20)])
            assert len(tts._speculative) <= tts._MAX_PREFETCH_PENDING
            assert len(tts._windows) <= tts._MAX_PREFETCH_WINDOWS
            assert len(tts._inflight) <= tts._MAX_PREFETCH_PENDING + 1
            assert len(tts._active) == 1
        await tts.shutdown_synthesis()
        assert not tts._inflight and not tts._active and not tts._windows

    asyncio.run(run())


def test_lookahead_and_demand_share_configured_concurrency(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "TTS_CONCURRENCY", 2)
        monkeypatch.setattr(tts, "_read_cache", lambda key: None)
        release = asyncio.Event()
        calls = []
        active, peak = 0, 0

        async def synth(key, text, voice):
            nonlocal active, peak
            calls.append(text)
            active += 1
            peak = max(active, peak)
            await release.wait()
            active -= 1
            return {}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        token = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", token, ["One sentence", "Two sentence", "Three sentence"])
        await _wait_until(lambda: len(calls) == 2)
        foreground = asyncio.create_task(_synthesize("Demand sentence", "af_heart"))
        await _wait_until(lambda: bool(tts._demand))
        assert active == 2
        release.set()
        await foreground
        await _wait_until(lambda: not tts._inflight)
        assert peak == 2
        assert calls == ["One sentence", "Two sentence", "Demand sentence", "Three sentence"]

    asyncio.run(run())


def test_voice_switch_clears_old_pending_and_disabled_lookahead_does_nothing(monkeypatch):
    async def run():
        monkeypatch.setattr(tts, "_read_cache", lambda key: None)
        started = asyncio.Event()

        async def synth(key, text, voice):
            started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        token = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", token, ["Active sentence", "Stale sentence"])
        await started.wait()
        tts.begin_document_request(1, "am_michael")
        assert not tts._speculative
        assert len(tts._active) == 1
        monkeypatch.setattr(tts, "TTS_LOOKAHEAD", 0)
        token = tts.begin_document_request(2, "af_heart")
        tts.prefetch_document(2, "af_heart", token, ["Disabled sentence"])
        assert not tts._speculative
        await tts.shutdown_synthesis()

    asyncio.run(run())


def test_failed_speculation_releases_slot_and_foreground_can_retry(monkeypatch, caplog):
    async def run():
        monkeypatch.setattr(tts, "_read_cache", lambda key: None)
        calls = []

        async def synth(key, text, voice):
            calls.append(text)
            if len(calls) == 1:
                raise RuntimeError("Secret source text")
            return {"audio_b64": "retry"}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        token = tts.begin_document_request(1, "af_heart")
        tts.prefetch_document(1, "af_heart", token, ["Secret source text"])
        await _wait_until(lambda: not tts._inflight)
        assert (await _synthesize("Secret source text", "af_heart"))["audio_b64"] == "retry"
        assert len(calls) == 2
        assert "Secret source text" not in caplog.text
        assert "error=RuntimeError" in caplog.text

    asyncio.run(run())


def test_synthesis_timing_logs_no_source_text(monkeypatch, caplog):
    async def run():
        monkeypatch.setattr(tts, "_read_cache", lambda key: None)

        async def synth(key, text, voice):
            return {"duration": 2.0, "_timing": (0.2, 0.1)}

        monkeypatch.setattr(tts, "_synth_uncached", synth)
        with caplog.at_level("INFO", logger="cedar.tts"):
            result = await _synthesize("Secret source text", "af_heart")
        assert "_timing" not in result
        assert "Secret source text" not in caplog.text
        for field in ("queue_s=", "elapsed_s=", "synth_s=0.200", "processing_s=0.100",
                      "audio_s=2.000", "rtf=", "cached=False", "source=demand"):
            assert field in caplog.text

    asyncio.run(run())


def test_document_route_prefetches_next_window_only_after_success(monkeypatch):
    async def run():
        monkeypatch.setattr(documents, "TTS_LOOKAHEAD", 3)
        monkeypatch.setattr(documents, "_sentence_for_tts", lambda doc_id, idx: f"Sentence {idx}")
        lookups, offered = [], []

        def upcoming(doc_id, start, limit):
            lookups.append((doc_id, start, limit))
            return [{"text": "Next sentence"}]

        async def synth(text, voice, speed):
            return {"audio_b64": "current"}

        monkeypatch.setattr(documents.db, "get_sentences", upcoming)
        monkeypatch.setattr(tts, "synthesize", synth)
        monkeypatch.setattr(tts, "prefetch_document", lambda *args: offered.append(args))
        assert (await documents.synth(7, 5, "af_heart", 1.0))["audio_b64"] == "current"
        assert lookups == [(7, 6, 3)]
        assert offered[0][-1] == ["Next sentence"]

        async def failed(text, voice, speed):
            raise RuntimeError("synthesis unavailable")

        monkeypatch.setattr(tts, "synthesize", failed)
        with pytest.raises(documents.HTTPException) as exc:
            await documents.synth(7, 6, "af_heart", 1.0)
        assert exc.value.status_code == 502
        assert len(lookups) == len(offered) == 1

        monkeypatch.setattr(tts, "synthesize", synth)

        def unavailable(*args):
            raise OSError()

        monkeypatch.setattr(documents.db, "get_sentences", unavailable)
        assert (await documents.synth(7, 5, "af_heart", 1.0))["audio_b64"] == "current"

    asyncio.run(run())


def test_document_request_freshness_is_stamped_before_slow_database_lookup(monkeypatch):
    async def run():
        import threading

        monkeypatch.setattr(documents, "TTS_LOOKAHEAD", 3)
        entered, release = threading.Event(), threading.Event()
        offered = []

        def sentence(doc_id, idx):
            if idx == 0:
                entered.set()
                assert release.wait(2)
            return f"Sentence {idx}"

        async def synth(text, voice, speed):
            return {"audio_b64": "current"}

        monkeypatch.setattr(documents, "_sentence_for_tts", sentence)
        monkeypatch.setattr(documents.db, "get_sentences", lambda *args: [{"text": "Next sentence"}])
        monkeypatch.setattr(tts, "synthesize", synth)

        def prefetch(doc_id, voice, token, texts):
            if tts._windows.get((doc_id, voice)) is token:
                offered.append(token)

        monkeypatch.setattr(tts, "prefetch_document", prefetch)
        older = asyncio.create_task(documents.synth(7, 0, "af_heart", 1.0))
        await _wait_until(entered.is_set)
        await documents.synth(7, 10, "af_heart", 1.0)
        release.set()
        await older
        assert offered == [tts._windows[(7, "af_heart")]]

    asyncio.run(run())
