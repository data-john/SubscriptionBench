"""Transcript and proxy egress leak auditor for SWE-bench task attempts."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from subbench.provision.network import DEFAULT_EGRESS_ALLOWLIST

logger = logging.getLogger(__name__)

Severity = Literal["info", "warn", "leak"]

WEB_TOOLS = frozenset(
    {
        "search_web",
        "read_url",
        "read_url_content",
        "open_browser_url",
        "read_browser_page",
        "capture_browser_screenshot",
        "capture_browser_console_logs",
        "click_browser_pixel",
        "execute_browser_javascript",
        "list_browser_pages",
        "browser_subagent",
    }
)

NETWORK_COMMAND_PATTERNS = [
    r"\bcurl\b",
    r"\bwget\b",
    r"\bgit\s+clone\b",
    r"\bgit\s+fetch\b",
    r"\bgit\s+remote\s+add\b",
    r"\bpip\s+install\b",
    r"\burllib\b",
    r"\brequests\.(get|post)\b",
    r"\bhttp\.client\b",
]

CODE_QA_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "api.github.com",
        "gitlab.com",
        "stackoverflow.com",
        "huggingface.co",
    }
)

BENIGN_PROXY_HOSTS = frozenset(
    {
        "play.googleapis.com",
        "lh3.googleusercontent.com",
        "doesnotexist.google.com",
        "10.255.255.1",
    }
)


@dataclass(frozen=True)
class Finding:
    kind: str
    severity: Severity
    evidence: str
    event_idx: int | None = None


def audit_ndjson_events(
    lines: list[str],
    task_repo: str = "",
    task_issue: str = "",
) -> list[Finding]:
    """Audit NDJSON transcript lines for web tools, network commands, and git probes."""
    findings: list[Finding] = []
    line_idx = 0

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        line_idx += 1
        try:
            item = json.loads(stripped)
        except Exception:
            continue

        event_type = item.get("event")
        if event_type == "step_update":
            step = item.get("step_update", {})
            tool_name = step.get("tool_name", "")
            state = step.get("state", "")

            # 1. Web / browser tool check
            if tool_name in WEB_TOOLS or tool_name.startswith("browser_"):
                # If completed with output -> leak; if failed/denied -> warn
                severity: Severity = "leak" if state == "DONE" else "warn"
                findings.append(
                    Finding(
                        kind="web_tool_call",
                        severity=severity,
                        evidence=f"Web tool call: {tool_name} (state={state})",
                        event_idx=line_idx,
                    )
                )

            # 2. Outbound network commands inside run_command / command
            if tool_name in ("command", "run_command"):
                args = step.get("args") or step.get("command") or {}
                cmd_text = ""
                if isinstance(args, dict):
                    cmd_text = str(args.get("command") or args.get("CommandLine") or "")
                elif isinstance(args, str):
                    cmd_text = args

                if cmd_text:
                    cmd_lower = cmd_text.lower()
                    has_net_cmd = any(re.search(pat, cmd_lower) for pat in NETWORK_COMMAND_PATTERNS)
                    has_url = any(u in cmd_lower for u in ("http://", "https://", "git@"))
                    is_local = any(loc in cmd_lower for loc in ("localhost", "127.0.0.1", "::1"))

                    if has_net_cmd and has_url and not is_local:
                        # Check if referencing task repo, PR, or code host
                        is_code_host = any(h in cmd_lower for h in CODE_QA_HOSTS)
                        is_task_ref = (task_repo and task_repo.lower() in cmd_lower) or (
                            task_issue and task_issue.lower() in cmd_lower
                        )
                        sev: Severity = "leak" if (is_code_host or is_task_ref) else "warn"
                        findings.append(
                            Finding(
                                kind="outbound_url_command",
                                severity=sev,
                                evidence=f"Network command targeting external URL: {cmd_text[:80]}",
                                event_idx=line_idx,
                            )
                        )

                    # 3. Git history probes
                    is_git_probe = any(
                        g in cmd_lower
                        for g in (
                            "git log",
                            "git reflog",
                            "git branch -a",
                            "git tag",
                            "git remote -v",
                        )
                    )
                    if is_git_probe:
                        output_str = str(step.get("output") or step.get("stdout") or "")
                        # Count commit hashes in output
                        commit_count = len(re.findall(r"\b[0-9a-f]{40}\b", output_str))
                        if commit_count > 1:
                            findings.append(
                                Finding(
                                    kind="git_history_probe",
                                    severity="leak",
                                    evidence=(
                                        f"Git history probe revealed {commit_count} commits: "
                                        f"{cmd_text[:80]}"
                                    ),
                                    event_idx=line_idx,
                                )
                            )
                        else:
                            findings.append(
                                Finding(
                                    kind="git_history_probe",
                                    severity="info",
                                    evidence=f"Single-commit git inspection: {cmd_text[:80]}",
                                    event_idx=line_idx,
                                )
                            )

    return findings


def audit_proxy_log(
    proxy_log_path: Path,
    allowlist: frozenset[str] = DEFAULT_EGRESS_ALLOWLIST,
) -> list[Finding]:
    """Audit proxy.log for unapproved allowed hosts or blocked code host attempts."""
    if not proxy_log_path.exists():
        return []

    findings: list[Finding] = []
    seen_blocked: set[str] = set()

    for line in proxy_log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except Exception:
            continue

        host = entry.get("host", "").lower()
        allowed = bool(entry.get("allowed", False))

        if allowed:
            if host not in allowlist:
                findings.append(
                    Finding(
                        kind="proxy_allowed_host",
                        severity="leak",
                        evidence=f"Proxy allowed unapproved outbound host: {host}",
                    )
                )
        else:
            if host in seen_blocked:
                continue
            seen_blocked.add(host)

            if host in CODE_QA_HOSTS:
                findings.append(
                    Finding(
                        kind="proxy_blocked_host",
                        severity="warn",
                        evidence=f"Proxy blocked outbound probe to code/QA host: {host}",
                    )
                )
            elif (
                host in BENIGN_PROXY_HOSTS
                or host.startswith("antigravity-cli-auto-updater-")
                or host.startswith("192.0.2.")
                or host.startswith("198.51.100.")
                or host.startswith("203.0.113.")
            ):
                findings.append(
                    Finding(
                        kind="proxy_blocked_host",
                        severity="info",
                        evidence=f"Proxy blocked benign background/test host: {host}",
                    )
                )
            else:
                findings.append(
                    Finding(
                        kind="proxy_blocked_host",
                        severity="info",
                        evidence=f"Proxy blocked external host: {host}",
                    )
                )

    return findings


def audit_attempt(
    conn: sqlite3.Connection,
    attempt_id: int,
    transcript_path: Path | None = None,
    proxy_log_path: Path | None = None,
    task_repo: str = "",
    task_issue: str = "",
) -> list[Finding]:
    """Audit a single attempt and record findings into SQLite."""
    findings: list[Finding] = []

    # 1. Audit transcript
    if transcript_path and transcript_path.exists():
        lines = transcript_path.read_text(encoding="utf-8", errors="replace").splitlines()
        findings.extend(audit_ndjson_events(lines, task_repo=task_repo, task_issue=task_issue))

    # 2. Audit proxy log
    if proxy_log_path and proxy_log_path.exists():
        findings.extend(audit_proxy_log(proxy_log_path))

    # Idempotent write: delete previous findings for this attempt
    conn.execute("DELETE FROM audit_findings WHERE attempt_id = ?", (attempt_id,))
    now_iso = datetime.now(UTC).isoformat()
    for f in findings:
        conn.execute(
            """
            INSERT INTO audit_findings (
                attempt_id, kind, severity, evidence, event_idx, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                attempt_id,
                f.kind,
                f.severity,
                f.evidence,
                f.event_idx,
                now_iso,
            ),
        )
    conn.commit()
    return findings


