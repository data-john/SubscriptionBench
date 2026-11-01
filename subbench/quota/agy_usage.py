"""Live quota probe driving `agy -p /usage --output-format json` in Docker."""

from __future__ import annotations

import logging
import subprocess

from subbench.provision.images import ensure_probe_image
from subbench.quota.base import QuotaSnapshot, parse_usage_json

logger = logging.getLogger(__name__)


class AgyUsageProbe:
    """Takes quota snapshots via `docker run` running `agy -p /usage --output-format json`."""

    def __init__(
        self,
        host_agy_path: str = "/home/john/.local/bin/agy",
        volume_auth: str = "subbench-agy-auth",
        image_tag: str = "subbench-agy:probe",
        timeout_seconds: int = 30,
    ) -> None:
        self.host_agy_path = host_agy_path
        self.volume_auth = volume_auth
        self.image_tag = image_tag
        self.timeout_seconds = timeout_seconds

    def ensure_image(self) -> str:
        return ensure_probe_image(self.image_tag)

    def snapshot(self) -> QuotaSnapshot:
        self.ensure_image()
        cmd = [
            "docker",
            "run",
            "--rm",
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
        logger.debug("Running agy usage snapshot command: %s", cmd)
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(f"agy -p /usage failed (exit code {res.returncode}):\n{res.stderr}")
        return parse_usage_json(res.stdout, provenance="agy_usage")
