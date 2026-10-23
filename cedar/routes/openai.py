"""OpenAI-compatible speech API, for clients that already speak it.

``POST /v1/audio/speech`` takes the same body as OpenAI's and answers with raw
audio, so an OpenAI SDK, Open WebUI and the like can use Cedar's voices by
pointing ``base_url`` here. It is gated by the TTS key (``CEDAR_TTS_KEY``), not
the owner key, so such a client can never reach the library (see main._gate).

mp3 comes from the same cached synthesis the reader uses; wav and pcm come from
``tts.synthesize_raw``, the engine's own audio. Both apply the owner's word
substitutions. ``speed``, ``model`` and ``instructions`` are accepted and
ignored, as the engine path ignores speed today.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import wave

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .. import settings, tts
from ..chunker import chunk_plain_text

router = APIRouter(prefix="/v1")
log = logging.getLogger("cedar.openai")

# OpenAI's own limit on `input`.
_MAX_INPUT_CHARS = 4096

_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav", "pcm": "audio/pcm"}

# OpenAI's voice names, spoken by the nearest Kokoro voice. Anything else a
# client sends (a Kokoro id, a blend, a Pocket voice) goes through unchanged.
VOICE_ALIASES = {
    "alloy": "af_alloy", "ash": "am_adam", "ballad": "bm_george", "coral": "af_bella",
    "echo": "am_echo", "fable": "bf_emma", "nova": "af_nova", "onyx": "am_onyx",
    "sage": "af_sarah", "shimmer": "af_sky", "verse": "am_michael",
}

_MODELS = ("tts-1", "tts-1-hd", "gpt-4o-mini-tts", "kokoro")


class OpenAIError(Exception):
    """Rendered by main as OpenAI's {"error": {...}} body."""

    def __init__(self, status: int, message: str, code: str | None = None,
                 kind: str = "invalid_request_error"):
        super().__init__(message)
        self.status, self.message, self.code, self.kind = status, message, code, kind


def error_response(status: int, message: str, code: str | None = None,
                   kind: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": kind, "param": None, "code": code}},
                        status_code=status)


class SpeechIn(BaseModel):
    input: str
    model: str | None = None
    voice: str | None = None
    response_format: str = "mp3"
    speed: float | None = None
    instructions: str | None = None
    stream_format: str | None = None


async def _wav_parts(chunks: list[str], voice: str) -> tuple[tuple[int, int, int], bytes]:
    """Each chunk's WAV read back to PCM, joined under one set of parameters."""
    params: tuple[int, int, int] | None = None
    frames: list[bytes] = []
    for text in chunks:
        raw = await tts.synthesize_raw(text, voice)
        with wave.open(io.BytesIO(raw), "rb") as clip:
            got = (clip.getnchannels(), clip.getsampwidth(), clip.getframerate())
            if params is None:
                params = got
            elif got != params:
                raise ValueError("The engine changed audio format mid-request")
            frames.append(clip.readframes(clip.getnframes()))
    assert params is not None
    return params, b"".join(frames)


def _wav_file(params: tuple[int, int, int], pcm: bytes) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(params[0])
        out.setsampwidth(params[1])
        out.setframerate(params[2])
        out.writeframes(pcm)
    return buf.getvalue()


@router.post("/audio/speech")
async def speech(body: SpeechIn):
    fmt = (body.response_format or "mp3").lower()
    if fmt not in _FORMATS:
        raise OpenAIError(400, f"response_format must be one of: {', '.join(_FORMATS)}.",
                          "unsupported_format")
    text = (body.input or "").strip()
    if not text:
        raise OpenAIError(400, "input must not be empty.", "empty_input")
    if len(text) > _MAX_INPUT_CHARS:
        raise OpenAIError(400, f"input is longer than {_MAX_INPUT_CHARS} characters.",
                          "string_above_max_length")

    requested = (body.voice or "").strip()
    if requested and not tts.valid_voice_id(VOICE_ALIASES.get(requested.lower(), requested)):
        raise OpenAIError(400, "Unknown voice.", "invalid_voice")

    def prepare() -> tuple[list[str], str]:
        main, _ = settings.reading_defaults()
        voice = VOICE_ALIASES.get(requested.lower(), requested) if requested else main
        return [c.text for c in chunk_plain_text("", text).chunks], voice

    chunks, voice = await asyncio.to_thread(prepare)
    if not chunks:
        raise OpenAIError(400, "input has nothing to say.", "empty_input")

    try:
        if fmt == "mp3":
            audio = b"".join([base64.b64decode((await tts.synthesize(c, voice))["audio_b64"])
                              for c in chunks])
        else:
            params, pcm = await _wav_parts(chunks, voice)
            audio = pcm if fmt == "pcm" else _wav_file(params, pcm)
    except Exception:
        # The exception text can carry the engine's internal URL or the input.
        log.exception("openai speech failed")
        raise OpenAIError(502, "The voice service could not read that text.",
                          "voice_service_failed", "server_error")
    return Response(audio, media_type=_FORMATS[fmt])


@router.get("/models")
async def models():
    return {"object": "list",
            "data": [{"id": m, "object": "model", "created": 0, "owned_by": "cedar"}
                     for m in _MODELS]}


@router.get("/audio/voices")
async def voices():
    engine = await tts.list_voices()
    return {"voices": [{"id": v} for v in [*VOICE_ALIASES, *engine]]}
