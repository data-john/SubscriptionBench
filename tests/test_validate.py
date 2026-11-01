import json

from subbench.tasks.validate import (
    NOOP_PATCH,
    InstanceResult,
    parse_report,
    run_harness,
    write_predictions,
)


def test_instance_passes_only_when_gold_all_true_and_noop_all_false():
    assert InstanceResult(gold=[True, True], noop=[False, False]).passed
    assert not InstanceResult(gold=[True, False], noop=[False, False]).passed
    assert not InstanceResult(gold=[True, True], noop=[False, True]).passed
    assert not InstanceResult().passed
    assert not InstanceResult(gold=[True], noop=[False], error="boom").passed


def test_parse_report():
    rep = {"resolved_ids": ["a"], "unresolved_ids": ["b"], "error_ids": ["c"]}
    assert parse_report(rep) == {"a": True, "b": False, "c": False}


def test_write_predictions(tmp_path):
    rows = {"x": {"patch": "GOLD"}}
    gold, noop = tmp_path / "g.jsonl", tmp_path / "n.jsonl"
    write_predictions(gold, ["x"], rows, "gold")
    write_predictions(noop, ["x"], rows, "noop")
    assert json.loads(gold.read_text())["model_patch"] == "GOLD"
    assert json.loads(noop.read_text())["model_patch"] == NOOP_PATCH
    assert NOOP_PATCH.startswith("diff --git")


def test_run_harness_passes_cache_level_instance(monkeypatch, tmp_path):
    called_cmd = None

    def fake_run(cmd, **kwargs):
        nonlocal called_cmd
        called_cmd = cmd
        import subprocess

        return subprocess.CompletedProcess(cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    run_harness(
        dataset_jsonl=tmp_path / "d.jsonl",
        predictions=tmp_path / "p.jsonl",
        ids=["id1"],
        run_id="r1",
        workdir=tmp_path / "work",
    )
    assert called_cmd is not None
    assert "--cache_level" in called_cmd
    idx = called_cmd.index("--cache_level")
    assert called_cmd[idx + 1] == "instance"
