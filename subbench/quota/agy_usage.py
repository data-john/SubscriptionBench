"""Live quota probe driving `agy -p /usage --output-format json` in Docker."""

from __future__ import annotations

import logging
import subprocess

from subbench.provision.images import ensure_probe_image
from subbench.quota.base import (
    QuotaSnapshot,
    force_remove_container,
    parse_usage_json,
    probe_container_name,
    run_with_timeout_retries,
)

logger = logging.getLogger(__name__)


class AgyUsageProbe:
    """Takes quota snapshots via `docker run` running `agy -p /usage --output-format json`."""

    def __init__(
        self,
        host_agy_path: str = "/home/john/.local/bin/agy",
        volume_auth: str = "subbench-agy-auth",
        image_tag: str = "subbench-agy:probe",
        timeout_seconds: int = 90,
        retries: int = 1,
    ) -> None:
        self.host_agy_path = host_agy_path
        self.volume_auth = volume_auth
        self.image_tag = image_tag
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)

    def ensure_image(self) -> str:
        return ensure_probe_image(self.image_tag)

    def build_docker_command(self, container_name: str | None = None) -> list[str]:
        """Build the `docker run` argv for `agy -p /usage --output-format json`."""
        cmd = ["docker", "run", "--rm"]
        if container_name:
            cmd.extend(["--name", container_name])
        cmd.extend(
            [
                "-v",
                f"{self.volume_auth}:/agy-state",
                "-v",
                f"{self.host_agy_path}:/usr/local/bin/agy:ro",
                self.image_tag,
                "/usr/local/bin/entrypoint-agy.sh",
                "/usr/local/bin/agy",
                "-p",
                "/usage",
                "--output-format",
                "json",
            ]
        )
        return cmd

    def _snapshot_once(self) -> QuotaSnapshot:
        cname = probe_container_name("subbench-agy-probe")
        cmd = self.build_docker_command(container_name=cname)
        logger.debug("Running agy usage snapshot command: %s", cmd)
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired:
            force_remove_container(cname)
            raise
        if res.returncode != 0:
            force_remove_container(cname)
            raise RuntimeError(f"agy -p /usage failed (exit code {res.returncode}):\n{res.stderr}")
        return parse_usage_json(res.stdout, provenance="agy_usage")

    def snapshot(self) -> QuotaSnapshot:
        self.ensure_image()
        return run_with_timeout_retries(
            self._snapshot_once,
            retries=self.retries,
            label="agy usage snapshot",
        )
