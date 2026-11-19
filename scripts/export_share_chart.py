#!/usr/bin/env python3
"""Generate a high-definition 16:9 publication PNG of the Antigravity Pareto chart.

Outputs:
  - artifacts/reports/antigravity_pareto_hd.png (2400x1350 @ 300 DPI — Ko-fi gallery, Reddit, X, YT)
  - site/og-image.png (1200x675 @ 150 DPI — OpenGraph social card)
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FixedFormatter, FixedLocator

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "site" / "data" / "benchmark_data.json"
HD_OUT = ROOT / "artifacts" / "reports" / "antigravity_pareto_hd.png"
OG_OUT = ROOT / "site" / "og-image.png"

EFFORT_SHORT = {
    "low": "Low",
    "medium": "Med",
    "high": "High",
    "xhigh": "xHigh",
    "max": "Max",
}


def compute_pareto(points: list[dict]) -> list[dict]:
    sorted_pts = sorted(
        points,
        key=lambda p: (-p["tasks_per_month_primary"], -p["aa_intelligence"]),
    )
    frontier: list[dict] = []
    max_y = float("-inf")
    for p in sorted_pts:
        if p["aa_intelligence"] > max_y + 1e-9:
            frontier.append(p)
            max_y = p["aa_intelligence"]
    return sorted(frontier, key=lambda p: p["tasks_per_month_primary"])


def render_chart(data: dict, out_path: Path, dpi: int) -> None:
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "axes.edgecolor": "#cbd5e1",
            "axes.linewidth": 0.9,
        }
    )

    fig, ax = plt.subplots(figsize=(8.0, 4.5), dpi=dpi)
    fig.patch.set_facecolor("#ffffff")
    ax.set_facecolor("#f8fafc")

    # All 6 currently available Antigravity models (exclude Codex Free)
    pts = [p for p in data["projections"] if p["weekly_bucket"] != "codex-30d"]
    nov7_pts = [p for p in pts if p["era"] == "nov7"]

    # Group by family
    families: dict[str, list[dict]] = defaultdict(list)
    for p in pts:
        families[p["family_key"]].append(p)

    # Order legend cleanly
    family_order = [
        "gemini-4-neon@gemini-weekly",
        "gemini-4-hydrogen@gemini-weekly",
        "gemini-4-argon@gemini-argon-wtu-weekly",
        "gemini-3.8-flash@gemini-weekly",
        "claude-opus-5-5@3p-weekly",
        "claude-sonnet-5-5@3p-weekly",
    ]

    # Plot Nov 7 baseline frontier (dashed)
    nov7_frontier = compute_pareto(nov7_pts)
    if len(nov7_frontier) >= 2:
        ax.plot(
            [p["tasks_per_month_primary"] for p in nov7_frontier],
            [p["aa_intelligence"] for p in nov7_frontier],
            color="#64748b",
            linestyle="--",
            linewidth=1.5,
            alpha=0.85,
            zorder=2,
        )
        mid_x = (
            nov7_frontier[0]["tasks_per_month_primary"]
            * nov7_frontier[-1]["tasks_per_month_primary"]
        ) ** 0.5
        mid_y = 0.5 * (
            nov7_frontier[0]["aa_intelligence"] + nov7_frontier[-1]["aa_intelligence"]
        )
        ax.annotate(
            "Nov 7 frontier",
            xy=(mid_x, mid_y),
            xytext=(8, 4),
            textcoords="offset points",
            fontsize=7.2,
            color="#64748b",
            style="italic",
            zorder=6,
        )

    # Plot Today's Pareto frontier (solid dark line)
    today_frontier = compute_pareto(pts)
    if len(today_frontier) >= 2:
        ax.plot(
            [p["tasks_per_month_primary"] for p in today_frontier],
            [p["aa_intelligence"] for p in today_frontier],
            color="#0f172a",
            linestyle="-",
            linewidth=2.1,
            alpha=0.92,
            zorder=3,
        )

    # Plot each model family curve + points
    legend_handles: list[Line2D] = []
    for fk in family_order:
        rows = families.get(fk)
        if not rows:
            continue
        rows = sorted(rows, key=lambda r: r["effort_order"])
        color = rows[0]["color"]
        label = rows[0]["family_label"]

        if len(rows) > 1:
            ax.plot(
                [r["tasks_per_month_primary"] for r in rows],
                [r["aa_intelligence"] for r in rows],
                color=color,
                linewidth=1.6,
                alpha=0.72,
                zorder=3,
            )

        measured = [r for r in rows if r["measured"]]
        scaled = [r for r in rows if not r["measured"]]

        if measured:
            ax.scatter(
                [r["tasks_per_month_primary"] for r in measured],
                [r["aa_intelligence"] for r in measured],
                s=46,
                color=color,
                edgecolors="#ffffff",
                linewidths=1.0,
                zorder=5,
            )
        if scaled:
            ax.scatter(
                [r["tasks_per_month_primary"] for r in scaled],
                [r["aa_intelligence"] for r in scaled],
                s=40,
                facecolors="#ffffff",
                edgecolors=color,
                linewidths=1.6,
                zorder=4,
            )

        legend_handles.append(
            Line2D(
                [0],
                [0],
                marker="o",
                color=color,
                label=label,
                markersize=6,
                linewidth=1.6,
                markeredgecolor="#ffffff",
                markeredgewidth=0.6,
            )
        )

    # Crisp non-overlapping labels on Today's Pareto frontier points
    label_offsets = {
        "gemini-4-neon@gemini-weekly:xhigh": (7, 6),
        "gemini-4-neon@gemini-weekly:high": (8, 5),
        "gemini-4-neon@gemini-weekly:medium": (8, 5),
        "gemini-4-neon@gemini-weekly:low": (8, 5),
        "gemini-4-hydrogen@gemini-weekly:low": (-88, 12),
    }
    for p in today_frontier:
        short_fam = p["family_label"].replace("Gemini 4 ", "")
        eff = EFFORT_SHORT.get(p["effort"], p["effort"])
        tasks = round(p["tasks_per_month_primary"])
        dx, dy = label_offsets.get(p["id"], (7, 5))
        ax.annotate(
            f"{short_fam} {eff} ({tasks}/mo)",
            xy=(p["tasks_per_month_primary"], p["aa_intelligence"]),
            xytext=(dx, dy),
            textcoords="offset points",
            fontsize=7.6,
            fontweight="bold",
            color="#0f172a",
            bbox=dict(
                boxstyle="round,pad=0.2",
                facecolor="#ffffff",
                edgecolor="none",
                alpha=0.84,
            ),
            zorder=7,
        )

    # Axes & Grid
    ax.set_xscale("log")
    ax.set_xlim(1.5, 1100)
    ax.set_ylim(22, 66.5)

    xticks = [2, 5, 10, 25, 50, 100, 250, 500, 1000]
    ax.xaxis.set_major_locator(FixedLocator(xticks))
    ax.xaxis.set_major_formatter(FixedFormatter([str(t) for t in xticks]))
    ax.xaxis.set_minor_locator(FixedLocator([]))

    ax.grid(True, which="major", color="#e2e8f0", linewidth=0.8, zorder=1)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    ax.tick_params(axis="both", labelsize=8.5, colors="#475569")
    ax.set_xlabel(
        "Estimated Tasks per Month (Google AI Pro · Log Scale)",
        fontsize=9.2,
        fontweight="semibold",
        color="#334155",
        labelpad=7,
    )
    ax.set_ylabel(
        "Intelligence Score (Artificial Analysis v5)",
        fontsize=9.2,
        fontweight="semibold",
        color="#334155",
        labelpad=7,
    )

    # Title & Subtitle
    fig.text(
        0.075,
        0.935,
        "Google AI Pro Antigravity: Intelligence vs. Monthly Capacity",
        fontsize=12.8,
        fontweight="bold",
        color="#0f172a",
    )
    fig.text(
        0.075,
        0.888,
        "Gemini 4 Neon dominates the Pareto frontier across every reasoning tier above Hydrogen Low  ·  Nov 19, 2026",
        fontsize=8.3,
        color="#475569",
    )

    # Legend (clean 2-row horizontal below title or inside bottom-left)
    legend_handles.append(
        Line2D([0], [0], color="#0f172a", linewidth=2.0, label="Pareto Frontier")
    )
    ax.legend(
        handles=legend_handles,
        loc="lower left",
        ncol=2,
        frameon=True,
        facecolor="#ffffff",
        edgecolor="#e2e8f0",
        framealpha=0.95,
        fontsize=7.3,
        handlelength=1.8,
        columnspacing=1.1,
        borderpad=0.55,
    )

    # Footer watermark / metadata
    fig.text(
        0.075,
        0.022,
        "SubscriptionBench  ·  data-john.github.io/SubscriptionBench  ·  Filled = directly measured, hollow = effort-scaled",
        fontsize=7.1,
        color="#64748b",
    )

    fig.subplots_adjust(left=0.095, right=0.965, top=0.84, bottom=0.165)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=dpi, facecolor="#ffffff")
    plt.close(fig)
    print(f"Saved {out_path} ({dpi} DPI)")


def main() -> None:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    render_chart(data, HD_OUT, dpi=300)
    render_chart(data, OG_OUT, dpi=150)


if __name__ == "__main__":
    main()
