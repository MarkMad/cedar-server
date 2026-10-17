#!/usr/bin/env python3
"""Engine contract probe: verify a Kokoro-FastAPI engine meets Cedar's needs.

Run this against a live engine whenever the pinned engine image is bumped
(docker-compose.yml) or an engine is updated in place. Everything it checks
is a contract the rest of Cedar silently depends on; a failure means the
upgrade needs code changes, not just a tag bump. Standard library only, so it
runs anywhere Python 3 runs without installing anything.

    python tools/engine_contract_probe.py http://kokoro:8880    # inside compose
    python tools/engine_contract_probe.py http://localhost:8880

Checked contracts:
  - /health answers.
  - /v1/audio/voices still serves every voice cedar/lang.py relies on.
  - POST /dev/captioned_speech returns {audio, timestamps}, with audio that
    is wav at 24 kHz mono: the encoder and alignment math assume exactly that.
  - Spoken-number G2P still expands digits ("1975") into "nineteen",
    "seventy-five" word tokens the aligner maps onto the digits. A change
    here shifts word highlights, not just sound.
  - Timestamps for non-English voices and behaviour on structure-heavy text
    (`--`, `[]`, `{}`, `|`) are reported, since cedar/tts.py caches
    assumptions about both.
"""
import base64
import json
import os
import struct
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cedar.config import DEFAULT_VOICE  # noqa: E402
from cedar.lang import _DEFAULT, _SPEAKS  # noqa: E402

# One voice per language registry entry Cedar uses, plus the account default.
PROBE_VOICES = sorted({_DEFAULT[code] for code in _SPEAKS if code in _DEFAULT} | {DEFAULT_VOICE})
ENG_TEXT = "The year 1975 was loud, and the 3 kings paid $4.50 each."
# Structure Cedar rewrites before sending; the engine must not 500 either way.
STRUCT_TEXT = "abusive double -- dash: [bracket] {brace} | pipe"

PASS, WARN = "ok    ", "warn  "


def _get(base, path, timeout=20):
    with urllib.request.urlopen(base + path, timeout=timeout) as r:
        return json.loads(r.read())


def _post(base, path, payload, timeout=180):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _wav_shape(audio: bytes):
    """(channels, sample_rate) parsed from a wav header, or None if not wav."""
    if len(audio) < 28 or audio[:4] != b"RIFF":
        return None
    channels, rate = struct.unpack("<HI", audio[22:28])
    return channels, rate


def _voice_names(listing):
    """The engine's voice list has changed shape before; parse tolerantly."""
    if isinstance(listing, list):
        raw = listing
    elif isinstance(listing, dict):
        raw = listing.get("voices") or listing.get("data") or []
    else:
        raw = []
    names = set()
    for entry in raw:
        if isinstance(entry, str):
            names.add(entry)
        elif isinstance(entry, dict):
            name = entry.get("id") or entry.get("name")
            if name:
                names.add(name)
    return names


