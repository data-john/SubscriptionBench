#!/usr/bin/env bash
set -euo pipefail

VOLUME_NAME="subbench-agy-auth"
IMAGE_TAG="subbench-agy-bootstrap"
AGY_HOST_BIN="${AGY_PATH:-/home/john/.local/bin/agy}"

if [ ! -f "$AGY_HOST_BIN" ]; then
    echo "Error: agy binary not found at $AGY_HOST_BIN" >&2
    exit 1
fi

echo "Building thin overlay image for auth bootstrap ($IMAGE_TAG)..."
docker build -t "$IMAGE_TAG" -f docker/Dockerfile.agy-overlay .

echo "Ensuring named volume '$VOLUME_NAME' exists..."
docker volume create "$VOLUME_NAME" >/dev/null

echo "======================================================================"
echo "Starting one-time interactive container login for SubscriptionBench..."
echo "Follow the link below in your host browser to authenticate."
echo "======================================================================"

exec docker run --rm -it \
    --network host \
    -v "$VOLUME_NAME":/agy-state \
    -v "$AGY_HOST_BIN":/usr/local/bin/agy:ro \
    "$IMAGE_TAG" \
    agy -p /usage --output-format json
