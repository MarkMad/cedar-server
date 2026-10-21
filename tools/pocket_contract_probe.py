#!/usr/bin/env python3
"""Pocket engine contract probe: verify a Pocket TTS server meets Cedar's needs.

Run this against a live engine whenever the pinned Pocket release is bumped
(Dockerfile.pocket / CEDAR_POCKET_VERSION), the model is switched
(CEDAR_POCKET_MODEL), or the engine implementation is
(CEDAR_POCKET_IMPL: python vs rust). Everything it checks is a contract the
rest of Cedar silently depends on; a failure means the upgrade needs code
changes, not just a tag bump. Standard library only, so it runs anywhere
Python 3 runs without installing anything.

    python tools/pocket_contract_probe.py http://pocket:8000   # inside compose
    python tools/pocket_contract_probe.py http://localhost:8000

Checked contracts:
  - / answers; /health calls itself healthy.
  - POST /tts accepts Cedar's exact request — an urlencoded body carrying
    "text" and "voice_url" with a predefined voice name — and streams
    audio/wav back. The Rust server accepts both form encodings; that
    acceptance is part of what Cedar relies on.
  - The WAV is 24 kHz mono 16-bit PCM, written with the header Pocket's
    nonseekable writer has always produced (a billion declared frames; the
    conventional unknown-size sentinel is accepted too). cedar/pocket_stream
    decodes exactly this, bounded and incrementally.
  - The audio is non-empty and a sane length, and voices differ — a voice
    change has to change the bytes.

Not checkable from here:
  - The active model is not exposed by any endpoint, so the probe cannot
    prove it prints the value .env asked for; check the container log
    (`docker compose logs pocket` shows the model origin on both engines).

A Pocket upgrade can legitimately produce different-sounding audio from the
same voice — re-recorded embeddings, a model switch — which Cedar isolates by
keying the cache on the engine identity (CEDAR_POCKET_IMPL / CEDAR_POCKET_
MODEL / CEDAR_POCKET_VERSION). This probe is for the contracts outside that:
transport, codec shape, and every endpoint Cedar calls.
"""
from __future__ import annotations

import os
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# A predefined voice: stable names across releases, no HF token needed, and
# every one of them must keep synthesizing.
VOICE = "alba"
VOICES = ("alba", "anna", "marius", "jean")
TEXT = "The year 1975 was loud, and the 3 kings paid $4.50 each."

# Billion-frame header from Pocket's nonseekable writer: 1_000_000_000 frames
# at 24 kHz mono 16-bit is 2_000_000_000 bytes, giving RIFF 2_000_000_036.
EXPECTED_DATA_SIZES = {2_000_000_000, 0xFFFFFFFF}
EXPECTED_RIFF_SIZES = {2_000_000_036, 0xFFFFFFFF}
# Shorter than this and the engine answered with a stub, not speech. Keep the
# floor tiny: Pocket's eos guard allows ending after 6 frames (~0.5s), so a
# short clip is genuine speech, not a failure.
MIN_PCM_BYTES = 24_000 * 2 // 10

PASS, WARN = "ok    ", "warn  "


def _get(base, path, timeout=30):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return r.status, r.read()


def _tts(base, form, timeout=300):
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        base + "/tts", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read()


