"""Guards the committed, validated suite against accidental edits."""

import json
from collections import Counter
from pathlib import Path

from subbench.config import Difficulty, load_suite

SUITES = Path(__file__).resolve().parent.parent / "suites"


def test_frozen_suite_shape():
    suite = load_suite(SUITES / "verified-mvp-v1.json")
    assert suite.version == "verified-mvp-v1"
    assert len(suite.dataset_revision) == 40
    assert len(suite.tasks) == 12
    assert set(Counter(t.difficulty for t in suite.tasks).values()) == {4}
    assert {t.difficulty for t in suite.tasks} == set(Difficulty)
    assert 4 <= len({t.repo for t in suite.tasks}) <= 6


def test_every_task_validated_stably():
    suite = load_suite(SUITES / "verified-mvp-v1.json")
    report = json.loads((SUITES / "verified-mvp-v1.validation.json").read_text())
    assert set(report) == {t.instance_id for t in suite.tasks}
    for iid, r in report.items():
        assert r["passed"], iid
        assert len(r["gold"]) >= 2 and all(r["gold"]), iid
        assert len(r["noop"]) >= 2 and not any(r["noop"]), iid


def test_rejected_ids_not_in_suite():
    suite = load_suite(SUITES / "verified-mvp-v1.json")
    rejected = set(json.loads((SUITES / "verified-mvp-v1.rejected.json").read_text()))
    assert not rejected & {t.instance_id for t in suite.tasks}
