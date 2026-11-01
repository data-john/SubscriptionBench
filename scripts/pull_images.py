#!/usr/bin/env python3
"""Pre-pull the SWE-bench instance images for a suite and record their footprint.

The harness removes pulled instance images after grading (cache_level=env), so the
suite's images are pulled explicitly and kept for provisioning and re-grading.
Stops early if free disk drops below --min-free-gb.

Usage: scripts/pull_images.py suites/verified-mvp-v1.json --out suites/verified-mvp-v1.images.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

REGISTRY = "swebench"


def image_name(instance_id: str, tag: str = "latest") -> str:
    # swebench 4.x naming: django__django-14771 -> sweb.eval.x86_64.django_1776_django-14771
    key = instance_id.replace("__", "_1776_")
    return f"{REGISTRY}/sweb.eval.x86_64.{key}:{tag}".lower()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--min-free-gb", type=float, default=25.0)
    args = ap.parse_args()
    suite = json.loads(args.suite.read_text())
    out: dict[str, dict] = {}
    for task in sorted(suite["tasks"], key=lambda t: t["order"]):
        iid = task["instance_id"]
        free = shutil.disk_usage("/").free / 1e9
        if free < args.min_free_gb:
            print(f"STOP: only {free:.1f} GB free")
            break
        name = image_name(iid)
        subprocess.run(["docker", "pull", "-q", name], check=True, stdout=subprocess.DEVNULL)
        info = json.loads(
            subprocess.run(
                ["docker", "image", "inspect", name], check=True, capture_output=True, text=True
            ).stdout
        )[0]
        out[iid] = {"image": name, "id": info["Id"], "size_bytes": info["Size"]}
        print(f"{iid}: {info['Size'] / 1e9:.2f} GB")
    total = sum(v["size_bytes"] for v in out.values())
    args.out.write_text(
        json.dumps({"images": out, "total_bytes": total}, indent=2, sort_keys=True) + "\n"
    )
    print(f"total (uncompressed, shared layers counted once per image): {total / 1e9:.2f} GB")


if __name__ == "__main__":
    main()
