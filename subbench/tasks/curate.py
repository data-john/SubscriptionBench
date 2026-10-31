"""Deterministic, seeded suite curation.

Pure functions only (no I/O, no Docker) so selection is unit-testable and reproducible:
the same dataset rows + seed always yield the same ranked candidates and the same order.
"""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from subbench.config import Difficulty, SuiteManifest, TaskSpec

# Repos with comparatively light environments and enough tasks (plan section Phase 2).
DEFAULT_REPOS = (
    "django/django",
    "sympy/sympy",
    "sphinx-doc/sphinx",
    "pytest-dev/pytest",
    "astropy/astropy",
    "psf/requests",
)

STRATA = (Difficulty.EASY, Difficulty.MEDIUM, Difficulty.HARD)

Row = Mapping[str, Any]


def ranked_candidates(
    rows: Iterable[Row],
    difficulty: Difficulty,
    seed: int,
    repos: Sequence[str] = DEFAULT_REPOS,
) -> list[str]:
    """Rank instance ids of one stratum so that any prefix spans as many repos as possible.

    Instances are shuffled within each repo, repos are shuffled, then interleaved
    round-robin. A validation failure is replaced by the next id in this list.
    """
    rng = random.Random(f"{seed}:{difficulty.value}")
    by_repo: dict[str, list[str]] = defaultdict(list)
    for row in sorted(rows, key=lambda r: r["instance_id"]):  # sort: input-order independent
        if row["difficulty"] == difficulty.value and row["repo"] in repos:
            by_repo[row["repo"]].append(row["instance_id"])
    repo_order = sorted(by_repo)
    rng.shuffle(repo_order)
    for repo in repo_order:
        rng.shuffle(by_repo[repo])
    ranked: list[str] = []
    depth = 0
    while any(depth < len(by_repo[r]) for r in repo_order):
        ranked.extend(by_repo[r][depth] for r in repo_order if depth < len(by_repo[r]))
        depth += 1
    return ranked


def select(
    rows: Sequence[Row],
    seed: int,
    per_stratum: int,
    repos: Sequence[str] = DEFAULT_REPOS,
    rejected: Iterable[str] = (),
) -> dict[Difficulty, list[str]]:
    """Take the first `per_stratum` non-rejected candidates of every stratum."""
    rejected_set = set(rejected)
    chosen: dict[Difficulty, list[str]] = {}
    for stratum in STRATA:
        ranked = [i for i in ranked_candidates(rows, stratum, seed, repos) if i not in rejected_set]
        if len(ranked) < per_stratum:
            raise ValueError(f"stratum {stratum.value!r}: only {len(ranked)} candidates")
        chosen[stratum] = ranked[:per_stratum]
    return chosen


def round_robin_order(chosen: Mapping[Difficulty, Sequence[str]]) -> list[tuple[Difficulty, str]]:
    """Interleave strata (easy, medium, hard, easy, ...) so a truncated run stays balanced."""
    n = max(len(v) for v in chosen.values())
    out: list[tuple[Difficulty, str]] = []
    for i in range(n):
        for stratum in STRATA:
            if i < len(chosen[stratum]):
                out.append((stratum, chosen[stratum][i]))
    return out


def build_suite(
    rows: Sequence[Row],
    *,
    version: str,
    dataset_revision: str,
    seed: int,
    per_stratum: int,
    repos: Sequence[str] = DEFAULT_REPOS,
    rejected: Iterable[str] = (),
) -> SuiteManifest:
    by_id = {r["instance_id"]: r for r in rows}
    chosen = select(rows, seed, per_stratum, repos, rejected)
    tasks = [
        TaskSpec(
            instance_id=iid,
            repo=by_id[iid]["repo"],
            base_commit=by_id[iid]["base_commit"],
            difficulty=stratum,
            order=order,
        )
        for order, (stratum, iid) in enumerate(round_robin_order(chosen))
    ]
    return SuiteManifest(version=version, dataset_revision=dataset_revision, seed=seed, tasks=tasks)
