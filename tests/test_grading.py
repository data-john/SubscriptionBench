"""Tests for offline batch grading and unique instance_id batching."""

from __future__ import annotations

import json
from pathlib import Path

from subbench.grading import (
    grade_attempt_empty,
    grade_cycle,
    parse_instance_report,
    partition_into_unique_instance_batches,
)
from subbench.store import connect, init_db


def test_partition_into_unique_instance_batches():
    # 6 attempts across 3 unique instance IDs
    attempts = [
        {"id": 1, "instance_id": "pytest-dev__pytest-5809", "patch_content": "diff 1"},
        {"id": 2, "instance_id": "psf__requests-2931", "patch_content": "diff 2"},
        {"id": 3, "instance_id": "pytest-dev__pytest-5809", "patch_content": "diff 3"},
        {"id": 4, "instance_id": "psf__requests-2931", "patch_content": "diff 4"},
        {"id": 5, "instance_id": "pytest-dev__pytest-5809", "patch_content": "diff 5"},
        {"id": 6, "instance_id": "django__django-11000", "patch_content": "diff 6"},
    ]

    batches = partition_into_unique_instance_batches(attempts)
    assert len(batches) == 3

    for b in batches:
        inst_ids = [x["instance_id"] for x in b]
        assert len(set(inst_ids)) == len(inst_ids), f"Duplicate instance_ids in batch: {inst_ids}"


def test_grade_attempt_empty(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_db(conn)

    # Insert test experiment, cycle, config, and attempt
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

    att_cur = conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid, patch_bytes
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, 'QUOTA_POST', 1, 0)
        """,
        (cycle_id, cfg_id),
    )
    att_id = att_cur.lastrowid
    conn.commit()

    grade_attempt_empty(conn, att_id)

    # Verify attempt transitioned to GRADED
    att_row = conn.execute("SELECT state FROM attempts WHERE id = ?", (att_id,)).fetchone()
    assert att_row[0] == "GRADED"

    # Verify grades row
    grade_row = conn.execute(
        "SELECT resolved, patch_applied, error_text FROM grades WHERE attempt_id = ?",
        (att_id,),
    ).fetchone()
    assert grade_row[0] == 0
    assert grade_row[1] == 0
    assert grade_row[2] == "empty_patch"


def test_parse_instance_report(tmp_path: Path):
    rep_file = tmp_path / "report.json"
    data = {
        "pytest-dev__pytest-5809": {
            "patch_successfully_applied": True,
            "resolved": True,
            "tests_status": {
                "FAIL_TO_PASS": {"success": ["test_fix"], "failure": []},
                "PASS_TO_PASS": {"success": ["test_prev"], "failure": []},
            },
        }
    }
    rep_file.write_text(json.dumps(data))

    resolved, applied, f2p, p2p = parse_instance_report(rep_file, "pytest-dev__pytest-5809")
    assert resolved is True
    assert applied is True
    assert "test_fix" in f2p
    assert "test_prev" in p2p


def test_grade_cycle_end_to_end_mock_harness(tmp_path: Path):
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

    # Create 1 empty-patch attempt and 1 non-empty patch attempt
    patch_file = tmp_path / "model.patch"
    patch_file.write_text("diff --git a/test.py b/test.py\n+resolved\n")

    conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid, patch_bytes
        ) VALUES (?, ?, 'empty-inst', 0, 1, 'QUOTA_POST', 1, 0)
        """,
        (cycle_id, cfg_id),
    )
    conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid,
            patch_path, patch_bytes
        ) VALUES (?, ?, 'resolved-inst', 0, 2, 'QUOTA_POST', 1, ?, 100)
        """,
        (cycle_id, cfg_id, str(patch_file)),
    )
    conn.commit()

    def fake_harness(dataset_jsonl, predictions, ids, run_id, workdir, max_workers):
        # Create summary report
        summary = {
            "resolved_ids": ["resolved-inst"],
            "unresolved_ids": [],
            "error_ids": [],
            "empty_patch_ids": [],
        }
        # Model name is subbench-c{cycle_id}-b0
        model_name = f"subbench-c{cycle_id}-b0"
        (workdir / f"{model_name}.{run_id}.json").write_text(json.dumps(summary))
        return type("Proc", (), {"returncode": 0})()

    res = grade_cycle(
        conn,
        cycle_id=cycle_id,
        harness_fn=fake_harness,
        workdir=tmp_path / "grading",
    )
    assert res["graded"] == 2
    assert res["resolved"] == 1

    # Check database state
    attempts = conn.execute("SELECT id, state FROM attempts ORDER BY id").fetchall()
    assert all(a[1] == "GRADED" for a in attempts)


def test_grade_cycle_preserves_exact_patch_whitespace_and_trailing_context_line(tmp_path: Path):
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

    # Patch ending in an empty context line (' \n')
    raw_patch = (
        "diff --git a/test.py b/test.py\n"
        "--- a/test.py\n"
        "+++ b/test.py\n"
        "@@ -1,3 +1,3 @@\n"
        " context_before\n"
        "-old\n"
        "+new\n"
        " \n"
    )
    patch_file = tmp_path / "test_trailing_space.patch"
    patch_file.write_text(raw_patch, encoding="utf-8")

    conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid,
            patch_path, patch_bytes
        ) VALUES (?, ?, 'inst-trailing-space', 0, 1, 'QUOTA_POST', 1, ?, ?)
        """,
        (cycle_id, cfg_id, str(patch_file), len(raw_patch.encode("utf-8"))),
    )
    conn.commit()

    captured_predictions: list[dict] = []

    def fake_harness(dataset_jsonl, predictions, ids, run_id, workdir, max_workers):
        for line in predictions.read_text(encoding="utf-8").splitlines():
            if line.strip():
                captured_predictions.append(json.loads(line))
        summary = {
            "resolved_ids": [],
            "unresolved_ids": ids,
            "error_ids": [],
            "empty_patch_ids": [],
        }
        model_name = f"subbench-c{cycle_id}-b0"
        (workdir / f"{model_name}.{run_id}.json").write_text(json.dumps(summary))
        return type("Proc", (), {"returncode": 0})()

    grade_cycle(
        conn,
        cycle_id=cycle_id,
        harness_fn=fake_harness,
        workdir=tmp_path / "grading",
    )

    assert len(captured_predictions) == 1
    assert captured_predictions[0]["model_patch"] == raw_patch
    assert captured_predictions[0]["model_patch"].endswith(" \n")
