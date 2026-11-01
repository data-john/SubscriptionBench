"""Antigravity agent adapter and stream-json parser."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
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
            "search_web",
            "search_web(*)",
            "read_url",
            "read_url(*)",
            "read_url_content",
            "read_url_content(*)",
            "open_browser_url",
            "open_browser_url(*)",
            "read_browser_page",
            "read_browser_page(*)",
            "capture_browser_screenshot",
            "capture_browser_screenshot(*)",
            "capture_browser_console_logs",
            "click_browser_pixel",
            "execute_browser_javascript",
            "list_browser_pages",
            "browser_subagent",
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
            "--disable-slash-commands",
            "--log-file",
            "/tmp/agy-cli.log",
            "--settings",
            json.dumps(self.settings),
        ]
        if self.effort:
            cmd.extend(["--effort", self.effort])
        cmd.extend(self.extra_flags)
        return cmd

    def build_container_exec_args(self, container_id: str, prompt_text: str) -> list[str]:
        inner_cmd = self.build_exec_command(prompt_text)
        return [
            "docker",
            "exec",
            "-i",
            "-e",
            "PATH=/opt/miniconda3/envs/testbed/bin:/usr/local/bin:/usr/bin:/bin",
            "-e",
            "SUBBENCH_ATTEMPT_RUN=1",
            "-w",
            "/testbed",
            container_id,
            "/usr/local/bin/entrypoint-agy.sh",
            *inner_cmd,
        ]

    def _kill_container_processes(self, container_id: str) -> None:
        """Kill running agy and child processes inside container without killing PID 1."""
        try:
            subprocess.run(
                ["docker", "exec", container_id, "pkill", "-KILL", "-f", "agy"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            subprocess.run(
                ["docker", "exec", container_id, "pkill", "-KILL", "-f", "/testbed"],
                capture_output=True,
                timeout=5,
                check=False,
            )
        except Exception as e:
            logger.warning("Failed to kill container processes in %s: %s", container_id, e)

    def run_attempt(
        self,
        *,
        instance_id: str,
        prompt: RenderedPrompt,
        container_id: str,
        timeout_seconds: int,
        raw_output_path: Path | None = None,
        stderr_output_path: Path | None = None,
    ) -> AttemptOutcome:
        ver_pre = self.get_version(container_id)
        exec_cmd = self.build_container_exec_args(container_id, prompt.text)

        if stderr_output_path:
            try:
                stderr_output_path.parent.mkdir(parents=True, exist_ok=True)
                stderr_output_path.touch(exist_ok=True)
            except Exception as e:
                logger.warning("Failed to initialize stderr file %s: %s", stderr_output_path, e)

        t0 = time.monotonic()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        exit_code: int | None = None
        error_text: str | None = None

        def _read_stream(pipe, lines_dest: list[str], out_path: Path | None) -> None:
            f = None
            if out_path:
                try:
                    out_path.parent.mkdir(parents=True, exist_ok=True)
                    f = open(out_path, "w", encoding="utf-8")
                except Exception as ex:
                    logger.warning("Failed to open %s for raw output: %s", out_path, ex)
            try:
                for line in iter(pipe.readline, ""):
                    lines_dest.append(line)
                    if f:
                        f.write(line)
                        f.flush()
            finally:
                if f:
                    try:
                        f.close()
                    except Exception:
                        pass
                try:
                    pipe.close()
                except Exception:
                    pass

        try:
            proc = subprocess.Popen(
                exec_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            assert proc.stdout is not None
            assert proc.stderr is not None
            t_out = threading.Thread(
                target=_read_stream,
                args=(proc.stdout, stdout_lines, raw_output_path),
                daemon=True,
            )
            t_err = threading.Thread(
                target=_read_stream,
                args=(proc.stderr, stderr_lines, stderr_output_path),
                daemon=True,
            )
            t_out.start()
            t_err.start()

            try:
                proc.wait(timeout=timeout_seconds)
                exit_code = proc.returncode
            except subprocess.TimeoutExpired:
                logger.warning("Attempt timed out after %ds; killing process", timeout_seconds)
                self._kill_container_processes(container_id)
                try:
                    proc.kill()
                    proc.wait(timeout=5)
                except Exception:
                    pass
                exit_code = -9
                error_text = f"Timeout after {timeout_seconds}s"

            t_out.join(timeout=2)
            t_err.join(timeout=2)

            if stderr_lines and not error_text:
                stderr_content = "".join(stderr_lines).strip()
                if stderr_content:
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
        elif summary.result_event is None and exit_code != 0:
            summary.halt_reason = (
                "infra_error"
                if (error_text and any(inf in error_text.lower() for inf in INFRA_INDICATORS))
                else "agent_error"
            )

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