def audit_cycle(
    conn: sqlite3.Connection,
    cycle_id: int,
) -> dict[str, Any]:
    """Audit all attempts in a cycle."""
    cur = conn.execute(
        """
        SELECT id, transcript_path, instance_id
        FROM attempts
        WHERE cycle_id = ?
        ORDER BY id
        """,
        (cycle_id,),
    )
    rows = cur.fetchall()

    total_findings = 0
    leak_count = 0
    warn_count = 0
    info_count = 0

    for att_id, t_path, inst_id in rows:
        trans_p = Path(t_path) if t_path else None
        proxy_p: Path | None = None
        if trans_p and trans_p.parent.exists():
            candidate = trans_p.parent / "proxy.log"
            if candidate.exists():
                proxy_p = candidate

        findings = audit_attempt(
            conn,
            attempt_id=att_id,
            transcript_path=trans_p,
            proxy_log_path=proxy_p,
            task_repo="",
            task_issue=inst_id,
        )
        total_findings += len(findings)
        for f in findings:
            if f.severity == "leak":
                leak_count += 1
            elif f.severity == "warn":
                warn_count += 1
            else:
                info_count += 1

    summary = {
        "cycle_id": cycle_id,
        "attempts_audited": len(rows),
        "total_findings": total_findings,
        "leak": leak_count,
        "warn": warn_count,
        "info": info_count,
    }
    logger.info("Audit summary for cycle %d: %s", cycle_id, summary)
    return summary
