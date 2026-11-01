"""Network isolation and egress proxy container management."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NETWORK_INTERNAL = "subbench-internal"
NETWORK_EGRESS = "subbench-egress"
PROXY_CONTAINER = "subbench-proxy"


def network_exists(name: str) -> bool:
    res = subprocess.run(
        ["docker", "network", "inspect", name],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


def ensure_networks() -> tuple[str, str]:
    """Ensure subbench-internal (--internal) and subbench-egress bridge networks exist."""
    if not network_exists(NETWORK_INTERNAL):
        logger.info("Creating internal network %s", NETWORK_INTERNAL)
        subprocess.run(
            ["docker", "network", "create", "--internal", NETWORK_INTERNAL],
            check=True,
            capture_output=True,
        )
    if not network_exists(NETWORK_EGRESS):
        logger.info("Creating egress network %s", NETWORK_EGRESS)
        subprocess.run(
            ["docker", "network", "create", NETWORK_EGRESS],
            check=True,
            capture_output=True,
        )
    return NETWORK_INTERNAL, NETWORK_EGRESS


def get_container_ip_on_network(container_name: str, network_name: str) -> str | None:
    res = subprocess.run(
        ["docker", "inspect", container_name],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    try:
        data = json.loads(res.stdout)
        nets = data[0].get("NetworkSettings", {}).get("Networks", {})
        net_info = nets.get(network_name, {})
        return net_info.get("IPAddress")
    except Exception:
        return None


class EgressProxyManager:
    """Manages the subbench-proxy container bridged between egress and internal networks."""

    def __init__(
        self,
        allowlist: set[str],
        log_path: Path,
        port: int = 8080,
    ) -> None:
        self.allowlist = allowlist
        self.log_path = log_path
        self.port = port
        self.container_name = PROXY_CONTAINER

    def start(self) -> dict[str, str]:
        """Start the proxy container and return proxy environment variables for task containers."""
        ensure_networks()
        self.stop()  # Clean up any stale container

        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.touch(exist_ok=True)

        allowlist_str = ",".join(sorted(self.allowlist))

        # Run proxy using Python in container
        cmd = [
            "docker",
            "run",
            "-d",
            "--name",
            self.container_name,
            "--network",
            NETWORK_EGRESS,
            "-v",
            f"{REPO_ROOT}:/app:ro",
            "-v",
            f"{self.log_path.resolve()}:/proxy.log:rw",
            "-w",
            "/app",
            "python:3.12-slim",
            "python3",
            "docker/egress_proxy.py",
            "--host",
            "0.0.0.0",
            "--port",
            str(self.port),
            "--allowlist",
            allowlist_str,
            "--log",
            "/proxy.log",
        ]

        logger.info("Starting proxy container %s", self.container_name)
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to start proxy container:\n{res.stderr}")

        # Connect to internal network as well
        subprocess.run(
            [
                "docker",
                "network",
                "connect",
                "--alias",
                "subbench-proxy",
                NETWORK_INTERNAL,
                self.container_name,
            ],
            check=True,
            capture_output=True,
        )

        # Wait for proxy to begin listening
        import time

        check_script = (
            f"import socket; s = socket.create_connection(('127.0.0.1', {self.port}), timeout=1); "
            "s.close()"
        )
        for _ in range(50):
            check = subprocess.run(
                ["docker", "exec", self.container_name, "python3", "-c", check_script],
                capture_output=True,
                check=False,
            )
            if check.returncode == 0:
                break
            time.sleep(0.1)

        internal_ip = get_container_ip_on_network(self.container_name, NETWORK_INTERNAL)
        proxy_host = internal_ip or "subbench-proxy"
        proxy_url = f"http://{proxy_host}:{self.port}"

        proxy_env = {
            "HTTP_PROXY": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "https_proxy": proxy_url,
            "NO_PROXY": "localhost,127.0.0.1",
        }
        logger.info("Proxy container started. Internal proxy URL: %s", proxy_url)
        return proxy_env

    def stop(self) -> None:
        """Stop and remove the proxy container."""
        subprocess.run(
            ["docker", "rm", "-f", self.container_name],
            capture_output=True,
            check=False,
        )


def launch_task_container(
    image: str,
    container_name: str,
    proxy_env: dict[str, str],
    volume_auth: str = "subbench-agy-auth",
    host_agy_path: str = "/home/john/.local/bin/agy",
) -> str:
    """Launch a task container attached exclusively to subbench-internal with proxy configured."""
    ensure_networks()
    # Remove any existing container with same name
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)

    cmd = [
        "docker",
        "run",
        "-d",
        "--name",
        container_name,
        "--network",
        NETWORK_INTERNAL,
        "-v",
        f"{volume_auth}:/agy-state",
        "-v",
        f"{host_agy_path}:/usr/local/bin/agy:ro",
    ]
    for k, v in proxy_env.items():
        cmd.extend(["-e", f"{k}={v}"])

    # Keep container running with idle command
    cmd.extend([image, "tail", "-f", "/dev/null"])

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"Failed to launch task container {container_name}:\n{res.stderr}")
    return container_name
