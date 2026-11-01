"""Zero-dependency pure-Python SVG chart generators for SubscriptionBench reports."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subbench.report.metrics import CycleReportData

ROLE_COLORS: dict[str, str] = {
    "default": "#2563eb",  # Blue
    "efficient": "#16a34a",  # Green
    "capable": "#9333ea",  # Purple
}


def render_efficiency_vs_accuracy_svg(report: CycleReportData) -> str:
    """Render a pure-Python SVG scatter chart of Quota Efficiency vs Resolved Accuracy."""
    width = 800
    height = 500
    pad_left = 80
    pad_right = 50
    pad_top = 60
    pad_bottom = 70

    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    # Y-axis is Tasks per Full Weekly Bucket
    max_eff = 1.0
    for cm in report.configs:
        val = cm.tasks_per_full_bucket_weekly
        if val is not None and val > max_eff:
            max_eff = val
    # Round max_eff up to nice increment
    y_max = math_ceil_nice(max_eff * 1.25)
    if y_max < 1.0:
        y_max = 1.0

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        'style="background-color: #ffffff; font-family: sans-serif;">',
        f'  <text x="{width // 2}" y="32" font-size="18" font-weight="bold" '
        f'text-anchor="middle" fill="#1e293b">'
        f"Quota Efficiency vs. Accuracy (Cycle {report.cycle_id})</text>",
        "  <!-- Axes and Grid -->",
    ]

    # Grid lines and X ticks (Accuracy: 0% to 100% in 20% steps)
    for x_pct in (0, 20, 40, 60, 80, 100):
        x_pos = pad_left + (x_pct / 100.0) * plot_w
        svg_lines.append(
            f'  <line x1="{x_pos}" y1="{pad_top}" x2="{x_pos}" y2="{pad_top + plot_h}" '
            'stroke="#e2e8f0" stroke-width="1" />'
        )
        svg_lines.append(
            f'  <text x="{x_pos}" y="{pad_top + plot_h + 20}" font-size="12" '
            f'text-anchor="middle" fill="#64748b">{x_pct}%</text>'
        )

    # Grid lines and Y ticks (Tasks per 100% Weekly Bucket)
    y_steps = 5
    for i in range(y_steps + 1):
        y_val = (y_max / y_steps) * i
        y_pos = pad_top + plot_h - (i / y_steps) * plot_h
        svg_lines.append(
            f'  <line x1="{pad_left}" y1="{y_pos}" x2="{pad_left + plot_w}" y2="{y_pos}" '
            'stroke="#e2e8f0" stroke-width="1" />'
        )
        svg_lines.append(
            f'  <text x="{pad_left - 12}" y="{y_pos + 4}" font-size="12" '
            f'text-anchor="end" fill="#64748b">{y_val:.1f}</text>'
        )

    # Axis Lines
    p_bot = pad_top + plot_h
    svg_lines.append(
        f'  <line x1="{pad_left}" y1="{p_bot}" x2="{pad_left + plot_w}" y2="{p_bot}" '
        'stroke="#475569" stroke-width="2" />'
    )
    svg_lines.append(
        f'  <line x1="{pad_left}" y1="{pad_top}" x2="{pad_left}" y2="{p_bot}" '
        'stroke="#475569" stroke-width="2" />'
    )

    # Axis Labels
    svg_lines.append(
        f'  <text x="{pad_left + plot_w // 2}" y="{p_bot + 48}" font-size="14" '
        'font-weight="600" text-anchor="middle" fill="#334155">'
        "Resolved Rate Planned (Wilson 95% CI)</text>"
    )
    mid_y = pad_top + plot_h // 2
    svg_lines.append(
        f'  <text x="24" y="{mid_y}" font-size="14" font-weight="600" text-anchor="middle" '
        f'fill="#334155" transform="rotate(-90, 24, {mid_y})">'
        "Resolved Tasks / 100% Weekly Bucket</text>"
    )

    # Legend at top right
    leg_x = pad_left + plot_w - 280
    leg_y = pad_top + 16
    for idx, cm in enumerate(report.configs):
        color = ROLE_COLORS.get(cm.role, "#3b82f6")
        ly = leg_y + idx * 22
        svg_lines.append(f'  <circle cx="{leg_x}" cy="{ly}" r="6" fill="{color}" />')
        svg_lines.append(
            f'  <text x="{leg_x + 14}" y="{ly + 4}" font-size="12" fill="#1e293b">{cm.label}</text>'
        )

    # Data Points with Error Bars
    for cm in report.configs:
        color = ROLE_COLORS.get(cm.role, "#3b82f6")
        x_rate = cm.resolve_rate_planned
        y_eff = cm.tasks_per_full_bucket_weekly or 0.0

        cx = pad_left + (x_rate * plot_w)
        cy = pad_top + plot_h - ((y_eff / y_max) * plot_h)

        ci_low, ci_high = cm.wilson_ci
        err_x1 = pad_left + (ci_low * plot_w)
        err_x2 = pad_left + (ci_high * plot_w)

        # Horizontal error bar
        svg_lines.append(
            f'  <line x1="{err_x1}" y1="{cy}" x2="{err_x2}" y2="{cy}" '
            f'stroke="{color}" stroke-width="2" stroke-opacity="0.6" />'
        )
        svg_lines.append(
            f'  <line x1="{err_x1}" y1="{cy - 4}" x2="{err_x1}" y2="{cy + 4}" '
            f'stroke="{color}" stroke-width="2" stroke-opacity="0.6" />'
        )
        svg_lines.append(
            f'  <line x1="{err_x2}" y1="{cy - 4}" x2="{err_x2}" y2="{cy + 4}" '
            f'stroke="{color}" stroke-width="2" stroke-opacity="0.6" />'
        )

        # Central dot
        svg_lines.append(
            f'  <circle cx="{cx}" cy="{cy}" r="7" fill="{color}" '
            'stroke="#ffffff" stroke-width="2" />'
        )
        # Value label
        label_text = f"{cm.label} ({x_rate * 100:.1f}%, {y_eff:.1f}t)"
        svg_lines.append(
            f'  <text x="{cx}" y="{cy - 12}" font-size="11" font-weight="bold" '
            f'text-anchor="middle" fill="{color}">{label_text}</text>'
        )

    svg_lines.append("</svg>")
    return "\n".join(svg_lines)


def render_task_matrix_svg(report: CycleReportData) -> str:
    """Render a pure-Python SVG heatmap grid of Tasks (rows) × Configs (columns)."""
    configs = report.configs
    rows = report.matrix_rows

    row_height = 30
    header_height = 50
    label_col_width = 240
    data_col_width = 160
    pad_left = 30
    pad_right = 30
    pad_top = 40
    pad_bottom = 30

    width = pad_left + label_col_width + (data_col_width * len(configs)) + pad_right
    height = pad_top + header_height + (row_height * len(rows)) + pad_bottom

    svg_lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        'style="background-color: #ffffff; font-family: monospace, sans-serif;">',
        f'  <text x="{width // 2}" y="24" font-size="16" font-weight="bold" '
        f'text-anchor="middle" fill="#1e293b">'
        f"Paired Task Outcome Matrix (Cycle {report.cycle_id})</text>",
    ]

    # Column Headers
    y_head = pad_top + header_height - 10
    svg_lines.append(
        f'  <text x="{pad_left + 8}" y="{y_head}" font-size="12" font-weight="bold" '
        'fill="#475569">SWE-bench Instance</text>'
    )
    for c_idx, cm in enumerate(configs):
        x_col = pad_left + label_col_width + (c_idx * data_col_width)
        color = ROLE_COLORS.get(cm.role, "#3b82f6")
        svg_lines.append(
            f'  <text x="{x_col + data_col_width // 2}" y="{y_head}" font-size="12" '
            f'font-weight="bold" text-anchor="middle" fill="{color}">{cm.label}</text>'
        )

    # Header line
    h_line_y = pad_top + header_height
    svg_lines.append(
        f'  <line x1="{pad_left}" y1="{h_line_y}" x2="{width - pad_right}" '
        f'y2="{h_line_y}" stroke="#cbd5e1" stroke-width="2" />'
    )

    # Grid Rows
    for r_idx, row in enumerate(rows):
        y_row = pad_top + header_height + (r_idx * row_height)
        bg_color = "#f8fafc" if r_idx % 2 == 1 else "#ffffff"

        svg_lines.append(
            f'  <rect x="{pad_left}" y="{y_row}" width="{width - pad_left - pad_right}" '
            f'height="{row_height}" fill="{bg_color}" />'
        )

        # Instance label
        inst_label = f"{row.seq_in_suite:2d}. {row.instance_id[:26]}"
        svg_lines.append(
            f'  <text x="{pad_left + 8}" y="{y_row + 19}" font-size="11" '
            f'fill="#334155">{inst_label}</text>'
        )

        for c_idx, cm in enumerate(configs):
            x_col = pad_left + label_col_width + (c_idx * data_col_width)
            cell = row.cells.get(cm.label)
            if cell is None or cell.status == "SKIP":
                status_color = "#94a3b8"
                status_text = "SKIP"
                sub_text = ""
            elif cell.status == "PASS":
                status_color = "#16a34a"
                status_text = "PASS"
                sub_text = f"{cell.weekly_pp:.1f}pp"
            else:
                status_color = "#dc2626"
                status_text = "FAIL"
                sub_text = f"{cell.weekly_pp:.1f}pp"

            # Badge pill
            badge_w = 46
            badge_h = 18
            bx = x_col + 12
            by = y_row + 6
            svg_lines.append(
                f'  <rect x="{bx}" y="{by}" width="{badge_w}" height="{badge_h}" rx="3" '
                f'fill="{status_color}" />'
            )
            svg_lines.append(
                f'  <text x="{bx + badge_w // 2}" y="{by + 13}" font-size="10" '
                f'font-weight="bold" text-anchor="middle" fill="#ffffff">{status_text}</text>'
            )
            if sub_text:
                svg_lines.append(
                    f'  <text x="{bx + badge_w + 10}" y="{by + 13}" font-size="10" '
                    f'fill="#64748b">{sub_text}</text>'
                )

        # Row separator line
        r_line_y = y_row + row_height
        svg_lines.append(
            f'  <line x1="{pad_left}" y1="{r_line_y}" x2="{width - pad_right}" '
            f'y2="{r_line_y}" stroke="#e2e8f0" stroke-width="1" />'
        )

    svg_lines.append("</svg>")
    return "\n".join(svg_lines)


def math_ceil_nice(val: float) -> float:
    """Round up to a clean integer or decimal step."""
    if val <= 1.0:
        return 1.0
    if val <= 5.0:
        return float(math.ceil(val))
    if val <= 20.0:
        return float(math.ceil(val / 2.0) * 2)
    return float(math.ceil(val / 5.0) * 5)
