"""Empirical quota-weight calibration via a synthetic 3-prompt probe suite.

Estimates internal subscription quota weights ``(w_in, w_cache, w_out)`` in
percentage-points of weekly-equivalent quota per 1M tokens by solving:

    delta_q_pp = w_in * T_in_M + w_cache * T_cache_M + w_out * T_out_M

across three near-orthogonal synthetic probes (``input_heavy``, ``output_heavy``,
``cache_heavy``), optionally combined with historical settled SWE-bench attempts
from ``data/subbench.sqlite``.
"""

from __future__ import annotations

import json
import logging
import math
import random
import sqlite3
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from subbench.aa import FIVE_H_TO_WEEKLY_RATIO, AAModel, match_aa_model
from subbench.agents.antigravity import DEFAULT_SETTINGS, parse_stream_json
from subbench.agents.codex import parse_codex_jsonl
from subbench.provision.images import ensure_probe_image
from subbench.quota import AgyUsageProbe, CodexUsageProbe, QuotaProbe, QuotaSnapshot
from subbench.quota.base import force_remove_container, probe_container_name
from subbench.runner import _is_post_snapshot_settled

logger = logging.getLogger(__name__)

ProbeKind = Literal["input_heavy", "output_heavy", "cache_heavy"]

_VOCAB = (
    "amber",
    "basalt",
    "cedar",
    "delta",
    "ember",
    "flint",
    "granite",
    "harbor",
    "ivory",
    "jasper",
    "kelp",
    "lumen",
    "marble",
    "nebula",
    "ochre",
    "prism",
    "quartz",
    "ripple",
    "slate",
    "timber",
    "umbra",
    "velvet",
    "willow",
    "xenon",
    "yarrow",
    "zenith",
    "anchor",
    "beacon",
    "cinder",
    "drift",
    "fjord",
    "glacier",
)


@dataclass(frozen=True)
class CalibrationTarget:
    """Configuration for calibrating one model's quota weights."""

    label: str
    agent: Literal["antigravity", "codex"]
    model_id: str
    effort: str | None
    primary_bucket: str
    weekly_bucket: str
    input_words: int = 12_000
    output_lines: int = 220
    cache_words: int = 10_000
    cache_turns: int = 8
    input_repeats: int = 1
    output_repeats: int = 1
    cache_repeats: int = 1


DEFAULT_CALIBRATION_TARGETS: list[CalibrationTarget] = [
    CalibrationTarget(
        label="efficient-flash-medium",
        agent="antigravity",
        model_id="gemini-3.8-flash-medium",
        effort=None,
        primary_bucket="gemini-5h",
        weekly_bucket="gemini-weekly",
        input_words=13_000,
        output_lines=260,
        cache_words=11_000,
        cache_turns=8,
    ),
    CalibrationTarget(
        label="hydrogen-medium",
        agent="antigravity",
        model_id="gemini-4-hydrogen-medium",
        effort=None,
        primary_bucket="gemini-5h",
        weekly_bucket="gemini-weekly",
        input_words=13_000,
        output_lines=260,
        cache_words=11_000,
        cache_turns=8,
    ),
    CalibrationTarget(
        label="neon-medium",
        agent="antigravity",
        model_id="gemini-4-neon-medium",
        effort=None,
        primary_bucket="gemini-5h",
        weekly_bucket="gemini-weekly",
        input_words=13_000,
        output_lines=260,
        cache_words=11_000,
        cache_turns=8,
    ),
    CalibrationTarget(
        label="frontier-argon-high",
        agent="antigravity",
        model_id="gemini-4-argon-high",
        effort=None,
        primary_bucket="gemini-argon-wtu-weekly",
        weekly_bucket="gemini-argon-wtu-weekly",
        input_words=13_000,
        output_lines=240,
        cache_words=10_000,
        cache_turns=8,
    ),
    CalibrationTarget(
        label="sonnet-5-5-medium",
        agent="antigravity",
        model_id="claude-sonnet-5-5-medium",
        effort=None,
        primary_bucket="3p-5h",
        weekly_bucket="3p-weekly",
        input_words=9_000,
        output_lines=160,
        cache_words=7_500,
        cache_turns=7,
    ),
    CalibrationTarget(
        label="opus-5-5-medium",
        agent="antigravity",
        model_id="claude-opus-5-5-medium",
        effort=None,
        primary_bucket="3p-5h",
        weekly_bucket="3p-weekly",
        input_words=7_000,
        output_lines=130,
        cache_words=6_000,
        cache_turns=6,
    ),
    CalibrationTarget(
        label="gpt-6-luna-high",
        agent="codex",
        model_id="gpt-6-luna",
        effort="high",
        primary_bucket="codex-30d",
        weekly_bucket="codex-30d",
        input_words=25_000,
        output_lines=450,
        cache_words=18_000,
        cache_turns=8,
        input_repeats=4,
        output_repeats=3,
        cache_repeats=2,
    ),
]


