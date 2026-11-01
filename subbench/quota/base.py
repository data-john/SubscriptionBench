"""Core data structures, protocol, and guards for subscription quota tracking."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BucketReading:
    """A snapshot reading for a single quota bucket."""

    bucket_id: str
    window: str  # "5h" | "weekly" | etc.
    remaining_fraction: float
    reset_time: str | None = None


@dataclass(frozen=True)
class QuotaSnapshot:
    """A point-in-time snapshot across all available quota buckets."""

    buckets: dict[str, BucketReading]
    taken_at: str  # ISO-8601 UTC
    provenance: Literal["agy_usage", "manual"]
    raw_json: str


@dataclass(frozen=True)
class BucketDelta:
    """Quota consumption delta for one bucket across an attempt."""

    bucket_id: str
    window: str
    pre_fraction: float
    post_fraction: float
    consumed: float
    pre_reset_time: str | None = None
    post_reset_time: str | None = None


@dataclass(frozen=True)
class AttemptQuotaResult:
    """Quota results across all buckets for an attempt, with reset validity checks."""

    deltas: dict[str, BucketDelta]
    invalid_reset: bool
    invalid_reason: str | None = None


class QuotaProbe(Protocol):
    """Protocol for fetching a point-in-time quota snapshot."""

    def snapshot(self) -> QuotaSnapshot: ...


def _check_unexpected_spend(obj: Any, path: str = "") -> None:
    """Defensive check for non-zero credit or monetary overage fields."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            curr_path = f"{path}.{k}" if path else k
            k_lower = k.lower()
            if ("credit" in k_lower or "overage" in k_lower) and not (
                k_lower == "useg1credits" and v is False
            ):
                if v not in (0, 0.0, False, None, "", []):
                    logger.warning(
                        "Unexpected non-zero credit/overage field in /usage: %s = %s",
                        curr_path,
                        v,
                    )
            _check_unexpected_spend(v, curr_path)
    elif isinstance(obj, list):
        for i, elem in enumerate(obj):
            _check_unexpected_spend(elem, f"{path}[{i}]")


def parse_usage_json(
    raw_stdout: str,
    taken_at: str | None = None,
    provenance: Literal["agy_usage", "manual"] = "agy_usage",
) -> QuotaSnapshot:
    """Parse raw JSON output from `agy -p /usage --output-format json`.

    Preserves full raw_stdout in raw_json and extracts all bucket readings.
    """
    try:
        data = json.loads(raw_stdout)
    except Exception as e:
        raise ValueError(f"Failed to parse usage JSON: {e}") from e

    status = data.get("status")
    if status is not None and status != "SUCCESS":
        raise ValueError(f"usage command returned non-SUCCESS status: {status}")

    _check_unexpected_spend(data)

    cmd_data = data.get("command", {}).get("data", {})
    groups = cmd_data.get("groups", [])
    buckets: dict[str, BucketReading] = {}

    for grp in groups:
        for b in grp.get("buckets", []):
            b_id = b.get("id")
            if not b_id:
                continue
            window = b.get("window", "")
            rem = float(b["remaining_fraction"])
            reset_time = b.get("reset_time")
            buckets[b_id] = BucketReading(
                bucket_id=b_id,
                window=window,
                remaining_fraction=rem,
                reset_time=reset_time,
            )

    if not buckets:
        raise ValueError("No quota buckets found in usage JSON output")

    ts = taken_at or datetime.now(UTC).isoformat()
    return QuotaSnapshot(
        buckets=buckets,
        taken_at=ts,
        provenance=provenance,
        raw_json=raw_stdout,
    )


