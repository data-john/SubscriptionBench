from pathlib import Path

from typer.testing import CliRunner

from subbench.cli import app

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
