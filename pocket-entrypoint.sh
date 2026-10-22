#!/bin/sh
set -eu
# The released pocket-tts binary is built for x86-64-v3 (AVX2 + FMA). A host
# CPU without it dies on the first vector instruction with SIGILL rather than
# any useful message, so refuse here instead, naming the fallback. This keys
# on the image's own marker, not on CEDAR_POCKET_IMPL: what the container runs
# is what the Dockerfile built, whatever the .env says.
if [ "${POCKET_IMPL:-}" = "rust" ] && [ "$(uname -m)" = "x86_64" ]; then
    if ! grep -m1 -q avx2 /proc/cpuinfo 2>/dev/null; then
        echo "This CPU has no AVX2, which the pocket-tts Rust binary needs." >&2
        echo "Use the Python engine for this machine:" >&2
        echo "  set CEDAR_POCKET_IMPL=python in .env" >&2
        echo "  docker compose -f docker-compose.yml -f docker-compose.pocket.yml up -d --build" >&2
        exit 1
    fi
fi
# Int8 inference is opt-in: .env sets CEDAR_POCKET_QUANTIZE=1 and the flag is
# appended to whatever command the container runs (Compose or docker run),
# instead of Compose hardcoding it for every engine image. The default stays
# off: on ARM64 the engine's fallback quantization runs slower than float,
# and the wheels for the fast int8 backend ship for x86-64 only.
if [ "${CEDAR_POCKET_QUANTIZE:-0}" = "1" ]; then
    set -- "$@" --quantize
fi
# Existing model volumes may be root-owned from the previous Pocket image.
if [ "$(id -u)" = "0" ]; then
    for cache in /home/pocket/.cache/pocket_tts /home/pocket/.cache/huggingface; do
        mkdir -p "$cache"
        if [ "$(stat -c %u "$cache")" != "1000" ]; then
            chown -R pocket:pocket "$cache"
        fi
    done
    exec setpriv --reuid=pocket --regid=pocket --init-groups \
        --inh-caps=-all --bounding-set=-all "$@"
fi
exec "$@"
