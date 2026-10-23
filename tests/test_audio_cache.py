"""The audio-cache eviction: LRU pairs shed under a cap, none without one."""
from __future__ import annotations

import os
import time

from cedar import tts


def _write_pair(cache, key, mtime, size=100):
    audio = cache / f"{key}.{tts.AUDIO_FORMAT}"
    meta = cache / f"{key}.json"
    audio.write_bytes(b"a" * size)
    meta.write_text('{"align": %d, "text": "", "raw": []}' % tts._ALIGN_VERSION)
    os.utime(audio, (mtime, mtime))
    os.utime(meta, (mtime, mtime))
    return audio, meta


# AUDIO_CACHE_GB is in GiB but these tests deal in bytes, so translate exactly
# the way _sweep_audio_cache does.
_BYTES_PER_GIB = 1 << 30


def test_oldest_pairs_shed_when_over_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    monkeypatch.setattr(tts, "AUDIO_CACHE_GB", 300 / _BYTES_PER_GIB)
    now = time.time()
    # Three 102-byte pairs against a 300-byte cap: the oldest one must go
    # whole (audio AND meta), the others survive.
    for i, key in enumerate("k1 k2 k3".split()):
        _write_pair(tmp_path, key, mtime=now - 100 + i)
    # A lone audio file without its meta can never be served; the sweep is
    # deliberately read-only on anything that isn't a complete pair.
    (tmp_path / "orphan.mp3").write_bytes(b"a" * 100)

    assert tts._sweep_audio_cache() == 1

    assert not (tmp_path / "k1.mp3").exists()
    assert not (tmp_path / "k1.json").exists()
    assert (tmp_path / "k2.mp3").exists() and (tmp_path / "k2.json").exists()
    assert (tmp_path / "k3.mp3").exists() and (tmp_path / "k3.json").exists()
    assert (tmp_path / "orphan.mp3").exists()
    # Under the cap now: a repeat sweep sheds nothing more.
    assert tts._sweep_audio_cache() == 0


def test_no_cap_never_evicts(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    monkeypatch.setattr(tts, "AUDIO_CACHE_GB", 0.0)
    _write_pair(tmp_path, "ancient", mtime=0)

    assert tts._sweep_audio_cache() == 0
    assert (tmp_path / "ancient.mp3").exists()
    assert (tmp_path / "ancient.json").exists()


def _over_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    monkeypatch.setattr(tts, "AUDIO_CACHE_GB", 300 / _BYTES_PER_GIB)
    now = time.time()
    for i, key in enumerate("k1 k2 k3".split()):
        _write_pair(tmp_path, key, mtime=now - 100 + i)


def test_a_running_process_sheds_once_the_interval_has_passed(monkeypatch, tmp_path):
    _over_cap(monkeypatch, tmp_path)
    monkeypatch.setattr(tts, "_last_sweep", time.monotonic() - tts._SWEEP_INTERVAL_S - 1)

    assert tts._maybe_sweep_audio_cache() == 1
    assert not (tmp_path / "k1.mp3").exists()


def test_sweeps_are_rate_limited(monkeypatch, tmp_path):
    _over_cap(monkeypatch, tmp_path)
    monkeypatch.setattr(tts, "_last_sweep", time.monotonic())

    assert tts._maybe_sweep_audio_cache() == 0
    assert (tmp_path / "k1.mp3").exists()


def test_storing_a_clip_enforces_the_cap(monkeypatch, tmp_path):
    _over_cap(monkeypatch, tmp_path)
    monkeypatch.setattr(tts, "_last_sweep", time.monotonic() - tts._SWEEP_INTERVAL_S - 1)

    tts._process_and_store("fresh", "Hello", b"a" * 100, [])

    assert (tmp_path / "fresh.mp3").exists()  # the clip just written is the newest
    assert not (tmp_path / "k1.mp3").exists()


def test_a_pair_shed_mid_read_is_a_miss_not_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    audio, _meta = _write_pair(tmp_path, "gone", mtime=time.time())
    real = tts.Path.read_bytes

    def vanish(self):
        self.unlink()
        return real(self)

    monkeypatch.setattr(tts.Path, "read_bytes", vanish)
    assert tts._read_cache("gone") is None
    assert not audio.exists()


def test_cache_hit_touches_pair_for_lru(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    monkeypatch.setattr(tts, "AUDIO_CACHE_GB", 1.0)
    audio, meta = _write_pair(tmp_path, "hitme", mtime=1000000)

    result = tts._read_cache("hitme")

    assert result["cached"] is True
    fresh = time.time()
    assert fresh - audio.stat().st_mtime < 10
    assert fresh - meta.stat().st_mtime < 10


def test_cache_hit_leaves_mtime_alone_when_cap_off(monkeypatch, tmp_path):
    monkeypatch.setattr(tts, "_CACHE", tmp_path)
    monkeypatch.setattr(tts, "AUDIO_CACHE_GB", 0.0)
    audio, _meta = _write_pair(tmp_path, "quiet", mtime=1000)

    assert tts._read_cache("quiet")["cached"] is True
    assert audio.stat().st_mtime == 1000
