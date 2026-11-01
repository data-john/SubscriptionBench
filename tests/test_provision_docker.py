import subprocess
from pathlib import Path

import pytest

from subbench.provision.images import build_overlay_image
from subbench.provision.network import EgressProxyManager, launch_task_container
from subbench.provision.workspace import prepare_workspace


@pytest.mark.docker
def test_docker_provisioning_and_proxy_isolation(tmp_path: Path):
    instance_id = "pytest-dev__pytest-5809"
    base_commit = "8aba863a634f40560e25055d179220f0eefabe9a"
    container_name = "subbench-test-task-container"

    # 1. Build/ensure overlay image
    image_tag = build_overlay_image(instance_id)
    assert image_tag.startswith("subbench-agy:")

    # 2. Start proxy allowlisting only Google Cloud endpoints
    proxy_log = tmp_path / "proxy.log"
    proxy_mgr = EgressProxyManager(
        allowlist={"daily-cloudcode-pa.googleapis.com"},
        log_path=proxy_log,
        port=8080,
    )
    proxy_env = proxy_mgr.start()

    try:
        # 3. Launch task container attached to subbench-internal
        cid = launch_task_container(
            image=image_tag,
            container_name=container_name,
            proxy_env=proxy_env,
        )
        assert cid == container_name

        # 4. Prepare workspace (.git stripping and base commit check)
        initial_sha = prepare_workspace(container_name, base_commit=base_commit)
        assert len(initial_sha) == 40
        assert initial_sha != base_commit

        # Verify only 1 commit in history
        res = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "git",
                "-C",
                "/testbed",
                "rev-list",
                "--count",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert res.stdout.strip() == "1"

        # Verify curl https://github.com is blocked by proxy (403 Forbidden)
        res_curl = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "curl",
                "-v",
                "https://github.com",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res_curl.returncode != 0
        assert "403" in (res_curl.stderr + res_curl.stdout)

        # Verify proxy.log has recorded the blocked attempt
        import json

        log_lines = [json.loads(line) for line in proxy_log.read_text().splitlines()]
        assert any(
            entry.get("host") == "github.com" and entry.get("allowed") is False
            for entry in log_lines
        )

        # Verify testbed environment python works
        res_pytest = subprocess.run(
            [
                "docker",
                "exec",
                container_name,
                "/opt/miniconda3/envs/testbed/bin/pytest",
                "--version",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert "pytest" in res_pytest.stdout or "pytest" in res_pytest.stderr

    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)
        proxy_mgr.stop()
