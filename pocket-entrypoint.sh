#!/bin/sh
set -eu
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
