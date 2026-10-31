import json

from subbench.tasks.validate import (
    NOOP_PATCH,
    InstanceResult,
    parse_report,
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
