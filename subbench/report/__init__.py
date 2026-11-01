"""Reporting, metrics calculation, tabular exports, and SVG charts."""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from subbench.report.charts import (
    render_efficiency_vs_accuracy_svg,
    render_task_matrix_svg,
)
from subbench.report.export import export_attempts, export_config_metrics
from subbench.report.metrics import (
    ConfigMetrics,
    CycleReportData,
    compute_cycle_metrics,
    wilson_score_interval,
)
from subbench.report.render import render_summary_markdown

logger = logging.getLogger(__name__)

__all__ = [
    "ConfigMetrics",
    "CycleReportData",
    "compute_cycle_metrics",
    "export_attempts",
    "export_config_metrics",
    "generate_report",
    "render_efficiency_vs_accuracy_svg",
    "render_summary_markdown",
    "render_task_matrix_svg",
    "wilson_score_interval",
]


def generate_report(
    conn: sqlite3.Connection,
    cycle_id: int,
    out_dir: Path,
    compare_cycle_id: int | None = None,
) -> dict[str, Path]:
    """Generate all Phase 7 reports, charts, CSVs, and Parquets for a cycle."""
    out_dir.mkdir(parents=True, exist_ok=True)
    report_data = compute_cycle_metrics(conn, cycle_id, compare_cycle_id=compare_cycle_id)

    # 1. SVGs
    chart_eff_svg = out_dir / "efficiency_vs_accuracy.svg"
    chart_eff_svg.write_text(render_efficiency_vs_accuracy_svg(report_data), encoding="utf-8")

    chart_matrix_svg = out_dir / "task_matrix.svg"
    chart_matrix_svg.write_text(render_task_matrix_svg(report_data), encoding="utf-8")

    # 2. Markdown summary
    summary_md = out_dir / "summary.md"
    summary_md.write_text(render_summary_markdown(report_data), encoding="utf-8")

    # 3. CSV and Parquet exports
    att_csv, att_pq = export_attempts(conn, cycle_id, out_dir)
    cfg_csv, cfg_pq = export_config_metrics(report_data, out_dir)

    outputs = {
        "summary_md": summary_md,
        "efficiency_svg": chart_eff_svg,
        "matrix_svg": chart_matrix_svg,
        "attempts_csv": att_csv,
        "attempts_parquet": att_pq,
        "config_metrics_csv": cfg_csv,
        "config_metrics_parquet": cfg_pq,
    }
    logger.info("Generated reports for cycle %d in %s", cycle_id, out_dir)
    return outputs
