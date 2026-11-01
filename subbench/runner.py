"""Sequential resumable runner for SubscriptionBench task cycles."""

from __future__ import annotations

import hashlib
import json
import logging
import platform
import shutil
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from subbench.agents.antigravity import AntigravityAdapter
from subbench.agents.base import AgentAdapter, AttemptOutcome
from subbench.agents.prompts import TEMPLATES, render_prompt
from subbench.config import (
    ConfigSpec,
    ExperimentManifest,
    SuiteManifest,
    TaskSpec,
    load_experiment,
    load_suite,
)
from subbench.provision.capture import (
    capture_patch_in_container,
    copy_container_file,
    save_attempt_artifacts,
)
from subbench.provision.images import build_overlay_image
from subbench.provision.network import (
    DEFAULT_EGRESS_ALLOWLIST,
    EgressProxyManager,
    launch_task_container,
)
from subbench.provision.workspace import prepare_workspace
from subbench.quota import (
    AgyUsageProbe,
    BucketReading,
    QuotaProbe,
    QuotaSnapshot,
    check_idle,
    compute_attempt_deltas,
    detect_external_use,
    save_quota_snapshot,
)
from subbench.store import connect, init_db

logger = logging.getLogger(__name__)
DEFAULT_DB = Path("data/subbench.sqlite")


class DockerTaskEnvironment:
    """Manages container lifecycle and patch capture for a single attempt."""

    def __init__(
        self,
        container_id: str,
        initial_sha: str,
        proxy_mgr: EgressProxyManager,
        proxy_log: Path,
        attempt_dir: Path,
    ) -> None:
        self.container_id = container_id
        self.initial_sha = initial_sha
        self.proxy_mgr = proxy_mgr
        self.proxy_log = proxy_log
        self.attempt_dir = attempt_dir

    def capture_patch(self) -> tuple[str, int]:
        return capture_patch_in_container(self.container_id, self.initial_sha)

    def cleanup(self) -> None:
        try:
            copy_container_file(
                self.container_id, "/tmp/agy-cli.log", self.attempt_dir / "agy-cli.log"
            )
        except Exception as e:
            logger.debug("Could not copy in-container agy-cli.log: %s", e)
        try:
            self.proxy_mgr.stop()
        except Exception as e:
            logger.debug("Failed stopping proxy: %s", e)
        try:
            import subprocess

            cmd = ["docker", "rm", "-f", self.container_id]
            subprocess.run(cmd, capture_output=True, check=False)
        except Exception as e:
            logger.debug("Failed removing container %s: %s", self.container_id, e)


class DockerProvisioner:
    """Provisions real Docker containers with egress allowlist proxy."""

    def __init__(
        self,
        host_agy_path: str = "/home/john/.local/bin/agy",
        allowlist: set[str] | frozenset[str] = DEFAULT_EGRESS_ALLOWLIST,
    ) -> None:
        self.host_agy_path = host_agy_path
        self.allowlist = set(allowlist)

    def provision(
        self, instance_id: str, base_commit: str, attempt_dir: Path
    ) -> DockerTaskEnvironment:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        proxy_log = attempt_dir / "proxy.log"
        proxy_mgr = EgressProxyManager(allowlist=self.allowlist, log_path=proxy_log)
        proxy_env = proxy_mgr.start()

        image_tag = build_overlay_image(instance_id)
        clean_inst = instance_id.replace("__", "-").replace("_", "-")
        container_name = f"subbench-task-{clean_inst}"
        cid = launch_task_container(
            image=image_tag,
            container_name=container_name,
            proxy_env=proxy_env,
            host_agy_path=self.host_agy_path,
        )
        initial_sha = prepare_workspace(cid, base_commit=base_commit)
        return DockerTaskEnvironment(cid, initial_sha, proxy_mgr, proxy_log, attempt_dir)


class FakeTaskEnvironment:
    """Lightweight offline test environment producing a predetermined patch."""

    def __init__(
        self,
        attempt_dir: Path,
        patch_text: str = "diff --git a/test.py b/test.py\n+print('fixed')\n",
    ) -> None:
        self.attempt_dir = attempt_dir
        self.patch_text = patch_text
        self.container_id = "fake-container"
        self.initial_sha = "initial_sha_12345"

    def capture_patch(self) -> tuple[str, int]:
        return self.patch_text, len(self.patch_text.encode("utf-8"))

    def cleanup(self) -> None:
        (self.attempt_dir / "proxy.log").touch(exist_ok=True)
        (self.attempt_dir / "agy-cli.log").write_text("fake agy log\n")


