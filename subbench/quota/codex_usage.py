"""Live quota probe driving `codex app-server --stdio` (`account/rateLimits/read`) in Docker."""

from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
from datetime import UTC, datetime
from typing import Any, Literal

from subbench.provision.images import ensure_probe_image
from subbench.quota.base import (
    BucketReading,
    QuotaSnapshot,
    force_remove_container,
    probe_container_name,
    run_with_timeout_retries,
)

logger = logging.getLogger(__name__)

DEFAULT_CODEX_PATH = "/usr/lib/chatgpt/resources/codex"


def _window_label(window_mins: int | None, slot_name: str) -> str:
    if window_mins is None:
        return slot_name
    if window_mins == 300:
        return "5h"
    if window_mins == 10080:
        return "weekly"
    if window_mins == 43200:
        return "30d"
    if window_mins > 0 and window_mins % 1440 == 0:
        return f"{window_mins // 1440}d"
    if window_mins > 0 and window_mins % 60 == 0:
        return f"{window_mins // 60}h"
    return f"{window_mins}m"


def _format_reset_time(resets_at: int | float | str | None) -> str | None:
    if resets_at is None:
        return None
    if isinstance(resets_at, str):
        return resets_at
    dt = datetime.fromtimestamp(float(resets_at), tz=UTC)
    return dt.isoformat().replace("+00:00", "Z")


def _check_codex_credits(snapshot_obj: dict[str, Any]) -> None:
    if snapshot_obj.get("spendControlReached") is True:
        logger.warning("Codex rate limit reports spendControlReached=True")
    credits = snapshot_obj.get("credits")
    if isinstance(credits, dict):
        if credits.get("hasCredits") or credits.get("unlimited"):
            logger.warning("Codex rate limit reports non-free credits: %s", credits)
        bal = credits.get("balance")
        if bal not in (None, "", "0", 0, 0.0):
            logger.warning("Codex rate limit reports non-zero credit balance: %s", bal)


def parse_codex_rate_limits_json(
    raw_stdout: str,
    taken_at: str | None = None,
    provenance: Literal["codex_usage", "manual"] = "codex_usage",
) -> QuotaSnapshot:
    """Parse JSON-RPC response from `account/rateLimits/read` into a QuotaSnapshot."""
    try:
        data = json.loads(raw_stdout)
    except Exception as e:
        raise ValueError(f"Failed to parse Codex rate-limits JSON: {e}") from e

    if isinstance(data, dict) and "error" in data and data["error"] is not None:
        raise ValueError(f"Codex account/rateLimits/read returned error: {data['error']}")

    result = data.get("result") if isinstance(data, dict) and "result" in data else data
    if not isinstance(result, dict):
        raise ValueError("Codex rate-limits payload is not a JSON object")

    by_limit = result.get("rateLimitsByLimitId")
    snapshots_by_key: list[tuple[str, dict[str, Any]]] = []
    if isinstance(by_limit, dict) and by_limit:
        for k, v in by_limit.items():
            if isinstance(v, dict):
                snapshots_by_key.append((str(k), v))
    elif isinstance(result.get("rateLimits"), dict):
        rl = result["rateLimits"]
        snapshots_by_key.append((str(rl.get("limitId") or "codex"), rl))

    buckets: dict[str, BucketReading] = {}
    for key, rl in snapshots_by_key:
        _check_codex_credits(rl)
        limit_id = str(rl.get("limitId") or key or "codex")
        for slot_name in ("primary", "secondary"):
            win = rl.get(slot_name)
            if not isinstance(win, dict) or "usedPercent" not in win:
                continue
            window_label = _window_label(win.get("windowDurationMins"), slot_name)
            bucket_id = f"{limit_id}-{window_label}"
            used_pct = float(win["usedPercent"])
            remaining_fraction = max(0.0, min(1.0, (100.0 - used_pct) / 100.0))
            reset_time = _format_reset_time(win.get("resetsAt"))
            buckets[bucket_id] = BucketReading(
                bucket_id=bucket_id,
                window=window_label,
                remaining_fraction=remaining_fraction,
                reset_time=reset_time,
            )

    if not buckets:
        raise ValueError("No quota buckets found in Codex rate-limits response")

    ts = taken_at or datetime.now(UTC).isoformat()
    return QuotaSnapshot(
        buckets=buckets,
        taken_at=ts,
        provenance=provenance,
        raw_json=raw_stdout,
    )


