import subprocess
from pathlib import Path

from subbench.provision.capture import capture_patch_in_repo, save_attempt_artifacts


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(path), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "test"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    (path / "file.txt").write_text("initial content\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    res = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return res.stdout.strip()


def test_capture_empty_diff(tmp_path: Path):
    repo = tmp_path / "repo"
    initial_sha = _init_repo(repo)
    patch, patch_bytes = capture_patch_in_repo(repo, initial_sha)
    assert patch == ""
    assert patch_bytes == 0


def test_capture_unstaged_and_untracked_changes(tmp_path: Path):
    repo = tmp_path / "repo"
    initial_sha = _init_repo(repo)

    # Modify existing file
    (repo / "file.txt").write_text("modified content\n")
    # Add new untracked file
    (repo / "new_file.py").write_text("print('hello')\n")

    patch, patch_bytes = capture_patch_in_repo(repo, initial_sha)
    assert patch_bytes > 0
    assert "diff --git a/file.txt b/file.txt" in patch
    assert "+modified content" in patch
    assert "diff --git a/new_file.py b/new_file.py" in patch
    assert "+print('hello')" in patch


def test_capture_preserves_agent_commits(tmp_path: Path):
    repo = tmp_path / "repo"
    initial_sha = _init_repo(repo)

    # Agent makes a commit
    (repo / "file.txt").write_text("agent edit\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "agent committed changes"],
        check=True,
        capture_output=True,
    )

    # Agent also leaves some unstaged edits after commit
    (repo / "another.txt").write_text("unstaged after commit\n")

    patch, patch_bytes = capture_patch_in_repo(repo, initial_sha)
    assert patch_bytes > 0
    assert "agent edit" in patch
    assert "unstaged after commit" in patch


def test_save_attempt_artifacts(tmp_path: Path):
    artifacts = tmp_path / "artifacts"
    log1 = tmp_path / "test.log"
    log1.write_text("dummy log")

    patch_path = save_attempt_artifacts(
        artifacts,
        "diff --git ...",
        log_files={"test.log": log1, "missing.log": None},
    )
    assert patch_path.exists()
    assert patch_path.read_text() == "diff --git ..."
    assert (artifacts / "test.log").exists()
    assert (artifacts / "test.log").read_text() == "dummy log"