class FakeProvisioner:
    """Provisions offline FakeTaskEnvironments without Docker."""

    def __init__(
        self,
        patch_text: str = "diff --git a/test.py b/test.py\n+print('fixed')\n",
    ) -> None:
        self.patch_text = patch_text

    def provision(
        self, instance_id: str, base_commit: str, attempt_dir: Path
    ) -> FakeTaskEnvironment:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        (attempt_dir / "proxy.log").write_text("")
        return FakeTaskEnvironment(attempt_dir, self.patch_text)


def generate_attempt_schedule(
    manifest: ExperimentManifest,
    suite: SuiteManifest,
) -> list[tuple[int, int, ConfigSpec, TaskSpec]]:
    """Generate the pass-interleaved sequence of attempts.

    Returns list of (seq, pass_index, config, task).
    """
    schedule: list[tuple[int, int, ConfigSpec, TaskSpec]] = []
    seq = 0
    tasks = suite.ordered()
    if manifest.task_limit is not None:
        tasks = tasks[: manifest.task_limit]

    for pass_index in range(manifest.passes):
        for config in manifest.configs:
            if config.max_passes is not None and pass_index >= config.max_passes:
                continue
            for task in tasks:
                seq += 1
                schedule.append((seq, pass_index, config, task))
    return schedule


def add_cycle_flag(conn: sqlite3.Connection, cycle_id: int, flag: str) -> None:
    """Append a flag string idempotently to cycles.flags_json."""
    cur = conn.execute("SELECT flags_json FROM cycles WHERE id = ?", (cycle_id,))
    row = cur.fetchone()
    flags = json.loads(row[0]) if row and row[0] else []
    if flag not in flags:
        flags.append(flag)
        conn.execute(
            "UPDATE cycles SET flags_json = ? WHERE id = ?",
            (json.dumps(flags), cycle_id),
        )
        conn.commit()


