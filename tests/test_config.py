from pathlib import Path

import pytest
from pydantic import ValidationError

from subbench.config import ConfigSpec, ExperimentManifest, SuiteManifest, load_experiment

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
        ExperimentManifest.model_validate(
            _exp(quota={"primary_bucket_by_config": {"zzz": "gemini-5h"}})
        )


def test_unknown_bucket_in_config_spec_rejected():
    with pytest.raises(ValidationError):
        ConfigSpec.model_validate(_cfg(buckets=["not-a-real-bucket"]))


def test_unknown_bucket_in_quota_policy_rejected():
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            _exp(quota={"primary_bucket_by_config": {"a": "invalid-bucket"}})
        )
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            _exp(quota={"weekly_bucket_by_config": {"a": "invalid-bucket"}})
        )


def test_bucket_must_be_in_config_spec_buckets():
    # config "a" only has ["gemini-5h"]
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            _exp(quota={"primary_bucket_by_config": {"a": "gemini-weekly"}})
        )
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            _exp(quota={"weekly_bucket_by_config": {"a": "gemini-weekly"}})
        )


def test_weekly_bucket_by_config_must_reference_known_config():
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(
            _exp(quota={"weekly_bucket_by_config": {"unknown": "gemini-weekly"}})
        )


def test_exhaustion_fields_validation():
    # Default values work
    exp = ExperimentManifest.model_validate(_exp())
    assert exp.quota.max_5h_windows_per_config_cycle == 2
    assert exp.quota.on_weekly_exhaustion == "abort_config"
    assert exp.quota.max_weekly_fraction_per_config_cycle is None

    # Valid custom values
    exp2 = ExperimentManifest.model_validate(
        _exp(
            quota={
                "max_5h_windows_per_config_cycle": 3,
                "on_weekly_exhaustion": "wait",
                "max_weekly_fraction_per_config_cycle": 0.5,
            }
        )
    )
    assert exp2.quota.max_5h_windows_per_config_cycle == 3
    assert exp2.quota.on_weekly_exhaustion == "wait"
    assert exp2.quota.max_weekly_fraction_per_config_cycle == 0.5

    # Invalid exhaustion values rejected
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(quota={"max_5h_windows_per_config_cycle": 0}))
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(quota={"on_weekly_exhaustion": "invalid"}))
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(quota={"max_weekly_fraction_per_config_cycle": 0.0}))
    with pytest.raises(ValidationError):
        ExperimentManifest.model_validate(_exp(quota={"max_weekly_fraction_per_config_cycle": 1.5}))


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
