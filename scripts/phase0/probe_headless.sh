#!/usr/bin/env bash
# Phase 0 spike: one trivial headless agy run, capturing the stream-json schema.
# Usage: probe_headless.sh <outdir> [model]
set -euo pipefail
OUT=${1:?outdir}
MODEL=${2:-gemini-3.8-flash-low}
AGY=${AGY:-$HOME/.local/bin/agy}
mkdir -p "$OUT/ws" && cd "$OUT/ws" && git init -q
env -i HOME="$HOME" PATH=/usr/bin:/bin USER="${USER:-}" \
  timeout 180 "$AGY" -p "Create a file hello.txt containing the word hi, then reply with DONE." \
  --model "$MODEL" --dangerously-skip-permissions --output-format stream-json \
  --settings '{"useG1Credits": false}' \
  >"$OUT/out.ndjson" 2>"$OUT/err.txt" || echo "exit=$?" >&2
wc -l "$OUT/out.ndjson"