class CodexUsageProbe:
    """Takes quota snapshots via `codex app-server --stdio` inside Docker."""

    def __init__(
        self,
        host_codex_path: str = DEFAULT_CODEX_PATH,
        volume_auth: str = "subbench-codex-auth",
        image_tag: str = "subbench-agy:probe",
        timeout_seconds: int = 45,
        retries: int = 1,
    ) -> None:
        self.host_codex_path = host_codex_path
        self.volume_auth = volume_auth
        self.image_tag = image_tag
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)

    def ensure_image(self) -> str:
        return ensure_probe_image(self.image_tag)

    def build_docker_command(self, container_name: str | None = None) -> list[str]:
        cmd = ["docker", "run", "--rm"]
        if container_name:
            cmd.extend(["--name", container_name])
        cmd.extend(
            [
                "-i",
                "-v",
                f"{self.volume_auth}:/codex-state",
                "-v",
                f"{self.host_codex_path}:/usr/local/bin/codex:ro",
                "-e",
                "CODEX_HOME=/codex-state",
                "--entrypoint",
                "/usr/local/bin/codex",
                self.image_tag,
                "app-server",
                "--stdio",
            ]
        )
        return cmd

    def _snapshot_once(self) -> QuotaSnapshot:
        cname = probe_container_name("subbench-codex-probe")
        cmd = self.build_docker_command(container_name=cname)
        logger.debug("Running codex usage snapshot command: %s", cmd)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None

        responses: dict[int, dict[str, Any]] = {}
        raw_rate_limits_line: str | None = None
        deadline = time.monotonic() + self.timeout_seconds
        timed_out_event = threading.Event()

        def _on_timeout() -> None:
            timed_out_event.set()
            force_remove_container(cname)
            try:
                proc.kill()
            except Exception:
                pass

        timer = threading.Timer(self.timeout_seconds, _on_timeout)
        timer.daemon = True
        timer.start()

        try:
            init_req = {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "subbench-probe", "version": "0.1.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
            proc.stdin.write(json.dumps(init_req) + "\n")
            proc.stdin.flush()

            while 1 not in responses and time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    if isinstance(msg, dict) and "id" in msg:
                        responses[int(msg["id"])] = msg
                except json.JSONDecodeError:
                    continue

            if 1 not in responses:
                if timed_out_event.is_set() or time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"Timed out waiting for codex app-server initialize "
                        f"after {self.timeout_seconds}s"
                    )
                raise RuntimeError("codex app-server exited before initialize response")

            proc.stdin.write(json.dumps({"method": "initialized", "params": {}}) + "\n")
            proc.stdin.write(
                json.dumps({"id": 2, "method": "account/rateLimits/read", "params": None}) + "\n"
            )
            proc.stdin.flush()

            while 2 not in responses and time.monotonic() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                    if isinstance(msg, dict) and msg.get("id") == 2:
                        responses[2] = msg
                        raw_rate_limits_line = line.strip()
                        break
                except json.JSONDecodeError:
                    continue
        finally:
            timer.cancel()
            try:
                proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            if timed_out_event.is_set() or raw_rate_limits_line is None:
                force_remove_container(cname)

        if raw_rate_limits_line is None:
            if timed_out_event.is_set() or time.monotonic() >= deadline:
                force_remove_container(cname)
                raise TimeoutError(
                    f"Timed out waiting for codex account/rateLimits/read "
                    f"after {self.timeout_seconds}s"
                )
            stderr_txt = proc.stderr.read().strip() if proc.stderr else ""
            raise RuntimeError(
                f"codex account/rateLimits/read failed (rc={proc.returncode}): {stderr_txt}"
            )

        return parse_codex_rate_limits_json(raw_rate_limits_line, provenance="codex_usage")

    def snapshot(self) -> QuotaSnapshot:
        self.ensure_image()
        return run_with_timeout_retries(
            self._snapshot_once,
            retries=self.retries,
            label="codex usage snapshot",
        )
