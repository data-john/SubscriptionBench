"""Patch and artifact capture after an agent execution attempt."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def unstage_binary_files(cmd_prefix: list[str], initial_sha: str) -> list[str]:
    """Identify and unstage binary files to avoid 'Binary files ... differ' in patches."""
    res = subprocess.run(
        [*cmd_prefix, "diff", "--cached", "--numstat", "-z", initial_sha],
        check=True,
        capture_output=True,
    )
    raw = res.stdout
    binary_paths: list[str] = []
    for entry in raw.split(b"\x00"):
        if not entry:
            continue
        parts = entry.split(b"\t", 2)
        if len(parts) >= 3 and parts[0] == b"-" and parts[1] == b"-":
            binary_paths.append(parts[2].decode("utf-8", errors="replace"))

    if binary_paths:
        logger.info("Excluding %d binary files from patch: %s", len(binary_paths), binary_paths[:5])
        for i in range(0, len(binary_paths), 100):
            chunk = binary_paths[i : i + 100]
            subprocess.run(
                [*cmd_prefix, "reset", "-q", initial_sha, "--", *chunk],
                check=True,
                capture_output=True,
            )
    return binary_paths


def capture_patch_in_repo(repo_dir: Path, initial_sha: str) -> tuple[str, int]:
    """Capture git patch against initial_sha including staged, unstaged, and committed edits.

    Runs `git add -A`, unstages binary files, then `git diff --cached --no-color <initial_sha>`.
    """
    cmd_prefix = ["git", "-C", str(repo_dir)]
    subprocess.run(
        [*cmd_prefix, "add", "-A"],
        check=True,
        capture_output=True,
    )
    unstage_binary_files(cmd_prefix, initial_sha)
    res = subprocess.run(
        [*cmd_prefix, "diff", "--cached", "--no-color", initial_sha],
        check=True,
        capture_output=True,
        text=True,
    )
    patch_text = res.stdout
    patch_bytes = len(patch_text.encode("utf-8"))
    return patch_text, patch_bytes


def capture_patch_in_container(
    container_id: str, initial_sha: str, workdir: str = "/testbed"
) -> tuple[str, int]:
    """Capture patch inside a running Docker container against initial_sha."""
    cmd_prefix = ["docker", "exec", container_id, "git", "-C", workdir]
    subprocess.run(
        [*cmd_prefix, "add", "-A"],
        check=True,
        capture_output=True,
    )
    unstage_binary_files(cmd_prefix, initial_sha)
    res = subprocess.run(
        [*cmd_prefix, "diff", "--cached", "--no-color", initial_sha],
        check=True,
        capture_output=True,
        text=True,
    )
    patch_text = res.stdout
    patch_bytes = len(patch_text.encode("utf-8"))
    return patch_text, patch_bytes


def copy_container_file(container_id: str, container_path: str, local_dest: Path) -> bool:
    """Copy a file from inside a container to a local path."""
    local_dest.parent.mkdir(parents=True, exist_ok=True)
    res = subprocess.run(
        ["docker", "cp", f"{container_id}:{container_path}", str(local_dest)],
        capture_output=True,
        check=False,
    )
    return res.returncode == 0


def save_attempt_artifacts(
    artifacts_dir: Path,
    patch_text: str,
    log_files: dict[str, Path | str | None] | None = None,
) -> Path:
    """Save model.patch and associated logs to artifacts_dir."""
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    patch_path = artifacts_dir / "model.patch"
    patch_path.write_text(patch_text, encoding="utf-8")

    if log_files:
        for name, src in log_files.items():
            if src is None:
                continue
            src_path = Path(src)
            if src_path.exists():
                dst = artifacts_dir / name
                if src_path.resolve() != dst.resolve():
                    shutil.copy2(src_path, dst)

    return patch_path
