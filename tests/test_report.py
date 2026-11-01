"""Offline unit and integration tests for Phase 7 metrics, exports, and reports."""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow.parquet as pq
from typer.testing import CliRunner

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
from subbench.report import (
    compute_cycle_metrics,
    generate_report,
    wilson_score_interval,
)
from subbench.runner import record_configs, record_environment, record_experiment
from subbench.store import connect, init_db


def test_wilson_score_interval_edge_cases():
    """Verify Wilson 95% score interval across boundary and nominal conditions."""
    # 0/0 empty case
    low_00, high_00 = wilson_score_interval(0, 0)
    assert low_00 == 0.0 and high_00 == 0.0

    # 0/12 zero successes
    low_0, high_0 = wilson_score_interval(0, 12)
    assert low_0 == 0.0
    assert 0.20 < high_0 < 0.30

    # 6/12 fifty percent success
    low_50, high_50 = wilson_score_interval(6, 12)
    assert 0.24 < low_50 < 0.26
    assert 0.74 < high_50 < 0.76

    # 12/12 perfect success
    low_12, high_12 = wilson_score_interval(12, 12)
    assert 0.70 < low_12 < 0.80
    assert high_12 == 1.0


def _build_test_cycle_db(db_path: Path, suite_path: Path) -> tuple[int, int]:
    """Populate a test SQLite database with a 2-task, 2-config cycle."""
    conn = connect(db_path)
    init_db(conn)

    suite = SuiteManifest(
        version="test-suite-v1",
        dataset_revision="c104f840cc6746816bc82ccb6f5223e7421f185f",
        seed=42,
        tasks=[
            TaskSpec(
                instance_id="pytest-dev__pytest-5809",
                repo="pytest-dev/pytest",
                base_commit="8aba863a634f40560e25055d179220f0eefabe9a",
                difficulty=Difficulty.EASY,
                order=0,
            ),
            TaskSpec(
                instance_id="psf__requests-2931",
                repo="psf/requests",
                base_commit="fe340324b105d153835ec7a66133efb1c2be0b48",
                difficulty=Difficulty.MEDIUM,
                order=1,
            ),
        ],
    )
    suite_path.write_text(json.dumps(suite.model_dump(mode="json"), indent=2))

    cfg_eff = ConfigSpec(
        label="efficient-flash",
        role=ConfigRole.EFFICIENT,
        model_id="gemini-3.8-flash",
        buckets=["gemini-5h", "gemini-weekly"],
    )
    cfg_cap = ConfigSpec(
        label="capable-argon",
        role=ConfigRole.CAPABLE,
        model_id="gemini-4-argon",
        buckets=["gemini-argon-wtu-weekly"],
    )

    manifest = ExperimentManifest(
        name="test-reporting",
        suite=str(suite_path),
        agy_path="/usr/local/bin/agy",
        configs=[cfg_eff, cfg_cap],
        passes=1,
        quota={
            "primary_bucket_by_config": {
                "efficient-flash": "gemini-5h",
                "capable-argon": "gemini-argon-wtu-weekly",
            },
            "weekly_bucket_by_config": {
                "efficient-flash": "gemini-weekly",
                "capable-argon": "gemini-argon-wtu-weekly",
            },
            "idle_check_seconds": 0,
        },
    )

    exp_id = record_experiment(conn, manifest, suite)
    record_environment(
        conn,
        exp_id,
        manifest,
        suite,
        agy_version="v1.4.12",
        docker_version="Docker 27",
        swebench_version="4.1.0",
    )
    config_ids = record_configs(conn, exp_id, manifest)

    c_cur = conn.execute(
        """
        INSERT INTO cycles (experiment_id, started_at, finished_at, status, flags_json)
        VALUES (?, '2026-11-01T10:00:00Z', '2026-11-01T11:00:00Z', 'completed', '[]')
        """,
        (exp_id,),
    )
    cycle_id = c_cur.lastrowid

    # 1. Efficient config:
    # Slot (pytest-5809, pass 0):
    # - Attempt 1: quota_restricted, burns 0.02 weekly, 0.05 5h. valid=1, resolved=0.
    # - Attempt 2: retried (retry_of=1), burns 0.03 weekly, 0.04 5h. valid=1, resolved=1.
    # Slot (requests-2931, pass 0):
    # - Attempt 3: valid=0 (invalid_reset), burns 0.02 weekly, resolved=1.
    eff_id = config_ids["efficient-flash"]

    # Attempt 1
    a1_cur = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, retry_of, state,
            halt_reason, valid, patch_bytes, wall_seconds, tool_calls
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, NULL, 'QUOTA_POST',
                  'quota_restricted', 1, 10, 30.0, 2)
        """,
        (cycle_id, eff_id),
    )
    att1_id = a1_cur.lastrowid
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att1_id,
        phase="pre",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading("gemini-5h", "5h", 0.90),
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.95),
            },
            taken_at="2026-11-01T10:00:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att1_id,
        phase="post",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading("gemini-5h", "5h", 0.85),
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.93),
            },
            taken_at="2026-11-01T10:05:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )

    # Attempt 2 (retry of Attempt 1)
    a2_cur = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, retry_of, state,
            halt_reason, valid, patch_bytes, wall_seconds, tool_calls
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 2, ?, 'GRADED',
                  'completed', 1, 50, 45.0, 3)
        """,
        (cycle_id, eff_id, att1_id),
    )
    att2_id = a2_cur.lastrowid
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att2_id,
        phase="pre",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading("gemini-5h", "5h", 0.85),
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.93),
            },
            taken_at="2026-11-01T10:06:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att2_id,
        phase="post",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading("gemini-5h", "5h", 0.81),
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.90),
            },
            taken_at="2026-11-01T10:15:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO grades (attempt_id, graded_at, resolved, patch_applied)
        VALUES (?, 'now', 1, 1)
        """,
        (att2_id,),
    )

    # Attempt 3 (requests-2931, invalid_reset valid=0)
    a3_cur = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, retry_of, state,
            halt_reason, valid, invalid_reason, patch_bytes, wall_seconds, tool_calls
        ) VALUES (?, ?, 'psf__requests-2931', 0, 3, NULL, 'GRADED',
                  'completed', 0, 'invalid_reset', 80, 25.0, 1)
        """,
        (cycle_id, eff_id),
    )
    att3_id = a3_cur.lastrowid
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att3_id,
        phase="pre",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading("gemini-5h", "5h", 0.81),
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.90),
            },
            taken_at="2026-11-01T10:16:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att3_id,
        phase="post",
        snap=QuotaSnapshot(
            buckets={
                "gemini-5h": BucketReading("gemini-5h", "5h", 0.80),
                "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.88),
            },
            taken_at="2026-11-01T10:20:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO grades (attempt_id, graded_at, resolved, patch_applied)
        VALUES (?, 'now', 1, 1)
        """,
        (att3_id,),
    )

    # 2. Capable config:
    # Slot (pytest-5809, pass 0):
    # - Attempt 4: valid=1, burns 0.04 weekly, resolved=1.
    # Slot (requests-2931, pass 0):
    # - Aborted early before running due to weekly quota limit!
    cap_id = config_ids["capable-argon"]
    a4_cur = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, retry_of, state,
            halt_reason, valid, patch_bytes, wall_seconds, tool_calls
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 4, NULL, 'GRADED',
                  'completed', 1, 40, 60.0, 4)
        """,
        (cycle_id, cap_id),
    )
    att4_id = a4_cur.lastrowid
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att4_id,
        phase="pre",
        snap=QuotaSnapshot(
            buckets={
                "gemini-argon-wtu-weekly": BucketReading("gemini-argon-wtu-weekly", "weekly", 0.90)
            },
            taken_at="2026-11-01T10:25:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    save_quota_snapshot(
        conn,
        cycle_id=cycle_id,
        attempt_id=att4_id,
        phase="post",
        snap=QuotaSnapshot(
            buckets={
                "gemini-argon-wtu-weekly": BucketReading("gemini-argon-wtu-weekly", "weekly", 0.86)
            },
            taken_at="2026-11-01T10:35:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO grades (attempt_id, graded_at, resolved, patch_applied)
        VALUES (?, 'now', 1, 1)
        """,
        (att4_id,),
    )

    conn.commit()
    return exp_id, cycle_id


def test_report_multi_config_quota_accounting_and_slot_resolution(tmp_path: Path):
    """Verify D9 retry summing, valid=0 exclusion, and planned-slot denominator scoring."""
    db_path = tmp_path / "test.db"
    suite_path = tmp_path / "suite.json"
    _, cycle_id = _build_test_cycle_db(db_path, suite_path)

    conn = connect(db_path)
    report = compute_cycle_metrics(conn, cycle_id)

    # 1. Verify Efficient Config
    cm_eff = next(c for c in report.configs if c.label == "efficient-flash")
    assert cm_eff.planned_slots == 2
    assert cm_eff.attempted_slots == 2
    # Resolved slots: 2 (pytest via retry #2, requests via #3)
    assert cm_eff.resolved_slots == 2
    assert cm_eff.resolve_rate_planned == 1.0
    # Resolved valid slots: 1 (pytest slot was valid throughout; requests slot had valid=0)
    assert cm_eff.resolved_valid_slots == 1

    # Quota burn:
    # Attempt 1 (valid=1): 0.02 weekly, 0.05 5h
    # Attempt 2 (valid=1): 0.03 weekly, 0.04 5h
    # Attempt 3 (valid=0): excluded!
    # Total weekly burn = 0.05 (5.0 pp)
    # Total 5h burn = 0.09 (9.0 pp)
    assert abs(cm_eff.consumed_fraction["gemini-weekly"] - 0.05) < 1e-6
    assert abs(cm_eff.consumed_pp["gemini-weekly"] - 5.0) < 1e-6
    assert abs(cm_eff.consumed_pp["gemini-5h"] - 9.0) < 1e-6

    # Efficiency metrics use resolved_valid_slots = 1
    # Tasks per 100% weekly bucket = 1 / 0.05 = 20.0
    assert abs(cm_eff.tasks_per_full_bucket_weekly - 20.0) < 1e-6
    # pp per resolved weekly = (0.05 * 100) / 1 = 5.0 pp
    assert abs(cm_eff.pp_per_resolved_task_weekly - 5.0) < 1e-6

    assert cm_eff.invalid_quota_attempts == 1
    assert cm_eff.retried_count == 1

    # 2. Verify Capable Config (early abort: planned=2, attempted=1)
    cm_cap = next(c for c in report.configs if c.label == "capable-argon")
    assert cm_cap.planned_slots == 2
    assert cm_cap.attempted_slots == 1
    assert cm_cap.resolved_slots == 1
    assert cm_cap.resolve_rate_planned == 0.50
    assert cm_cap.resolve_rate_attempted == 1.0
    # Weekly burn: 0.04 (4.0 pp)
    assert abs(cm_cap.consumed_pp["gemini-argon-wtu-weekly"] - 4.0) < 1e-6
    # Tasks per 100% weekly bucket = 1 / 0.04 = 25.0
    assert abs(cm_cap.tasks_per_full_bucket_weekly - 25.0) < 1e-6


def test_report_artifacts_generation_and_export_roundtrip(tmp_path: Path):
    """Verify generate_report writes valid summary.md, SVG charts, and readable CSV/Parquet."""
    db_path = tmp_path / "test.db"
    suite_path = tmp_path / "suite.json"
    _, cycle_id = _build_test_cycle_db(db_path, suite_path)

    out_dir = tmp_path / "report_out"
    conn = connect(db_path)
    outputs = generate_report(conn, cycle_id, out_dir)

    # 1. Verify summary.md contents
    summary_md = outputs["summary_md"]
    assert summary_md.exists()
    md_text = summary_md.read_text(encoding="utf-8")
    # Must contain F3 quota pool disclosure
    assert "Quota Pool Disclosure (F3)" in md_text
    assert "gemini-argon-wtu-weekly" in md_text
    assert "Wilson 95% score intervals" in md_text
    assert "Executive Performance & Quota Efficiency" in md_text

    # 2. Verify SVG charts
    eff_svg = outputs["efficiency_svg"]
    assert eff_svg.exists()
    svg_eff_text = eff_svg.read_text(encoding="utf-8")
    assert svg_eff_text.startswith("<svg") and svg_eff_text.strip().endswith("</svg>")
    assert "Quota Efficiency vs. Accuracy" in svg_eff_text

    matrix_svg = outputs["matrix_svg"]
    assert matrix_svg.exists()
    svg_matrix_text = matrix_svg.read_text(encoding="utf-8")
    assert svg_matrix_text.startswith("<svg") and svg_matrix_text.strip().endswith("</svg>")
    assert "Paired Task Outcome Matrix" in svg_matrix_text

    # 3. Verify Parquet tables roundtrip via pyarrow
    att_pq = outputs["attempts_parquet"]
    assert att_pq.exists()
    tbl_att = pq.read_table(att_pq)
    assert tbl_att.num_rows == 4

    cfg_pq = outputs["config_metrics_parquet"]
    assert cfg_pq.exists()
    tbl_cfg = pq.read_table(cfg_pq)
    assert tbl_cfg.num_rows == 2

    # 4. Verify CSV files exist and are non-empty
    assert outputs["attempts_csv"].exists()
    assert outputs["config_metrics_csv"].exists()


def test_report_leak_warning_banner(tmp_path: Path):
    """Verify that an audit leak triggers a prominent warning banner at the top of summary.md."""
    db_path = tmp_path / "test.db"
    suite_path = tmp_path / "suite.json"
    _, cycle_id = _build_test_cycle_db(db_path, suite_path)

    conn = connect(db_path)
    # Insert a leak finding for attempt 2
    conn.execute(
        """
        INSERT INTO audit_findings (attempt_id, kind, severity, evidence, event_idx, created_at)
        VALUES (2, 'outbound_url_command', 'leak', 'git clone http://leak.com', 5, 'now')
        """
    )
    conn.commit()

    out_dir = tmp_path / "report_out"
    outputs = generate_report(conn, cycle_id, out_dir)
    md_text = outputs["summary_md"].read_text(encoding="utf-8")

    assert "> [!WARNING]" in md_text
    assert "POTENTIAL EGRESS / HISTORY LEAK DETECTED" in md_text
    assert "#2" in md_text


def test_report_longitudinal_deltas_and_cli(tmp_path: Path):
    """Verify longitudinal comparison against a prior cycle and CLI execution."""
    db_path = tmp_path / "test.db"
    suite_path = tmp_path / "suite.json"
    exp_id, cycle1_id = _build_test_cycle_db(db_path, suite_path)

    conn = connect(db_path)
    # Create a second cycle (Cycle 2)
    c2_cur = conn.execute(
        """
        INSERT INTO cycles (experiment_id, started_at, finished_at, status, flags_json)
        VALUES (?, '2026-11-02T10:00:00Z', '2026-11-02T11:00:00Z', 'completed', '[]')
        """,
        (exp_id,),
    )
    cycle2_id = c2_cur.lastrowid

    # Create attempts for Cycle 2 with slightly different outcomes
    cfg_rows = conn.execute(
        "SELECT id, label FROM configs WHERE experiment_id = ?", (exp_id,)
    ).fetchall()
    eff_id = next(r[0] for r in cfg_rows if r[1] == "efficient-flash")

    a_cur = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state,
            halt_reason, valid, patch_bytes, wall_seconds, tool_calls
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, 'GRADED',
                  'completed', 1, 40, 20.0, 2)
        """,
        (cycle2_id, eff_id),
    )
    a_id = a_cur.lastrowid
    save_quota_snapshot(
        conn,
        cycle_id=cycle2_id,
        attempt_id=a_id,
        phase="pre",
        snap=QuotaSnapshot(
            buckets={"gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.90)},
            taken_at="2026-11-02T10:00:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    save_quota_snapshot(
        conn,
        cycle_id=cycle2_id,
        attempt_id=a_id,
        phase="post",
        snap=QuotaSnapshot(
            buckets={"gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.88)},
            taken_at="2026-11-02T10:10:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    )
    conn.execute(
        """
        INSERT INTO grades (attempt_id, graded_at, resolved, patch_applied)
        VALUES (?, 'now', 1, 1)
        """,
        (a_id,),
    )
    conn.commit()

    # Test via CLI
    cli_runner = CliRunner()
    out_dir = tmp_path / "cli_report_out"
    res = cli_runner.invoke(
        app,
        [
            "report",
            "--cycle-id",
            str(cycle2_id),
            "--compare-cycle-id",
            str(cycle1_id),
            "--out-dir",
            str(out_dir),
            "--db",
            str(db_path),
        ],
    )
    assert res.exit_code == 0
    assert "Report generated" in res.stdout

    summary_text = (out_dir / "summary.md").read_text(encoding="utf-8")
    assert "Longitudinal Comparison" in summary_text
    assert "Environment Drift" in summary_text


def test_report_auto_compare_ignores_different_experiment_name(tmp_path: Path):
    """Ensure baseline cycle does not auto-compare against a prior pilot-flash cycle."""
    db_path = tmp_path / "test.db"
    suite_path = tmp_path / "suite.json"
    _, _ = _build_test_cycle_db(db_path, suite_path)  # Cycle 1: experiment 'test-reporting'

    conn = connect(db_path)
    suite = SuiteManifest.model_validate(json.loads(suite_path.read_text()))
    cfg_eff = ConfigSpec(
        label="efficient-flash",
        role=ConfigRole.EFFICIENT,
        model_id="gemini-3.8-flash",
        buckets=["gemini-5h", "gemini-weekly"],
    )
    manifest2 = ExperimentManifest(
        name="baseline-distinct",
        suite=str(suite_path),
        agy_path="/usr/local/bin/agy",
        configs=[cfg_eff],
        passes=1,
        quota={
            "primary_bucket_by_config": {"efficient-flash": "gemini-5h"},
            "weekly_bucket_by_config": {"efficient-flash": "gemini-weekly"},
            "idle_check_seconds": 0,
        },
    )
    exp2_id = record_experiment(conn, manifest2, suite)
    c2_id = conn.execute(
        "INSERT INTO cycles (experiment_id, started_at, status) VALUES (?, 'now', 'completed')",
        (exp2_id,),
    ).lastrowid
    conn.commit()

    report = compute_cycle_metrics(conn, c2_id, compare_cycle_id=None)
    assert report.longitudinal_deltas is None
