"""Offline tests for Artificial Analysis integration, key resolution, and projections."""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from subbench.aa import (
    EffortProjectionPoint,
    _compute_net_contiguous_burn,
    compute_aa_projections,
    compute_token_weighted_projections,
    fetch_aa_models,
    load_aa_cache,
    match_aa_model,
    parse_aa_model_page_token_breakdowns,
    parse_aa_models,
    project_effort_variants,
    render_aa_intelligence_vs_tasks_svg,
    resolve_aa_api_key,
    store_aa_api_key_file,
    write_aa_projection_reports,
    write_token_weighted_reports,
)
from subbench.cli import app
from subbench.config import (
    ConfigRole,
    ConfigSpec,
    Difficulty,
    ExperimentManifest,
    SuiteManifest,
    TaskSpec,
)
from subbench.quota import BucketReading, QuotaSnapshot, save_quota_snapshot
from subbench.runner import record_configs, record_experiment
from subbench.store import connect, init_db


def test_resolve_aa_api_key_precedence(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("AA_API_KEY", raising=False)
    monkeypatch.setattr("subbench.aa.shutil.which", lambda _: None)

    key_file = tmp_path / "aa_api_key"
    store_aa_api_key_file("file-secret", key_file=key_file)
    assert (key_file.stat().st_mode & 0o777) == 0o600
    assert resolve_aa_api_key(repo_root=tmp_path, key_file=key_file) == "file-secret"

    (tmp_path / ".env.local").write_text('AA_API_KEY="dotenv-secret"\n', encoding="utf-8")
    assert resolve_aa_api_key(repo_root=tmp_path, key_file=key_file) == "dotenv-secret"

    monkeypatch.setenv("AA_API_KEY", "env-secret")
    assert resolve_aa_api_key(repo_root=tmp_path, key_file=key_file) == "env-secret"


def test_parse_and_match_aa_models() -> None:
    payload = {
        "data": [
            {
                "slug": "gemini-4-neon-medium",
                "name": "Gemini 4 Neon (medium)",
                "model_creator": {"name": "Google"},
                "evaluations": {"artificial_analysis_intelligence_index": 58.2},
                "pricing": {"price_1m_input_tokens": 1.25, "price_1m_output_tokens": 10.0},
            },
            {
                "slug": "gpt-6-luna-xhigh",
                "name": "GPT-6 Luna (xhigh)",
                "model_creator": {"name": "OpenAI"},
                "evaluations": {"artificial_analysis_intelligence_index": 42.0},
                "pricing": {"price_1m_input_tokens": 0.10, "price_1m_output_tokens": 0.50},
            },
            {
                "slug": "claude-sonnet-5-5",
                "name": "Claude Sonnet 5.5 (medium)",
                "model_creator": {"name": "Anthropic"},
                "evaluations": {"artificial_analysis_intelligence_index": 56.0},
                "pricing": {"price_1m_input_tokens": 3.0, "price_1m_output_tokens": 15.0},
            },
        ]
    }
    models = parse_aa_models(payload)
    assert len(models) == 3

    m_neon = match_aa_model("gemini-4-neon-medium", None, models)
    assert m_neon is not None and m_neon.slug == "gemini-4-neon-medium"

    m_luna = match_aa_model("gpt-6-luna", "xhigh", models)
    assert m_luna is not None and m_luna.slug == "gpt-6-luna-xhigh"

    m_sonnet = match_aa_model("claude-sonnet-5-5-medium", None, models)
    assert m_sonnet is not None and m_sonnet.slug == "claude-sonnet-5-5"

    # Prefer effort-qualified slug over bare base_id (Max)
    models_with_max = [
        *models,
        *parse_aa_models(
            {
                "data": [
                    {
                        "slug": "gpt-6-luna",
                        "name": "GPT-6 Luna (Max)",
                        "model_creator": {"name": "OpenAI"},
                        "evaluations": {"artificial_analysis_intelligence_index": 27.9},
                        "pricing": {"price_1m_input_tokens": 0.1, "price_1m_output_tokens": 0.5},
                    }
                ]
            }
        ),
    ]
    m_luna_xhigh = match_aa_model("gpt-6-luna", "xhigh", models_with_max)
    assert m_luna_xhigh is not None and m_luna_xhigh.slug == "gpt-6-luna-xhigh"


def test_fetch_aa_models_caches_without_network(monkeypatch, tmp_path: Path) -> None:
    sample = {
        "data": [
            {
                "slug": "gpt-6-luna-high",
                "name": "GPT-6 Luna (high)",
                "model_creator": {"name": "OpenAI"},
                "evaluations": {"artificial_analysis_intelligence_index": 39.5},
                "pricing": {"price_1m_input_tokens": 0.10, "price_1m_output_tokens": 0.50},
            }
        ]
    }

    class FakeResp:
        headers = {"X-RateLimit-Remaining": "99", "X-AA-Tier": "free"}

        def read(self) -> bytes:
            return json.dumps(sample).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    seen_headers: dict[str, str] = {}

    def fake_urlopen(req, timeout=30):
        seen_headers.update(dict(req.header_items()))
        return FakeResp()

    monkeypatch.setattr("subbench.aa.urllib.request.urlopen", fake_urlopen)
    res = fetch_aa_models(api_key="test-key-123", cache_dir=tmp_path)
    assert seen_headers.get("X-api-key") == "test-key-123"
    assert res.model_count == 1
    assert res.cache_path.exists()
    assert res.archive_path.exists()
    assert res.rate_limit_headers["x-aa-tier"] == "free"

    fetched_at, loaded = load_aa_cache(res.cache_path)
    assert fetched_at == res.fetched_at
    assert loaded[0].slug == "gpt-6-luna-high"


def test_compute_aa_projections_and_cli(tmp_path: Path) -> None:
    db_path = tmp_path / "subbench.sqlite"
    conn = connect(db_path)
    init_db(conn)

    suite = SuiteManifest(
        version="verified-core-6",
        dataset_revision="0123456789abcdef0123456789abcdef01234567",
        seed=1,
        tasks=[
            TaskSpec(
                instance_id="pytest-dev__pytest-5809",
                repo="pytest-dev/pytest",
                base_commit="abc",
                difficulty=Difficulty.EASY,
                order=0,
            )
        ],
    )
    cfg = ConfigSpec(
        label="gpt-6-luna-high",
        role=ConfigRole.DEFAULT,
        model_id="gpt-6-luna",
        effort="high",
        buckets=["codex-30d"],
    )
    manifest = ExperimentManifest(
        name="codex-test",
        agent="codex",
        suite="s.json",
        codex_path="/usr/lib/chatgpt/resources/codex",
        configs=[cfg],
        quota={
            "primary_bucket_by_config": {"gpt-6-luna-high": "codex-30d"},
            "weekly_bucket_by_config": {"gpt-6-luna-high": "codex-30d"},
        },
    )
    exp_id = record_experiment(conn, manifest, suite)
    cfg_ids = record_configs(conn, exp_id, manifest)
    c_id = conn.execute(
        "INSERT INTO cycles (experiment_id, started_at, status) "
        "VALUES (?, '2026-11-18Z', 'completed')",
        (exp_id,),
    ).lastrowid
    a_id = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid,
            input_tokens, output_tokens, cache_read_tokens
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, 'GRADED', 1, 1500000, 20000, 1000000)
        """,
        (c_id, cfg_ids["gpt-6-luna-high"]),
    ).lastrowid
    conn.execute(
        "INSERT INTO grades (attempt_id, graded_at, resolved, patch_applied) "
        "VALUES (?, 'now', 1, 1)",
        (a_id,),
    )
    save_quota_snapshot(
        conn,
        cycle_id=c_id,
        attempt_id=a_id,
        phase="pre",
        snap=QuotaSnapshot(
            buckets={"codex-30d": BucketReading("codex-30d", "30d", 0.78)},
            taken_at="now",
            provenance="codex_usage",
            raw_json="{}",
        ),
    )
    save_quota_snapshot(
        conn,
        cycle_id=c_id,
        attempt_id=a_id,
        phase="post",
        snap=QuotaSnapshot(
            buckets={"codex-30d": BucketReading("codex-30d", "30d", 0.77)},
            taken_at="now",
            provenance="codex_usage",
            raw_json="{}",
        ),
    )
    conn.commit()

    cache_path = tmp_path / "aa_models_latest.json"
    cache_path.write_text(
        json.dumps(
            {
                "fetched_at": "2026-11-19T00:00:00Z",
                "data": [
                    {
                        "slug": "gpt-6-luna-high",
                        "name": "GPT-6 Luna (high)",
                        "model_creator": {"name": "OpenAI"},
                        "evaluations": {"artificial_analysis_intelligence_index": 39.5},
                        "artificial_analysis_intelligence_index_cost": {
                            "cost_per_task": {"total_cost": 0.08}
                        },
                        "pricing": {
                            "price_1m_input_tokens": 0.10,
                            "price_1m_output_tokens": 0.50,
                            "price_1m_cache_hit_tokens": 0.01,
                        },
                    },
                    {
                        "slug": "gpt-6-luna-medium",
                        "name": "GPT-6 Luna (medium)",
                        "model_creator": {"name": "OpenAI"},
                        "evaluations": {"artificial_analysis_intelligence_index": 30.0},
                        "artificial_analysis_intelligence_index_cost": {
                            "cost_per_task": {"total_cost": 0.04}
                        },
                        "pricing": {
                            "price_1m_input_tokens": 0.10,
                            "price_1m_output_tokens": 0.50,
                            "price_1m_cache_hit_tokens": 0.01,
                        },
                    },
                ],
            }
        )
    )

    _, models = load_aa_cache(cache_path)
    summaries = compute_aa_projections(conn, models)
    assert len(summaries) == 1
    s = summaries[0]
    # Codex uncached = 1.5M - 1.0M = 0.5M ($0.05) + 1.0M*0.01 ($0.01) + 0.02M*0.50 ($0.01) = $0.07
    assert abs((s.api_cost_usd or 0) - 0.07) < 1e-6
    assert abs((s.implied_pool_usd_per_month or 0) - 7.0) < 1e-4
    # 7.0 / 0.08 (AA cost_per_task) = 87.5
    assert abs((s.tasks_per_month_attempted or 0) - 87.5) < 1e-4

    effort_pts = project_effort_variants(summaries, models)
    assert len(effort_pts) == 2
    med_pt = next(p for p in effort_pts if p.effort == "medium")
    assert not med_pt.measured
    # 7.0 / 0.04 (AA cost_per_task) = 175.0
    assert abs(med_pt.tasks_per_month_attempted - 175.0) < 1e-4

    reports = write_aa_projection_reports(
        summaries, effort_points=effort_pts, out_dir=tmp_path / "rep"
    )
    assert reports["md"].exists() and reports["csv"].exists()
    assert reports["svg"].exists()
    assert "<svg " in reports["svg"].read_text(encoding="utf-8")

    runner = CliRunner()
    res = runner.invoke(
        app,
        [
            "aa",
            "project",
            "--cache-path",
            str(cache_path),
            "--db",
            str(db_path),
            "--out-dir",
            str(tmp_path / "cli_rep"),
        ],
    )
    assert res.exit_code == 0, res.stdout
    assert "gpt-6-luna-high" in res.stdout


def test_5h_calibration_overrides_noisy_weekly(tmp_path: Path) -> None:
    db_path = tmp_path / "c.sqlite"
    conn = connect(db_path)
    init_db(conn)
    suite = SuiteManifest(
        version="verified-core-6",
        dataset_revision="0123456789abcdef0123456789abcdef01234567",
        seed=1,
        tasks=[
            TaskSpec(
                instance_id="pytest-dev__pytest-5809",
                repo="pytest-dev/pytest",
                base_commit="abc",
                difficulty=Difficulty.EASY,
                order=0,
            )
        ],
    )
    cfg = ConfigSpec(
        label="neon-medium",
        role=ConfigRole.DEFAULT,
        model_id="gemini-4-neon-medium",
        buckets=["gemini-5h", "gemini-weekly"],
    )
    manifest = ExperimentManifest(
        name="neon",
        suite="s.json",
        agy_path="/usr/local/bin/agy",
        configs=[cfg],
        quota={
            "primary_bucket_by_config": {"neon-medium": "gemini-5h"},
            "weekly_bucket_by_config": {"neon-medium": "gemini-weekly"},
        },
    )
    exp_id = record_experiment(conn, manifest, suite)
    cfg_ids = record_configs(conn, exp_id, manifest)
    c_id = conn.execute(
        "INSERT INTO cycles (experiment_id, started_at, status) "
        "VALUES (?, '2026-11-19Z', 'completed')",
        (exp_id,),
    ).lastrowid
    a_id = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid,
            input_tokens, output_tokens, cache_read_tokens
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, 'GRADED', 1, 100000, 10000, 500000)
        """,
        (c_id, cfg_ids["neon-medium"]),
    ).lastrowid
    conn.execute(
        "INSERT INTO grades (attempt_id, graded_at, resolved, patch_applied) "
        "VALUES (?, 'now', 1, 1)",
        (a_id,),
    )
    save_quota_snapshot(
        conn,
        cycle_id=c_id,
        attempt_id=a_id,
        phase="pre",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading("gemini-5h", "5h", 1.00),
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.80),
            },
            taken_at="now",
            provenance="agy_usage",
            raw_json="{}",
        ),
    )
    save_quota_snapshot(
        conn,
        cycle_id=c_id,
        attempt_id=a_id,
        phase="post",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading(
                    "gemini-5h", "5h", 0.965
                ),  # 3.5 pp 5h -> 1.0 pp weekly eq
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.795),  # noisy 0.5 pp
            },
            taken_at="now",
            provenance="agy_usage",
            raw_json="{}",
        ),
    )
    conn.commit()
    aa_models = parse_aa_models(
        {
            "data": [
                {
                    "slug": "gemini-4-neon-medium",
                    "name": "Gemini 4 Neon (Medium)",
                    "model_creator": {"name": "Google"},
                    "evaluations": {"artificial_analysis_intelligence_index": 54.9},
                    "pricing": {"price_1m_input_tokens": 3.0, "price_1m_output_tokens": 15.0},
                }
            ]
        }
    )
    s_cal = compute_aa_projections(conn, aa_models, use_5h_calibration=True)[0]
    s_raw = compute_aa_projections(conn, aa_models, use_5h_calibration=False)[0]
    assert abs(s_cal.weekly_burn_pp - 1.0) < 1e-6
    assert abs(s_raw.weekly_burn_pp - 0.5) < 1e-6
    # Missing aa_ii_cost_per_task must not silently fall back to SWE-bench tasks/mo
    assert s_cal.tasks_per_month_attempted is None


