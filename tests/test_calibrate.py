"""Offline unit tests for synthetic 3-prompt quota-weight calibration and Tier 1 hardening."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from subbench.aa import _compute_pool_uncertainty, parse_aa_models
from subbench.agents.antigravity import parse_stream_json
from subbench.agents.codex import parse_codex_jsonl
from subbench.calibrate import (
    DEFAULT_CALIBRATION_TARGETS,
    CalibrationProbeSample,
    build_calibration_prompt,
    build_model_calibration_report,
    fit_quota_weights,
    write_calibration_reports,
)
from subbench.cli import app
from subbench.quota import BucketReading, QuotaSnapshot
from subbench.runner import _is_post_snapshot_settled


def test_fit_quota_weights_exact_recovery_and_nonnegativity() -> None:
    # Ground truth: w_in = 4.0, w_cache = 0.2, w_out = 20.0 pp per 1M tokens
    samples = [
        CalibrationProbeSample(
            source="synthetic",
            probe_kind="input_heavy",
            uncached_input_tokens=500_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=1_000,
            thinking_tokens=0,
            primary_burn_pp=2.02,
            weekly_burn_pp=2.02,
            weekly_eq_burn_pp=2.02,
            settled=True,
            wall_seconds=5.0,
        ),
        CalibrationProbeSample(
            source="synthetic",
            probe_kind="output_heavy",
            uncached_input_tokens=5_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=100_000,
            thinking_tokens=0,
            primary_burn_pp=2.02,
            weekly_burn_pp=2.02,
            weekly_eq_burn_pp=2.02,
            settled=True,
            wall_seconds=10.0,
        ),
        CalibrationProbeSample(
            source="synthetic",
            probe_kind="cache_heavy",
            uncached_input_tokens=50_000,
            cache_read_tokens=2_000_000,
            cache_write_tokens=0,
            output_tokens=5_000,
            thinking_tokens=0,
            primary_burn_pp=0.70,
            weekly_burn_pp=0.70,
            weekly_eq_burn_pp=0.70,
            settled=True,
            wall_seconds=15.0,
        ),
    ]
    fit = fit_quota_weights(samples)
    assert fit is not None
    assert abs(fit.w_in_pp_per_1m - 4.0) < 1e-6
    assert abs(fit.w_cache_pp_per_1m - 0.2) < 1e-6
    assert abs(fit.w_out_pp_per_1m - 20.0) < 1e-6
    assert abs((fit.ratio_out_to_in or 0) - 5.0) < 1e-6
    assert abs((fit.ratio_cache_to_in or 0) - 0.05) < 1e-6
    assert fit.rmse_pp < 1e-9

    # Add a 4th noisy sample so df > 0 produces standard errors and R^2
    noisy = CalibrationProbeSample(
        source="subbench_db",
        probe_kind="c9:task1",
        uncached_input_tokens=100_000,
        cache_read_tokens=1_000_000,
        cache_write_tokens=0,
        output_tokens=20_000,
        thinking_tokens=0,
        primary_burn_pp=1.05,
        weekly_burn_pp=1.05,
        weekly_eq_burn_pp=1.05,
        settled=True,
        wall_seconds=20.0,
    )
    fit4 = fit_quota_weights([*samples, noisy])
    assert fit4 is not None
    assert fit4.se_in is not None and fit4.se_cache is not None and fit4.se_out is not None
    assert fit4.r_squared is not None and fit4.r_squared > 0.99


def test_agy_prompts_fit_within_linux_max_arg_strlen() -> None:
    """Antigravity passes -p <prompt> via argv; keep every default agy prompt < 115 KB."""
    for target in DEFAULT_CALIBRATION_TARGETS:
        if target.agent != "antigravity":
            continue
        for kind in ("input_heavy", "output_heavy", "cache_heavy"):
            txt = build_calibration_prompt(kind, target, nonce="testnonce123")
            assert len(txt.encode("utf-8")) < 115_000, f"{target.label} {kind} too large for argv"


def test_calibrate_dry_run_cli_and_report_writer(tmp_path: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(app, ["calibrate", "--dry-run", "--models", "neon-medium,opus-5-5-medium"])
    assert res.exit_code == 0, res.stdout
    assert "neon-medium" in res.stdout
    assert "opus-5-5-medium" in res.stdout

    target = next(t for t in DEFAULT_CALIBRATION_TARGETS if t.label == "neon-medium")
    aa_models = parse_aa_models(
        {
            "data": [
                {
                    "slug": "gemini-4-neon-medium",
                    "name": "Gemini 4 Neon (Medium)",
                    "model_creator": {"name": "Google"},
                    "evaluations": {"artificial_analysis_intelligence_index": 54.9},
                    "pricing": {
                        "price_1m_input_tokens": 3.0,
                        "price_1m_output_tokens": 15.0,
                        "price_1m_cache_hit_tokens": 0.15,
                        "price_1m_cache_write_tokens": 3.0,
                    },
                }
            ]
        }
    )
    samples = [
        CalibrationProbeSample(
            source="synthetic",
            probe_kind="input_heavy",
            uncached_input_tokens=30_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=50,
            thinking_tokens=0,
            primary_burn_pp=0.45,
            weekly_burn_pp=0.13,
            weekly_eq_burn_pp=0.13,
            settled=True,
            wall_seconds=4.0,
        ),
        CalibrationProbeSample(
            source="synthetic",
            probe_kind="output_heavy",
            uncached_input_tokens=2_000,
            cache_read_tokens=0,
            cache_write_tokens=0,
            output_tokens=5_000,
            thinking_tokens=0,
            primary_burn_pp=0.40,
            weekly_burn_pp=0.115,
            weekly_eq_burn_pp=0.115,
            settled=True,
            wall_seconds=9.0,
        ),
        CalibrationProbeSample(
            source="synthetic",
            probe_kind="cache_heavy",
            uncached_input_tokens=20_000,
            cache_read_tokens=200_000,
            cache_write_tokens=0,
            output_tokens=600,
            thinking_tokens=0,
            primary_burn_pp=0.49,
            weekly_burn_pp=0.14,
            weekly_eq_burn_pp=0.14,
            settled=True,
            wall_seconds=18.0,
        ),
    ]
    rep = build_model_calibration_report(target, samples, aa_models=aa_models)
    out = write_calibration_reports([rep], out_dir=tmp_path)
    assert out["json"].exists() and out["md"].exists()
    md = out["md"].read_text(encoding="utf-8")
    assert "neon-medium" in md
    assert "Synthetic (3p)" in md


def test_tier1_cache_write_parsing_settling_and_uncertainty() -> None:
    # Codex cache_write_input_tokens parsing
    codex_sum = parse_codex_jsonl(
        [
            '{"type":"turn.completed","usage":{"input_tokens":10000,"cached_input_tokens":4000,'
            '"cache_write_input_tokens":2000,"output_tokens":500}}'
        ]
    )
    assert codex_sum.cache_write_tokens == 2000

    # Antigravity cache_creation_tokens parsing
    agy_sum = parse_stream_json(
        [
            '{"event":"result","result":{"status":"SUCCESS","usage":{"input_tokens":5000,'
            '"output_tokens":200,"cache_read_tokens":1000,"cache_creation_tokens":1500}}}'
        ]
    )
    assert agy_sum.cache_write_tokens == 1500

    # Post-attempt settling check
    pre = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.50),
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.80),
        },
        taken_at="t0",
        provenance="agy_usage",
        raw_json="{}",
    )
    post_unsettled = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.47),  # 3.0 pp flat reservation
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.80),  # 0 pp weekly
        },
        taken_at="t1",
        provenance="agy_usage",
        raw_json="{}",
    )
    post_settled = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.465),  # 3.5 pp
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.790),  # 1.0 pp (3.50x)
        },
        taken_at="t2",
        provenance="agy_usage",
        raw_json="{}",
    )
    assert not _is_post_snapshot_settled(pre, post_unsettled, "gemini-5h", "gemini-weekly")
    assert _is_post_snapshot_settled(pre, post_settled, "gemini-5h", "gemini-weekly")

    # Quantized integer bucket uncertainty bounds
    se_pct, lo, hi = _compute_pool_uncertainty(
        implied_mo_usd=1.62,
        api_cost_usd=0.0487,
        total_burn_frac=0.03,
        weeks_per_month=1.0,
        is_quantized_pct=True,
        attempt_cost_burn=[],
    )
    assert se_pct is not None and abs(se_pct - 13.608) < 0.1
    assert lo is not None and hi is not None and lo < 1.62 < hi
