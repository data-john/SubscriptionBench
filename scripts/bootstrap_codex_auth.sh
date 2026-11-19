#!/usr/bin/env bash
set -euo pipefail

VOLUME_NAME="subbench-codex-auth"
IMAGE_TAG="subbench-agy:probe"
CODEX_HOST_BIN="${CODEX_PATH:-/usr/lib/chatgpt/resources/codex}"
HOST_AUTH_JSON="${CODEX_HOME:-$HOME/.codex}/auth.json"

if [ ! -f "$CODEX_HOST_BIN" ]; then
    echo "Error: codex binary not found at $CODEX_HOST_BIN" >&2
    exit 1
fi

if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    echo "Building probe image ($IMAGE_TAG)..."
    docker build -t "$IMAGE_TAG" -f docker/Dockerfile.agy-overlay .
fi

echo "Ensuring named volume '$VOLUME_NAME' exists..."
docker volume create "$VOLUME_NAME" >/dev/null

if [ -f "$HOST_AUTH_JSON" ]; then
    echo "Seeding $VOLUME_NAME/auth.json from $HOST_AUTH_JSON..."
    docker run --rm -i \
        -v "$VOLUME_NAME":/codex-state \
        --entrypoint /bin/bash \
        "$IMAGE_TAG" \
        -c 'cat > /codex-state/auth.json && chmod 600 /codex-state/auth.json' < "$HOST_AUTH_JSON"
else
    echo "Host $HOST_AUTH_JSON not found; starting interactive device login..."
    docker run --rm -it \
        -v "$VOLUME_NAME":/codex-state \
        -v "$CODEX_HOST_BIN":/usr/local/bin/codex:ro \
        -e CODEX_HOME=/codex-state \
        --entrypoint /usr/local/bin/codex \
        "$IMAGE_TAG" \
        login --device-auth
fi

echo "Verifying Codex login status inside container..."
docker run --rm \
    -v "$VOLUME_NAME":/codex-state \
    -v "$CODEX_HOST_BIN":/usr/local/bin/codex:ro \
    -e CODEX_HOME=/codex-state \
    --entrypoint /usr/local/bin/codex \
    "$IMAGE_TAG" \
    login status