def test_repeated_version_tokens_do_not_collide() -> None:
    """claude-opus-5 must not match or project into claude-opus-5-5."""
    models = parse_aa_models(
        {
            "data": [
                {
                    "slug": "claude-opus-5",
                    "name": "Claude Opus 5 (Max)",
                    "model_creator": {"name": "Anthropic"},
                    "evaluations": {"artificial_analysis_intelligence_index": 44.2},
                    "artificial_analysis_intelligence_index_cost": {
                        "cost_per_task": {"total_cost": 5.0}
                    },
                    "pricing": {"price_1m_input_tokens": 4.0, "price_1m_output_tokens": 20.0},
                },
                {
                    "slug": "claude-opus-5-5",
                    "name": "Claude Opus 5.5 (Max)",
                    "model_creator": {"name": "Anthropic"},
                    "evaluations": {"artificial_analysis_intelligence_index": 59.1},
                    "artificial_analysis_intelligence_index_cost": {
                        "cost_per_task": {"total_cost": 10.0}
                    },
                    "pricing": {"price_1m_input_tokens": 4.0, "price_1m_output_tokens": 20.0},
                },
                {
                    "slug": "claude-opus-5-5-medium",
                    "name": "Claude Opus 5.5 (Medium)",
                    "model_creator": {"name": "Anthropic"},
                    "evaluations": {"artificial_analysis_intelligence_index": 48.2},
                    "artificial_analysis_intelligence_index_cost": {
                        "cost_per_task": {"total_cost": 2.5}
                    },
                    "pricing": {"price_1m_input_tokens": 4.0, "price_1m_output_tokens": 20.0},
                },
            ]
        }
    )

    # Fallback matcher should never match opus-5-5 to opus-5 when opus-5-5 is absent
    only_v5 = [m for m in models if m.slug == "claude-opus-5"]
    assert match_aa_model("claude-opus-5-5-medium", None, only_v5) is None

    # Fallback matcher with only bare opus-5-5 + opus-5 picks opus-5-5, not opus-5
    no_med = [m for m in models if m.slug != "claude-opus-5-5-medium"]
    matched = match_aa_model("claude-opus-5-5-medium", None, no_med)
    assert matched is not None and matched.slug == "claude-opus-5-5"

    # Effort projection for opus-5-5 excludes claude-opus-5
    from subbench.aa import SubbenchConfigCostSummary

    summary = SubbenchConfigCostSummary(
        cycle_id=11,
        config_label="opus-5-5-medium",
        model_id="claude-opus-5-5-medium",
        effort=None,
        agent="antigravity",
        weekly_bucket="3p-weekly",
        resolved_valid=5,
        attempted_valid=6,
        weekly_burn_pp=25.0,
        uncached_input_tokens=100_000,
        cache_read_tokens=1_000_000,
        output_tokens=20_000,
        aa_slug="claude-opus-5-5-medium",
        aa_name="Claude Opus 5.5 (Medium)",
        aa_intelligence=48.2,
        aa_ii_cost_per_task=2.5,
        aa_price_1m_in=4.0,
        aa_price_1m_out=20.0,
        aa_price_1m_cache=0.2,
        api_cost_usd=1.0,
        implied_pool_usd_per_week=4.0,
        implied_pool_usd_per_month=20.0,
        tasks_per_month_attempted=8.0,
    )
    pts = project_effort_variants([summary], models)
    slugs = {p.aa_slug for p in pts}
    assert slugs == {"claude-opus-5-5-medium", "claude-opus-5-5"}


