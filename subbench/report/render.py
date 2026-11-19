"""Self-contained GitHub-Flavored Markdown renderer for SubscriptionBench summary reports."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from subbench.report.metrics import CycleReportData


def render_summary_markdown(report: CycleReportData) -> str:
    """Render a comprehensive, self-contained summary.md document in GitHub-Flavored Markdown."""
    lines: list[str] = []

    # 1. Leak Warning Banner (if leaks detected)
    if report.leaked_attempt_ids:
        att_str = ", ".join(f"#{a}" for a in sorted(report.leaked_attempt_ids))
        lines.append(
            f"> [!WARNING]\n"
            f"> **POTENTIAL EGRESS / HISTORY LEAK DETECTED:** Egress or git inspection findings "
            f"were flagged on attempt(s): {att_str}. Review `subbench audit` results immediately "
            f"before relying on these benchmark scores.\n"
        )

    # 2. Header and Title
    lines.append(f"# Benchmark Cycle Report: Cycle {report.cycle_id}\n")
    lines.append(f"**Experiment:** `{report.experiment_name}` (ID: {report.experiment_id})  ")
    lines.append(f"**Suite:** `{report.suite_version}` (`{report.suite_digest[:16]}`)  ")
    lines.append(f"**Status:** `{report.status.upper()}`  ")
    if report.flags:
        flags_fmt = ", ".join(f"`{fl}`" for fl in report.flags)
        lines.append(f"**Flags:** {flags_fmt}  ")
    lines.append(f"**Started At:** `{report.started_at}`  ")
    if report.finished_at:
        lines.append(f"**Finished At:** `{report.finished_at}`  ")
    lines.append("")

    # 3. Mandatory F3 Quota Pool Disclosure Alert
    if report.manifest.agent == "codex":
        lines.append(
            "> [!NOTE]\n"
            "> **Quota Pool Disclosure (Codex):** Quota readings come from Codex "
            "`account/rateLimits/read` (`usedPercent` integer percentage points). On ChatGPT Free "
            "plans, configurations share the single `codex-30d` (30-day rolling/monthly) pool. "
            "All reported confidence intervals are Wilson 95% score intervals evaluated on "
            "planned task slots.\n"
        )
    else:
        lines.append(
            "> [!NOTE]\n"
            "> **Quota Pool Disclosure (F3):** Configurations `default` and `capable` draw from "
            "the **shared** `gemini-argon-wtu-weekly` quota pool. Configuration `efficient` "
            "draws from **disjoint** `gemini-5h` and `gemini-weekly` quota pools. Direct quota "
            "efficiency comparisons between Flash and Argon reflect consumption relative to their "
            "respective subscription pools. All reported confidence intervals are Wilson 95% "
            "score intervals evaluated on planned task slots.\n"
        )

    # 4. Executive Summary Table
    lines.append("## 1. Executive Performance & Quota Efficiency\n")
    lines.append(
        "| Config | Role | Model | Resolve Rate (Planned) | Wilson 95% CI | Primary Quota | "
        "Weekly Quota (pp) | Tasks / 100% Weekly | pp / Resolved (Weekly) | Median Time |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for cm in report.configs:
        ci_str = f"[{cm.wilson_ci[0] * 100:.1f}%, {cm.wilson_ci[1] * 100:.1f}%]"
        primary_pp = cm.consumed_pp.get(cm.primary_bucket, 0.0) if cm.primary_bucket else 0.0
        primary_str = f"{primary_pp:.2f}pp (`{cm.primary_bucket}`)" if cm.primary_bucket else "N/A"
        weekly_pp = cm.consumed_pp.get(cm.weekly_bucket, 0.0) if cm.weekly_bucket else 0.0
        tasks_full = (
            f"{cm.tasks_per_full_bucket_weekly:.1f}"
            if cm.tasks_per_full_bucket_weekly is not None
            else "N/A"
        )
        pp_res = (
            f"{cm.pp_per_resolved_task_weekly:.2f}pp"
            if cm.pp_per_resolved_task_weekly is not None
            else "N/A"
        )
        med_w = f"{cm.median_wall_seconds:.1f}s" if cm.median_wall_seconds is not None else "N/A"

        lines.append(
            f"| `{cm.label}` | {cm.role} | `{cm.model_id}` | "
            f"**{cm.resolve_rate_planned * 100:.1f}%** ({cm.resolved_slots}/{cm.planned_slots}) | "
            f"{ci_str} | {primary_str} | {weekly_pp:.2f}pp | {tasks_full} | {pp_res} | {med_w} |"
        )
    lines.append("")

    # 5. Visual Charts
    lines.append("## 2. Visual Visualizations\n")
    lines.append("![Quota Efficiency vs. Accuracy](./efficiency_vs_accuracy.svg)\n")
    lines.append("![Task Outcome Matrix](./task_matrix.svg)\n")

    # 6. Paired Task × Config Outcome Matrix
    lines.append("## 3. Paired Task × Config Outcome Matrix\n")
    header_cols = ["#", "Instance ID", "Repo", "Difficulty"] + [
        f"`{cm.label}`" for cm in report.configs
    ]
    lines.append("| " + " | ".join(header_cols) + " |")
    lines.append("|" + "|".join(["---"] * len(header_cols)) + "|")

    for row in report.matrix_rows:
        row_cells = [
            str(row.seq_in_suite),
            f"`{row.instance_id}`",
            row.repo,
            row.difficulty,
        ]
        for cm in report.configs:
            cell = row.cells.get(cm.label)
            if cell is None or cell.status == "SKIP":
                row_cells.append("`SKIP`")
            elif cell.status == "PASS":
                time_s = f"{cell.mean_wall_seconds:.0f}s" if cell.mean_wall_seconds else ""
                row_cells.append(f"**PASS** ({time_s}, {cell.weekly_pp:.1f}pp)")
            else:
                time_s = f"{cell.mean_wall_seconds:.0f}s" if cell.mean_wall_seconds else ""
                row_cells.append(f"FAIL ({time_s}, {cell.weekly_pp:.1f}pp)")
        lines.append("| " + " | ".join(row_cells) + " |")
    lines.append("")

    # 7. Head-to-Head Win/Tie/Loss Comparisons
    if report.paired_comparisons:
        lines.append("## 4. Head-to-Head Paired Comparisons\n")
        lines.append("| Config A vs Config B | A Wins | B Wins | Ties | Tasks Compared |")
        lines.append("|---|---|---|---|---|")
        for pc in report.paired_comparisons:
            lines.append(
                f"| `{pc.config_a}` vs `{pc.config_b}` | {pc.wins_a} | "
                f"{pc.wins_b} | {pc.ties} | {pc.total_compared} |"
            )
        lines.append("")

    # 8. Halt Reasons & Failure Taxonomy
    lines.append("## 5. Attempt Halt Reasons & Failure Taxonomy\n")
    lines.append(
        "| Config | Completed | Timeout | Quota Restricted | Agent Error | Infra Error | "
        "Retried Attempts | Empty Patch | Patch Apply Failed |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for cm in report.configs:
        lines.append(
            f"| `{cm.label}` | {cm.completed_count} | {cm.timeout_count} | "
            f"{cm.quota_restricted_count} | {cm.agent_error_count} | {cm.infra_error_count} | "
            f"{cm.retried_count} | {cm.empty_patch_count} | {cm.patch_apply_failed_count} |"
        )
    lines.append("")

    # 9. Quota Integrity & Egress Audit Summary
    lines.append("## 6. Allowance Integrity & Egress Audit\n")
    lines.append("| Config | Invalid Quota Attempts | Audit Leaks | Audit Warnings | Audit Info |")
    lines.append("|---|---|---|---|---|")
    for cm in report.configs:
        lines.append(
            f"| `{cm.label}` | {cm.invalid_quota_attempts} | {cm.audit_leak_count} | "
            f"{cm.audit_warn_count} | {cm.audit_info_count} |"
        )
    lines.append("")

    # 10. Longitudinal Comparison (if available)
    if report.longitudinal_deltas:
        lines.append("## 7. Longitudinal Comparison\n")
        lines.append(
            "| Config | Prior Resolve Rate | Current Resolve Rate | Δ Resolve Rate | "
            "Prior pp / Res | Current pp / Res | Δ Total Weekly Quota | Δ Median Wall Time |"
        )
        lines.append("|---|---|---|---|---|---|---|---|")
        for ld in report.longitudinal_deltas:
            d_res_sign = "+" if ld.delta_resolve_rate_planned >= 0 else ""
            d_res_str = f"{d_res_sign}{ld.delta_resolve_rate_planned * 100:.1f}pp"

            prior_pp = (
                f"{ld.prior_weekly_pp_per_resolved:.2f}pp"
                if ld.prior_weekly_pp_per_resolved
                else "N/A"
            )
            curr_pp = (
                f"{ld.curr_weekly_pp_per_resolved:.2f}pp"
                if ld.curr_weekly_pp_per_resolved
                else "N/A"
            )

            d_tot_sign = "+" if ld.delta_total_weekly_pp >= 0 else ""
            d_tot_str = f"{d_tot_sign}{ld.delta_total_weekly_pp:.1f}pp"

            d_w_str = (
                f"{ld.delta_median_wall_seconds:+.1f}s"
                if ld.delta_median_wall_seconds is not None
                else "N/A"
            )

            lines.append(
                f"| `{ld.config_label}` | {ld.prior_resolve_rate_planned * 100:.1f}% | "
                f"{ld.curr_resolve_rate_planned * 100:.1f}% | **{d_res_str}** | "
                f"{prior_pp} | {curr_pp} | {d_tot_str} | {d_w_str} |"
            )
        lines.append("")

        if report.environment_drift:
            ed = report.environment_drift
            lines.append("### Environment Drift\n")
            cli_label = "Codex CLI" if report.manifest.agent == "codex" else "Antigravity CLI"
            lines.append(f"- **{cli_label}:** `{ed.agy_version[0]}` → `{ed.agy_version[1]}`")
            lines.append(
                f"- **SWE-bench:** `{ed.swebench_version[0]}` → `{ed.swebench_version[1]}`"
            )
            h_prior = ed.prompt_hash[0][:12]
            h_curr = ed.prompt_hash[1][:12]
            lines.append(f"- **Prompt Template Hash:** `{h_prior}` → `{h_curr}`")
            for cfg_name, (m_prior, m_curr) in ed.model_ids.items():
                if m_prior != m_curr:
                    lines.append(f"- **Model (`{cfg_name}`):** `{m_prior}` → `{m_curr}`")
            lines.append("")

    return "\n".join(lines)
