"""Tests for the sequential resumable runner, state machine, and exhaustion rules."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from subbench.agents.fake import FakeAgentAdapter
from subbench.config import (
    ConfigRole,
    ConfigSpec,
    Difficulty,
    ExperimentManifest,
    SuiteManifest,
    TaskSpec,
)
from subbench.quota import BucketReading, QuotaSnapshot
from subbench.runner import (
    FakeProvisioner,
    Runner,
    generate_attempt_schedule,
)
from subbench.store import connect, init_db


class FakeQuotaProbe:
    """Configurable sequence of quota snapshots."""

    def __init__(self, snapshots: list[QuotaSnapshot] | None = None) -> None:
        self.snapshots = snapshots or []
        self.idx = 0
        self.default_buckets = {
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.90, "2026-11-01T15:00:00Z"),
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.95, "2026-11-07T00:00:00Z"),
            "gemini-argon-wtu-weekly": BucketReading(
                "gemini-argon-wtu-weekly", "weekly", 0.95, "2026-11-07T00:00:00Z"
            ),
        }

    def snapshot(self) -> QuotaSnapshot:
        if self.idx < len(self.snapshots):
            snap = self.snapshots[self.idx]
            self.idx += 1
            return snap
        return QuotaSnapshot(
            buckets=dict(self.default_buckets),
            taken_at=datetime.now(UTC).isoformat(),
            provenance="manual",
            raw_json="{}",
        )


def _make_dummy_manifest_and_suite(tmp_path: Path, **manifest_kw) -> tuple[Path, Path]:
    suite = SuiteManifest(
        version="test-suite-v1",
        dataset_revision="0123456789abcdef0123456789abcdef01234567",
        seed=42,
        tasks=[
            TaskSpec(
                instance_id="pytest-dev__pytest-5809",
                repo="pytest-dev/pytest",
                base_commit="8aba863a634f40560e25055d179220f0eefabe9a",
                difficulty=Difficulty.EASY,
                order=0,
            ),
            TaskSpec(
                instance_id="psf__requests-2931",
                repo="psf/requests",
                base_commit="fe340324b105d153835ec7a66133efb1c2be0b48",
                difficulty=Difficulty.MEDIUM,
                order=1,
            ),
        ],
    )
    suite_path = tmp_path / "suite.json"
    suite_path.write_text(json.dumps(suite.model_dump(mode="json"), indent=2))

    cfg_efficient = ConfigSpec(
        label="efficient-flash",
        role=ConfigRole.EFFICIENT,
        model_id="gemini-3.8-flash",
        buckets=["gemini-5h", "gemini-weekly"],
    )
    cfg_capable = ConfigSpec(
        label="capable-argon",
        role=ConfigRole.CAPABLE,
        model_id="gemini-4-argon",
        buckets=["gemini-argon-wtu-weekly"],
        max_passes=manifest_kw.pop("capable_max_passes", None),
    )

    exp_data = {
        "name": "test-run",
        "suite": str(suite_path),
        "agy_path": "/usr/local/bin/agy",
        "configs": [cfg_efficient, cfg_capable],
        "passes": 2,
        "quota": {
            "primary_bucket_by_config": {
                "efficient-flash": "gemini-5h",
                "capable-argon": "gemini-argon-wtu-weekly",
            },
            "weekly_bucket_by_config": {
                "efficient-flash": "gemini-weekly",
                "capable-argon": "gemini-argon-wtu-weekly",
            },
            "idle_check_seconds": 0,
        },
    } | manifest_kw

    exp = ExperimentManifest.model_validate(exp_data)
    manifest_path = tmp_path / "manifest.yaml"
    manifest_path.write_text(json.dumps(exp.model_dump(mode="json"), indent=2))
    return manifest_path, suite_path


def test_schedule_pass_interleaving_and_max_passes(tmp_path: Path):
    m_path, s_path = _make_dummy_manifest_and_suite(tmp_path, capable_max_passes=1)
    manifest = ExperimentManifest.model_validate(json.loads(m_path.read_text()))
    suite = SuiteManifest.model_validate(json.loads(s_path.read_text()))

    schedule = generate_attempt_schedule(manifest, suite)

    # Pass 0: efficient (2 tasks), capable (2 tasks) -> 4 tasks
    # Pass 1: efficient (2 tasks), capable skipped (max_passes=1) -> 2 tasks
    # Total = 6 attempts
    assert len(schedule) == 6

    # Verify pass interleaving
    assert schedule[0][1] == 0  # Pass 0
    assert schedule[0][2].label == "efficient-flash"
    assert schedule[1][2].label == "efficient-flash"

    assert schedule[2][1] == 0  # Pass 0
    assert schedule[2][2].label == "capable-argon"
    assert schedule[3][2].label == "capable-argon"

    assert schedule[4][1] == 1  # Pass 1
    assert schedule[4][2].label == "efficient-flash"
    assert schedule[5][2].label == "efficient-flash"


def test_runner_happy_path_state_machine(tmp_path: Path):
    m_path, _ = _make_dummy_manifest_and_suite(tmp_path, task_limit=1, passes=1)
    db_path = tmp_path / "test.db"

    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=FakeQuotaProbe(),
        adapter_factory=lambda cfg: FakeAgentAdapter(halt_reason="completed"),
        provisioner=FakeProvisioner(),
        skip_idle_check=True,
    )

    cycle_id = runner.run()
    assert cycle_id == 1

    # Verify prompt.txt contains real problem statement and NOT "Problem placeholder"
    prompt_file = Path("artifacts") / "cycles" / f"cycle-{cycle_id}" / "attempt-1" / "prompt.txt"
    assert prompt_file.exists()
    prompt_text = prompt_file.read_text()
    assert "Problem placeholder" not in prompt_text
    assert len(prompt_text) > 100

    # Verify stderr.txt exists
    stderr_file = Path("artifacts") / "cycles" / f"cycle-{cycle_id}" / "attempt-1" / "stderr.txt"
    assert stderr_file.exists()

    conn = connect(db_path)
    # Check attempts
    attempts = conn.execute("SELECT id, state, halt_reason, valid FROM attempts").fetchall()
    assert len(attempts) == 2  # 1 task * 2 configs
    for att in attempts:
        assert att[1] == "QUOTA_POST"
        assert att[2] == "completed"
        assert att[3] == 1  # valid

    # Check agent_events were recorded
    events = conn.execute("SELECT count(*) FROM agent_events").fetchone()
    assert events[0] >= 4


def test_runner_weekly_exhaustion_abort_config(tmp_path: Path):
    # Capable config starts with exhausted weekly bucket (0.000)
    m_path, _ = _make_dummy_manifest_and_suite(tmp_path, task_limit=1, passes=1)
    db_path = tmp_path / "test.db"

    probe = FakeQuotaProbe()
    probe.default_buckets["gemini-argon-wtu-weekly"] = BucketReading(
        "gemini-argon-wtu-weekly", "weekly", 0.0005, "2026-11-07T00:00:00Z"
    )

    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=probe,
        adapter_factory=lambda cfg: FakeAgentAdapter(halt_reason="completed"),
        provisioner=FakeProvisioner(),
        skip_idle_check=True,
    )

    cycle_id = runner.run()
    assert cycle_id == 1

    conn = connect(db_path)
    flags = conn.execute("SELECT flags_json FROM cycles WHERE id = ?", (cycle_id,)).fetchone()[0]
    assert "weekly_exhausted:capable-argon" in flags

    # efficient completed, capable was aborted without running
    attempts = conn.execute("SELECT config_id, state FROM attempts").fetchall()
    assert len(attempts) == 1  # only efficient-flash attempted!


def test_runner_5h_exhaustion_retry_and_cap(tmp_path: Path):
    cfg_eff = ConfigSpec(
        label="efficient-flash",
        role=ConfigRole.EFFICIENT,
        model_id="gemini-3.8-flash",
        buckets=["gemini-5h", "gemini-weekly"],
    )
    m_path, _ = _make_dummy_manifest_and_suite(
        tmp_path,
        task_limit=1,
        passes=1,
        configs=[cfg_eff],
        quota={
            "primary_bucket_by_config": {"efficient-flash": "gemini-5h"},
            "weekly_bucket_by_config": {"efficient-flash": "gemini-weekly"},
            "idle_check_seconds": 0,
        },
    )
    db_path = tmp_path / "test.db"

    slept_durations: list[float] = []

    # Sequence:
    # 1. pre: 0.90
    # 2. post: 0.000 (quota_restricted, reset in 100s)
    # 3. retry pre: 0.85
    # 4. retry post: 0.80 (completed)
    snaps = [
        QuotaSnapshot(
            buckets={"gemini-5h": BucketReading("gemini-5h", "5h", 0.90, "2026-11-01T15:00:00Z")},
            taken_at="2026-11-01T10:00:00Z",
            provenance="manual",
            raw_json="{}",
        ),
        QuotaSnapshot(
            buckets={"gemini-5h": BucketReading("gemini-5h", "5h", 0.0001, "2026-11-01T15:00:00Z")},
            taken_at="2026-11-01T10:10:00Z",
            provenance="manual",
            raw_json="{}",
        ),
        QuotaSnapshot(
            buckets={"gemini-5h": BucketReading("gemini-5h", "5h", 0.85, "2026-11-01T20:00:00Z")},
            taken_at="2026-11-01T15:00:30Z",
            provenance="manual",
            raw_json="{}",
        ),
        QuotaSnapshot(
            buckets={"gemini-5h": BucketReading("gemini-5h", "5h", 0.80, "2026-11-01T20:00:00Z")},
            taken_at="2026-11-01T15:15:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    ]

    call_num = 0

    def adapter_fac(cfg):
        nonlocal call_num
        call_num += 1
        if call_num == 1:
            return FakeAgentAdapter(halt_reason="quota_restricted")
        return FakeAgentAdapter(halt_reason="completed")

    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=FakeQuotaProbe(snapshots=snaps),
        adapter_factory=adapter_fac,
        provisioner=FakeProvisioner(),
        sleep_fn=lambda s: slept_durations.append(s),
        skip_idle_check=True,
    )

    cycle_id = runner.run()
    assert cycle_id == 1

    conn = connect(db_path)
    attempts = conn.execute(
        "SELECT id, retry_of, state, halt_reason FROM attempts ORDER BY id"
    ).fetchall()
    assert len(attempts) >= 2
    # First attempt was quota_restricted
    assert attempts[0][1] is None
    assert attempts[0][3] == "quota_restricted"
    # Second attempt was retry_of first attempt
    assert attempts[1][1] == attempts[0][0]
    assert attempts[1][3] == "completed"
    assert len(slept_durations) == 1


def test_runner_crash_recovery_resume(tmp_path: Path):
    m_path, s_path = _make_dummy_manifest_and_suite(tmp_path, task_limit=1, passes=1)
    db_path = tmp_path / "test.db"

    manifest = ExperimentManifest.model_validate(json.loads(m_path.read_text()))
    suite = SuiteManifest.model_validate(json.loads(s_path.read_text()))

    # Pre-populate an interrupted RUNNING attempt in the DB
    conn = connect(db_path)
    init_db(conn)
    from subbench.runner import record_configs, record_experiment

    exp_id = record_experiment(conn, manifest, suite)
    config_ids = record_configs(conn, exp_id, manifest)
    cfg_id = config_ids["efficient-flash"]

    c_cur = conn.execute(
        """
        INSERT INTO cycles (experiment_id, started_at, status)
        VALUES (?, '2026-11-01T10:00:00Z', 'running')
        """,
        (exp_id,),
    )
    cycle_id = c_cur.lastrowid

    # Simulated crashed attempt
    conn.execute(
        """
        INSERT INTO attempts (
            cycle_id, config_id, instance_id, pass_index, seq, state, valid
        ) VALUES (?, ?, 'pytest-dev__pytest-5809', 0, 1, 'RUNNING', 1)
        """,
        (cycle_id, cfg_id),
    )
    conn.commit()

    # Now resume the run
    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=FakeQuotaProbe(),
        adapter_factory=lambda cfg: FakeAgentAdapter(halt_reason="completed"),
        provisioner=FakeProvisioner(),
        skip_idle_check=True,
    )
    runner.run(cycle_id=cycle_id)

    # Stale attempt was finalized as infra_error and retried
    query = (
        "SELECT id, retry_of, state, halt_reason, error_text FROM attempts "
        "WHERE config_id = ? ORDER BY id"
    )
    rows = conn.execute(query, (cfg_id,)).fetchall()
    assert len(rows) == 2
    assert rows[0][2] == "QUOTA_POST"
    assert rows[0][3] == "infra_error"
    assert rows[0][4] == "interrupted_before_completion"

    # Retried attempt succeeded
    assert rows[1][1] == rows[0][0]
    assert rows[1][3] == "completed"


def test_runner_container_cleanup_on_adapter_exception(tmp_path: Path):
    """Verify task_env.cleanup() runs via try/finally on unexpected adapter exception."""
    m_path, _ = _make_dummy_manifest_and_suite(tmp_path, task_limit=1, passes=1)
    db_path = tmp_path / "test.db"

    cleaned_up = False

    class TrackingEnv(FakeProvisioner):
        def provision(self, instance_id: str, base_commit: str, attempt_dir: Path):
            env = super().provision(instance_id, base_commit, attempt_dir)
            orig_cleanup = env.cleanup

            def tracked_cleanup():
                nonlocal cleaned_up
                cleaned_up = True
                orig_cleanup()

            env.cleanup = tracked_cleanup
            return env

    def exploding_adapter(cfg):
        class ExplodingAdapter:
            def run_attempt(self, **kwargs):
                raise RuntimeError("Explosive failure inside agent execution!")

        return ExplodingAdapter()

    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=FakeQuotaProbe(),
        adapter_factory=exploding_adapter,
        provisioner=TrackingEnv(),
        skip_idle_check=True,
    )

    cycle_id = runner.run()
    assert cycle_id == 1
    assert cleaned_up is True

    conn = connect(db_path)
    att = conn.execute("SELECT halt_reason, error_text FROM attempts WHERE id = 1").fetchone()
    assert att[0] == "agent_error"
    assert "Explosive failure" in att[1]


def test_runner_max_weekly_fraction_per_config_cycle(tmp_path: Path):
    """Verify max_weekly_fraction_per_config_cycle halts config after burn exceeds cap."""
    cfg_eff = ConfigSpec(
        label="efficient-flash",
        role=ConfigRole.EFFICIENT,
        model_id="gemini-3.8-flash",
        buckets=["gemini-5h", "gemini-weekly"],
    )
    m_path, _ = _make_dummy_manifest_and_suite(
        tmp_path,
        task_limit=2,
        passes=1,
        configs=[cfg_eff],
        quota={
            "primary_bucket_by_config": {"efficient-flash": "gemini-5h"},
            "weekly_bucket_by_config": {"efficient-flash": "gemini-weekly"},
            "idle_check_seconds": 0,
            "max_weekly_fraction_per_config_cycle": 0.05,
        },
    )
    db_path = tmp_path / "test.db"

    # Snapshots sequence:
    # Attempt 1: pre weekly=0.95, post weekly=0.88 (burns 0.07 >= 0.05 cap!)
    snaps = [
        QuotaSnapshot(
            buckets={"gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.95)},
            taken_at="2026-11-01T10:00:00Z",
            provenance="manual",
            raw_json="{}",
        ),
        QuotaSnapshot(
            buckets={"gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.88)},
            taken_at="2026-11-01T10:15:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    ]

    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=FakeQuotaProbe(snapshots=snaps),
        adapter_factory=lambda cfg: FakeAgentAdapter(halt_reason="completed"),
        provisioner=FakeProvisioner(),
        skip_idle_check=True,
    )

    cycle_id = runner.run()
    conn = connect(db_path)
    flags = conn.execute("SELECT flags_json FROM cycles WHERE id = ?", (cycle_id,)).fetchone()[0]
    assert "weekly_cap_reached:efficient-flash" in flags

    # Task 2 should have been skipped because config was aborted
    attempts = conn.execute("SELECT count(*) FROM attempts").fetchone()[0]
    assert attempts == 1


def test_runner_pre_attempt_5h_exhaustion(tmp_path: Path):
    """Verify pre-attempt 5h bucket exhaustion sleeps without provisioning doomed attempt."""
    cfg_eff = ConfigSpec(
        label="efficient-flash",
        role=ConfigRole.EFFICIENT,
        model_id="gemini-3.8-flash",
        buckets=["gemini-5h", "gemini-weekly"],
    )
    m_path, _ = _make_dummy_manifest_and_suite(
        tmp_path,
        task_limit=1,
        passes=1,
        configs=[cfg_eff],
        quota={
            "primary_bucket_by_config": {"efficient-flash": "gemini-5h"},
            "weekly_bucket_by_config": {"efficient-flash": "gemini-weekly"},
            "idle_check_seconds": 0,
            "max_5h_windows_per_config_cycle": 2,
        },
    )
    db_path = tmp_path / "test.db"
    slept: list[float] = []

    # Snapshots:
    # 1. pre: 5h=0.0005 (exhausted pre-attempt, reset in 100s)
    # 2. pre after sleep: 5h=0.90 (restored)
    # 3. post: 5h=0.85 (completed)
    snaps = [
        QuotaSnapshot(
            buckets={"gemini-5h": BucketReading("gemini-5h", "5h", 0.0005, "2026-11-01T15:00:00Z")},
            taken_at="2026-11-01T10:00:00Z",
            provenance="manual",
            raw_json="{}",
        ),
        QuotaSnapshot(
            buckets={"gemini-5h": BucketReading("gemini-5h", "5h", 0.90, "2026-11-01T20:00:00Z")},
            taken_at="2026-11-01T15:00:30Z",
            provenance="manual",
            raw_json="{}",
        ),
        QuotaSnapshot(
            buckets={"gemini-5h": BucketReading("gemini-5h", "5h", 0.85, "2026-11-01T20:00:00Z")},
            taken_at="2026-11-01T15:15:00Z",
            provenance="manual",
            raw_json="{}",
        ),
    ]

    provision_count = 0

    class CountingProvisioner(FakeProvisioner):
        def provision(self, instance_id, base_commit, attempt_dir):
            nonlocal provision_count
            provision_count += 1
            return super().provision(instance_id, base_commit, attempt_dir)

    runner = Runner(
        manifest_path=m_path,
        db_path=db_path,
        quota_probe=FakeQuotaProbe(snapshots=snaps),
        adapter_factory=lambda cfg: FakeAgentAdapter(halt_reason="completed"),
        provisioner=CountingProvisioner(),
        sleep_fn=lambda s: slept.append(s),
        skip_idle_check=True,
    )

    runner.run()
    assert len(slept) == 1
    # Only 1 provision occurred because pre-attempt check prevented provisioning when exhausted!
    assert provision_count == 1
