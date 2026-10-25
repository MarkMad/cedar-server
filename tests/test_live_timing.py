"""/api/live/tts reports synthesis timing and the real-time factor only when asked."""
import asyncio

from conftest import AUTH
from cedar import tts

# The client fixture swaps tts.synthesize for a stub; keep the real one.
_real_synthesize = tts.synthesize


def test_the_route_passes_the_opt_in_through(client):
    plain = client.post("/api/live/tts", headers=AUTH, json={"text": "Hello there."}).json()
    asked = client.post("/api/live/tts", headers=AUTH, json={"text": "Hello there.", "timing": True}).json()
    assert "timing" not in plain
    assert asked["timing"]["rtf"] == 0.1


def test_timing_is_dropped_unless_requested(monkeypatch):
    async def fresh(text, voice):
        return {"duration": 2.0, "timing": tts._timing_payload(False, 0.1, 0.5, 0.2, 2.0, 0.4)}
    monkeypatch.setattr(tts, "_synthesize", fresh)

    assert "timing" not in asyncio.run(_real_synthesize("Hello.", "af_heart"))
    got = asyncio.run(_real_synthesize("Hello.", "af_heart", timing=True))["timing"]
    assert got == {"cached": False, "queue_s": 0.1, "synth_s": 0.5, "processing_s": 0.2,
                   "audio_s": 2.0, "rtf": 0.4}


def test_a_cache_hit_has_no_real_time_factor(monkeypatch):
    async def hit(text, voice):
        return {"duration": 1.5, "cached": True}
    monkeypatch.setattr(tts, "_synthesize", hit)

    got = asyncio.run(_real_synthesize("Hello.", "af_heart", timing=True))["timing"]
    assert got == {"cached": True, "audio_s": 1.5, "rtf": None}


def test_a_silent_clip_reports_a_lookup_not_synthesis():
    got = asyncio.run(_real_synthesize("...", "af_heart", timing=True))["timing"]
    assert got["cached"] is True and got["rtf"] is None
    assert "timing" not in asyncio.run(_real_synthesize("...", "af_heart"))
