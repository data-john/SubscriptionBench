"""Manual quota probe loading operator-supplied snapshot files."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from subbench.quota.base import BucketReading, QuotaSnapshot, parse_usage_json


class ManualQuotaProbe:
    """Provides quota snapshots from a local JSON file or in-memory dict."""

    def __init__(self, source: Path | str | dict[str, Any]) -> None:
        if isinstance(source, (str, Path)):
            self.path = Path(source)
            self._raw = self.path.read_text(encoding="utf-8")
        else:
            self.path = None
            self._raw = json.dumps(source)

    def snapshot(self) -> QuotaSnapshot:
        try:
            data = json.loads(self._raw)
        except Exception as e:
            raise ValueError(f"Failed to parse manual quota JSON: {e}") from e

        # Check if full agy /usage JSON format
        if "command" in data or "status" in data:
            return parse_usage_json(self._raw, provenance="manual")

        # Otherwise expect a dictionary of bucket_id -> info
        buckets: dict[str, BucketReading] = {}
        for b_id, b_info in data.items():
            if isinstance(b_info, dict):
                window = b_info.get("window", "unknown")
                rem = float(b_info.get("remaining_fraction", 1.0))
                reset_time = b_info.get("reset_time")
            else:
                window = "unknown"
                rem = float(b_info)
                reset_time = None

            buckets[b_id] = BucketReading(
                bucket_id=b_id,
                window=window,
                remaining_fraction=rem,
                reset_time=reset_time,
            )

        now_iso = datetime.now(UTC).isoformat()
        return QuotaSnapshot(
            buckets=buckets,
            taken_at=now_iso,
            provenance="manual",
            raw_json=self._raw,
        )
