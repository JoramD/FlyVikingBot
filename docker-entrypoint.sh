#!/bin/sh
set -e

# The ./data bind mount is usually created by Docker as root. Fix ownership,
# then drop privileges to the unprivileged "bot" user.
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    chown -R bot:bot "$DATA_DIR"
    exec setpriv --reuid=bot --regid=bot --init-groups "$@"
fi

exec "$@"
