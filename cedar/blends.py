"""Voice blends: one voice averaged from two Kokoro voices.

Kokoro's engine accepts a voice of "af_bella+af_sky" or "af_bella(2)+af_sky(1)"
and averages the two style vectors, so the result speaks with traits of both.
Cedar keeps a small catalog of the blends it offers: an id the app can list and
pick ("af_addie_lily"), a name to show ("Addie & Lily"), and the mix the engine
is asked for ("af_aoede(1)+bf_lily(1)").

The ids look like ordinary voice ids so clients that parse them keep working;
the mix is what reaches the engine. A caller can also ask for a mix of its own
("af_aoede+bf_lily") — Cedar passes it through and the engine does the rest.
"""
from __future__ import annotations

import re
from typing import Iterable

# id -> (mix for the engine, name to show). Weights are equal unless the name
# says otherwise, so the engine's normalization makes each voice half of the
# blend.
BLENDS: dict[str, tuple[str, str]] = {
    "af_addie_lily": ("af_aoede(1)+bf_lily(1)", "Addie & Lily"),
    "af_addie_lily_2": ("af_aoede(2)+bf_lily(1)", "Addie & Lily (2:1)"),
    "af_heart_bella": ("af_heart(1)+af_bella(1)", "Heart & Bella"),
    "af_nicole_emma": ("af_nicole(1)+bf_emma(1)", "Nicole & Emma"),
    "am_echo_daniel": ("am_echo(1)+bm_daniel(1)", "Echo & Daniel"),
    "am_michael_george": ("am_michael(1)+bm_george(1)", "Michael & George"),
}

# A voice id inside a mix, as the engine names them ("af_aoede").
_VOICE = re.compile(r"^[A-Za-z0-9_]+$")


def resolve(voice: str) -> str:
    """The voice the engine should be asked for.

    A catalog blend expands to its mix; anything else is passed through, so a
    caller can still ask for a mix of its own.
    """
    entry = BLENDS.get(voice)
    return entry[0] if entry else voice


def name(voice: str) -> str | None:
    """The name to show for a catalog blend, or None if it is not one."""
    entry = BLENDS.get(voice)
    return entry[1] if entry else None


def components(mix: str) -> list[str]:
    """The voice ids a mix is built from, in order, without their weights."""
    parts = [part.split("(")[0].strip() for part in re.split(r"[+\-]", mix)]
    return [part for part in parts if part]


def is_mix(voice: str) -> bool:
    """Is this a mix of two or more voices the engine understands?"""
    if "+" not in voice and "-" not in voice:
        return False
    parts = components(voice)
    return len(parts) > 1 and all(_VOICE.match(part) for part in parts)


def available(engine_voices: Iterable[str]) -> list[str]:
    """The catalog blends whose voices the engine actually has.

    A self-hoster may run a Kokoro build without some voices; offering a blend
    the engine cannot speak would fail on the first sentence.
    """
    have = set(engine_voices)
    return [voice_id for voice_id, (mix, _) in BLENDS.items()
            if all(part in have for part in components(mix))]


def names(engine_voices: Iterable[str]) -> dict[str, str]:
    """The names of the blends that are available."""
    return {voice_id: BLENDS[voice_id][1] for voice_id in available(engine_voices)}
