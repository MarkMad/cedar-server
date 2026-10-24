"""Health, the owner's identity, the voice list + previews, and settings."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel

from .. import __version__, blends, db, lang, settings, substitute, tts
from ..config import SERVER_NAME
from ..textsafe import printable

router = APIRouter(prefix="/api")

# What the app reads to recognise a self-hosted server before it has a key.
SERVER_KIND = "cedar-server"
MODE = "single-user"


# The engine probe behind /health, remembered for a few seconds. /health is the
# one open route, so without this every anonymous hit would cost two requests
# to Kokoro — a free way to keep the engine busy from outside.
_HEALTH_TTL_S = 10.0
_health: tuple[float, bool] = (0.0, False)  # (monotonic time of probe, result)
_health_lock = asyncio.Lock()


async def _kokoro_ok() -> bool:
    global _health
    if time.monotonic() - _health[0] < _HEALTH_TTL_S:
        return _health[1]
    async with _health_lock:
        if time.monotonic() - _health[0] < _HEALTH_TTL_S:
            return _health[1]
        # Keep the app's existing kokoro_ok field meaningful when Pocket is the
        # selected voice and Kokoro happens to be unavailable.
        ok = await tts.health() or await tts.pocket_health()
        _health = (time.monotonic(), ok)
        return ok


@router.get("/health")
async def health():
    """Open (no key): the app calls this to check an address before pairing,
    and for the connection dot afterwards."""
    return {"ok": True, "server": SERVER_KIND, "version": __version__, "mode": MODE,
            "name": SERVER_NAME, "kokoro_ok": await _kokoro_ok()}


@router.get("/me")
def me():
    """Who the key belongs to. There is exactly one owner, so this is really a
    description of the server, shaped so the app can treat it like a signed-in
    account: no plan, nothing to upgrade, nothing that expires."""
    return {
        "id": 1,
        "tier": "owner",
        "name": None,
        "email": "",
        "plan": "self_hosted",
        "plan_label": "Self-hosted",
        "plan_is_paid": False,
        "plan_expires_at": None,
        "email_verified": True,
        "settings": settings.prefs(),
        "server": {"kind": SERVER_KIND, "name": SERVER_NAME, "version": __version__,
                   "mode": MODE, **db.counts()},
    }


# ------------------------------------------------------------------- voices

@router.get("/voices")
async def voices():
    """Every voice the engines have, plus which one each language falls back to.

    `defaults` lets the app label a language the owner hasn't chosen for
    ("Spanish — Dora") without keeping its own copy of a table that lives in
    lang.py. `names` names the voice blends; the plain voices the app already
    knows by heart.
    """
    engine_voices = await tts.list_voices()
    return {"voices": engine_voices + blends.available(engine_voices),
            "names": blends.names(engine_voices),
            "defaults": lang.defaults(),
            "languages": lang.LANG_LABEL}


# Kokoro voice ids look like af_heart = [lang][gender]_[name].
_VOICE_RE = re.compile(r"^([a-z])([fm])_([a-z0-9_]+)$")

# Per-language intro lines for the voice picker's preview, spoken by the voice
# itself. The name is included only where a Latin name reads naturally.
_INTROS = {
    "a": "Hi, I'm {name}! This is what I sound like. Pick me, and I'll read your books and articles out loud.",
    "b": "Hello, I'm {name}. This is what I sound like. Choose me, and I'll read your books and articles aloud.",
    "e": "¡Hola! Soy {name}. Así suena mi voz. Elígeme y te leeré tus libros y artículos.",
    "f": "Bonjour, je m'appelle {name}. Voici ma voix. Choisissez-moi et je vous lirai vos livres et vos articles.",
    "i": "Ciao, sono {name}. Questa è la mia voce. Scegli me e ti leggerò i tuoi libri e articoli.",
    "j": "こんにちは。これが私の声です。本や記事を、この声で読み上げます。",
    "p": "Olá, eu sou {name}. Esta é a minha voz. Escolha-me e eu leio seus livros e artigos.",
    "z": "你好！这是我的声音。选择我，我来为你朗读书籍和文章。",
    "r": "Здравствуйте, я {name}. Так звучит мой голос. Выберите меня, и я прочитаю ваши книги и статьи.",
}

# Friendlier speaker names for ids whose raw suffix reads oddly as a person.
# The app displays the same names — keep the two in sync.
_NICE_NAMES = {"aoede": "Addie", "kore": "Cora", "alloy": "Allie", "fenrir": "Finn"}


def _nice_name(voice: str) -> str:
    """A speaker's friendly name for a voice id ("af_aoede" -> "Addie").

    Takes a whole id or just the suffix after it, so "af_aoede" and "aoede"
    both land on "Addie".
    """
    suffix = voice.split("_", 1)[-1] if _VOICE_RE.match(voice) else voice
    return _NICE_NAMES.get(suffix, suffix.replace("_", " ").title())


def _intro_for(voice: str) -> str:
    label = blends.name(voice)
    parts = blends.components(voice) if blends.is_mix(voice) else []
    if label is None and parts:
        # A mix the caller made up: name the voices it is made of.
        label = "a blend of " + " and ".join(_nice_name(part) for part in parts)
    # A mix speaks the language of its first voice.
    m = _VOICE_RE.match(parts[0] if parts else voice)
    lang_, gender, name = (m.group(1), m.group(2), m.group(3)) if m else ("a", "f", voice)
    if voice.startswith("ru_"):
        lang_, name = "r", voice[3:]
    if lang_ == "h":  # Hindi verb forms are gendered
        return ("नमस्ते! यह मेरी आवाज़ है। मुझे चुनिए, मैं आपकी किताबें और लेख पढ़कर "
                + ("सुनाऊँगी।" if gender == "f" else "सुनाऊँगा।"))
    tmpl = _INTROS.get(lang_, _INTROS["a"])
    nice = label or _nice_name(name)
    return tmpl.format(name=nice)


def _known_voice(voice: str) -> bool:
    """A voice the picker may ask about: a plain id, a Russian id, or a mix."""
    if len(voice) > 40:
        # A mix names its voices, so it needs more room than a single id.
        return len(voice) <= 80 and blends.is_mix(voice)
    return bool(_VOICE_RE.match(voice) or voice.startswith("ru_")) or blends.is_mix(voice)


@router.get("/voices/{voice}/preview")
async def voice_preview(voice: str):
    """A short spoken self-introduction for a voice (the picker's ▶ button).
    Cached by (text, voice) after the first synthesis like any other clip."""
    if not _known_voice(voice):
        raise HTTPException(400, "Unknown voice.")
    try:
        result = await tts.synthesize(_intro_for(voice), voice)
    except Exception:
        raise HTTPException(502, "Could not synthesize a preview for that voice.")
    result.pop("cached", None)
    return result


# ----------------------------------------------------------------- settings

class PrefsIn(BaseModel):
    voice: str | None = None
    # Which language `voice` is being chosen for. Omitted means the language
    # the voice itself speaks.
    lang: str | None = None
    speed: float | None = None
    theme: str | None = None


def _prefs_payload() -> dict[str, Any]:
    return {**settings.prefs(), "voices": settings.chosen_voices()}


@router.get("/settings")
def get_settings():
    """The synced preferences, with `voices` = the voice chosen per language."""
    return _prefs_payload()


@router.put("/settings")
def put_settings(body: PrefsIn):
    if body.voice is not None:
        if not tts.valid_voice_id(body.voice):
            raise HTTPException(400, "Unknown voice.")
        # An unknown language code means "the voice's own language".
        language = body.lang if body.lang in lang.LANG_LABEL else None
        settings.set_voice(body.voice, language)
    patch: dict[str, Any] = {}
    if body.speed is not None:
        patch["speed"] = max(0.5, min(3.0, body.speed))
    if body.theme is not None:
        patch["theme"] = body.theme[:24]
    if patch:
        settings.update_prefs(patch)
    return _prefs_payload()


@router.get("/settings/server")
def get_server_settings():
    """The server knobs (book store switch, dictionary/translation tuning)."""
    return settings.knobs()


@router.put("/settings/server")
def put_server_settings(body: dict[str, Any]):
    if "substitutions" in body and body["substitutions"] is not None:
        # The rules are the one knob whose shape carries meaning, so they are
        # checked here rather than at synthesis time, when a bad one would be
        # too late to tell the owner about it.
        try:
            body["substitutions"] = substitute.validate(body["substitutions"])
        except substitute.RulesError as e:
            raise HTTPException(400, str(e))
    try:
        return settings.update_knobs(body)
    except KeyError as e:
        raise HTTPException(400, f"Unknown setting: {e.args[0]}")
    except (TypeError, ValueError):
        raise HTTPException(400, "A value has the wrong type.")


class PreviewIn(BaseModel):
    text: str
    # The language and engine of the rule being heard, so the voice is the one
    # the rule will actually be spoken by. "*" or omitted: the owner's own voice.
    lang: str | None = None
    engine: str | None = None


_PREVIEW_MAX_CHARS = 200
log = logging.getLogger("cedar.system")


def _preview_voice(language: str | None, engine: str | None) -> str:
    """The voice a rule for this language and engine is heard in."""
    main, _ = settings.reading_defaults()
    code = (language or "").strip().lower()
    code = code if code in lang.LANG_LABEL else None
    voice = lang.resolve(code, settings.chosen_voices(), main)
    engine = (engine or "").strip().lower()
    if engine == "pocket" and not tts.is_pocket_voice(voice):
        pocket = tts.pocket_voice_ids()
        if not pocket:
            raise HTTPException(400, "No Pocket voice is available.")
        voice = pocket[0]
    elif engine == "kokoro" and tts.is_pocket_voice(voice):
        voice = lang.default_voice(code) or lang.DEFAULT_VOICE
    return voice


@router.post("/substitutions/preview")
async def preview_substitution(body: PreviewIn):
    """A word, spoken exactly as written and without any substitution rule, so
    the owner can hear the original and a replacement side by side."""
    text = (body.text or "").strip()
    if not text:
        raise HTTPException(400, "Nothing to say.")
    if len(text) > _PREVIEW_MAX_CHARS:
        raise HTTPException(413, "That is too long to preview.")
    voice = await asyncio.to_thread(_preview_voice, body.lang, body.engine)
    try:
        audio = await tts.synthesize_raw(text, voice, substitutions=False)
    except Exception:
        # The exception text can carry the engine's internal URL.
        log.exception("substitution preview failed")
        raise HTTPException(502, "The voice service could not read that.")
    return Response(audio, media_type="audio/wav")


# ------------------------------------------------------------- client events

class ClientEventIn(BaseModel):
    kind: str
    detail: str | None = None
    device: str | None = None


@router.post("/client_event")
def client_event(body: ClientEventIn):
    """A breadcrumb from the app about something only it can see (a request
    that hung, say). Logged, nothing more — there is no one to report to."""
    kind = "".join(ch for ch in body.kind if ch.isalnum() or ch in "_-")[:24]
    logging.getLogger("cedar.client").info("client event %s (%s): %s", kind,
                                           printable(body.device, 32), printable(body.detail, 200))
    return {"ok": True}
