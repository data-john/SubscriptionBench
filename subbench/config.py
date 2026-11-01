"""Pydantic models for experiment and suite manifests.

Manifests are plain YAML/JSON files that are committed to the repo, so every run is
reproducible from a manifest plus the pinned suite.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmpty = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]

# Quota bucket ids observed from `agy -p /usage --output-format json` (Phase 0).
KNOWN_BUCKETS = (
    "gemini-5h",
    "gemini-weekly",
    "gemini-argon-wtu-weekly",
    "3p-5h",
    "3p-weekly",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ConfigRole(StrEnum):
    """The three MVP baselines from the README."""

    DEFAULT = "default"
    EFFICIENT = "efficient"
    CAPABLE = "capable"


class Difficulty(StrEnum):
    """SWE-bench Verified difficulty annotations."""

    EASY = "<15 min fix"
    MEDIUM = "15 min - 1 hour"
    HARD = "1-4 hours"


class ConfigSpec(_Strict):
    """One model configuration under test. Compared on identical prompts and limits."""

    label: NonEmpty
    role: ConfigRole
    model_id: NonEmpty
    # Buckets this model draws from (verified in Phase 0); used by the quota analysis.
    buckets: list[NonEmpty] = Field(min_length=1)
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None

    @model_validator(mode="after")
    def _validate_buckets(self) -> ConfigSpec:
        invalid = set(self.buckets) - set(KNOWN_BUCKETS)
        if invalid:
            raise ValueError(f"Unknown bucket(s) in buckets: {invalid}. Must be in {KNOWN_BUCKETS}")
        return self


class QuotaPolicy(_Strict):
    """Quota-related guards."""

    # Buckets used as allowance units when computing efficiency.
    primary_bucket_by_config: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)
    weekly_bucket_by_config: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)
    max_5h_windows_per_config_cycle: int = Field(default=2, ge=1)
    on_weekly_exhaustion: Literal["abort_config", "wait"] = "abort_config"
    max_weekly_fraction_per_config_cycle: float | None = Field(default=None, gt=0, le=1.0)
    idle_check_seconds: int = Field(default=120, ge=0)
    # An unexplained drop of more than this fraction between post(i) and pre(i+1) is
    # recorded as external use.
    external_use_threshold: float = Field(default=1e-4, ge=0)
    use_g1_credits: Literal[False] = False  # must stay off: no silent paid overage

    @model_validator(mode="after")
    def _validate_buckets(self) -> QuotaPolicy:
        invalid_primary = set(self.primary_bucket_by_config.values()) - set(KNOWN_BUCKETS)
        if invalid_primary:
            raise ValueError(
                f"Unknown bucket(s) in primary_bucket_by_config: {invalid_primary}."
                f" Must be in {KNOWN_BUCKETS}"
            )
        invalid_weekly = set(self.weekly_bucket_by_config.values()) - set(KNOWN_BUCKETS)
        if invalid_weekly:
            raise ValueError(
                f"Unknown bucket(s) in weekly_bucket_by_config: {invalid_weekly}."
                f" Must be in {KNOWN_BUCKETS}"
            )
        return self


class ExperimentManifest(_Strict):
    """A complete, resumable run definition."""

    name: NonEmpty
    description: str = ""
    suite: NonEmpty = Field(description="Path to a pinned suite JSON, relative to repo root.")
    agy_path: NonEmpty = Field(description="Absolute path to the agy binary.")
    configs: list[ConfigSpec] = Field(min_length=1)
    passes: int = Field(default=1, ge=1, description="k: full passes over all configs.")
    timeout_minutes: int = Field(default=45, ge=1)
    prompt_template: NonEmpty = Field(default="minimal-v1")
    quota: QuotaPolicy = Field(default_factory=QuotaPolicy)
    task_limit: int | None = Field(
        default=None, ge=1, description="Run only the first N suite tasks (pilot)."
    )

    @model_validator(mode="after")
    def _validate(self) -> ExperimentManifest:
        labels = [c.label for c in self.configs]
        if len(set(labels)) != len(labels):
            raise ValueError("config labels must be unique")
        if not Path(self.agy_path).is_absolute():
            raise ValueError("agy_path must be absolute (agy may not be on PATH)")
        configs_by_label = {c.label: c for c in self.configs}

        unknown_primary = set(self.quota.primary_bucket_by_config) - set(labels)
        if unknown_primary:
            raise ValueError(
                f"primary_bucket_by_config references unknown configs: {unknown_primary}"
            )
        for label, bucket in self.quota.primary_bucket_by_config.items():
            if bucket not in configs_by_label[label].buckets:
                raise ValueError(
                    f"primary bucket '{bucket}' for config '{label}' is not in"
                    f" config's buckets: {configs_by_label[label].buckets}"
                )

        unknown_weekly = set(self.quota.weekly_bucket_by_config) - set(labels)
        if unknown_weekly:
            raise ValueError(
                f"weekly_bucket_by_config references unknown configs: {unknown_weekly}"
            )
        for label, bucket in self.quota.weekly_bucket_by_config.items():
            if bucket not in configs_by_label[label].buckets:
                raise ValueError(
                    f"weekly bucket '{bucket}' for config '{label}' is not in"
                    f" config's buckets: {configs_by_label[label].buckets}"
                )
        return self

    def canonical_json(self) -> str:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))

    def digest(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


class TaskSpec(_Strict):
    """One pinned SWE-bench task."""

    instance_id: NonEmpty
    repo: NonEmpty
    base_commit: NonEmpty
    difficulty: Difficulty
    # Position in the immutable stratified round-robin order (0-based).
    order: int = Field(ge=0)


class SuiteManifest(_Strict):
    """A frozen task suite. Longitudinal comparisons only span the same `version`."""

    version: NonEmpty
    dataset: NonEmpty = "princeton-nlp/SWE-bench_Verified"
    dataset_revision: NonEmpty
    seed: int
    tasks: list[TaskSpec] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate(self) -> SuiteManifest:
        ids = [t.instance_id for t in self.tasks]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate instance_id in suite")
        if sorted(t.order for t in self.tasks) != list(range(len(self.tasks))):
            raise ValueError("task order must be a contiguous 0..n-1 permutation")
        return self

    def ordered(self) -> list[TaskSpec]:
        return sorted(self.tasks, key=lambda t: t.order)

    def digest(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()


def load_experiment(path: str | Path) -> ExperimentManifest:
    with open(path) as f:
        return ExperimentManifest.model_validate(yaml.safe_load(f))


def load_suite(path: str | Path) -> SuiteManifest:
    with open(path) as f:
        return SuiteManifest.model_validate(json.load(f))
