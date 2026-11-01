"""Workspace provisioning and runtime git history stripping inside the task container."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)


def _exec(container_id: str, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    exec_cmd = ["docker", "exec", container_id, *cmd]
    return subprocess.run(exec_cmd, capture_output=True, text=True, check=False)


def prepare_workspace(
    container_id: str,
    base_commit: str,
    workdir: str = "/testbed",
) -> str:
    """Prepare /testbed inside container: verify base commit and strip .git to <initial_sha>."""
    # 1. Assert git HEAD or HEAD~1 equals base_commit (SWE-bench images have a setup commit)
    res = _exec(container_id, ["git", "-C", workdir, "rev-parse", "HEAD"])
    if res.returncode != 0:
        raise RuntimeError(f"Failed to check git HEAD in {workdir}: {res.stderr}")
    current_head = res.stdout.strip()
    if current_head != base_commit:
        res_parent = _exec(container_id, ["git", "-C", workdir, "rev-parse", "HEAD~1"])
        parent_sha = res_parent.stdout.strip() if res_parent.returncode == 0 else ""
        if parent_sha != base_commit:
            raise RuntimeError(
                f"Base commit mismatch in {workdir}: HEAD={current_head}, "
                f"HEAD~1={parent_sha}, expected {base_commit}"
            )
        # Reset hard to base_commit to ensure clean repository state
        _exec(container_id, ["git", "-C", workdir, "reset", "--hard", base_commit])

    # 2. Strip all history, tags, branches, stashes, and remotes
    script = (
        f"rm -rf {workdir}/.git && "
        f"git -C {workdir} init && "
        f"git -C {workdir} config user.name subbench && "
        f"git -C {workdir} config user.email subbench@local && "
        f"git -C {workdir} add -A && "
        f"git -C {workdir} commit -m 'base' && "
        f"git -C {workdir} rev-parse HEAD"
    )
    res = _exec(container_id, ["/bin/bash", "-c", script])
    if res.returncode != 0:
        raise RuntimeError(f"Failed to strip git history in {workdir}: {res.stderr}")
    initial_sha = res.stdout.strip().splitlines()[-1].strip()

    # 3. Assert clean single-commit git state
    res_count = _exec(container_id, ["git", "-C", workdir, "rev-list", "--count", "HEAD"])
    if res_count.stdout.strip() != "1":
        raise RuntimeError(f"Expected 1 commit in {workdir}, got {res_count.stdout.strip()}")

    res_remotes = _exec(container_id, ["git", "-C", workdir, "remote"])
    if res_remotes.stdout.strip():
        raise RuntimeError(f"Expected no remotes in {workdir}, found: {res_remotes.stdout.strip()}")

    res_tags = _exec(container_id, ["git", "-C", workdir, "tag"])
    if res_tags.stdout.strip():
        raise RuntimeError(f"Expected no tags in {workdir}, found: {res_tags.stdout.strip()}")

    # 4. Editable-install / testbed environment verification
    check_env = (
        "if [ -f /opt/miniconda3/envs/testbed/bin/python ]; then "
        "  /opt/miniconda3/envs/testbed/bin/python -c 'import sys; print(sys.version)' && "
        "  /opt/miniconda3/envs/testbed/bin/pytest --version || true; "
        "fi"
    )
    res_env = _exec(container_id, ["/bin/bash", "-c", check_env])
    if res_env.returncode != 0:
        logger.warning(
            "Testbed python verification returned non-zero in %s: %s",
            container_id,
            res_env.stderr,
        )

    logger.info("Workspace %s prepared cleanly with initial_sha=%s", workdir, initial_sha[:12])
    return initial_sha
