"""Statistical and quota accounting engine for SubscriptionBench cycles."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from subbench.config import ExperimentManifest, SuiteManifest, load_suite


def wilson_score_interval(resolved: int, total: int, z: float = 1.959964) -> tuple[float, float]:
    """Calculate the Wilson 95% score interval for binomial proportions.

    Handles edge cases (0/12, 12/12, 0/0) gracefully.
    """
    if total <= 0:
        return 0.0, 0.0

    p_hat = resolved / total
    n = float(total)
    z2 = z * z

    denominator = 1.0 + z2 / n
    center = (p_hat + z2 / (2.0 * n)) / denominator
    margin = (z * math.sqrt((p_hat * (1.0 - p_hat) / n) + (z2 / (4.0 * n * n)))) / denominator

    low = max(0.0, center - margin)
    high = min(1.0, center + margin)
    return low, high


@dataclass(frozen=True)
class TaskMatrixCell:
    status: str  # "PASS", "FAIL", "SKIP"
    resolved_passes: int
    total_passes: int
    mean_wall_seconds: float | None
    weekly_pp: float
    tool_calls: int


@dataclass(frozen=True)
class TaskMatrixRow:
    seq_in_suite: int
    instance_id: str
    repo: str
    difficulty: str
    cells: dict[str, TaskMatrixCell]


@dataclass(frozen=True)
class PairedComparison:
    config_a: str
    config_b: str
    wins_a: int
    wins_b: int
    ties: int
    total_compared: int


@dataclass
class ConfigMetrics:
    config_id: int
    label: str
    role: str
    model_id: str
    effort: str | None
    planned_slots: int
    attempted_slots: int
    resolved_slots: int
    resolved_valid_slots: int
    resolve_rate_planned: float
    wilson_ci: tuple[float, float]
    resolve_rate_attempted: float
    primary_bucket: str | None
    weekly_bucket: str | None
    consumed_fraction: dict[str, float] = field(default_factory=dict)
    consumed_pp: dict[str, float] = field(default_factory=dict)
    tasks_per_full_bucket_primary: float | None = None
    pp_per_resolved_task_primary: float | None = None
    tasks_per_full_bucket_weekly: float | None = None
    pp_per_resolved_task_weekly: float | None = None
    median_wall_seconds: float | None = None
    max_wall_seconds: float | None = None
    median_resolved_wall_seconds: float | None = None
    median_tool_calls: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0
    completed_count: int = 0
    timeout_count: int = 0
    quota_restricted_count: int = 0
    agent_error_count: int = 0
    infra_error_count: int = 0
    retried_count: int = 0
    empty_patch_count: int = 0
    patch_apply_failed_count: int = 0
    invalid_quota_attempts: int = 0
    audit_leak_count: int = 0
    audit_warn_count: int = 0
    audit_info_count: int = 0


@dataclass(frozen=True)
class LongitudinalDelta:
    config_label: str
    role: str
    prior_resolve_rate_planned: float
    curr_resolve_rate_planned: float
    delta_resolve_rate_planned: float
    prior_weekly_pp_per_resolved: float | None
    curr_weekly_pp_per_resolved: float | None
    delta_weekly_pp_per_resolved: float | None
    prior_total_weekly_pp: float
    curr_total_weekly_pp: float
    delta_total_weekly_pp: float
    prior_median_wall_seconds: float | None
    curr_median_wall_seconds: float | None
    delta_median_wall_seconds: float | None


@dataclass(frozen=True)
class EnvironmentDrift:
    agy_version: tuple[str, str]
    model_ids: dict[str, tuple[str, str]]
    swebench_version: tuple[str, str]
    prompt_hash: tuple[str, str]


@dataclass
class CycleReportData:
    cycle_id: int
    experiment_id: int
    experiment_name: str
    suite_version: str
    suite_digest: str
    status: str
    flags: list[str]
    started_at: str
    finished_at: str | None
    environment: dict[str, Any]
    manifest: ExperimentManifest
    suite: SuiteManifest
    configs: list[ConfigMetrics]
    matrix_rows: list[TaskMatrixRow]
    paired_comparisons: list[PairedComparison]
    leaked_attempt_ids: list[int]
    longitudinal_deltas: list[LongitudinalDelta] | None = None
    environment_drift: EnvironmentDrift | None = None


def compute_cycle_metrics(
    conn: sqlite3.Connection,
    cycle_id: int,
    compare_cycle_id: int | None = None,
) -> CycleReportData:
    """Compute all statistical, quota, and integrity metrics for a cycle."""
    # 1. Fetch cycle and experiment metadata
    cur = conn.execute(
        """
        SELECT c.id, c.experiment_id, c.started_at, c.finished_at, c.status, c.flags_json,
               e.name, e.manifest_json, e.suite_version, e.suite_digest
        FROM cycles c
        JOIN experiments e ON c.experiment_id = e.id
        WHERE c.id = ?
        """,
        (cycle_id,),
    )
    cycle_row = cur.fetchone()
    if not cycle_row:
        raise ValueError(f"Cycle id {cycle_id} not found")

    (
        c_id,
        exp_id,
        started_at,
        finished_at,
        status,
        flags_json,
        exp_name,
        manifest_json,
        suite_ver,
        suite_digest,
    ) = cycle_row

    flags = json.loads(flags_json) if flags_json else []
    manifest = ExperimentManifest.model_validate(json.loads(manifest_json))
    suite = load_suite(Path(manifest.suite))

    # Fetch environment metadata
    cur = conn.execute(
        "SELECT * FROM environment WHERE experiment_id = ?",
        (exp_id,),
    )
    env_row = cur.fetchone()
    env_cols = [d[0] for d in cur.description] if cur.description else []
    env_dict = dict(zip(env_cols, env_row, strict=False)) if env_row else {}

    # Fetch configs
    cur = conn.execute(
        """
        SELECT id, label, role, model_id, effort, buckets_json
        FROM configs
        WHERE experiment_id = ?
        ORDER BY id
        """,
        (exp_id,),
    )
    config_rows = cur.fetchall()

    # Pre-fetch audit findings for cycle
    cur = conn.execute(
        """
        SELECT a.id, f.severity
        FROM attempts a
        JOIN audit_findings f ON a.id = f.attempt_id
        WHERE a.cycle_id = ?
        """,
        (cycle_id,),
    )
    audit_rows = cur.fetchall()
    audit_by_attempt: dict[int, list[str]] = {}
    leaked_attempt_ids: list[int] = []
    for att_id, sev in audit_rows:
        audit_by_attempt.setdefault(att_id, []).append(sev)
        if sev == "leak" and att_id not in leaked_attempt_ids:
            leaked_attempt_ids.append(att_id)

    # Pre-fetch quota snapshots for cycle (pre and post per attempt and bucket)
    cur = conn.execute(
        """
        SELECT attempt_id, phase, bucket_id, remaining_fraction
        FROM quota_snapshots
        WHERE cycle_id = ? AND attempt_id IS NOT NULL
        """,
        (cycle_id,),
    )
    snap_readings: dict[tuple[int, str, str], float] = {}
    for att_id, phase, b_id, rem in cur.fetchall():
        snap_readings[(att_id, phase, b_id)] = rem

    # Fetch all attempts for this cycle
    cur = conn.execute(
        """
        SELECT a.id, a.config_id, a.instance_id, a.pass_index, a.seq, a.retry_of,
               a.state, a.halt_reason, a.valid, a.patch_bytes, a.wall_seconds,
               a.num_turns, a.input_tokens, a.output_tokens, a.thinking_tokens,
               a.cache_read_tokens, a.tool_calls,
               g.resolved, g.patch_applied
        FROM attempts a
        LEFT JOIN grades g ON a.id = g.attempt_id
        WHERE a.cycle_id = ?
        ORDER BY a.seq ASC, a.id ASC
        """,
        (cycle_id,),
    )
    all_attempts = cur.fetchall()

    tasks = suite.ordered()
    if manifest.task_limit is not None:
        tasks = tasks[: manifest.task_limit]
    task_count = len(tasks)

    configs_metrics: list[ConfigMetrics] = []
    slot_final_attempts: dict[tuple[int, str, int], Any] = {}
    attempts_by_slot: dict[tuple[int, str, int], list[Any]] = {}

    for att in all_attempts:
        cfg_id = att[1]
        inst_id = att[2]
        p_idx = att[3]
        slot_key = (cfg_id, inst_id, p_idx)
        attempts_by_slot.setdefault(slot_key, []).append(att)
        slot_final_attempts[slot_key] = att  # Last one ordered by seq/id

    for cfg_id, label, role, model_id, effort, _buckets_json in config_rows:
        cfg_spec = next((c for c in manifest.configs if c.label == label), None)
        effective_passes = (
            min(manifest.passes, cfg_spec.max_passes or manifest.passes)
            if cfg_spec
            else manifest.passes
        )
        planned_slots = task_count * effective_passes

        primary_b = manifest.quota.primary_bucket_by_config.get(label)
        weekly_b = manifest.quota.weekly_bucket_by_config.get(label)
        all_watched = cfg_spec.buckets if cfg_spec else []

        cfg_attempts = [a for a in all_attempts if a[1] == cfg_id]
        attempted_slots_set = {(a[2], a[3]) for a in cfg_attempts if a[3] < effective_passes}
        attempted_slots = len(attempted_slots_set)

        # Slot resolution scoring
        resolved_slots = 0
        resolved_valid_slots = 0
        final_attempts_for_cfg = [
            att
            for (c_id_k, _, p_idx_k), att in slot_final_attempts.items()
            if c_id_k == cfg_id and p_idx_k < effective_passes
        ]

        for final_att in final_attempts_for_cfg:
            inst_id = final_att[2]
            p_idx = final_att[3]
            slot_key = (cfg_id, inst_id, p_idx)
            is_resolved = bool(final_att[17]) if final_att[17] is not None else False

            if is_resolved:
                resolved_slots += 1
                # Check if all attempts contributing to this slot chain had valid = 1
                chain_attempts = attempts_by_slot.get(slot_key, [])
                if all(ca[8] == 1 for ca in chain_attempts):
                    resolved_valid_slots += 1

        resolve_rate_planned = resolved_slots / planned_slots if planned_slots > 0 else 0.0
        wilson_ci = wilson_score_interval(resolved_slots, planned_slots)
        resolve_rate_attempted = resolved_slots / attempted_slots if attempted_slots > 0 else 0.0

        # Allowance consumption across all valid=1 attempts
        consumed_fraction: dict[str, float] = {b: 0.0 for b in all_watched}
        invalid_quota_attempts = 0

        for att in cfg_attempts:
            att_id = att[0]
            valid = att[8]
            if valid != 1:
                invalid_quota_attempts += 1
                continue

            for b in all_watched:
                pre_rem = snap_readings.get((att_id, "pre", b))
                post_rem = snap_readings.get((att_id, "post", b))
                if pre_rem is not None and post_rem is not None:
                    diff = max(0.0, pre_rem - post_rem)
                    consumed_fraction[b] += diff

        consumed_pp = {b: frac * 100.0 for b, frac in consumed_fraction.items()}

        # Tasks per full bucket and pp per resolved task
        tasks_per_full_primary = None
        pp_per_resolved_primary = None
        if primary_b and primary_b in consumed_fraction:
            c_frac = consumed_fraction[primary_b]
            if c_frac > 1e-9:
                tasks_per_full_primary = resolved_valid_slots / c_frac
            if resolved_valid_slots > 0:
                pp_per_resolved_primary = (c_frac * 100.0) / resolved_valid_slots

        tasks_per_full_weekly = None
        pp_per_resolved_weekly = None
        if weekly_b and weekly_b in consumed_fraction:
            c_frac_w = consumed_fraction[weekly_b]
            if c_frac_w > 1e-9:
                tasks_per_full_weekly = resolved_valid_slots / c_frac_w
            if resolved_valid_slots > 0:
                pp_per_resolved_weekly = (c_frac_w * 100.0) / resolved_valid_slots

        # Wall seconds and tool calls over final attempts
        walls = [fa[10] for fa in final_attempts_for_cfg if fa[10] is not None]
        resolved_walls = [
            fa[10] for fa in final_attempts_for_cfg if fa[10] is not None and fa[17] == 1
        ]
        tool_counts = [fa[16] for fa in final_attempts_for_cfg if fa[16] is not None]

        median_wall = statistics.median(walls) if walls else None
        max_wall = max(walls) if walls else None
        median_resolved_wall = statistics.median(resolved_walls) if resolved_walls else None
        median_tools = statistics.median(tool_counts) if tool_counts else None

        # Tokens across ALL attempts
        input_tokens = sum(a[12] or 0 for a in cfg_attempts)
        output_tokens = sum(a[13] or 0 for a in cfg_attempts)
        thinking_tokens = sum(a[14] or 0 for a in cfg_attempts)
        cache_read_tokens = sum(a[15] or 0 for a in cfg_attempts)

        # Failure & halt taxonomy across ALL attempts
        completed_c = sum(1 for a in cfg_attempts if a[7] == "completed")
        timeout_c = sum(1 for a in cfg_attempts if a[7] == "timeout")
        quota_restricted_c = sum(1 for a in cfg_attempts if a[7] == "quota_restricted")
        agent_error_c = sum(1 for a in cfg_attempts if a[7] == "agent_error")
        infra_error_c = sum(1 for a in cfg_attempts if a[7] == "infra_error")
        retried_c = sum(1 for a in cfg_attempts if a[5] is not None)
        empty_patch_c = sum(1 for a in cfg_attempts if (a[9] or 0) == 0)
        patch_apply_failed_c = sum(
            1 for a in cfg_attempts if (a[9] or 0) > 0 and (a[18] == 0 or a[18] is None)
        )

        # Audit findings for this config's attempts
        cfg_att_ids = {a[0] for a in cfg_attempts}
        leak_c = sum(
            1 for att_id in cfg_att_ids for sev in audit_by_attempt.get(att_id, []) if sev == "leak"
        )
        warn_c = sum(
            1 for att_id in cfg_att_ids for sev in audit_by_attempt.get(att_id, []) if sev == "warn"
        )
        info_c = sum(
            1 for att_id in cfg_att_ids for sev in audit_by_attempt.get(att_id, []) if sev == "info"
        )

        cm = ConfigMetrics(
            config_id=cfg_id,
            label=label,
            role=role,
            model_id=model_id,
            effort=effort,
            planned_slots=planned_slots,
            attempted_slots=attempted_slots,
            resolved_slots=resolved_slots,
            resolved_valid_slots=resolved_valid_slots,
            resolve_rate_planned=resolve_rate_planned,
            wilson_ci=wilson_ci,
            resolve_rate_attempted=resolve_rate_attempted,
            primary_bucket=primary_b,
            weekly_bucket=weekly_b,
            consumed_fraction=consumed_fraction,
            consumed_pp=consumed_pp,
            tasks_per_full_bucket_primary=tasks_per_full_primary,
            pp_per_resolved_task_primary=pp_per_resolved_primary,
            tasks_per_full_bucket_weekly=tasks_per_full_weekly,
            pp_per_resolved_task_weekly=pp_per_resolved_weekly,
            median_wall_seconds=median_wall,
            max_wall_seconds=max_wall,
            median_resolved_wall_seconds=median_resolved_wall,
            median_tool_calls=median_tools,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            thinking_tokens=thinking_tokens,
            cache_read_tokens=cache_read_tokens,
            completed_count=completed_c,
            timeout_count=timeout_c,
            quota_restricted_count=quota_restricted_c,
            agent_error_count=agent_error_c,
            infra_error_count=infra_error_c,
            retried_count=retried_c,
            empty_patch_count=empty_patch_c,
            patch_apply_failed_count=patch_apply_failed_c,
            invalid_quota_attempts=invalid_quota_attempts,
            audit_leak_count=leak_c,
            audit_warn_count=warn_c,
            audit_info_count=info_c,
        )
        configs_metrics.append(cm)

    # 2. Build Paired Task × Config Outcome Matrix
    matrix_rows: list[TaskMatrixRow] = []
    for seq_in_suite, t in enumerate(tasks, start=1):
        cells: dict[str, TaskMatrixCell] = {}
        for cm in configs_metrics:
            cfg_id = cm.config_id
            cfg_label = cm.label
            weekly_b = cm.weekly_bucket

            # Find all passes for (cfg_id, t.instance_id)
            pass_resolved_count = 0
            pass_total_count = 0
            walls_for_task: list[float] = []
            tool_calls_for_task = 0
            task_weekly_burn = 0.0

            cfg_spec = next((c for c in manifest.configs if c.label == cfg_label), None)
            effective_passes = (
                min(manifest.passes, cfg_spec.max_passes or manifest.passes)
                if cfg_spec
                else manifest.passes
            )

            for p_idx in range(effective_passes):
                slot_key = (cfg_id, t.instance_id, p_idx)
                final_att = slot_final_attempts.get(slot_key)
                if final_att:
                    pass_total_count += 1
                    is_res = bool(final_att[17]) if final_att[17] is not None else False
                    if is_res:
                        pass_resolved_count += 1
                    if final_att[10] is not None:
                        walls_for_task.append(final_att[10])
                    if final_att[16] is not None:
                        tool_calls_for_task += final_att[16]

                # Sum quota burn across all attempts (including retries) for this slot
                all_ca = attempts_by_slot.get(slot_key, [])
                for ca in all_ca:
                    if ca[8] == 1 and weekly_b:
                        pre_r = snap_readings.get((ca[0], "pre", weekly_b))
                        post_r = snap_readings.get((ca[0], "post", weekly_b))
                        if pre_r is not None and post_r is not None:
                            task_weekly_burn += max(0.0, pre_r - post_r)

            if pass_total_count == 0:
                cell_status = "SKIP"
            elif pass_resolved_count > 0:
                cell_status = "PASS"
            else:
                cell_status = "FAIL"

            mean_w = sum(walls_for_task) / len(walls_for_task) if walls_for_task else None
            weekly_pp_task = task_weekly_burn * 100.0

            cells[cfg_label] = TaskMatrixCell(
                status=cell_status,
                resolved_passes=pass_resolved_count,
                total_passes=effective_passes,
                mean_wall_seconds=mean_w,
                weekly_pp=weekly_pp_task,
                tool_calls=tool_calls_for_task,
            )

        matrix_rows.append(
            TaskMatrixRow(
                seq_in_suite=seq_in_suite,
                instance_id=t.instance_id,
                repo=t.repo,
                difficulty=t.difficulty.value,
                cells=cells,
            )
        )

    # 3. Paired head-to-head comparisons
    paired_comparisons: list[PairedComparison] = []
    cfg_labels = [cm.label for cm in configs_metrics]
    for i in range(len(cfg_labels)):
        for j in range(i + 1, len(cfg_labels)):
            c_a = cfg_labels[i]
            c_b = cfg_labels[j]
            wins_a = 0
            wins_b = 0
            ties = 0
            total_comp = 0

            for row in matrix_rows:
                cell_a = row.cells.get(c_a)
                cell_b = row.cells.get(c_b)
                if (
                    cell_a
                    and cell_b
                    and cell_a.status in ("PASS", "FAIL")
                    and cell_b.status in ("PASS", "FAIL")
                ):
                    total_comp += 1
                    if cell_a.status == "PASS" and cell_b.status == "FAIL":
                        wins_a += 1
                    elif cell_b.status == "PASS" and cell_a.status == "FAIL":
                        wins_b += 1
                    else:
                        ties += 1

            paired_comparisons.append(
                PairedComparison(
                    config_a=c_a,
                    config_b=c_b,
                    wins_a=wins_a,
                    wins_b=wins_b,
                    ties=ties,
                    total_compared=total_comp,
                )
            )

    # 4. Longitudinal comparison
    prior_c_id = compare_cycle_id
    if prior_c_id is None:
        cur = conn.execute(
            """
            SELECT c.id FROM cycles c
            JOIN experiments e ON c.experiment_id = e.id
            WHERE c.id < ? AND c.status = 'completed' AND e.suite_digest = ?
            ORDER BY c.id DESC LIMIT 1
            """,
            (cycle_id, suite_digest),
        )
        row = cur.fetchone()
        if row:
            prior_c_id = row[0]

    longitudinal_deltas: list[LongitudinalDelta] | None = None
    env_drift: EnvironmentDrift | None = None

    if prior_c_id is not None:
        try:
            prior_data = compute_cycle_metrics(conn, prior_c_id, compare_cycle_id=-1)
            deltas: list[LongitudinalDelta] = []
            for curr_cm in configs_metrics:
                prior_cm = next(
                    (p for p in prior_data.configs if p.label == curr_cm.label),
                    None,
                )
                if prior_cm:
                    d_res = curr_cm.resolve_rate_planned - prior_cm.resolve_rate_planned
                    curr_pp_res = curr_cm.pp_per_resolved_task_weekly
                    prior_pp_res = prior_cm.pp_per_resolved_task_weekly
                    d_pp_res = (
                        (curr_pp_res - prior_pp_res)
                        if (curr_pp_res is not None and prior_pp_res is not None)
                        else None
                    )
                    curr_tot_pp = (
                        curr_cm.consumed_pp.get(curr_cm.weekly_bucket, 0.0)
                        if curr_cm.weekly_bucket
                        else 0.0
                    )
                    prior_tot_pp = (
                        prior_cm.consumed_pp.get(prior_cm.weekly_bucket, 0.0)
                        if prior_cm.weekly_bucket
                        else 0.0
                    )
                    d_tot_pp = curr_tot_pp - prior_tot_pp

                    curr_med_w = curr_cm.median_wall_seconds
                    prior_med_w = prior_cm.median_wall_seconds
                    d_med_w = (
                        (curr_med_w - prior_med_w)
                        if (curr_med_w is not None and prior_med_w is not None)
                        else None
                    )

                    deltas.append(
                        LongitudinalDelta(
                            config_label=curr_cm.label,
                            role=curr_cm.role,
                            prior_resolve_rate_planned=prior_cm.resolve_rate_planned,
                            curr_resolve_rate_planned=curr_cm.resolve_rate_planned,
                            delta_resolve_rate_planned=d_res,
                            prior_weekly_pp_per_resolved=prior_pp_res,
                            curr_weekly_pp_per_resolved=curr_pp_res,
                            delta_weekly_pp_per_resolved=d_pp_res,
                            prior_total_weekly_pp=prior_tot_pp,
                            curr_total_weekly_pp=curr_tot_pp,
                            delta_total_weekly_pp=d_tot_pp,
                            prior_median_wall_seconds=prior_med_w,
                            curr_median_wall_seconds=curr_med_w,
                            delta_median_wall_seconds=d_med_w,
                        )
                    )
            longitudinal_deltas = deltas

            # Environment drift
            prior_env = prior_data.environment
            curr_env = env_dict
            model_map: dict[str, tuple[str, str]] = {}
            for curr_cm in configs_metrics:
                prior_cm = next(
                    (p for p in prior_data.configs if p.label == curr_cm.label),
                    None,
                )
                if prior_cm:
                    model_map[curr_cm.label] = (
                        prior_cm.model_id,
                        curr_cm.model_id,
                    )

            env_drift = EnvironmentDrift(
                agy_version=(
                    prior_env.get("agy_version", "unknown"),
                    curr_env.get("agy_version", "unknown"),
                ),
                model_ids=model_map,
                swebench_version=(
                    prior_env.get("swebench_version", "unknown"),
                    curr_env.get("swebench_version", "unknown"),
                ),
                prompt_hash=(
                    prior_env.get("prompt_hash", "unknown"),
                    curr_env.get("prompt_hash", "unknown"),
                ),
            )
        except Exception:
            longitudinal_deltas = None
            env_drift = None

    return CycleReportData(
        cycle_id=cycle_id,
        experiment_id=exp_id,
        experiment_name=exp_name,
        suite_version=suite_ver,
        suite_digest=suite_digest,
        status=status,
        flags=flags,
        started_at=started_at,
        finished_at=finished_at,
        environment=env_dict,
        manifest=manifest,
        suite=suite,
        configs=configs_metrics,
        matrix_rows=matrix_rows,
        paired_comparisons=paired_comparisons,
        leaked_attempt_ids=leaked_attempt_ids,
        longitudinal_deltas=longitudinal_deltas,
        environment_drift=env_drift,
    )
