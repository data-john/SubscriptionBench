"""Offline unit and integration tests for Codex adapter, quota probe, audit, and runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from subbench.agents.codex import CodexAdapter, parse_codex_jsonl
from subbench.agents.fake import FakeAgentAdapter
from subbench.audit import audit_cycle, audit_ndjson_events, audit_proxy_log
from subbench.config import ExperimentManifest, load_experiment
from subbench.provision.network import CODEX_EGRESS_ALLOWLIST, launch_task_container
from subbench.quota import (
    BucketReading,
    CodexUsageProbe,
    QuotaSnapshot,
    parse_codex_rate_limits_json,
)
from subbench.report import generate_report
from subbench.runner import FakeProvisioner, Runner
from subbench.store import connect, init_db

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", ["codex-free-pilot.yaml", "codex-free-core.yaml"])
def test_committed_codex_manifests_validate(name: str) -> None:
    exp = load_experiment(ROOT / "experiments" / name)
    assert exp.agent == "codex"
    assert exp.codex_path == "/usr/lib/chatgpt/resources/codex"
    assert exp.binary_path == exp.codex_path
    assert exp.configs[0].buckets == ["codex-30d"]


def test_codex_manifest_validation_errors() -> None:
    base = {
        "name": "cx",
        "agent": "codex",
        "suite": "s.json",
        "codex_path": "/usr/bin/codex",
        "configs": [
            {
                "label": "c1",
                "role": "default",
                "model_id": "gpt-6-luna",
                "buckets": ["codex-30d"],
            }
        ],
    }
    ExperimentManifest.model_validate(base)

    with pytest.raises(ValidationError, match="codex_path is required"):
        ExperimentManifest.model_validate(base | {"codex_path": None})

    with pytest.raises(ValidationError, match="codex_path must be absolute"):
        ExperimentManifest.model_validate(base | {"codex_path": "codex"})


def test_parse_codex_rate_limits_json_free_plan() -> None:
    payload = {
        "id": 2,
        "result": {
            "ordinaryUsageAllowed": True,
            "rateLimits": {
                "limitId": "codex",
                "primary": {
                    "usedPercent": 22,
                    "windowDurationMins": 43200,
                    "resetsAt": 1795876811,
                },
                "secondary": None,
                "credits": {"hasCredits": False, "unlimited": False, "balance": None},
                "spendControlReached": False,
                "planType": "free",
            },
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 22,
                        "windowDurationMins": 43200,
                        "resetsAt": 1795876811,
                    },
                    "secondary": None,
                    "credits": {"hasCredits": False, "unlimited": False, "balance": None},
                    "spendControlReached": False,
                    "planType": "free",
                }
            },
        },
    }
    snap = parse_codex_rate_limits_json(json.dumps(payload), taken_at="2026-11-18T12:00:00Z")
    assert snap.provenance == "codex_usage"
    assert "codex-30d" in snap.buckets
    b = snap.buckets["codex-30d"]
    assert b.window == "30d"
    assert abs(b.remaining_fraction - 0.78) < 1e-9
    assert b.reset_time is not None and b.reset_time.endswith("Z")


def test_parse_codex_rate_limits_json_5h_and_weekly() -> None:
    payload = {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "primary": {"usedPercent": 15, "windowDurationMins": 300, "resetsAt": 1795876811},
                "secondary": {
                    "usedPercent": 40,
                    "windowDurationMins": 10080,
                    "resetsAt": 1796400000,
                },
            }
        }
    }
    snap = parse_codex_rate_limits_json(json.dumps(payload))
    assert abs(snap.buckets["codex-5h"].remaining_fraction - 0.85) < 1e-9
    assert snap.buckets["codex-5h"].window == "5h"
    assert abs(snap.buckets["codex-weekly"].remaining_fraction - 0.60) < 1e-9
    assert snap.buckets["codex-weekly"].window == "weekly"


def test_codex_probe_command_shape() -> None:
    probe = CodexUsageProbe(host_codex_path="/opt/codex")
    cmd = probe.build_docker_command()
    assert "subbench-codex-auth:/codex-state" in cmd
    assert "/opt/codex:/usr/local/bin/codex:ro" in cmd
    assert "CODEX_HOME=/codex-state" in cmd
    assert cmd[-2:] == ["app-server", "--stdio"]


def test_parse_codex_jsonl_success_and_truncated() -> None:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "th_1"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "item.started",
                "item": {"id": "it_1", "type": "command_execution", "command": "pytest"},
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "it_1",
                    "type": "command_execution",
                    "command": "pytest",
                    "exit_code": 0,
                    "status": "completed",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "it_2", "type": "file_change", "status": "completed"},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 200},
            }
        ),
        '{"type": "item.started", "truncated',
    ]
    summary = parse_codex_jsonl(lines)
    assert summary.halt_reason == "completed"
    assert summary.finished_reason == "SUCCESS"
    assert summary.num_turns == 1
    assert summary.tool_calls == 2
    assert summary.input_tokens == 1000
    assert summary.cache_read_tokens == 400
    assert summary.output_tokens == 200
    assert summary.total_tokens == 1200


def test_parse_codex_jsonl_quota_error() -> None:
    lines = [
        json.dumps({"type": "thread.started", "thread_id": "th_2"}),
        json.dumps({"type": "turn.started"}),
        json.dumps(
            {
                "type": "turn.failed",
                "error": {
                    "message": (
                        "You've hit your usage limit. Upgrade to Plus to continue using Codex"
                    )
                },
            }
        ),
    ]
    summary = parse_codex_jsonl(lines)
    assert summary.halt_reason == "quota_restricted"
    assert summary.finished_reason == "FAILED"
    assert "usage limit" in (summary.error_text or "").lower()


def test_codex_adapter_exec_command_flags() -> None:
    adapter = CodexAdapter(model_id="gpt-6-luna", effort="medium")
    cmd = adapter.build_exec_command()
    assert cmd[:4] == ["/usr/local/bin/codex", "exec", "-m", "gpt-6-luna"]
    assert 'web_search="disabled"' in cmd
    assert "--disable" in cmd
    assert "plugins" in cmd
    assert "apps" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert "--ephemeral" in cmd
    assert "--json" in cmd
    assert 'model_reasoning_effort="medium"' in cmd
    assert cmd[-1] == "-"


def test_launch_task_container_mounts_codex_helpers(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return type("Res", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr("subbench.provision.network.ensure_networks", lambda: ("i", "e"))
    monkeypatch.setattr("subbench.provision.network.subprocess.run", fake_run)

    launch_task_container(
        image="subbench-agy:test",
        container_name="c1",
        proxy_env={"HTTPS_PROXY": "http://proxy:8080"},
        volume_auth="subbench-codex-auth",
        host_agy_path="/usr/lib/chatgpt/resources/codex",
        state_mount="/codex-state",
        container_bin_path="/usr/local/bin/codex",
        extra_ro_mounts={
            "/usr/lib/chatgpt/resources/codex-code-mode-host": "/usr/local/bin/codex-code-mode-host"
        },
    )
    run_cmd = calls[-1]
    assert "/usr/lib/chatgpt/resources/codex:/usr/local/bin/codex:ro" in run_cmd
    assert (
        "/usr/lib/chatgpt/resources/codex-code-mode-host:/usr/local/bin/codex-code-mode-host:ro"
        in run_cmd
    )


def test_audit_codex_ndjson_and_proxy_allowlist(tmp_path: Path) -> None:
    lines = [
        json.dumps(
            {
                "type": "item.started",
                "item": {
                    "id": "cmd_1",
                    "type": "command_execution",
                    "command": "curl https://github.com/pytest-dev/pytest/pull/1.diff",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "id": "cmd_1",
                    "type": "command_execution",
                    "command": "curl https://github.com/pytest-dev/pytest/pull/1.diff",
                    "aggregated_output": "diff --git",
                },
            }
        ),
        json.dumps(
            {
                "type": "item.completed",
                "item": {"id": "ws_1", "type": "web_search", "query": "pytest bug"},
            }
        ),
    ]
    findings = audit_ndjson_events(lines, task_repo="pytest-dev/pytest")
    kinds = {(f.kind, f.severity) for f in findings}
    assert ("outbound_url_command", "leak") in kinds
    assert ("web_tool_call", "leak") in kinds
    assert len(findings) == 2

    proxy_log = tmp_path / "proxy.log"
    proxy_entries = [
        {"host": "chatgpt.com", "port": 443, "allowed": True},
        {"host": "auth.openai.com", "port": 443, "allowed": True},
        {"host": "ab.chatgpt.com", "port": 443, "allowed": False},
    ]
    proxy_log.write_text("\n".join(json.dumps(e) for e in proxy_entries) + "\n")
    p_findings = audit_proxy_log(proxy_log, allowlist=CODEX_EGRESS_ALLOWLIST)
    assert [f for f in p_findings if f.severity == "leak"] == []


def test_codex_runner_and_report_e2e(tmp_path: Path) -> None:
    suite_path = ROOT / "suites" / "verified-core-6.json"
    manifest_data = {
        "name": "codex-test",
        "agent": "codex",
        "suite": str(suite_path),
        "codex_path": "/usr/lib/chatgpt/resources/codex",
        "passes": 1,
        "task_limit": 2,
        "configs": [
            {
                "label": "gpt-6-luna-medium",
                "role": "default",
                "model_id": "gpt-6-luna",
                "effort": "medium",
                "buckets": ["codex-30d"],
            }
        ],
        "quota": {
            "idle_check_seconds": 0,
            "primary_bucket_by_config": {"gpt-6-luna-medium": "codex-30d"},
            "weekly_bucket_by_config": {"gpt-6-luna-medium": "codex-30d"},
        },
    }
    m_path = tmp_path / "codex-manifest.yaml"
    m_path.write_text(json.dumps(manifest_data))

    class FakeCodexQuotaProbe:
        def __init__(self) -> None:
            self.rem = 0.78

        def snapshot(self) -> QuotaSnapshot:
            snap = QuotaSnapshot(
                buckets={"codex-30d": BucketReading("codex-30d", "30d", self.rem, "2026-11-29Z")},
                taken_at=datetime.now(UTC).isoformat(),
                provenance="codex_usage",
                raw_json="{}",
            )
            self.rem = max(0.0, self.rem - 0.01)
            return snap

    db_path = tmp_path / "subbench.sqlite"
    art_dir = tmp_path / "artifacts"
    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=FakeCodexQuotaProbe(),
        adapter_factory=lambda cfg: FakeAgentAdapter(halt_reason="completed"),
        provisioner=FakeProvisioner(),
        skip_idle_check=True,
        problem_statements={
            "pytest-dev__pytest-5809": "Fix pytest issue",
            "psf__requests-2931": "Fix requests issue",
        },
        artifacts_dir=art_dir,
    )
    cycle_id = runner.run()
    assert cycle_id == 1

    conn = connect(db_path)
    init_db(conn)
    audit_res = audit_cycle(conn, cycle_id=cycle_id)
    assert audit_res["leak"] == 0

    out_dir = tmp_path / "report"
    outputs = generate_report(conn, cycle_id=cycle_id, out_dir=out_dir)
    md = Path(outputs["summary_md"]).read_text(encoding="utf-8")
    assert "Quota Pool Disclosure (Codex)" in md
    assert "codex-30d" in md
