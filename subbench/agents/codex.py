"""Codex CLI agent adapter and JSONL event parser."""

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

CODEX_QUOTA_INDICATORS = (
    "usage limit",
    "usage_limit",
    "rate limit",
    "rate_limit",
    "429",
    "quota",
    "exhausted",
    "upgrade to plus",
    "purchase more credits",
    "insufficient_quota",
    "credits_depleted",
)

CODEX_INFRA_INDICATORS = (
    "authentication",
    "unauthorized",
    "401",
    "token_invalidated",
    "not_chatgpt_auth",
    "no codex credentials",
    "connection refused",
    "docker",
)

TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "file_change",
        "mcp_tool_call",
        "web_search",
        "dynamic_tool_call",
    }
)


def _classify_codex_error(err_text: str) -> str:
    lowered = err_text.lower()
    if any(q in lowered for q in CODEX_QUOTA_INDICATORS):
        return "quota_restricted"
    if any(inf in lowered for inf in CODEX_INFRA_INDICATORS):
        return "infra_error"
    return "agent_error"


def parse_codex_jsonl(lines: Iterable[str]) -> StreamSummary:
    """Parse NDJSON lines from `codex exec --json`.

    Tolerates partial or truncated final lines (e.g. after SIGKILL on timeout).
    """
    summary = StreamSummary()
    line_idx = 0
    turn_count = 0
    completed_seen = False
    failed_seen = False
    last_error: str | None = None

    in_tokens = 0
    cached_tokens = 0
    cache_write_tokens = 0
    has_cache_write = False
    out_tokens = 0
    has_usage = False

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped:
            continue
        line_idx += 1
        try:
            item = json.loads(stripped)
        except json.JSONDecodeError:
            logger.debug("Skipping unparseable codex jsonl line %d: %s", line_idx, stripped[:80])
            continue

        if not isinstance(item, dict):
            continue

        event_type = str(item.get("type") or "unknown")
        parsed = ParsedEvent(idx=line_idx, event=event_type)

        if event_type == "turn.started":
            turn_count += 1

        elif event_type == "turn.completed":
            completed_seen = True
            usage = item.get("usage")
            if isinstance(usage, dict):
                has_usage = True
                t_in = int(usage.get("input_tokens") or 0)
                t_cached = int(usage.get("cached_input_tokens") or 0)
                t_out = int(usage.get("output_tokens") or 0)
                if usage.get("cache_write_input_tokens") is not None:
                    has_cache_write = True
                    cache_write_tokens += int(usage.get("cache_write_input_tokens") or 0)
                in_tokens += t_in
                cached_tokens += t_cached
                out_tokens += t_out
                parsed.total_tokens = t_in + t_out
            parsed.state = "DONE"

        elif event_type == "turn.failed":
            failed_seen = True
            err = item.get("error")
            if isinstance(err, dict):
                last_error = str(err.get("message") or json.dumps(err))
            elif err is not None:
                last_error = str(err)
            parsed.state = "ERROR"

        elif event_type == "error":
            msg = item.get("message")
            if msg:
                last_error = str(msg)
            parsed.state = "ERROR"

        elif event_type in ("item.started", "item.updated", "item.completed"):
            inner = item.get("item")
            if isinstance(inner, dict):
                item_type = str(inner.get("type") or "unknown")
                is_tool = item_type in TOOL_ITEM_TYPES
                parsed.step_type = "tool" if is_tool else item_type
                parsed.tool_name = item_type if is_tool else None

                if event_type == "item.started":
                    parsed.state = "ACTIVE"
                elif event_type == "item.completed":
                    parsed.state = "ERROR" if inner.get("status") == "failed" else "DONE"
                    if is_tool:
                        summary.tool_calls += 1
                else:
                    parsed.state = "ACTIVE"

        summary.events.append(parsed)

    if turn_count > 0:
        summary.num_turns = turn_count
    elif completed_seen:
        summary.num_turns = 1

    if has_usage:
        summary.input_tokens = in_tokens
        summary.cache_read_tokens = cached_tokens
        summary.cache_write_tokens = cache_write_tokens if has_cache_write else None
        summary.output_tokens = out_tokens
        summary.total_tokens = in_tokens + out_tokens

    if failed_seen:
        summary.finished_reason = "FAILED"
        summary.error_text = last_error or "Codex turn failed"
        summary.halt_reason = _classify_codex_error(summary.error_text)
    elif completed_seen:
        summary.halt_reason = "completed"
        summary.finished_reason = "SUCCESS"
        summary.result_event = {"status": "SUCCESS"}
    elif last_error:
        summary.error_text = last_error
        summary.finished_reason = "ERROR"
        summary.halt_reason = _classify_codex_error(last_error)

    return summary


