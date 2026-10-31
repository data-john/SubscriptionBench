"""Pinned SWE-bench Verified loader (parquet from a fixed Hugging Face revision)."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

DATASET = "princeton-nlp/SWE-bench_Verified"
PARQUET_PATH = "data/test-00000-of-00001.parquet"


def parquet_url(revision: str) -> str:
    return f"https://huggingface.co/datasets/{DATASET}/resolve/{revision}/{PARQUET_PATH}"


def fetch(revision: str, cache_dir: Path = Path("data/cache")) -> Path:
    """Download the dataset parquet for an exact commit sha (cached; sha256 recorded)."""
    if len(revision) != 40:
        raise ValueError("pin an exact 40-char commit sha, not a branch name")
    dest = cache_dir / f"verified-{revision[:12]}.parquet"
    if not dest.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        urllib.request.urlretrieve(parquet_url(revision), tmp)  # noqa: S310 (https, fixed host)
        tmp.rename(dest)
    return dest


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_rows(parquet: Path) -> list[dict[str, Any]]:
    import pyarrow.parquet as pq  # suite dependency group

    return pq.read_table(parquet).to_pylist()


def write_jsonl(rows: list[dict[str, Any]], path: Path) -> None:
    """Local jsonl the swebench harness accepts as `--dataset_name`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
