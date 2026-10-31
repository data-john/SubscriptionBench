#!/usr/bin/env python3
"""Phase 0 spike: snapshot `agy -p /usage --output-format json` at full precision.

Prints one JSON object per snapshot: {bucket_id: [remaining_fraction, reset_time]}.
Runs agy with a scrubbed environment (no inherited ANTIGRAVITY_* variables) so the
probe can never attach to a parent Antigravity session.

Usage: probe_usage.py [--agy PATH] [--count N] [--interval SECONDS]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time

DEFAULT_AGY = os.path.expanduser("~/.local/bin/agy")


def snapshot(agy: str) -> dict[str, list]:
    proc = subprocess.run(
        [agy, "-p", "/usage", "--output-format", "json"],
        capture_output=True,
        text=True,
        timeout=90,
        env={"HOME": os.environ["HOME"], "PATH": "/usr/bin:/bin"},
        check=True,
    )
    data = json.loads(proc.stdout)
    out: dict[str, list] = {}
    for group in data["command"]["data"]["groups"]:
        for bucket in group["buckets"]:
            out[bucket["id"]] = [bucket["remaining_fraction"], bucket["reset_time"]]
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--agy", default=DEFAULT_AGY)
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.0)
    args = ap.parse_args()
    for i in range(args.count):
        if i and args.interval:
            time.sleep(args.interval)
        print(json.dumps(snapshot(args.agy)))


if __name__ == "__main__":
    main()
