from pathlib import Path

import pytest
from pydantic import ValidationError

from subbench.config import ExperimentManifest, SuiteManifest, load_experiment

ROOT = Path(__file__).resolve().parent.parent


def _cfg(label="a", **kw):
    base = {"label": label, "role": "default", "model_id": "m", "buckets": ["gemini-5h"]}
    return base | kw


def _exp(**kw):
    base = {"name": "t", "suite": "s.json", "agy_path": "/bin/agy", "configs": [_cfg()]}
    return base | kw


@pytest.mark.parametrize("name", ["pilot-flash.yaml", "baseline.yaml"])
def test_committed_manifests_validate(name):
    exp = load_experiment(ROOT / "experiments" / name)
    assert exp.quota.use_g1_credits is False
    assert len(exp.digest()) == 64


def test_digest_is_stable_and_sensitive():
    a = ExperimentManifest.model_validate(_exp())
    b = ExperimentManifest.model_validate(_exp())
    c = ExperimentManifest.model_validate(_exp(passes=2))
    assert a.digest() == b.digest() != c.digest()


def test_duplicate_labels_rejected():
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(configs=[_cfg("a"), _cfg("a")]))


def test_relative_agy_path_rejected():
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(agy_path="agy"))


def test_credits_cannot_be_enabled():
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(quota={"use_g1_credits": True}))


def test_unknown_field_rejected():
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(typo=1))


def test_primary_bucket_must_reference_known_config():
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(quota={"primary_bucket_by_config": {"zzz": "x"}}))


def _task(i, iid=None):
    return {
        "instance_id": iid or f"r__r-{i}",
        "repo": "r/r",
        "base_commit": "abc",
        "difficulty": "<15 min fix",
        "order": i,
    }


def test_suite_order_must_be_contiguous():
    ok = {"version": "v", "dataset_revision": "x", "seed": 1, "tasks": [_task(0), _task(1)]}
    SuiteManifest.model_validate(ok)
    bad = ok | {"tasks": [_task(0), _task(2)]}
    with pytest.raises(ValidationError):
        SuiteManifest.model_validate(bad)


def test_suite_duplicate_ids_rejected():
    tasks = [_task(0, "same"), _task(1, "same")]
    with pytest.raises(ValidationError):
        SuiteManifest.model_validate(
            {"version": "v", "dataset_revision": "x", "seed": 1, "tasks": tasks}
        )