class Probe:
    def __init__(self, base):
        self.base = base.rstrip("/")
        self.fails = 0

    def ok(self, detail):
        print(PASS + detail)

    def fail(self, detail):
        self.fails += 1
        print(f"FAIL  {detail}")

    def timed(self, stamps):
        """True if every timestamp entry carries start/end times (any key name)."""
        return all(
            any(str(k).startswith("start") or str(k).startswith("end") for k in entry)
            for entry in stamps
        ) if stamps else False

    def run(self):
        base = self.base
        # 1. the engine answers ----------------------------------------
        try:
            health = _get(base, "/health")
        except Exception as e:
            self.fail(f"health probe failed: {e}")
            return self.verdict()
        self.ok(f"health: {json.dumps(health)}")

        # 2. every voice cedar/lang.py relies on must exist --------------
        try:
            names = _voice_names(_get(base, "/v1/audio/voices"))
            self.ok(f"voices endpoint: {len(names)} voices")
            for want in PROBE_VOICES:
                if want in names:
                    self.ok(f"voice {want}")
                else:
                    self.fail(f"missing voice {want} (cedar/lang.py relies on it)")
        except Exception as e:
            self.fail(f"voices endpoint failed: {e}")

        # 3. english synthesis: audio shape + timestamp behaviour --------
        payload = {
            "model": "kokoro",
            "input": ENG_TEXT,
            "voice": DEFAULT_VOICE,
            "speed": 1.0,
            "response_format": "wav",
            "stream": False,
        }
        try:
            resp = _post(base, "/dev/captioned_speech", payload)
        except urllib.error.HTTPError as e:
            self.fail(f"captioned_speech rejected english request: "
                      f"HTTP {e.code} {e.read().decode()[:200]}")
            return self.verdict()
        except Exception as e:
            self.fail(f"captioned_speech failed on english request: {e}")
            return self.verdict()

        shape = _wav_shape(base64.b64decode(resp.get("audio") or ""))
        if shape == (1, 24000):
            self.ok("english audio is 24 kHz mono wav (contract)")
        else:
            self.fail(f"english audio shape {shape} — need 24 kHz mono wav "
                      "(encoder + alignment math assume it)")

        stamps = resp.get("timestamps") or []
        words = [entry.get("word", "") for entry in stamps]
        if stamps and self.timed(stamps):
            self.ok(f"english timestamps: {len(stamps)} entries "
                    f"({sorted(stamps[0])} keys)")
        else:
            self.fail(f"english word timestamps missing or malformed; got {len(stamps)}")
        if "nineteen" in words and any(w.startswith("seventy") for w in words):
            self.ok("spoken-number G2P: '1975' expands to word tokens the aligner maps")
        else:
            self.fail("spoken-number G2P drifted: '1975' tokens are "
                      + " | ".join(words[:6] or ["<none>"]))
        if words:
            print("  tokens: " + " | ".join(w for w in words if w)[:200])

        # 4. non-english timestamps: report, cedar assumes absence -------
        for voice, text in (
            ("ff_siwis", "La voiture etait garee devant la maison depuis le petit matin."),
            ("ef_dora", "El coche estaba aparcado delante de la casa desde primera hora."),
        ):
            try:
                fr = _post(base, "/dev/captioned_speech", {
                    "model": "kokoro",
                    "input": text,
                    "voice": voice, "speed": 1.0, "response_format": "wav", "stream": False,
                })
                count = len(fr.get("timestamps") or [])
                if count:
                    print(WARN + f"non-english ({voice}): {count} timestamps returned — "
                                 "cedar/tts.py assumes non-English has none; "
                                 "recheck the aligner fallback before shipping")
                else:
                    self.ok(f"non-english ({voice}): no timestamps, as cedar expects")
            except Exception as e:
                print(WARN + f"non-english ({voice}) probe skipped: {e}")

        # 5. structure text must not 500; zero timestamps is legitimate ---
        try:
            odd = _post(base, "/dev/captioned_speech", {
                "model": "kokoro", "input": STRUCT_TEXT, "voice": DEFAULT_VOICE,
                "speed": 1.0, "response_format": "wav", "stream": False,
            })
            self.ok(f"structure text accepted: {len(odd.get('timestamps') or [])} "
                    "timestamps (zero is expected; cedar rewrites these first)")
        except urllib.error.HTTPError as e:
            self.fail(f"structure text rejected: HTTP {e.code} {e.read().decode()[:200]}")
        except Exception as e:
            self.fail(f"structure text probe failed: {e}")

        return self.verdict()

    def verdict(self):
        result = "PASS" if self.fails == 0 else f"FAIL ({self.fails})"
        print(result)
        sys.exit(0 if self.fails == 0 else 1)


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else \
        os.environ.get("CEDAR_KOKORO_URL", "http://localhost:8880")
    Probe(base).run()