class Probe:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.fails = 0

    def ok(self, detail):
        print(PASS + detail)

    def warn(self, detail):
        print(WARN + detail)

    def fail(self, detail):
        self.fails += 1
        print(f"FAIL  {detail}")

    def run(self):
        base = self.base
        # 1. The engine answers, on both endpoints Cedar touches. Cedar's
        #    pocket_health() polls / and expects HTTP 200.
        try:
            status, body = _get(base, "/")
        except Exception as e:
            self.fail(f"the engine does not answer: {e}")
            return self.verdict()
        if status == 200:
            self.ok(f"/ answers (status {status}, {len(body)} bytes)")
        else:
            self.fail(f"/ answered {status} — cedar's pocket_health expects 200")

        try:
            status, health = _get(base, "/health")
            healthy = health.strip()
        except Exception as e:
            self.fail(f"/health probe failed: {e}")
            return self.verdict()
        if status == 200 and b"healthy" in health:
            self.ok(f"/health: {healthy.decode(errors='replace')}")
        else:
            self.fail(f"/health: status {status}, body {healthy[:100]!r}")

        # 2. Cedar's exact /tts request: urlencoded text + voice_url, the same
        #    encoding httpx data={...} and the two engines' form handling rely on.
        started = time.time()
        try:
            status, ctype, audio = _tts(base, {"text": TEXT, "voice_url": VOICE})
        except urllib.error.HTTPError as e:
            self.fail(f"/tts rejected Cedar's request: "
                      f"HTTP {e.code} {e.read().decode(errors='replace')[:200]}")
            return self.verdict()
        except Exception as e:
            self.fail(f"/tts failed on Cedar's request: {e}")
            return self.verdict()
        if status == 200 and audio:
            self.ok(f"/tts ({VOICE}): HTTP 200, {len(audio)} bytes "
                    f"in {time.time() - started:.1f}s, content-type {ctype!r}")
        else:
            self.fail(f"/tts ({VOICE}): status {status}, {len(audio)} bytes")

        # 3. The WAV shape the encoder and the stream parser assume.
        self.check_wav(audio, VOICE)

        # 4. Voices differ, and every other predefined voice speaks too.
        reference = audio
        for name in VOICES:
            if name == VOICE:
                continue
            try:
                _, _, other = _tts(base, {"text": TEXT, "voice_url": name})
                self.check_wav(other, name)
                if other == reference:
                    self.fail(f"voice {name} returned bytes identical to {VOICES[0]}")
                else:
                    self.ok(f"voice {name} differs from {VOICES[0]}")
            except Exception as e:
                self.fail(f"/tts ({name}) failed: {e}")

        return self.verdict()

    def check_wav(self, audio, voice):
        """24 kHz mono 16-bit PCM, Pocket's nonseekable header, sane length."""
        if len(audio) < 44 or audio[:4] != b"RIFF" or audio[8:12] != b"WAVE":
            self.fail(f"({voice}) no RIFF/WAVE header — was streaming audio expected?")
            return False
        riff = struct.unpack("<I", audio[4:8])[0]
        fmt_code, channels, rate = struct.unpack("<HHI", audio[20:28])
        bits = struct.unpack("<H", audio[34:36])[0]
        data_size = struct.unpack("<I", audio[40:44])[0]
        shape_ok = (fmt_code == 1 and channels == 1 and rate == 24_000 and bits == 16)
        if shape_ok:
            self.ok(f"({voice}) wav is 24 kHz mono 16-bit PCM (contract)")
        else:
            self.fail(f"({voice}) wav shape changed: format {fmt_code}, {channels} channel(s),"
                      f" {rate} Hz, {bits} bit — encoder + stream parser assume"
                      " 24 kHz mono 16-bit PCM")
        header_ok = riff in EXPECTED_RIFF_SIZES and data_size in EXPECTED_DATA_SIZES
        if header_ok:
            self.ok(f"({voice}) header carries Pocket's nonseekable writer "
                    f"(data size {data_size})")
        else:
            self.fail(f"({voice}) unexpected header sizes: RIFF {riff}, data {data_size}; "
                      "cedar/pocket_stream.py must learn the new one")
        pcm = audio[44:]
        if len(pcm) >= MIN_PCM_BYTES and len(pcm) % 2 == 0:
            seconds = len(pcm) / 48_000
            self.ok(f"({voice}) {len(pcm)} PCM bytes = about {seconds:.1f}s of audio")
        else:
            self.fail(f"({voice}) PCM is {len(pcm)} bytes (odd={len(pcm) % 2}): too short or"
                      " not a whole sample — a stub, not speech")
        return shape_ok and header_ok

    def verdict(self):
        self.warn("the model is not exposed at runtime: verify `docker compose logs pocket`"
                  " reports the origin CEDAR_POCKET_MODEL asks for")
        result = "PASS" if self.fails == 0 else f"FAIL ({self.fails})"
        print(result)
        sys.exit(0 if self.fails == 0 else 1)


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else \
        os.environ.get("CEDAR_POCKET_URL", "http://localhost:8000")
    Probe(base).run()
