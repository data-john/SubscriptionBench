"""Tests for transcript and proxy egress leak auditor."""

from __future__ import annotations

import json
from pathlib import Path

from subbench.audit import (
    audit_cycle,
    audit_ndjson_events,
    audit_proxy_log,
)
from subbench.store import connect, init_db

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "calibration_sample"


def test_audit_real_calibration_sample_fixture_has_zero_leaks():
    """Verify that the real calibration attempt produces zero false-positive leak findings."""
    ndjson_path = FIXTURE_DIR / "stream_json.ndjson"
    proxy_path = FIXTURE_DIR / "proxy.log"

    assert ndjson_path.exists()
    assert proxy_path.exists()

    ndjson_lines = ndjson_path.read_text(encoding="utf-8").splitlines()
    findings_ndjson = audit_ndjson_events(
        ndjson_lines, task_repo="pytest-dev/pytest", task_issue="pytest-dev__pytest-5809"
    )
    # Zero leak findings on legitimate passive file reads (e.g. CHANGELOG.rst or pastebin.py)
    leaks_ndjson = [f for f in findings_ndjson if f.severity == "leak"]
    assert len(leaks_ndjson) == 0

    findings_proxy = audit_proxy_log(proxy_path)
    # Zero leak findings on proxy log (only Google APIs allowed, benign play.googleapis.com blocked)
    leaks_proxy = [f for f in findings_proxy if f.severity == "leak"]
    assert len(leaks_proxy) == 0


def test_audit_web_tool_call_leak_and_warn():
    # 1. Successful web tool call -> leak
    leak_line = json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "search_web",
                "state": "DONE",
            },
        }
    )
    findings = audit_ndjson_events([leak_line])
    assert len(findings) == 1
    assert findings[0].kind == "web_tool_call"
    assert findings[0].severity == "leak"

    # 2. Denied web tool call -> warn
    warn_line = json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "step_type": "tool",
                "tool_name": "read_url",
                "state": "ERROR",
            },
        }
    )
    findings_warn = audit_ndjson_events([warn_line])
    assert len(findings_warn) == 1
    assert findings_warn[0].severity == "warn"


def test_audit_outbound_url_command_leak():
    # Command downloading pull request diff from github
    cmd = "curl -sL https://github.com/pytest-dev/pytest/pull/5809.diff | git apply"
    cmd_line = json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "tool_name": "command",
                "args": {"command": cmd},
            },
        }
    )
    findings = audit_ndjson_events([cmd_line], task_repo="pytest-dev/pytest")
    assert len(findings) == 1
    assert findings[0].kind == "outbound_url_command"
    assert findings[0].severity == "leak"


def test_audit_git_history_probe_leak():
    # Agent probes git log and output contains 3 commits
    multi_commit_output = (
        "commit 8aba863a634f40560e25055d179220f0eefabe9a\n"
        "commit 1111111111111111111111111111111111111111\n"
        "commit 2222222222222222222222222222222222222222\n"
    )
    line = json.dumps(
        {
            "event": "step_update",
            "step_update": {
                "tool_name": "run_command",
                "args": {"command": "git log --oneline"},
                "output": multi_commit_output,
            },
        }
    )
    findings = audit_ndjson_events([line])
    assert len(findings) == 1
    assert findings[0].kind == "git_history_probe"
    assert findings[0].severity == "leak"


def test_audit_proxy_log_allowed_and_blocked(tmp_path: Path):
    proxy_log = tmp_path / "proxy.log"
    lines = [
        # Approved host allowed
        json.dumps({"host": "www.googleapis.com", "allowed": True}),
        # Unapproved host allowed -> LEAK
        json.dumps({"host": "evil-hacker-site.com", "allowed": True}),
        # Code host blocked -> WARN
        json.dumps({"host": "github.com", "allowed": False}),
        # Benign noise blocked -> INFO
        json.dumps({"host": "play.googleapis.com", "allowed": False}),
    ]
    proxy_log.write_text("\n".join(lines) + "\n")

    findings = audit_proxy_log(proxy_log)
    severities = {f.kind: f.severity for f in findings}
    assert severities["proxy_allowed_host"] == "leak"
    assert severities["proxy_blocked_host"] in ("warn", "info")


def test_audit_cycle_db_idempotency(tmp_path: Path):
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

    # Create dummy attempt
    att_cur = conn.execute(
        """
        INSERT INTO attempts (cycle_id, config_id, instance_id, pass_index, seq, state, valid)
        VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, 'GRADED', 1)
        """,
        (cycle_id, cfg_id),
    )
    att_id = att_cur.lastrowid
    conn.commit()

    # First audit run
    res1 = audit_cycle(conn, cycle_id)
    assert res1["attempts_audited"] == 1

    # Second audit run - verify idempotency (no duplicate entries in audit_findings)
    res2 = audit_cycle(conn, cycle_id)
    assert res2["total_findings"] == res1["total_findings"]

    query = "SELECT count(*) FROM audit_findings WHERE attempt_id = ?"
    rows = conn.execute(query, (att_id,)).fetchone()
    assert rows[0] == res1["total_findings"]
