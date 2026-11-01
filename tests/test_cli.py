from pathlib import Path

from typer.testing import CliRunner

from subbench.cli import app
from subbench.store import connect, init_db

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def test_version():
    assert runner.invoke(app, ["version"]).exit_code == 0


def test_init_db(tmp_path):
    db = tmp_path / "x" / "r.sqlite"
    result = runner.invoke(app, ["init-db", "--db", str(db)])
    assert result.exit_code == 0, result.output
    assert db.exists()


def test_validate_baseline():
    result = runner.invoke(app, ["validate", str(ROOT / "experiments" / "baseline.yaml")])
    assert result.exit_code == 0, result.output
    assert "3 configs" in result.output


def test_quota_snapshot_cli(tmp_path: Path):
    sample_file = ROOT / "docs" / "phase0_samples" / "usage_raw_schema.json"
    db_path = tmp_path / "test.db"
    res = runner.invoke(
        app,
        ["quota", "snapshot", "--manual-file", str(sample_file), "--db", str(db_path)],
    )
    assert res.exit_code == 0, res.output
    assert "gemini-weekly" in res.output
    assert "Saved snapshot" in res.output


def test_run_dry_run_cli():
    baseline_yaml = ROOT / "experiments" / "baseline.yaml"
    res = runner.invoke(app, ["run", str(baseline_yaml), "--dry-run"])
    assert res.exit_code == 0, res.output
    assert "Dry run for manifest: baseline" in res.output
    assert "pytest-dev__pytest-5809" in res.output


def test_grade_and_audit_cli(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_db(conn)

    cur = conn.execute(
        """
        INSERT INTO experiments (
            name, manifest_json, manifest_digest, suite_version, suite_digest, created_at
        ) VALUES ('t', '{}', 'd', 's', 'd', '2026-11-01T10:00:00Z')
        """
    )
    exp_id = cur.lastrowid
    c_cur = conn.execute(
        """
        INSERT INTO cycles (experiment_id, started_at, status)
        VALUES (?, '2026-11-01T10:00:00Z', 'running')
        """,
        (exp_id,),
    )
    cycle_id = c_cur.lastrowid
    cfg_cur = conn.execute(
        """
        INSERT INTO configs (experiment_id, label, role, model_id, buckets_json)
        VALUES (?, 'cfg', 'default', 'm', '[]')
        """,
        (exp_id,),
    )
    cfg_id = cfg_cur.lastrowid

    # Add an empty-patch attempt (can be graded without Docker)
    conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid, patch_bytes
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, 'QUOTA_POST', 1, 0)
        """,
        (cycle_id, cfg_id),
    )
    conn.commit()

    # Test grade CLI
    grade_res = runner.invoke(app, ["grade", "--cycle-id", str(cycle_id), "--db", str(db_path)])
    assert grade_res.exit_code == 0, grade_res.output
    assert "Grading complete" in grade_res.output

    # Test audit CLI
    audit_res = runner.invoke(app, ["audit", "--cycle-id", str(cycle_id), "--db", str(db_path)])
    assert audit_res.exit_code == 0, audit_res.output
    assert "Audit complete" in audit_res.output