def record_experiment(
    conn: sqlite3.Connection,
    manifest: ExperimentManifest,
    suite: SuiteManifest,
) -> int:
    cur = conn.execute(
        "SELECT id FROM experiments WHERE name = ? AND manifest_digest = ?",
        (manifest.name, manifest.digest()),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    now_iso = datetime.now(UTC).isoformat()
    cur = conn.execute(
        """
        INSERT INTO experiments (
            name, manifest_json, manifest_digest, suite_version, suite_digest, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            manifest.name,
            manifest.canonical_json(),
            manifest.digest(),
            suite.version,
            suite.digest(),
            now_iso,
        ),
    )
    conn.commit()
    return cur.lastrowid


def record_environment(
    conn: sqlite3.Connection,
    experiment_id: int,
    manifest: ExperimentManifest,
    suite: SuiteManifest,
    agy_version: str | None = None,
    docker_version: str | None = None,
    swebench_version: str | None = None,
) -> None:
    now_iso = datetime.now(UTC).isoformat()
    cur = conn.execute("SELECT id FROM environment WHERE experiment_id = ?", (experiment_id,))
    if cur.fetchone():
        return

    real_agy = agy_version
    if real_agy is None:
        if Path(manifest.agy_path).is_file():
            try:
                import subprocess

                res = subprocess.run(
                    [manifest.agy_path, "--version"],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if res.returncode == 0 and res.stdout.strip():
                    real_agy = res.stdout.strip()
            except Exception:
                pass
        real_agy = real_agy or "v1.4.12"

    real_docker = docker_version
    if real_docker is None:
        try:
            import subprocess

            res = subprocess.run(
                ["docker", "--version"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                real_docker = res.stdout.strip()
        except Exception:
            pass
        real_docker = real_docker or "Docker"

    real_swebench = swebench_version
    if real_swebench is None:
        try:
            import importlib.metadata

            real_swebench = importlib.metadata.version("swebench")
        except Exception:
            real_swebench = "unknown"

    tmpl_text = TEMPLATES.get(manifest.prompt_template, manifest.prompt_template)
    prompt_hash = hashlib.sha256(tmpl_text.encode("utf-8")).hexdigest()

    conn.execute(
        """
        INSERT INTO environment (
            experiment_id, agy_path, agy_version, os, kernel,
            docker_version, swebench_version, dataset, dataset_revision,
            prompt_hash, config_state_hash, recorded_at, execution_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'container')
        """,
        (
            experiment_id,
            manifest.agy_path,
            real_agy,
            platform.system(),
            platform.release(),
            real_docker,
            real_swebench,
            suite.dataset,
            suite.dataset_revision,
            prompt_hash,
            manifest.digest()[:16],
            now_iso,
        ),
    )
    conn.commit()


def record_configs(
    conn: sqlite3.Connection,
    experiment_id: int,
    manifest: ExperimentManifest,
) -> dict[str, int]:
    config_ids: dict[str, int] = {}
    for c in manifest.configs:
        cur = conn.execute(
            "SELECT id FROM configs WHERE experiment_id = ? AND label = ?",
            (experiment_id, c.label),
        )
        row = cur.fetchone()
        if row:
            config_ids[c.label] = row[0]
        else:
            cur = conn.execute(
                """
                INSERT INTO configs (
                    experiment_id, label, role, model_id, effort, buckets_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    experiment_id,
                    c.label,
                    c.role.value,
                    c.model_id,
                    c.effort,
                    json.dumps(c.buckets),
                ),
            )
            config_ids[c.label] = cur.lastrowid
    conn.commit()
    return config_ids


def get_or_create_cycle(
    conn: sqlite3.Connection,
    experiment_id: int,
    cycle_id: int | None = None,
) -> int:
    if cycle_id is not None:
        cur = conn.execute("SELECT id, status FROM cycles WHERE id = ?", (cycle_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"Cycle id {cycle_id} not found in database")
        return row[0]
    cur = conn.execute(
        """
        SELECT id FROM cycles
        WHERE experiment_id = ? AND status = 'running'
        ORDER BY id DESC LIMIT 1
        """,
        (experiment_id,),
    )
    row = cur.fetchone()
    if row:
        logger.info("Resuming existing running cycle %d", row[0])
        return row[0]
    now_iso = datetime.now(UTC).isoformat()
    cur = conn.execute(
        "INSERT INTO cycles (experiment_id, started_at, status) VALUES (?, ?, 'running')",
        (experiment_id, now_iso),
    )
    conn.commit()
    return cur.lastrowid


def get_cumulative_weekly_burn(
    conn: sqlite3.Connection,
    cycle_id: int,
    config_id: int,
    weekly_bucket: str,
) -> float:
    """Calculate cumulative valid=1 quota consumed on a weekly bucket for a config in a cycle."""
    cur = conn.execute(
        """
        SELECT q_pre.remaining_fraction, q_post.remaining_fraction
        FROM attempts a
        JOIN quota_snapshots q_pre
            ON a.id = q_pre.attempt_id AND q_pre.phase = 'pre' AND q_pre.bucket_id = ?
        JOIN quota_snapshots q_post
            ON a.id = q_post.attempt_id AND q_post.phase = 'post' AND q_post.bucket_id = ?
        WHERE a.cycle_id = ? AND a.config_id = ? AND a.valid = 1
        """,
        (weekly_bucket, weekly_bucket, cycle_id, config_id),
    )
    total_burn = 0.0
    for pre_rem, post_rem in cur.fetchall():
        diff = pre_rem - post_rem
        if diff > 0:
            total_burn += diff
    return total_burn


class Runner:
    """Resumable sequential runner executing an ExperimentManifest over a SuiteManifest."""

    def __init__(
        self,
        manifest_path: Path,
        db_path: Path = DEFAULT_DB,
        quota_probe: QuotaProbe | None = None,
        adapter_factory: Callable[[ConfigSpec], AgentAdapter] | None = None,
        provisioner: Any | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        now_fn: Callable[[], datetime] = lambda: datetime.now(UTC),
        skip_idle_check: bool = False,
        min_free_gb: float = 15.0,
        problem_statements: Mapping[str, str] | None = None,
        artifacts_dir: Path = Path("artifacts"),
    ) -> None:
        self.manifest_path = manifest_path
        self.db_path = db_path
        self.manifest = load_experiment(manifest_path)
        self.suite = load_suite(Path(self.manifest.suite))
        self.quota_probe = quota_probe or AgyUsageProbe(host_agy_path=self.manifest.agy_path)
        self.adapter_factory = adapter_factory or (
            lambda cfg: AntigravityAdapter(
                model_id=cfg.model_id,
                effort=cfg.effort,
                agy_bin="/usr/local/bin/agy",
            )
        )
        self.provisioner = provisioner or DockerProvisioner(host_agy_path=self.manifest.agy_path)
        self.sleep_fn = sleep_fn
        self.now_fn = now_fn
        self.skip_idle_check = skip_idle_check
        self.min_free_gb = min_free_gb
        self.problem_statements = problem_statements
        self.artifacts_dir = artifacts_dir

    def run_preflight(self, conn: sqlite3.Connection) -> None:
        """Verify pre-flight constraints before starting task execution."""
        if self.manifest.quota.use_g1_credits is not False:
            raise RuntimeError("Fatal: use_g1_credits must be False (no paid overage permitted)!")

        # Free disk space check
        disk_free_gb = shutil.disk_usage(Path.cwd()).free / (1024**3)
        if disk_free_gb < self.min_free_gb:
            raise RuntimeError(
                f"Insufficient disk space: {disk_free_gb:.1f} GB free, "
                f"minimum required: {self.min_free_gb:.1f} GB"
            )

        # Docker and image pre-flight checks when using real Docker provisioner
        if isinstance(self.provisioner, DockerProvisioner):
            if not Path(self.manifest.agy_path).is_file():
                raise RuntimeError(f"Host agy binary not found at {self.manifest.agy_path}!")
            import subprocess

            res = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
            if res.returncode != 0:
                raise RuntimeError(f"Docker daemon not accessible: {res.stderr}")

            from subbench.provision.images import verify_base_image_exists

            for t in self.suite.ordered():
                if self.manifest.task_limit is not None and t.order >= self.manifest.task_limit:
                    continue
                if not verify_base_image_exists(t.instance_id):
                    raise RuntimeError(f"Docker base image missing for instance {t.instance_id}!")

        # Idle check across all watched buckets
        all_buckets = {b for cfg in self.manifest.configs for b in cfg.buckets}
        if not self.skip_idle_check and self.manifest.quota.idle_check_seconds > 0:
            logger.info(
                "Running idle check for %ds on %s...",
                self.manifest.quota.idle_check_seconds,
                all_buckets,
            )
            _, _, leaked = check_idle(
                self.quota_probe,
                all_buckets,
                idle_seconds=self.manifest.quota.idle_check_seconds,
                threshold=self.manifest.quota.external_use_threshold,
                sleep_fn=self.sleep_fn,
            )
            if leaked:
                raise RuntimeError(
                    f"External quota usage detected on buckets {leaked} before run started! "
                    "Ensure no concurrent Gemini/Antigravity sessions are active."
                )

    def run(self, cycle_id: int | None = None, dry_run: bool = False) -> int:
        """Execute the cycle or print the dry-run schedule."""
        schedule = generate_attempt_schedule(self.manifest, self.suite)

        if dry_run:
            print(f"Dry run for manifest: {self.manifest.name} (passes={self.manifest.passes})")
            print(f"Total planned attempts: {len(schedule)}")
            print(f"{'Seq':<5} {'Pass':<6} {'Config':<25} {'Instance ID'}")
            print("-" * 65)
            for seq, p_idx, cfg, task in schedule:
                print(f"{seq:<5} {p_idx:<6} {cfg.label:<25} {task.instance_id}")
            return 0

        conn = connect(self.db_path)
        init_db(conn)

        self.run_preflight(conn)

        problem_map: dict[str, str] = {}
        if self.problem_statements is not None:
            problem_map = dict(self.problem_statements)
        elif not dry_run:
            rows: list[dict[str, Any]] = []
            cached_parquet = (
                Path("data/cache") / f"verified-{self.suite.dataset_revision[:12]}.parquet"
            )
            if cached_parquet.exists():
                try:
                    from subbench.tasks.dataset import fetch, load_rows

                    rows = load_rows(fetch(self.suite.dataset_revision))
                except Exception as e:
                    logger.warning("Failed to load cached parquet %s: %s", cached_parquet, e)
            if not rows:
                fallback_jsonl = Path("data/validation/dataset.jsonl")
                if fallback_jsonl.exists():
                    try:
                        for line in fallback_jsonl.read_text(encoding="utf-8").splitlines():
                            if line.strip():
                                rows.append(json.loads(line))
                    except Exception as e:
                        logger.warning("Failed to load fallback jsonl %s: %s", fallback_jsonl, e)
            if not rows:
                try:
                    from subbench.tasks.dataset import fetch, load_rows

                    pq_path = fetch(self.suite.dataset_revision)
                    rows = load_rows(pq_path)
                except Exception as e:
                    logger.warning(
                        "Failed to fetch parquet for %s: %s", self.suite.dataset_revision, e
                    )

            for r in rows:
                iid = r.get("instance_id")
                ps = r.get("problem_statement")
                if iid and ps:
                    problem_map[iid] = ps

            for _, _, _, task in schedule:
                if (
                    task.instance_id not in problem_map
                    or not str(problem_map[task.instance_id]).strip()
                ):
                    raise RuntimeError(
                        f"Missing or empty problem_statement for task '{task.instance_id}'! "
                        "Cannot run without valid problem statement."
                    )

        if cycle_id is not None:
            cur = conn.execute("SELECT experiment_id FROM cycles WHERE id = ?", (cycle_id,))
            row = cur.fetchone()
            if row:
                exp_id = row[0]
            else:
                exp_id = record_experiment(conn, self.manifest, self.suite)
        else:
            exp_id = record_experiment(conn, self.manifest, self.suite)

        record_environment(conn, exp_id, self.manifest, self.suite)
        config_ids = record_configs(conn, exp_id, self.manifest)
        c_id = get_or_create_cycle(conn, exp_id, cycle_id)

        logger.info("Beginning execution of Cycle %d (Experiment %d)", c_id, exp_id)

        windows_used: dict[str, int] = {cfg.label: 1 for cfg in self.manifest.configs}
        aborted_configs: set[str] = set()
        last_post_snapshot: QuotaSnapshot | None = None

        for seq, pass_index, config, task in schedule:
            if config.label in aborted_configs:
                logger.info(
                    "Skipping task %s for aborted config %s",
                    task.instance_id,
                    config.label,
                )
                continue

            cfg_id = config_ids[config.label]

            # Resume check
            cur = conn.execute(
                """
                SELECT id, state, halt_reason, retry_of FROM attempts
                WHERE cycle_id = ? AND config_id = ? AND instance_id = ? AND pass_index = ?
                ORDER BY id DESC LIMIT 1
                """,
                (c_id, cfg_id, task.instance_id, pass_index),
            )
            existing = cur.fetchone()
            if existing:
                att_id, att_state, att_halt, _ = existing
                if att_state in ("QUOTA_POST", "GRADED") and att_halt != "quota_restricted":
                    logger.info(
                        "Skipping completed attempt %d for %s/%s",
                        att_id,
                        config.label,
                        task.instance_id,
                    )
                    continue
                if att_state in (
                    "PENDING",
                    "PROVISIONED",
                    "QUOTA_PRE",
                    "RUNNING",
                    "HALTED",
                    "CAPTURED",
                ):
                    logger.warning("Finalizing interrupted attempt %d as infra_error", att_id)
                    conn.execute(
                        """
                        UPDATE attempts
                        SET state = 'QUOTA_POST', halt_reason = 'infra_error',
                            error_text = 'interrupted_before_completion'
                        WHERE id = ?
                        """,
                        (att_id,),
                    )
                    conn.commit()
                    retry_parent: int | None = att_id
                else:
                    retry_parent = att_id if att_halt == "quota_restricted" else None
            else:
                retry_parent = None

            # Attempt loop (handles 5h sleep-and-retry within the task)
            current_retry_of = retry_parent
            while True:
                # Inter-attempt contamination check
                snap_pre = self.quota_probe.snapshot()
                leaked_buckets: list[str] = []
                if last_post_snapshot is not None:
                    leaked_buckets = detect_external_use(
                        last_post_snapshot,
                        snap_pre,
                        watched_buckets=config.buckets,
                        threshold=self.manifest.quota.external_use_threshold,
                    )
                    if leaked_buckets:
                        logger.warning("Inter-attempt external use detected on %s", leaked_buckets)
                        for lb in leaked_buckets:
                            add_cycle_flag(conn, c_id, f"external_use:{lb}")

                # Check weekly cap before start
                weekly_bucket = self.manifest.quota.weekly_bucket_by_config.get(config.label)
                max_weekly = self.manifest.quota.max_weekly_fraction_per_config_cycle
                if weekly_bucket and max_weekly is not None:
                    current_burn = get_cumulative_weekly_burn(conn, c_id, cfg_id, weekly_bucket)
                    if current_burn >= max_weekly:
                        logger.warning(
                            "Config %s reached weekly fraction cap (burned=%.4f >= max=%.4f)",
                            config.label,
                            current_burn,
                            max_weekly,
                        )
                        aborted_configs.add(config.label)
                        add_cycle_flag(conn, c_id, f"weekly_cap_reached:{config.label}")
                        break

                # Check if weekly bucket is exhausted before start
                if weekly_bucket and snap_pre.buckets.get(weekly_bucket):
                    rem_w = snap_pre.buckets[weekly_bucket].remaining_fraction
                    if rem_w <= 1e-3:
                        logger.warning(
                            "Weekly bucket %s exhausted before attempt (rem=%.4f)",
                            weekly_bucket,
                            rem_w,
                        )
                        aborted_configs.add(config.label)
                        add_cycle_flag(conn, c_id, f"weekly_exhausted:{config.label}")
                        break

                # Check pre-attempt 5h bucket exhaustion (<= 1e-3)
                five_h_exhausted_bucket: BucketReading | None = None
                for b_id in config.buckets:
                    b_reading = snap_pre.buckets.get(b_id)
                    if (
                        b_reading
                        and b_reading.window == "5h"
                        and b_reading.remaining_fraction <= 1e-3
                    ):
                        five_h_exhausted_bucket = b_reading
                        break

                if five_h_exhausted_bucket is not None:
                    w_used = windows_used.get(config.label, 1)
                    if w_used < self.manifest.quota.max_5h_windows_per_config_cycle:
                        windows_used[config.label] = w_used + 1
                        reset_time = five_h_exhausted_bucket.reset_time
                        sleep_duration = 30.0
                        if reset_time:
                            try:
                                z_clean = reset_time.replace("Z", "+00:00")
                                reset_dt = datetime.fromisoformat(z_clean)
                                wait_s = (reset_dt - self.now_fn()).total_seconds()
                                sleep_duration = max(0.0, wait_s) + 30.0
                            except Exception:
                                sleep_duration = 18030.0
                        logger.info(
                            "5h quota exhausted on %s. Sleeping %.1fs until reset...",
                            five_h_exhausted_bucket.bucket_id,
                            sleep_duration,
                        )
                        self.sleep_fn(sleep_duration)
                        continue
                    else:
                        logger.warning("Max 5h windows reached for %s", config.label)
                        add_cycle_flag(conn, c_id, f"max_5h_windows_reached:{config.label}")
                        aborted_configs.add(config.label)
                        break

                prompt_rendered = render_prompt(
                    self.manifest.prompt_template,
                    instance_id=task.instance_id,
                    repo=task.repo,
                    problem_statement=problem_map[task.instance_id],
                )

                # PENDING
                now_str = self.now_fn().isoformat()
                is_valid = 0 if leaked_buckets else 1
                inv_reason = f"external_use:{','.join(leaked_buckets)}" if leaked_buckets else None

                cur = conn.execute(
                    """
                    INSERT INTO attempts (
                        cycle_id, config_id, instance_id, pass_index, seq,
                        retry_of, state, valid, invalid_reason, prompt_hash, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                    """,
                    (
                        c_id,
                        cfg_id,
                        task.instance_id,
                        pass_index,
                        seq,
                        current_retry_of,
                        is_valid,
                        inv_reason,
                        prompt_rendered.sha256_hash,
                        now_str,
                    ),
                )
                attempt_id = cur.lastrowid
                conn.commit()

                attempt_dir = (
                    self.artifacts_dir / "cycles" / f"cycle-{c_id}" / f"attempt-{attempt_id}"
                )
                attempt_dir.mkdir(parents=True, exist_ok=True)
                (attempt_dir / "prompt.txt").write_text(prompt_rendered.text)

                # PROVISIONED
                task_env = self.provisioner.provision(
                    task.instance_id, task.base_commit, attempt_dir
                )
                try:
                    conn.execute(
                        "UPDATE attempts SET state = 'PROVISIONED' WHERE id = ?",
                        (attempt_id,),
                    )
                    conn.commit()

                    # QUOTA_PRE
                    save_quota_snapshot(
                        conn,
                        cycle_id=c_id,
                        attempt_id=attempt_id,
                        phase="pre",
                        snap=snap_pre,
                    )
                    conn.execute(
                        "UPDATE attempts SET state = 'QUOTA_PRE' WHERE id = ?",
                        (attempt_id,),
                    )
                    conn.commit()

                    # RUNNING
                    conn.execute(
                        "UPDATE attempts SET state = 'RUNNING' WHERE id = ?",
                        (attempt_id,),
                    )
                    conn.commit()

                    adapter = self.adapter_factory(config)
                    timeout_s = self.manifest.timeout_minutes * 60
                    raw_ndjson = attempt_dir / "stream_json.ndjson"
                    stderr_file = attempt_dir / "stderr.txt"

                    outcome: AttemptOutcome
                    try:
                        outcome = adapter.run_attempt(
                            instance_id=task.instance_id,
                            prompt=prompt_rendered,
                            container_id=task_env.container_id,
                            timeout_seconds=timeout_s,
                            raw_output_path=raw_ndjson,
                            stderr_output_path=stderr_file,
                        )
                    except Exception as e:
                        logger.exception("Adapter run failed: %s", e)
                        outcome = AttemptOutcome(
                            halt_reason="agent_error",
                            exit_code=-1,
                            finished_reason="ERROR",
                            agy_version_pre=None,
                            agy_version_post=None,
                            wall_seconds=0.0,
                            num_turns=0,
                            input_tokens=0,
                            output_tokens=0,
                            thinking_tokens=0,
                            cache_read_tokens=0,
                            tool_calls=0,
                            events=[],
                            error_text=str(e),
                        )

                    # Ensure stderr.txt exists on disk
                    stderr_file.touch(exist_ok=True)

                    # HALTED
                    ended_str = self.now_fn().isoformat()
                    conn.execute(
                        """
                        UPDATE attempts
                        SET state = 'HALTED', halt_reason = ?, exit_code = ?, finished_reason = ?,
                            wall_seconds = ?, num_turns = ?, input_tokens = ?, output_tokens = ?,
                            thinking_tokens = ?, cache_read_tokens = ?, tool_calls = ?,
                            error_text = ?, agy_version = ?, ended_at = ?
                        WHERE id = ?
                        """,
                        (
                            outcome.halt_reason,
                            outcome.exit_code,
                            outcome.finished_reason,
                            outcome.wall_seconds,
                            outcome.num_turns,
                            outcome.input_tokens,
                            outcome.output_tokens,
                            outcome.thinking_tokens,
                            outcome.cache_read_tokens,
                            outcome.tool_calls,
                            outcome.error_text,
                            outcome.agy_version_post or outcome.agy_version_pre,
                            ended_str,
                            attempt_id,
                        ),
                    )
                    for ev in outcome.events:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO agent_events (
                                attempt_id, idx, event, step_type, state, tool_name,
                                duration_seconds, total_tokens
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                attempt_id,
                                ev.idx,
                                ev.event,
                                ev.step_type,
                                ev.state,
                                ev.tool_name,
                                ev.duration_seconds,
                                ev.total_tokens,
                            ),
                        )
                    conn.commit()

                    # CAPTURED
                    patch_text, patch_bytes = task_env.capture_patch()
                    patch_path = save_attempt_artifacts(
                        attempt_dir,
                        patch_text,
                        log_files={
                            "stream_json.ndjson": raw_ndjson,
                        },
                    )
                finally:
                    task_env.cleanup()

                conn.execute(
                    """
                    UPDATE attempts
                    SET state = 'CAPTURED', patch_path = ?, patch_bytes = ?,
                        transcript_path = ?, stdout_path = ?, stderr_path = ?
                    WHERE id = ?
                    """,
                    (
                        str(patch_path),
                        patch_bytes,
                        str(raw_ndjson),
                        str(raw_ndjson),
                        str(attempt_dir / "stderr.txt"),
                        attempt_id,
                    ),
                )
                conn.commit()

                # QUOTA_POST
                snap_post = self.quota_probe.snapshot()
                save_quota_snapshot(
                    conn,
                    cycle_id=c_id,
                    attempt_id=attempt_id,
                    phase="post",
                    snap=snap_post,
                )

                delta_res = compute_attempt_deltas(
                    snap_pre, snap_post, watched_buckets=config.buckets
                )
                if delta_res.invalid_reset:
                    conn.execute(
                        "UPDATE attempts SET valid = 0, invalid_reason = ? WHERE id = ?",
                        (delta_res.invalid_reason or "invalid_reset", attempt_id),
                    )

                conn.execute(
                    "UPDATE attempts SET state = 'QUOTA_POST' WHERE id = ?",
                    (attempt_id,),
                )
                conn.commit()
                last_post_snapshot = snap_post

                # Check weekly fraction cap post-attempt
                if weekly_bucket and max_weekly is not None:
                    current_burn = get_cumulative_weekly_burn(conn, c_id, cfg_id, weekly_bucket)
                    if current_burn >= max_weekly:
                        logger.warning(
                            "Config %s reached weekly cap after attempt (burned=%.4f >= max=%.4f)",
                            config.label,
                            current_burn,
                            max_weekly,
                        )
                        aborted_configs.add(config.label)
                        add_cycle_flag(conn, c_id, f"weekly_cap_reached:{config.label}")

                # Quota exhaustion checks
                if outcome.halt_reason == "quota_restricted":
                    # Check which bucket triggered it
                    weekly_b_id = self.manifest.quota.weekly_bucket_by_config.get(config.label)
                    weekly_reading = snap_post.buckets.get(weekly_b_id) if weekly_b_id else None
                    if weekly_reading and weekly_reading.remaining_fraction <= 1e-3:
                        logger.warning("Config %s hit weekly exhaustion", config.label)
                        aborted_configs.add(config.label)
                        add_cycle_flag(conn, c_id, f"weekly_exhausted:{config.label}")
                        break

                    # 5h window exhaustion handling across config.buckets
                    five_h_b = next(
                        (
                            snap_post.buckets[b_id]
                            for b_id in config.buckets
                            if b_id in snap_post.buckets and snap_post.buckets[b_id].window == "5h"
                        ),
                        None,
                    )
                    if five_h_b is not None:
                        w_used = windows_used.get(config.label, 1)
                        if w_used < self.manifest.quota.max_5h_windows_per_config_cycle:
                            windows_used[config.label] = w_used + 1
                            # Sleep until reset
                            reset_time = five_h_b.reset_time
                            sleep_duration = 30.0
                            if reset_time:
                                try:
                                    z_clean = reset_time.replace("Z", "+00:00")
                                    reset_dt = datetime.fromisoformat(z_clean)
                                    wait_s = (reset_dt - self.now_fn()).total_seconds()
                                    sleep_duration = max(0.0, wait_s) + 30.0
                                except Exception:
                                    sleep_duration = 18030.0
                            logger.info(
                                "5h quota restricted on %s. Sleeping %.1fs until reset...",
                                five_h_b.bucket_id,
                                sleep_duration,
                            )
                            self.sleep_fn(sleep_duration)
                            # Retry this task from scratch
                            current_retry_of = attempt_id
                            continue
                        else:
                            logger.warning("Max 5h windows reached for %s", config.label)
                            add_cycle_flag(conn, c_id, f"max_5h_windows_reached:{config.label}")
                            aborted_configs.add(config.label)
                            break
                    else:
                        logger.warning(
                            "Config %s quota restricted without 5h bucket; aborting config",
                            config.label,
                        )
                        add_cycle_flag(conn, c_id, f"quota_restricted_abort:{config.label}")
                        aborted_configs.add(config.label)
                        break
                else:
                    # Successful completion / normal halt, break attempt retry loop
                    break

        now_end = self.now_fn().isoformat()
        conn.execute(
            "UPDATE cycles SET status = 'completed', finished_at = ? WHERE id = ?",
            (now_end, c_id),
        )
        conn.commit()
        logger.info("Cycle %d completed successfully at %s", c_id, now_end)
        return c_id