def test_net_contiguous_burn_handles_recredits_resets_and_gaps() -> None:
    # 1. Contiguous attempts with an inter-attempt recredit (+0.0021)
    rows_recredit = [
        (1, 10, 1, 1.0000, 0.9792, "2026-11-19T18:00Z", "2026-11-19T18:00Z"),
        (2, 10, 1, 0.9813, 0.9645, "2026-11-19T18:00Z", "2026-11-19T18:00Z"),
    ]
    # Net burn = 1.0000 - 0.9645 = 0.0355 (not 0.0208 + 0.0168 = 0.0376)
    assert abs(_compute_net_contiguous_burn(rows_recredit, 10) - 0.0355) < 1e-9

    # 2. Bucket reset between attempts must break segment (not count as recredit)
    rows_reset = [
        (1, 10, 1, 0.30, 0.25, "2026-11-19T12:00Z", "2026-11-19T12:00Z"),
        (2, 10, 1, 1.00, 0.95, "2026-11-19T17:00Z", "2026-11-19T17:00Z"),
    ]
    assert abs(_compute_net_contiguous_burn(rows_reset, 10) - 0.10) < 1e-9

    # 3. Invalid attempt in between breaks segment
    rows_invalid = [
        (1, 10, 1, 0.90, 0.85, "2026-11-19T18:00Z", "2026-11-19T18:00Z"),
        (2, 10, 0, 0.85, 0.70, "2026-11-19T18:00Z", "2026-11-19T18:00Z"),
        (3, 10, 1, 0.70, 0.65, "2026-11-19T18:00Z", "2026-11-19T18:00Z"),
    ]
    assert abs(_compute_net_contiguous_burn(rows_invalid, 10) - 0.10) < 1e-9


