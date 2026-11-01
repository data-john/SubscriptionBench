"""Antigravity agent adapter and stream-json parser."""

from __future__ import annotations

import json
import logging
import subprocess
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from subbench.agents.base import AttemptOutcome, ParsedEvent, StreamSummary
from subbench.agents.prompts import RenderedPrompt

logger = logging.getLogger(__name__)

QUOTA_INDICATORS = (
    "quota",
    "exhausted",
    "rate_limit",
    "resource_exhausted",
    "429",
    "capacity",
    "usage limit",
)

INFRA_INDICATORS = (
    "authentication failed",
    "authentication required",
    "docker",
    "connection refused",
    "keyring",
    "dbus",
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "useG1Credits": False,
    "permissions": {
        "allow": [
            "command(*)",
            "write_file(*)",
            "read_file(*)",
            "edit_file(*)",
            "view_file(*)",
        ],
        "deny": [
            "search_web(*)",
            "search_web",
            "read_url(*)",
            "read_url",
            "browser_*",
        ],
    },
}


def parse_stream_json(lines: Iterable[str]) -> StreamSummary:
    """Parse NDJSON lines from agy --output-format stream-json.

    Tolerates partial or truncated final lines (e.g. after SIGTERM/SIGKILL on timeout).
    """
    summary = StreamSummary()
    line_idx = 0

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        line_idx += 1
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            # Tolerates truncated lines on timeout
            logger.debug("Skipping unparseable stream-json line %d: %s", line_idx, stripped[:80])
            continue

        event_type = item.get("event", "unknown")
        parsed = ParsedEvent(idx=line_idx, event=event_type)

        if event_type == "step_update":
            step = item.get("step_update", {})
            parsed.step_type = step.get("step_type")
            parsed.state = step.get("state")
            parsed.tool_name = step.get("tool_name")
            parsed.duration_seconds = step.get("duration_seconds")
            usage = step.get("usage", {})
            parsed.total_tokens = usage.get("total_tokens")

            if parsed.step_type == "tool" and parsed.state == "DONE":
                summary.tool_calls += 1

        elif event_type == "result":
            res = item.get("result", {})
            summary.result_event = res
            status = res.get("status", "")
            summary.finished_reason = res.get("finished_reason") or status
            summary.num_turns = res.get("num_turns")

            usage = res.get("usage", {})
            summary.input_tokens = usage.get("input_tokens")
            summary.output_tokens = usage.get("output_tokens")
            summary.thinking_tokens = usage.get("thinking_tokens")
            summary.cache_read_tokens = usage.get("cache_read_tokens")
            summary.total_tokens = usage.get("total_tokens")

            err_text = str(res.get("error") or res.get("response") or "")
            summary.error_text = err_text if status != "SUCCESS" else None

            lowered_err = err_text.lower()
            if status == "SUCCESS":
                summary.halt_reason = "completed"
            elif any(q in lowered_err for q in QUOTA_INDICATORS):
                summary.halt_reason = "quota_restricted"
            elif any(inf in lowered_err for inf in INFRA_INDICATORS):
                summary.halt_reason = "infra_error"
            else:
                summary.halt_reason = "agent_error"

        summary.events.append(parsed)

    return summary


class AntigravityAdapter:
    """Drives the Antigravity (agy) CLI inside a running Docker container."""

    def __init__(
        self,
        model_id: str,
        effort: str | None = None,
        agy_bin: str = "/usr/local/bin/agy",
        settings: dict[str, Any] | None = None,
        extra_flags: list[str] | None = None,
    ) -> None:
        self.model_id = model_id
        self.effort = effort
        self.agy_bin = agy_bin
        self.settings = settings if settings is not None else DEFAULT_SETTINGS
        self.extra_flags = extra_flags or []

    def get_version(self, container_id: str) -> str | None:
        cmd = [
            "docker",
            "exec",
            container_id,
            "/usr/local/bin/entrypoint-agy.sh",
            self.agy_bin,
            "--version",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
            return res.stdout.strip() if res.returncode == 0 else None
        except Exception:
            return None

    def build_exec_command(self, prompt_text: str) -> list[str]:
        cmd = [
            self.agy_bin,
            "-p",
            prompt_text,
            "--model",
            self.model_id,
            "--output-format",
            "stream-json",
            "--log-file",
            "/tmp/agy-cli.log",
            "--settings",
            json.dumps(self.settings),
        ]
        if self.effort:
            cmd.extend(["--effort", self.effort])
        cmd.extend(self.extra_flags)
        return cmd

    def run_attempt(
        self,
        *,
        instance_id: str,
        prompt: RenderedPrompt,
        container_id: str,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
    ) -> AttemptOutcome:
        ver_pre = self.get_version(container_id)
        inner_cmd = self.build_exec_command(prompt.text)

        exec_cmd = [
            "docker",
            "exec",
            "-i",
            "-e",
            "PATH=/opt/miniconda3/envs/testbed/bin:/usr/local/bin:/usr/bin:/bin",
            "-w",
            "/testbed",
            container_id,
            "/usr/local/bin/entrypoint-agy.sh",
            *inner_cmd,
        ]

        t0 = time.monotonic()
        stdout_lines: list[str] = []
        exit_code: int | None = None
        error_text: str | None = None

        try:
            proc = subprocess.Popen(
                exec_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    stdout_lines.append(line)
                    if raw_output_path:
                        with open(raw_output_path, "a") as f:
                            f.write(line)
                proc.wait(timeout=timeout_seconds)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                logger.warning("Attempt timed out after %ds; killing process", timeout_seconds)
                proc.kill()
                proc.wait()
                exit_code = -9
                error_text = f"Timeout after {timeout_seconds}s"
            if proc.stderr:
                stderr_content = proc.stderr.read()
                if stderr_content and not error_text:
                    error_text = stderr_content[-1000:]
        except Exception as e:
            logger.exception("Failed to run docker exec: %s", e)
            error_text = str(e)
            exit_code = -1

        wall_seconds = time.monotonic() - t0
        ver_post = self.get_version(container_id)

        summary = parse_stream_json(stdout_lines)
        if exit_code == -9 and summary.halt_reason != "completed":
            summary.halt_reason = "timeout"

        return AttemptOutcome(
            halt_reason=summary.halt_reason,
            exit_code=exit_code,
            finished_reason=summary.finished_reason,
            agy_version_pre=ver_pre,
            agy_version_post=ver_post,
            wall_seconds=wall_seconds,
            num_turns=summary.num_turns,
            input_tokens=summary.input_tokens,
            output_tokens=summary.output_tokens,
            thinking_tokens=summary.thinking_tokens,
            cache_read_tokens=summary.cache_read_tokens,
            tool_calls=summary.tool_calls,
            events=summary.events,
            error_text=summary.error_text or error_text,
        )