def compute_attempt_deltas(pre: QuotaSnapshot, post: QuotaSnapshot) -> AttemptQuotaResult:
    """Compute per-bucket consumption and detect mid-attempt rolling window resets.

    Accounts for Phase 0's rolling-until-first-spend behavior: an unused rolling bucket
    (pre.remaining_fraction == 1.0) locks in reset_time on first spend; this is normal
    and not treated as an invalid reset.
    """
    deltas: dict[str, BucketDelta] = {}
    invalid_reset = False
    invalid_reasons: list[str] = []

    for bucket_id, pre_b in pre.buckets.items():
        if bucket_id not in post.buckets:
            continue
        post_b = post.buckets[bucket_id]
        consumed = pre_b.remaining_fraction - post_b.remaining_fraction

        deltas[bucket_id] = BucketDelta(
            bucket_id=bucket_id,
            window=pre_b.window,
            pre_fraction=pre_b.remaining_fraction,
            post_fraction=post_b.remaining_fraction,
            consumed=consumed,
            pre_reset_time=pre_b.reset_time,
            post_reset_time=post_b.reset_time,
        )

        # 1. Did the bucket fraction increase mid-attempt?
        if post_b.remaining_fraction > pre_b.remaining_fraction + 1e-6:
            invalid_reset = True
            invalid_reasons.append(
                f"Bucket '{bucket_id}' fraction increased mid-attempt: "
                f"{pre_b.remaining_fraction:.4f} -> {post_b.remaining_fraction:.4f}"
            )
        # 2. Did reset_time shift when pre was already spent (< 1.0)?
        elif pre_b.remaining_fraction < 1.0 - 1e-6:
            if pre_b.reset_time != post_b.reset_time:
                invalid_reset = True
                invalid_reasons.append(
                    f"Bucket '{bucket_id}' reset_time changed mid-attempt from "
                    f"{pre_b.reset_time} to {post_b.reset_time}"
                )

    reason = "; ".join(invalid_reasons) if invalid_reasons else None
    return AttemptQuotaResult(
        deltas=deltas,
        invalid_reset=invalid_reset,
        invalid_reason=reason,
    )


def detect_external_use(
    prev_post: QuotaSnapshot,
    next_pre: QuotaSnapshot,
    watched_buckets: Iterable[str],
    threshold: float = 1e-4,
) -> list[str]:
    """Detect unexplained quota drops between attempts on watched buckets.

    Ignores buckets whose window reset upward.
    """
    external_buckets: list[str] = []
    for bucket_id in watched_buckets:
        if bucket_id in prev_post.buckets and bucket_id in next_pre.buckets:
            b_prev = prev_post.buckets[bucket_id]
            b_next = next_pre.buckets[bucket_id]
            # Ignore upward resets
            if b_next.remaining_fraction > b_prev.remaining_fraction + 1e-6:
                continue
            drop = b_prev.remaining_fraction - b_next.remaining_fraction
            if drop > threshold:
                external_buckets.append(bucket_id)
    return external_buckets


def check_idle(
    probe: QuotaProbe,
    watched_buckets: Iterable[str],
    idle_seconds: int,
    threshold: float = 1e-4,
    sleep_fn=time.sleep,
) -> tuple[QuotaSnapshot, QuotaSnapshot, list[str]]:
    """Probe for external activity by comparing two snapshots separated by idle_seconds."""
    snap1 = probe.snapshot()
    if idle_seconds <= 0:
        return snap1, snap1, []
    sleep_fn(idle_seconds)
    snap2 = probe.snapshot()
    leaked = detect_external_use(snap1, snap2, watched_buckets, threshold=threshold)
    return snap1, snap2, leaked


def save_quota_snapshot(
    conn: sqlite3.Connection,
    *,
    cycle_id: int,
    attempt_id: int | None,
    phase: str,
    snap: QuotaSnapshot,
) -> None:
    """Persist a QuotaSnapshot into the quota_snapshots SQLite table."""
    for b in snap.buckets.values():
        conn.execute(
            """
            INSERT INTO quota_snapshots (
                cycle_id, attempt_id, phase, bucket_id, window,
                remaining_fraction, reset_time, taken_at, provenance, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                cycle_id,
                attempt_id,
                phase,
                b.bucket_id,
                b.window,
                b.remaining_fraction,
                b.reset_time,
                snap.taken_at,
                snap.provenance,
                snap.raw_json,
            ),
        )
    conn.commit()