class CodexAdapter:
    """Drives the Codex (`codex exec`) CLI inside a running Docker container."""

    def __init__(
        self,
        model_id: str,
        effort: str | None = None,
        codex_bin: str = "/usr/local/bin/codex",
        codex_home: str = "/codex-state",
        extra_flags: list[str] | None = None,
    ) -> None:
        self.model_id = model_id
        self.effort = effort
        self.codex_bin = codex_bin
        self.codex_home = codex_home
        self.extra_flags = extra_flags or []

    def get_version(self, container_id: str) -> str | None:
        cmd = [
            "docker",
            "exec",
            "-e",
            f"CODEX_HOME={self.codex_home}",
            container_id,
            self.codex_bin,
            "--version",
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=10)
            if res.returncode != 0:
                return None
            lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
            return lines[-1] if lines else None
        except Exception:
            return None

    def build_exec_command(self) -> list[str]:
        cmd = [
            self.codex_bin,
            "exec",
            "-m",
            self.model_id,
            "-c",
            'web_search="disabled"',
            "--disable",
            "plugins",
            "--disable",
            "apps",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--json",
            "-C",
            "/testbed",
        ]
        if self.effort:
            cmd.extend(["-c", f'model_reasoning_effort="{self.effort}"'])
        cmd.extend(self.extra_flags)
        cmd.append("-")
        return cmd

    def build_container_exec_args(self, container_id: str) -> list[str]:
        inner_cmd = self.build_exec_command()
        return [
            "docker",
            "exec",
            "-i",
            "-e",
            f"CODEX_HOME={self.codex_home}",
            "-e",
            "PATH=/opt/miniconda3/envs/testbed/bin:/usr/local/bin:/usr/bin:/bin",
            "-e",
            "SUBBENCH_ATTEMPT_RUN=1",
            "-w",
            "/testbed",
            container_id,
            *inner_cmd,
        ]

    def _kill_container_processes(self, container_id: str) -> None:
        """Kill running codex and child processes inside container without killing PID 1."""
        try:
            subprocess.run(
                ["docker", "exec", container_id, "pkill", "-KILL", "-f", "codex"],
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
        exec_cmd = self.build_container_exec_args(container_id)

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

        def _read_stream(pipe: Any, lines_dest: list[str], out_path: Path | None) -> None:
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
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

            assert proc.stdin is not None
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
                proc.stdin.write(prompt.text)
                if not prompt.text.endswith("\n"):
                    proc.stdin.write("\n")
                proc.stdin.flush()
                proc.stdin.close()
            except BrokenPipeError:
                pass

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

        summary = parse_codex_jsonl(stdout_lines)
        if exit_code == -9 and summary.halt_reason != "completed":
            summary.halt_reason = "timeout"
        elif summary.halt_reason != "completed" and exit_code != 0:
            combined_err = summary.error_text or error_text or ""
            summary.halt_reason = (
                _classify_codex_error(combined_err) if combined_err else "agent_error"
            )
        elif summary.result_event is None and summary.halt_reason == "timeout":
            # No turn.completed and exit_code == 0 -> unexpected early exit
            combined_err = summary.error_text or error_text or ""
            summary.halt_reason = (
                _classify_codex_error(combined_err) if combined_err else "agent_error"
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
            cache_write_tokens=summary.cache_write_tokens,
        )