def test_svg_supports_sub_unit_tasks_per_month() -> None:
    pts = [
        EffortProjectionPoint(
            family_key="opus@3p-weekly",
            family_label="Claude Opus 5.5",
            effort="ultra",
            aa_slug="claude-opus-5-5-ultra",
            aa_name="Claude Opus 5.5 (Ultra)",
            weekly_bucket="3p-weekly",
            measured=False,
            source_cycle_id=11,
            source_config_label="opus-5-5-medium",
            aa_intelligence=62.0,
            aa_ii_cost_per_task=40.0,
            tasks_per_month_attempted=0.4,
            measured_resolve_rate=None,
        ),
        EffortProjectionPoint(
            family_key="opus@3p-weekly",
            family_label="Claude Opus 5.5",
            effort="medium",
            aa_slug="claude-opus-5-5-medium",
            aa_name="Claude Opus 5.5 (Medium)",
            weekly_bucket="3p-weekly",
            measured=True,
            source_cycle_id=11,
            source_config_label="opus-5-5-medium",
            aa_intelligence=48.2,
            aa_ii_cost_per_task=2.5,
            tasks_per_month_attempted=8.0,
            measured_resolve_rate=0.83,
            tasks_mo_ci_low=7.0,
            tasks_mo_ci_high=9.0,
        ),
    ]
    svg_default = render_aa_intelligence_vs_tasks_svg(pts)
    assert ">0.5</text>" in svg_default
    assert "Opus 5.5 (Medium)" in svg_default
    assert "95% CI (measured)" not in svg_default

    svg_ci = render_aa_intelligence_vs_tasks_svg(pts, show_ci=True)
    assert "95% CI (measured)" in svg_ci