def _generate_corpus(word_count: int, seed: str) -> str:
    """Generate a unique, deterministic synthetic text block to prevent cross-probe cache hits."""
    rng = random.Random(seed)
    lines: list[str] = []
    words_per_line = 12
    n_lines = max(1, (word_count + words_per_line - 1) // words_per_line)
    for i in range(1, n_lines + 1):
        picked = " ".join(rng.choice(_VOCAB) for _ in range(words_per_line))
        lines.append(f"R{i:05d} {seed[:6]} {picked}.")
    return "\n".join(lines)


def build_calibration_prompt(
    kind: ProbeKind,
    target: CalibrationTarget,
    *,
    nonce: str | None = None,
) -> str:
    """Build one of the three orthogonal calibration prompts."""
    tag = nonce or uuid.uuid4().hex[:12]
    if kind == "input_heavy":
        corpus = _generate_corpus(target.input_words, f"in-{tag}")
        return (
            f"CALIBRATION PROBE [input_heavy:{tag}].\n"
            "Do NOT call any tools. Do NOT summarize or analyze the block below. "
            "Think as briefly as possible and reply with ONLY the single word: OK\n\n"
            f"--- BEGIN DATA {tag} ---\n{corpus}\n--- END DATA {tag} ---\n\n"
            "Remember: reply with ONLY the exact word OK."
        )
    if kind == "output_heavy":
        return (
            f"CALIBRATION PROBE [output_heavy:{tag}].\n"
            "Do NOT call any tools. Think minimally and output EVERY numbered line from "
            f"1 to {target.output_lines} verbatim without skipping, abbreviating, or adding "
            "extra commentary.\n"
            "Use this exact template for each line i:\n"
            f"LINE 0001 ({tag[:6]}): alpha bravo charlie delta echo foxtrot golf hotel india "
            "juliet kilo lima.\n"
            f"Output all {target.output_lines} lines from LINE 0001 through "
            f"LINE {target.output_lines:04d}."
        )
    if kind == "cache_heavy":
        corpus = _generate_corpus(target.cache_words, f"cch-{tag}")
        return (
            f"CALIBRATION PROBE [cache_heavy:{tag}].\n"
            "Ignore the reference block below; it exists solely to occupy prompt context.\n"
            f"--- BEGIN CONTEXT {tag} ---\n{corpus}\n--- END CONTEXT {tag} ---\n\n"
            f"INSTRUCTIONS: Run exactly {target.cache_turns} shell commands using run_command "
            "(or your shell tool), STRICTLY ONE command per turn (NEVER batch multiple tool calls "
            "in the same turn). Wait for each tool result before issuing the next command:\n"
            + "\n".join(
                f"- Turn {k}: echo cal-{tag[:6]}-{k}" for k in range(1, target.cache_turns + 1)
            )
            + f"\nAfter turn {target.cache_turns} finishes, reply with ONLY the word: DONE"
        )
    raise ValueError(f"Unknown calibration probe kind: {kind}")


@dataclass(frozen=True)
class CalibrationProbeSample:
    """Observed token usage and quota burn for one calibration probe (or DB attempt)."""

    source: Literal["synthetic", "subbench_db"]
    probe_kind: str
    uncached_input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    thinking_tokens: int
    primary_burn_pp: float
    weekly_burn_pp: float
    weekly_eq_burn_pp: float
    settled: bool
    wall_seconds: float
    pre_primary: float | None = None
    post_primary: float | None = None
    error_text: str | None = None


@dataclass(frozen=True)
class WeightRegressionResult:
    """Non-negative least-squares fit of weekly-equivalent pp burn per 1M tokens."""

    n_samples: int
    w_in_pp_per_1m: float
    w_cache_pp_per_1m: float
    w_out_pp_per_1m: float
    se_in: float | None
    se_cache: float | None
    se_out: float | None
    r_squared: float | None
    rmse_pp: float
    ratio_out_to_in: float | None
    ratio_cache_to_in: float | None
    condition_number: float | None


def _solve_linear(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve k x k linear system A x = b via Gaussian elimination with partial pivoting."""
    k = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(k):
        piv = max(range(col, k), key=lambda r: abs(m[r][col]))
        if abs(m[piv][col]) < 1e-18:
            return None
        m[col], m[piv] = m[piv], m[col]
        div = m[col][col]
        for j in range(col, k + 1):
            m[col][j] /= div
        for r in range(k):
            if r != col:
                fac = m[r][col]
                for j in range(col, k + 1):
                    m[r][j] -= fac * m[col][j]
    return [m[i][k] for i in range(k)]


def _invert_matrix(a: list[list[float]]) -> list[list[float]] | None:
    k = len(a)
    inv_cols: list[list[float]] = []
    for i in range(k):
        e = [1.0 if r == i else 0.0 for r in range(k)]
        col = _solve_linear(a, e)
        if col is None:
            return None
        inv_cols.append(col)
    return [[inv_cols[c][r] for c in range(k)] for r in range(k)]


def fit_quota_weights(
    samples: list[CalibrationProbeSample],
) -> WeightRegressionResult | None:
    """Fit ``delta_q_pp = w_in * T_in_M + w_cache * T_cache_M + w_out * T_out_M`` (w >= 0).

    Uses exact 3-variable active-set NNLS (exhaustive over the 7 non-empty subsets
    of ``{0, 1, 2}``), returning exact OLS standard errors when ``n > 3`` and all
    three weights are active.
    """
    valid = [
        s
        for s in samples
        if s.weekly_eq_burn_pp > 0
        and (s.uncached_input_tokens + s.cache_read_tokens + s.output_tokens) > 0
    ]
    n = len(valid)
    if n == 0:
        return None

    x_rows = [
        [
            s.uncached_input_tokens / 1e6,
            s.cache_read_tokens / 1e6,
            s.output_tokens / 1e6,
        ]
        for s in valid
    ]
    y_vec = [s.weekly_eq_burn_pp for s in valid]

    best_w = [0.0, 0.0, 0.0]
    best_rss = sum(y * y for y in y_vec)
    best_active: tuple[int, ...] = ()

    subsets = [
        (0,),
        (1,),
        (2,),
        (0, 1),
        (0, 2),
        (1, 2),
        (0, 1, 2),
    ]
    for active in subsets:
        k = len(active)
        if n < k:
            continue
        xtx = [
            [sum(row[active[r]] * row[active[c]] for row in x_rows) for c in range(k)]
            for r in range(k)
        ]
        xty = [sum(x_rows[i][active[r]] * y_vec[i] for i in range(n)) for r in range(k)]
        sol = _solve_linear(xtx, xty)
        if sol is None or any(val < -1e-12 for val in sol):
            continue
        w_full = [0.0, 0.0, 0.0]
        for idx, col in enumerate(active):
            w_full[col] = max(0.0, sol[idx])
        rss = sum(
            (y_vec[i] - sum(x_rows[i][j] * w_full[j] for j in range(3))) ** 2 for i in range(n)
        )
        if rss < best_rss - 1e-15:
            best_rss = rss
            best_w = w_full
            best_active = active

    rmse = math.sqrt(best_rss / n)
    y_mean = sum(y_vec) / n
    tss = sum((y - y_mean) ** 2 for y in y_vec)
    r_sq = (1.0 - best_rss / tss) if (tss > 1e-15 and n >= 2) else None

    # Condition number on column-normalized X (for diagnostics)
    cond: float | None = None
    try:
        import numpy as np

        arr = np.array(x_rows, dtype=float)
        norms = np.linalg.norm(arr, axis=0)
        if np.all(norms > 0):
            cond = float(np.linalg.cond(arr / norms))
    except Exception:
        cond = None

    se_vals: list[float | None] = [None, None, None]
    if best_active and n > len(best_active):
        k = len(best_active)
        xtx = [
            [sum(row[best_active[r]] * row[best_active[c]] for row in x_rows) for c in range(k)]
            for r in range(k)
        ]
        inv_xtx = _invert_matrix(xtx)
        if inv_xtx is not None:
            s2 = best_rss / (n - k)
            for idx, col in enumerate(best_active):
                se_vals[col] = math.sqrt(max(0.0, s2 * inv_xtx[idx][idx]))

    w_in, w_cch, w_out = best_w
    ratio_out = (w_out / w_in) if w_in > 1e-12 else None
    ratio_cch = (w_cch / w_in) if w_in > 1e-12 else None

    return WeightRegressionResult(
        n_samples=n,
        w_in_pp_per_1m=w_in,
        w_cache_pp_per_1m=w_cch,
        w_out_pp_per_1m=w_out,
        se_in=se_vals[0],
        se_cache=se_vals[1],
        se_out=se_vals[2],
        r_squared=r_sq,
        rmse_pp=rmse,
        ratio_out_to_in=ratio_out,
        ratio_cache_to_in=ratio_cch,
        condition_number=cond,
    )


def load_db_samples_for_target(
    conn: sqlite3.Connection,
    target: CalibrationTarget,
    *,
    suite_version: str | None = "verified-core-6",
) -> list[CalibrationProbeSample]:
    """Load recredit-adjusted settled Subbench attempts from SQLite for joint fitting."""
    where_suite = "AND e.suite_version = ?" if suite_version else ""
    params: list[Any] = [target.model_id]
    if target.effort:
        where_eff = "AND cf.effort = ?"
        params.append(target.effort)
    else:
        where_eff = "AND (cf.effort IS NULL OR cf.effort = '')"
    if suite_version:
        params.append(suite_version)

    cfg_rows = conn.execute(
        f"""
        SELECT c.id, cf.id
        FROM cycles c
        JOIN experiments e ON c.experiment_id = e.id
        JOIN configs cf ON cf.experiment_id = e.id
        WHERE c.status = 'completed' AND cf.model_id = ? {where_eff} {where_suite}
        ORDER BY c.id ASC
        """,
        tuple(params),
    ).fetchall()

    attempt_cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    cw_expr = "COALESCE(a.cache_write_tokens, 0)" if "cache_write_tokens" in attempt_cols else "0"

    ratio_5h = FIVE_H_TO_WEEKLY_RATIO.get(target.primary_bucket)
    measure_bucket = target.primary_bucket if ratio_5h else target.weekly_bucket
    samples: list[CalibrationProbeSample] = []

    for cycle_id, config_id in cfg_rows:
        rows = conn.execute(
            f"""
            SELECT a.id, a.instance_id, a.valid,
                   COALESCE(a.input_tokens, 0), COALESCE(a.output_tokens, 0),
                   COALESCE(a.cache_read_tokens, 0), {cw_expr},
                   COALESCE(a.thinking_tokens, 0), COALESCE(a.wall_seconds, 0.0),
                   q_pre.remaining_fraction, q_post.remaining_fraction,
                   q_pre.reset_time, q_post.reset_time,
                   qw_pre.remaining_fraction, qw_post.remaining_fraction
            FROM attempts a
            LEFT JOIN quota_snapshots q_pre
                ON q_pre.attempt_id = a.id AND q_pre.phase = 'pre' AND q_pre.bucket_id = ?
            LEFT JOIN quota_snapshots q_post
                ON q_post.attempt_id = a.id AND q_post.phase = 'post' AND q_post.bucket_id = ?
            LEFT JOIN quota_snapshots qw_pre
                ON qw_pre.attempt_id = a.id AND qw_pre.phase = 'pre' AND qw_pre.bucket_id = ?
            LEFT JOIN quota_snapshots qw_post
                ON qw_post.attempt_id = a.id AND qw_post.phase = 'post' AND qw_post.bucket_id = ?
            WHERE a.cycle_id = ? AND a.config_id = ?
            ORDER BY a.id ASC
            """,
            (
                measure_bucket,
                measure_bucket,
                target.weekly_bucket,
                target.weekly_bucket,
                cycle_id,
                config_id,
            ),
        ).fetchall()

        for i, r in enumerate(rows):
            aid, inst_id, valid, r_in, r_out, r_cch, r_cw, r_thk, wall_s = r[:9]
            pre_p, post_p, pre_rst, post_rst, pre_w, post_w = r[9:]
            if valid != 1 or pre_p is None or post_p is None:
                continue
            d_p = max(0.0, float(pre_p) - float(post_p))
            if i + 1 < len(rows):
                nxt = rows[i + 1]
                if (
                    nxt[2] == 1
                    and nxt[9] is not None
                    and not (post_rst and nxt[11] and post_rst != nxt[11])
                ):
                    rec = float(nxt[9]) - float(post_p)
                    if 0.0 < rec <= d_p + 1e-9 and float(nxt[9]) <= float(pre_p) + 1e-9:
                        d_p = max(0.0, d_p - rec)

            d_w = (
                max(0.0, float(pre_w) - float(post_w))
                if (pre_w is not None and post_w is not None)
                else d_p
            )
            wk_eq = (d_p / ratio_5h) if ratio_5h else d_p
            if ratio_5h and d_p > 1e-6:
                settled = d_w > 1e-6 and abs((d_p / d_w) - ratio_5h) / ratio_5h <= 0.12
            else:
                settled = True
            unc_in = (
                max(0, int(r_in) - int(r_cch) - int(r_cw)) if target.agent == "codex" else int(r_in)
            )
            samples.append(
                CalibrationProbeSample(
                    source="subbench_db",
                    probe_kind=f"c{cycle_id}:{inst_id}",
                    uncached_input_tokens=unc_in,
                    cache_read_tokens=int(r_cch),
                    cache_write_tokens=int(r_cw),
                    output_tokens=int(r_out),
                    thinking_tokens=int(r_thk),
                    primary_burn_pp=d_p * 100.0,
                    weekly_burn_pp=d_w * 100.0,
                    weekly_eq_burn_pp=wk_eq * 100.0,
                    settled=settled,
                    wall_seconds=float(wall_s),
                    pre_primary=float(pre_p),
                    post_primary=float(post_p),
                )
            )
    return samples


def _run_probe_once_in_docker(
    target: CalibrationTarget,
    prompt_text: str,
    *,
    allow_shell: bool,
    host_agy_path: str = "/home/john/.local/bin/agy",
    host_codex_path: str = "/usr/lib/chatgpt/resources/codex",
    image_tag: str = "subbench-agy:probe",
    timeout_seconds: int = 300,
) -> tuple[int, int, int, int, int, int, float, str | None]:
    """Run one synthetic prompt inside ``subbench-agy:probe`` and return token counts."""
    ensure_probe_image(image_tag)
    cname = probe_container_name(f"subbench-cal-{target.label[:12]}")
    t0 = time.monotonic()

    with tempfile.TemporaryDirectory(prefix="subbench_cal_") as tmpdir:
        ws = Path(tmpdir)
        prompt_file = ws / "prompt.txt"
        prompt_file.write_text(prompt_text, encoding="utf-8")

        if target.agent == "antigravity":
            settings = json.loads(json.dumps(DEFAULT_SETTINGS))
            if not allow_shell:
                settings["permissions"]["allow"] = []
                settings["permissions"]["deny"] = list(settings["permissions"]["deny"]) + [
                    "command(*)",
                    "run_command",
                    "run_command(*)",
                    "write_file(*)",
                    "edit_file(*)",
                    "read_file(*)",
                ]
            else:
                settings["permissions"]["allow"] = ["command(*)"]

            inner_parts = [
                "/usr/local/bin/entrypoint-agy.sh",
                "/usr/local/bin/agy",
                "-p",
                prompt_text,
                "--model",
                target.model_id,
                "--output-format",
                "stream-json",
                "--disable-slash-commands",
                "--log-file",
                "/tmp/agy-cli.log",
                "--settings",
                json.dumps(settings),
            ]
            if target.effort:
                inner_parts.extend(["--effort", target.effort])

            cmd = [
                "docker",
                "run",
                "--rm",
                "--name",
                cname,
                "-v",
                "subbench-agy-auth:/agy-state",
                "-v",
                f"{host_agy_path}:/usr/local/bin/agy:ro",
                "-v",
                f"{ws}:/workspace",
                "-w",
                "/workspace",
                image_tag,
                *inner_parts,
            ]
            stdin_data = None
        else:
            codex_cmd = [
                "/usr/local/bin/codex",
                "exec",
                "-m",
                target.model_id,
                "-c",
                'web_search="disabled"',
                "--disable",
                "plugins",
                "--disable",
                "apps",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--ignore-user-config",
                "--ignore-rules",
                "--ephemeral",
                "--json",
                "-C",
                "/workspace",
            ]
            if target.effort:
                codex_cmd.extend(["-c", f'model_reasoning_effort="{target.effort}"'])
            codex_cmd.append("-")
            cmd = [
                "docker",
                "run",
                "--rm",
                "-i",
                "--name",
                cname,
                "-e",
                "CODEX_HOME=/codex-state",
                "-v",
                "subbench-codex-auth:/codex-state",
                "-v",
                f"{host_codex_path}:/usr/local/bin/codex:ro",
                "-v",
                f"{ws}:/workspace",
                "-w",
                "/workspace",
                image_tag,
                *codex_cmd,
            ]
            stdin_data = prompt_text + "\n"

        try:
            res = subprocess.run(
                cmd,
                input=stdin_data,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            force_remove_container(cname)
            return 0, 0, 0, 0, 0, -9, time.monotonic() - t0, f"Timeout after {timeout_seconds}s"

    wall_s = time.monotonic() - t0
    lines = res.stdout.splitlines()
    if target.agent == "antigravity":
        summary = parse_stream_json(lines)
        raw_in = int(summary.input_tokens or 0)
        raw_cch = int(summary.cache_read_tokens or 0)
        raw_cw = int(summary.cache_write_tokens or 0)
        raw_out = int(summary.output_tokens or 0)
        raw_thk = int(summary.thinking_tokens or 0)
        unc_in = raw_in
    else:
        summary = parse_codex_jsonl(lines)
        raw_in = int(summary.input_tokens or 0)
        raw_cch = int(summary.cache_read_tokens or 0)
        raw_cw = int(summary.cache_write_tokens or 0)
        raw_out = int(summary.output_tokens or 0)
        raw_thk = int(summary.thinking_tokens or 0)
        unc_in = max(0, raw_in - raw_cch - raw_cw)

    err = summary.error_text
    if res.returncode != 0 and not err:
        err = (res.stderr or res.stdout)[-400:].strip() or f"exit_code={res.returncode}"
    return unc_in, raw_cch, raw_cw, raw_out, raw_thk, res.returncode, wall_s, err


def _take_settled_post_snapshot(
    probe: QuotaProbe,
    snap_pre: QuotaSnapshot,
    target: CalibrationTarget,
    *,
    settle_max_seconds: int = 25,
    poll_seconds: int = 5,
) -> tuple[QuotaSnapshot, bool]:
    snap_post = probe.snapshot()
    settled = _is_post_snapshot_settled(
        snap_pre, snap_post, target.primary_bucket, target.weekly_bucket
    )
    if settled or settle_max_seconds <= 0 or snap_post.provenance == "manual":
        return snap_post, settled

    waited = 0
    while waited < settle_max_seconds:
        step = min(poll_seconds, settle_max_seconds - waited)
        time.sleep(step)
        waited += step
        snap_post = probe.snapshot()
        settled = _is_post_snapshot_settled(
            snap_pre, snap_post, target.primary_bucket, target.weekly_bucket
        )
        if settled:
            break
    return snap_post, settled


def run_calibration_for_target(
    target: CalibrationTarget,
    *,
    quota_probe: QuotaProbe | None = None,
    settle_max_seconds: int = 25,
    timeout_seconds: int = 300,
) -> list[CalibrationProbeSample]:
    """Execute the 3-prompt synthetic calibration suite for one target model."""
    if quota_probe is None:
        quota_probe = AgyUsageProbe() if target.agent == "antigravity" else CodexUsageProbe()

    ratio_5h = FIVE_H_TO_WEEKLY_RATIO.get(target.primary_bucket)
    probe_specs: list[tuple[ProbeKind, int, bool]] = [
        ("input_heavy", target.input_repeats, False),
        ("output_heavy", target.output_repeats, False),
        ("cache_heavy", target.cache_repeats, True),
    ]

    raw_samples: list[dict[str, Any]] = []
    for kind, repeats, allow_shell in probe_specs:
        snap_pre = quota_probe.snapshot()
        tot_unc = tot_cch = tot_cw = tot_out = tot_thk = 0
        tot_wall = 0.0
        last_err: str | None = None

        for _ in range(max(1, repeats)):
            prompt_text = build_calibration_prompt(kind, target)
            u_in, cch, cw, out, thk, _rc, wall_s, err = _run_probe_once_in_docker(
                target,
                prompt_text,
                allow_shell=allow_shell,
                timeout_seconds=timeout_seconds,
            )
            tot_unc += u_in
            tot_cch += cch
            tot_cw += cw
            tot_out += out
            tot_thk += thk
            tot_wall += wall_s
            if err:
                last_err = err

        snap_post, settled = _take_settled_post_snapshot(
            quota_probe, snap_pre, target, settle_max_seconds=settle_max_seconds
        )
        pre_p = snap_pre.buckets.get(target.primary_bucket)
        post_p = snap_post.buckets.get(target.primary_bucket)
        pre_w = snap_pre.buckets.get(target.weekly_bucket)
        post_w = snap_post.buckets.get(target.weekly_bucket)

        d_p = (
            max(0.0, pre_p.remaining_fraction - post_p.remaining_fraction)
            if (pre_p and post_p)
            else 0.0
        )
        d_w = (
            max(0.0, pre_w.remaining_fraction - post_w.remaining_fraction)
            if (pre_w and post_w)
            else d_p
        )

        raw_samples.append(
            {
                "kind": kind,
                "unc_in": tot_unc,
                "cch": tot_cch,
                "cw": tot_cw,
                "out": tot_out,
                "thk": tot_thk,
                "d_p": d_p,
                "d_w": d_w,
                "pre_p": pre_p.remaining_fraction if pre_p else None,
                "post_p": post_p.remaining_fraction if post_p else None,
                "pre_rst": pre_p.reset_time if pre_p else None,
                "post_rst": post_p.reset_time if post_p else None,
                "settled": settled,
                "wall_s": tot_wall,
                "err": last_err,
            }
        )

    # Net inter-probe recredits (if probe i+1 pre > probe i post within same reset window)
    for i in range(len(raw_samples) - 1):
        cur = raw_samples[i]
        nxt = raw_samples[i + 1]
        if (
            cur["post_p"] is not None
            and nxt["pre_p"] is not None
            and cur["pre_p"] is not None
            and not (cur["post_rst"] and nxt["pre_rst"] and cur["post_rst"] != nxt["pre_rst"])
        ):
            rec = nxt["pre_p"] - cur["post_p"]
            if 0.0 < rec <= cur["d_p"] + 1e-9 and nxt["pre_p"] <= cur["pre_p"] + 1e-9:
                cur["d_p"] = max(0.0, cur["d_p"] - rec)

    results: list[CalibrationProbeSample] = []
    for item in raw_samples:
        wk_eq = (item["d_p"] / ratio_5h) if ratio_5h else item["d_p"]
        results.append(
            CalibrationProbeSample(
                source="synthetic",
                probe_kind=item["kind"],
                uncached_input_tokens=item["unc_in"],
                cache_read_tokens=item["cch"],
                cache_write_tokens=item["cw"],
                output_tokens=item["out"],
                thinking_tokens=item["thk"],
                primary_burn_pp=item["d_p"] * 100.0,
                weekly_burn_pp=item["d_w"] * 100.0,
                weekly_eq_burn_pp=wk_eq * 100.0,
                settled=item["settled"],
                wall_seconds=item["wall_s"],
                pre_primary=item["pre_p"],
                post_primary=item["post_p"],
                error_text=item["err"],
            )
        )
    return results


@dataclass(frozen=True)
class ModelCalibrationReport:
    """Complete empirical quota-weight report for one target model."""

    target: CalibrationTarget
    aa_slug: str | None
    aa_price_1m_in: float | None
    aa_price_1m_cache: float | None
    aa_price_1m_out: float | None
    synthetic_samples: list[CalibrationProbeSample]
    db_samples: list[CalibrationProbeSample]
    synthetic_fit: WeightRegressionResult | None
    joint_fit: WeightRegressionResult | None
    joint_settled_fit: WeightRegressionResult | None


def build_model_calibration_report(
    target: CalibrationTarget,
    synthetic_samples: list[CalibrationProbeSample],
    *,
    conn: sqlite3.Connection | None = None,
    aa_models: list[AAModel] | None = None,
) -> ModelCalibrationReport:
    """Combine synthetic calibration probes with DB history and AA list prices."""
    db_samples = load_db_samples_for_target(conn, target) if conn is not None else []
    syn_fit = fit_quota_weights(synthetic_samples)
    joint_fit = fit_quota_weights([*synthetic_samples, *db_samples]) if db_samples else syn_fit
    settled_pool = [s for s in [*synthetic_samples, *db_samples] if s.settled]
    joint_settled_fit = fit_quota_weights(settled_pool) if len(settled_pool) >= 3 else joint_fit

    aa_m = match_aa_model(target.model_id, target.effort, aa_models or [])
    p_in = aa_m.price_1m_input if aa_m else None
    p_out = aa_m.price_1m_output if aa_m else None
    p_cch = (
        aa_m.price_1m_cache_read
        if (aa_m and aa_m.price_1m_cache_read is not None)
        else (p_in * 0.10 if p_in is not None else None)
    )

    return ModelCalibrationReport(
        target=target,
        aa_slug=aa_m.slug if aa_m else None,
        aa_price_1m_in=p_in,
        aa_price_1m_cache=p_cch,
        aa_price_1m_out=p_out,
        synthetic_samples=synthetic_samples,
        db_samples=db_samples,
        synthetic_fit=syn_fit,
        joint_fit=joint_fit,
        joint_settled_fit=joint_settled_fit,
    )


def _format_pool_usd(price: float | None, w_pp: float, wpm: float) -> str:
    if price is None or w_pp <= 1e-9:
        return "N/A"
    val = (price / (w_pp / 100.0)) * wpm
    return f"${val:.2f}" if val < 10.0 else f"${val:.0f}"


def write_calibration_reports(
    reports: list[ModelCalibrationReport],
    *,
    out_dir: Path = Path("artifacts/reports"),
) -> dict[str, Path]:
    """Write JSON and Markdown reports for empirical quota-weight calibration."""
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "quota_weight_calibration.json"
    md_path = out_dir / "quota_weight_calibration.md"

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "models": [asdict(r) for r in reports],
    }
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Empirical Subscription Quota-Weight Calibration",
        "",
        f"**Generated:** `{payload['generated_at']}`  ",
        "**Model:** `weekly_eq_burn_pp = w_in × T_in_M + w_cache × T_cache_M + w_out × T_out_M` "
        "(NNLS, weekly-equivalent percentage points per 1M tokens; `codex-30d` uses 30d pp).",
        "",
        "## 1. Regressed Weights vs. AA List-Price Ratios",
        "",
        "| Model | Pool | Fit | n | `w_in` (pp/1M) | `w_cache` (pp/1M) | `w_out` (pp/1M) | "
        "`w_out/w_in` (AA) | `w_cache/w_in` (AA) | $R^2$ | Implied $/mo (In / Cache / Out) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]

    for rep in reports:
        wpm = 1.0 if rep.target.weekly_bucket.endswith("-30d") else 4.33
        aa_out_r = (
            f"{rep.aa_price_1m_out / rep.aa_price_1m_in:.1f}×"
            if (rep.aa_price_1m_in and rep.aa_price_1m_out)
            else "N/A"
        )
        aa_cch_r = (
            f"{rep.aa_price_1m_cache / rep.aa_price_1m_in:.3f}×"
            if (rep.aa_price_1m_in and rep.aa_price_1m_cache is not None)
            else "N/A"
        )

        for fit_lbl, fit in (
            ("Synthetic (3p)", rep.synthetic_fit),
            ("Joint (syn+DB)", rep.joint_settled_fit),
        ):
            if fit is None:
                continue
            r_out = f"{fit.ratio_out_to_in:.2f}×" if fit.ratio_out_to_in is not None else "N/A"
            r_cch = f"{fit.ratio_cache_to_in:.3f}×" if fit.ratio_cache_to_in is not None else "N/A"
            r2_s = f"{fit.r_squared:.3f}" if fit.r_squared is not None else "N/A"

            pools_s = (
                f"{_format_pool_usd(rep.aa_price_1m_in, fit.w_in_pp_per_1m, wpm)} / "
                f"{_format_pool_usd(rep.aa_price_1m_cache, fit.w_cache_pp_per_1m, wpm)} / "
                f"{_format_pool_usd(rep.aa_price_1m_out, fit.w_out_pp_per_1m, wpm)}"
            )
            lines.append(
                f"| `{rep.target.label}` | `{rep.target.weekly_bucket}` | {fit_lbl} | "
                f"{fit.n_samples} | {fit.w_in_pp_per_1m:.3f} | {fit.w_cache_pp_per_1m:.4f} | "
                f"{fit.w_out_pp_per_1m:.3f} | {r_out} ({aa_out_r}) | {r_cch} ({aa_cch_r}) | "
                f"{r2_s} | {pools_s} |"
            )

    lines.extend(
        [
            "",
            "## 2. Raw Synthetic Probe Observations",
            "",
            "| Model | Probe | Uncached In | Cache Read | Output | Primary Burn (pp) | "
            "Weekly-Eq Burn (pp) | Settled | Wall (s) |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
    )
    for rep in reports:
        for s in rep.synthetic_samples:
            lines.append(
                f"| `{rep.target.label}` | `{s.probe_kind}` | {s.uncached_input_tokens:,} | "
                f"{s.cache_read_tokens:,} | {s.output_tokens:,} | {s.primary_burn_pp:.3f} | "
                f"{s.weekly_eq_burn_pp:.3f} | {'yes' if s.settled else 'no'} | "
                f"{s.wall_seconds:.1f} |"
            )

    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"json": json_path, "md": md_path}
