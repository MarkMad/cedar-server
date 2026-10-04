"""Bounded, incremental decoding of Pocket's streaming PCM WAV response."""
from __future__ import annotations

import struct
from collections.abc import AsyncIterator

PCM_CHUNK_BYTES = 8192
MAX_PCM_BYTES = 24_000 * 2 * 300  # At most five minutes for one sentence.
MAX_HEADER_BYTES = 65_536
# Pocket 3.3.0's nonseekable wave writer declares one billion frames, then
# closes without fixing the header. Also accept the conventional unknown size.
_UNKNOWN_DATA = {2_000_000_000, 0xFFFFFFFF}
_UNKNOWN_RIFF = {2_000_000_036, 0xFFFFFFFF}


class _Reader:
    def __init__(self, source: AsyncIterator[bytes]):
        self.source = source
        self.buffer = b""
        self.position = 0

    async def read(self, size: int) -> bytes:
        while not self.buffer:
            try:
                self.buffer = await anext(self.source)
            except StopAsyncIteration:
                return b""
            # The HTTP caller uses aiter_bytes(chunk_size=PCM_CHUNK_BYTES).
            if len(self.buffer) > PCM_CHUNK_BYTES:
                raise ValueError("Oversized WAV input chunk")
        result, self.buffer = self.buffer[:size], self.buffer[size:]
        self.position += len(result)
        return result

    async def exact(self, size: int) -> bytes:
        parts = bytearray()
        while len(parts) < size:
            part = await self.read(size - len(parts))
            if not part:
                raise ValueError("Truncated WAV")
            parts.extend(part)
        return bytes(parts)


async def wav_pcm(source: AsyncIterator[bytes]) -> AsyncIterator[bytes | None]:
    """Yield None once the format is validated, then bounded even PCM chunks.

    Exhaustion means a complete WAV was checked. Unknown data lengths can only
    be checked for clean HTTP completion and sample alignment; a producer that
    silently closes early cannot be distinguished from a short valid clip.
    """
    reader = _Reader(source)
    header = await reader.exact(12)
    if header[:4] != b"RIFF" or header[8:] != b"WAVE":
        raise ValueError("Not a RIFF WAV")
    riff_size = int.from_bytes(header[4:8], "little")
    unknown_riff = riff_size in _UNKNOWN_RIFF
    if not unknown_riff and riff_size < 36:
        raise ValueError("Invalid RIFF length")
    riff_end = riff_size + 8
    overhead, pcm_bytes = 12, 0
    have_fmt, have_data = False, False
    while True:
        chunk_header = await reader.read(8)
        if not chunk_header:
            break
        chunk_header += await reader.exact(8 - len(chunk_header))
        name = chunk_header[:4]
        size = int.from_bytes(chunk_header[4:], "little")
        overhead += 8
        if overhead > MAX_HEADER_BYTES:
            raise ValueError("WAV header too large")
        unknown_data = name == b"data" and size in _UNKNOWN_DATA
        if not unknown_riff and (unknown_data or reader.position + size + (size % 2) > riff_end):
            raise ValueError("WAV chunk exceeds RIFF length")
        if name == b"data":
            if not have_fmt or have_data or not size or (not unknown_data and size % 2):
                raise ValueError("Invalid WAV data chunk")
            have_data = True
            yield None
            if not unknown_data and size > MAX_PCM_BYTES:
                raise ValueError("WAV audio too large")
            remaining = size
            pending = b""
            while unknown_data or remaining:
                part = await reader.read(PCM_CHUNK_BYTES if unknown_data else min(remaining, PCM_CHUNK_BYTES))
                if not part:
                    if not unknown_data:
                        raise ValueError("Truncated WAV audio")
                    break
                remaining -= len(part)
                pcm_bytes += len(part)
                if pcm_bytes > MAX_PCM_BYTES:
                    raise ValueError("WAV audio too large")
                part = pending + part
                even = len(part) - len(part) % 2
                # A one-byte carry must not grow an output frame past the limit.
                for offset in range(0, even, PCM_CHUNK_BYTES):
                    yield part[offset:min(offset + PCM_CHUNK_BYTES, even)]
                pending = part[even:]
            if pending or not pcm_bytes:
                raise ValueError("Incomplete WAV sample")
            if unknown_data:
                break
        else:
            overhead += size + size % 2
            if overhead > MAX_HEADER_BYTES:
                raise ValueError("WAV header too large")
            if name == b"fmt ":
                if have_fmt or have_data or size < 16:
                    raise ValueError("Invalid WAV format chunk")
                fmt = await reader.exact(size)
                if struct.unpack("<HHIIHH", fmt[:16]) != (1, 1, 24_000, 48_000, 2, 16):
                    raise ValueError("WAV must be 24 kHz mono PCM16")
                have_fmt = True
            else:
                remaining = size
                while remaining:
                    part = await reader.exact(min(remaining, PCM_CHUNK_BYTES))
                    remaining -= len(part)
        if size % 2:
            await reader.exact(1)
    if not have_data or not pcm_bytes or (not unknown_riff and reader.position != riff_end):
        raise ValueError("Incomplete WAV")