def test_parse_aa_token_breakdowns_and_token_weighted_projection(tmp_path: Path) -> None:
    fake_html = (
        r'{"slug":"gemini-4-neon-high","name":"Gemini 4 Neon (High)",'
        r'"shortName":"Gemini 4 Neon (high)",'
        r'"modelFamily":{"slug":"gemini-4-neon","name":"Gemini 4 Neon"},'
        r'"price1mInputTokens":3,"price1mOutputTokens":15,"cacheHitPrice":0.15,'
        r'"intelligenceIndexCostPerTask":{"cost":{"total":6.4448,'
        r'"nonCacheInput":0.064428,"cacheWrite":2.594639,"cacheRead":1.661309,'
        r'"output":2.124462,"reasoning":1.20755,"answer":0.916912}},'
        r'"canonicalIntelligenceIndexTokenCount":{"input":8118042892,"output":282710380}}'
    )
    bds = parse_aa_model_page_token_breakdowns(fake_html)
    assert "gemini-4-neon-high" in bds
    assert "gemini-4-neon" not in bds
    bd = bds["gemini-4-neon-high"]
    assert abs(bd.output_tokens_per_task - (2.124462 / 15.0) * 1e6) < 1.0
    assert abs(bd.cache_read_tokens_per_task - (1.661309 / 0.15) * 1e6) < 1.0
    assert bd.canonical_input_tokens == 8118042892

    eff_pt = EffortProjectionPoint(
        family_key="gemini-4-neon@gemini-weekly",
        family_label="Gemini 4 Neon",
        effort="high",
        aa_slug="gemini-4-neon-high",
        aa_name="Gemini 4 Neon (High)",
        weekly_bucket="gemini-weekly",
        measured=False,
        source_cycle_id=9,
        source_config_label="neon-medium",
        aa_intelligence=59.6,
        aa_ii_cost_per_task=6.4448,
        tasks_per_month_attempted=45.4,
        measured_resolve_rate=None,
    )
    cal = {
        "models": [
            {
                "target": {
                    "model_id": "gemini-4-neon-medium",
                    "effort": None,
                    "weekly_bucket": "gemini-weekly",
                },
                "aa_slug": "gemini-4-neon-medium",
                "synthetic_fit": {
                    "n_samples": 3,
                    "w_in_pp_per_1m": 2.857143,
                    "w_cache_pp_per_1m": 0.3011,
                    "w_out_pp_per_1m": 14.285714,
                },
            }
        ]
    }
    tw_pts = compute_token_weighted_projections([eff_pt], bds, cal)
    assert len(tw_pts) == 1
    # Expected ~54.9 AA tasks/mo (433 / 7.89 pp/task)
    assert 54.0 < tw_pts[0].token_weighted_tasks_per_month < 56.0
    assert tw_pts[0].weight_source == "synthetic-3p"

    out = write_token_weighted_reports(tw_pts, out_dir=tmp_path)
    assert out["md"].exists() and out["csv"].exists() and out["svg"].exists()
    assert "Token-Weighted" in out["svg"].read_text(encoding="utf-8")
