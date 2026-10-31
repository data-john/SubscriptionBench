import itertools

import pytest

from subbench.config import Difficulty
from subbench.tasks.curate import (
    DEFAULT_REPOS,
    STRATA,
    build_suite,
    ranked_candidates,
    round_robin_order,
    select,
)


def make_rows():
    rows = []
    for repo in DEFAULT_REPOS:
        for diff in Difficulty:
            for i in range(5):
                rows.append(
                    {
                        "instance_id": f"{repo.replace('/', '__')}-{diff.name}-{i}",
                        "repo": repo,
                        "difficulty": diff.value,
                        "base_commit": "c" * 40,
                    }
                )
    rows.append(
        {
            "instance_id": "other-1",
            "repo": "x/y",
            "difficulty": Difficulty.EASY.value,
            "base_commit": "d",
        }
    )
    rows.append(
        {
            "instance_id": "huge-1",
            "repo": "django/django",
            "difficulty": ">4 hours",
            "base_commit": "d",
        }
    )
    return rows


def test_ranking_is_deterministic_and_input_order_independent():
    rows = make_rows()
    a = ranked_candidates(rows, Difficulty.EASY, 7)
    b = ranked_candidates(list(reversed(rows)), Difficulty.EASY, 7)
    assert a == b
    assert a != ranked_candidates(rows, Difficulty.EASY, 8)


def test_ranking_excludes_other_repos_and_strata():
    ranked = ranked_candidates(make_rows(), Difficulty.EASY, 1)
    assert "other-1" not in ranked and "huge-1" not in ranked
    assert all("EASY" in i for i in ranked)


def test_prefix_spans_distinct_repos():
    rows = make_rows()
    by_id = {r["instance_id"]: r["repo"] for r in rows}
    top = ranked_candidates(rows, Difficulty.MEDIUM, 3)[: len(DEFAULT_REPOS)]
    assert len({by_id[i] for i in top}) == len(DEFAULT_REPOS)


def test_rejected_candidates_replaced_from_same_stratum():
    rows = make_rows()
    first = select(rows, 3, 4)
    victim = first[Difficulty.HARD][0]
    second = select(rows, 3, 4, rejected=[victim])
    assert victim not in second[Difficulty.HARD]
    assert second[Difficulty.EASY] == first[Difficulty.EASY]
    assert second[Difficulty.HARD][:3] == first[Difficulty.HARD][1:4]


def test_not_enough_candidates_raises():
    with pytest.raises(ValueError):
        select(make_rows(), 1, 100)


def test_round_robin_balanced_prefixes():
    chosen = select(make_rows(), 1, 4)
    order = round_robin_order(chosen)
    assert len(order) == 12
    for i in range(0, 12, 3):
        assert [s for s, _ in order[i : i + 3]] == list(STRATA)


def test_build_suite_shape():
    suite = build_suite(make_rows(), version="v1", dataset_revision="a" * 40, seed=1, per_stratum=4)
    assert [t.order for t in suite.ordered()] == list(range(12))
    counts = {d: sum(t.difficulty == d for t in suite.tasks) for d in STRATA}
    assert set(counts.values()) == {4}
    assert len({t.repo for t in suite.tasks}) >= 4
    assert (
        suite.digest()
        == build_suite(
            make_rows(), version="v1", dataset_revision="a" * 40, seed=1, per_stratum=4
        ).digest()
    )


def test_9_task_suite():
    suite = build_suite(make_rows(), version="v", dataset_revision="a" * 40, seed=1, per_stratum=3)
    assert len(suite.tasks) == 9
    assert list(itertools.islice((t.difficulty for t in suite.ordered()), 3)) == list(STRATA)
