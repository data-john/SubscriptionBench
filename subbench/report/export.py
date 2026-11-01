"""CSV and Parquet tabular export writers for SubscriptionBench reports."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from subbench.report.metrics import CycleReportData


def export_attempts(conn: sqlite3.Connection, cycle_id: int, out_dir: Path) -> tuple[Path, Path]:
    """Export cycle attempts to both CSV and Parquet."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "attempts.csv"
    parquet_path = out_dir / "attempts.parquet"

    cur = conn.execute(
        """
        SELECT
            a.id,
            a.cycle_id,
            c.label AS config_label,
            c.role AS config_role,
            c.model_id,
            a.instance_id,
            a.pass_index,
            a.seq,
            a.retry_of,
            a.state,
            a.halt_reason,
            a.valid,
            a.invalid_reason,
            a.exit_code,
            a.finished_reason,
            a.started_at,
            a.ended_at,
            a.wall_seconds,
            a.num_turns,
            a.input_tokens,
            a.output_tokens,
            a.thinking_tokens,
            a.cache_read_tokens,
            a.tool_calls,
            a.patch_bytes,
            g.resolved,
            g.patch_applied,
            g.error_text AS grading_error
        FROM attempts a
        JOIN configs c ON a.config_id = c.id
        LEFT JOIN grades g ON a.id = g.attempt_id
        WHERE a.cycle_id = ?
        ORDER BY a.seq ASC, a.id ASC
        """,
        (cycle_id,),
    )
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()

    data: list[dict[str, Any]] = []
    for r in rows:
        d = dict(zip(cols, r, strict=False))
        # Ensure clean types
        d["resolved"] = int(d["resolved"]) if d["resolved"] is not None else 0
        d["patch_applied"] = int(d["patch_applied"]) if d["patch_applied"] is not None else 0
        data.append(d)

    # 1. Write CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(data)

    # 2. Write Parquet via pyarrow
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(data) if data else pa.table({c: [] for c in cols})
    pq.write_table(table, parquet_path)

    return csv_path, parquet_path


def export_config_metrics(report: CycleReportData, out_dir: Path) -> tuple[Path, Path]:
    """Export summary metrics per config to both CSV and Parquet."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "config_metrics.csv"
    parquet_path = out_dir / "config_metrics.parquet"

    rows: list[dict[str, Any]] = []
    for cm in report.configs:
        primary_burn_pp = cm.consumed_pp.get(cm.primary_bucket, 0.0) if cm.primary_bucket else 0.0
        weekly_burn_pp = cm.consumed_pp.get(cm.weekly_bucket, 0.0) if cm.weekly_bucket else 0.0

        rows.append(
            {
                "cycle_id": report.cycle_id,
                "config_id": cm.config_id,
                "label": cm.label,
                "role": cm.role,
                "model_id": cm.model_id,
                "effort": cm.effort or "",
                "planned_slots": cm.planned_slots,
                "attempted_slots": cm.attempted_slots,
                "resolved_slots": cm.resolved_slots,
                "resolved_valid_slots": cm.resolved_valid_slots,
                "resolve_rate_planned": cm.resolve_rate_planned,
                "wilson_ci_low": cm.wilson_ci[0],
                "wilson_ci_high": cm.wilson_ci[1],
                "resolve_rate_attempted": cm.resolve_rate_attempted,
                "primary_bucket": cm.primary_bucket or "",
                "weekly_bucket": cm.weekly_bucket or "",
                "primary_burn_pp": primary_burn_pp,
                "weekly_burn_pp": weekly_burn_pp,
                "tasks_per_full_primary": cm.tasks_per_full_bucket_primary or 0.0,
                "pp_per_resolved_primary": cm.pp_per_resolved_task_primary or 0.0,
                "tasks_per_full_weekly": cm.tasks_per_full_bucket_weekly or 0.0,
                "pp_per_resolved_weekly": cm.pp_per_resolved_task_weekly or 0.0,
                "median_wall_seconds": cm.median_wall_seconds or 0.0,
                "max_wall_seconds": cm.max_wall_seconds or 0.0,
                "median_resolved_wall_seconds": cm.median_resolved_wall_seconds or 0.0,
                "median_tool_calls": cm.median_tool_calls or 0.0,
                "input_tokens": cm.input_tokens,
                "output_tokens": cm.output_tokens,
                "thinking_tokens": cm.thinking_tokens,
                "cache_read_tokens": cm.cache_read_tokens,
                "completed_count": cm.completed_count,
                "timeout_count": cm.timeout_count,
                "quota_restricted_count": cm.quota_restricted_count,
                "agent_error_count": cm.agent_error_count,
                "infra_error_count": cm.infra_error_count,
                "retried_count": cm.retried_count,
                "empty_patch_count": cm.empty_patch_count,
                "patch_apply_failed_count": cm.patch_apply_failed_count,
                "invalid_quota_attempts": cm.invalid_quota_attempts,
                "audit_leak_count": cm.audit_leak_count,
                "audit_warn_count": cm.audit_warn_count,
                "audit_info_count": cm.audit_info_count,
            }
        )

    cols = list(rows[0].keys()) if rows else []

    # 1. Write CSV
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=cols)
        writer.writeheader()
        writer.writerows(rows)

    # 2. Write Parquet via pyarrow
    import pyarrow as pa
    import pyarrow.parquet as pq

    table = pa.Table.from_pylist(rows) if rows else pa.table({c: [] for c in cols})
    pq.write_table(table, parquet_path)

    return csv_path, parquet_path
