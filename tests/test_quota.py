"""Tests for quota probing, parsing, delta calculations, and persistence."""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from subbench.quota import (
    AgyUsageProbe,
    BucketReading,
    CodexUsageProbe,
    ManualQuotaProbe,
    QuotaSnapshot,
    check_idle,
    compute_attempt_deltas,
    detect_external_use,
    parse_usage_json,
    save_quota_snapshot,
)
from subbench.store import connect, init_db

ROOT = Path(__file__).resolve().parent.parent
USAGE_SCHEMA_PATH = ROOT / "docs" / "phase0_samples" / "usage_raw_schema.json"


def test_parse_real_usage_schema_json():
    raw = USAGE_SCHEMA_PATH.read_text(encoding="utf-8")
    snap = parse_usage_json(raw, taken_at="2026-11-01T12:00:00Z")

    assert snap.taken_at == "2026-11-01T12:00:00Z"
    assert snap.provenance == "agy_usage"
    assert snap.raw_json == raw

    # Verify all 5 expected buckets from the sample
    assert "gemini-weekly" in snap.buckets
    assert "gemini-5h" in snap.buckets
    assert "3p-weekly" in snap.buckets
    assert "3p-5h" in snap.buckets
    assert "gemini-argon-wtu-weekly" in snap.buckets

    b_argon = snap.buckets["gemini-argon-wtu-weekly"]
    assert b_argon.window == "weekly"
    assert abs(b_argon.remaining_fraction - 0.9256717562675476) < 1e-6
    assert b_argon.reset_time == "2026-11-07T22:19:22Z"


def test_parse_usage_error_and_empty():
    bad_status = json.dumps({"status": "ERROR", "error": "Auth failed"})
    with pytest.raises(ValueError, match="non-SUCCESS"):
        parse_usage_json(bad_status)

    no_buckets = json.dumps({"status": "SUCCESS", "command": {"data": {"groups": []}}})
    with pytest.raises(ValueError, match="No quota buckets"):
        parse_usage_json(no_buckets)


def test_compute_attempt_deltas_normal():
    pre = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.90, "2026-11-01T15:00:00Z"),
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.95, "2026-11-07T00:00:00Z"),
        },
        taken_at="2026-11-01T10:00:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )
    post = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.82, "2026-11-01T15:00:00Z"),
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.94, "2026-11-07T00:00:00Z"),
        },
        taken_at="2026-11-01T10:15:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )

    res = compute_attempt_deltas(pre, post)
    assert not res.invalid_reset
    assert res.invalid_reason is None
    assert abs(res.deltas["gemini-5h"].consumed - 0.08) < 1e-6
    assert abs(res.deltas["gemini-weekly"].consumed - 0.01) < 1e-6


def test_compute_attempt_deltas_watched_buckets_scoping():
    # 3p-5h resets mid-attempt (fraction increases from 0.10 to 1.00),
    # but the attempt config only watched ['gemini-argon-wtu-weekly'].
    pre = QuotaSnapshot(
        buckets={
            "3p-5h": BucketReading("3p-5h", "5h", 0.10, "2026-11-01T15:00:00Z"),
            "gemini-argon-wtu-weekly": BucketReading(
                "gemini-argon-wtu-weekly", "weekly", 0.90, "2026-11-07T00:00:00Z"
            ),
        },
        taken_at="2026-11-01T10:00:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )
    post = QuotaSnapshot(
        buckets={
            "3p-5h": BucketReading("3p-5h", "5h", 1.00, "2026-11-01T20:00:00Z"),
            "gemini-argon-wtu-weekly": BucketReading(
                "gemini-argon-wtu-weekly", "weekly", 0.88, "2026-11-07T00:00:00Z"
            ),
        },
        taken_at="2026-11-01T10:15:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )

    # Scoped to Argon: 3p-5h reset must NOT invalidate the Argon attempt
    res = compute_attempt_deltas(pre, post, watched_buckets=["gemini-argon-wtu-weekly"])
    assert res.invalid_reset is False
    assert res.invalid_reason is None
    assert res.deltas["3p-5h"].reset_detected is True
    assert res.deltas["gemini-argon-wtu-weekly"].reset_detected is False
    assert abs(res.deltas["gemini-argon-wtu-weekly"].consumed - 0.02) < 1e-6

    # When unwatched (None), 3p-5h reset does trigger invalid_reset
    res_unwatched = compute_attempt_deltas(pre, post, watched_buckets=None)
    assert res_unwatched.invalid_reset is True
    assert "3p-5h" in (res_unwatched.invalid_reason or "")


def test_compute_attempt_deltas_rolling_first_spend():
    # If pre was 1.0 (virgin rolling window), reset_time anchors on first spend.
    # This must NOT be flagged as an invalid_reset!
    pre = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 1.0, "2026-11-01T15:00:00Z"),
        },
        taken_at="2026-11-01T10:00:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )
    post = QuotaSnapshot(
        buckets={
            # Spent from 1.0 to 0.98, and reset_time locked into 5h from spend time
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.98, "2026-11-01T15:05:00Z"),
        },
        taken_at="2026-11-01T10:05:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )

    res = compute_attempt_deltas(pre, post)
    assert not res.invalid_reset
    assert res.invalid_reason is None
    assert abs(res.deltas["gemini-5h"].consumed - 0.02) < 1e-6


def test_compute_attempt_deltas_invalid_reset_mid_attempt():
    # 1. Reset time changed when pre was already < 1.0 (window expired mid-task)
    pre = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.20, "2026-11-01T11:00:00Z"),
        },
        taken_at="2026-11-01T10:50:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )
    post = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.15, "2026-11-01T16:00:00Z"),
        },
        taken_at="2026-11-01T11:10:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )
    res = compute_attempt_deltas(pre, post)
    assert res.invalid_reset
    assert "reset_time changed" in str(res.invalid_reason)

    # 2. Remaining fraction increased mid-attempt
    post_up = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.80, "2026-11-01T11:00:00Z"),
        },
        taken_at="2026-11-01T11:10:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )
    res_up = compute_attempt_deltas(pre, post_up)
    assert res_up.invalid_reset
    assert "fraction increased" in str(res_up.invalid_reason)


def test_detect_external_use():
    post_prev = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.80),
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.90),
        },
        taken_at="2026-11-01T10:00:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )
    pre_next = QuotaSnapshot(
        buckets={
            # 5h dropped by 0.05 without an attempt running
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.75),
            # weekly unchanged
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.90),
        },
        taken_at="2026-11-01T10:10:00Z",
        provenance="agy_usage",
        raw_json="{}",
    )

    leaks = detect_external_use(post_prev, pre_next, watched_buckets=["gemini-5h", "gemini-weekly"])
    assert leaks == ["gemini-5h"]

    # Below threshold is ignored
    leaks_high_thresh = detect_external_use(
        post_prev, pre_next, watched_buckets=["gemini-5h"], threshold=0.10
    )
    assert leaks_high_thresh == []


def test_check_idle():
    call_count = 0

    class MockProbe:
        def snapshot(self):
            nonlocal call_count
            call_count += 1
            rem = 0.90 if call_count == 1 else 0.85
            return QuotaSnapshot(
                buckets={"gemini-5h": BucketReading("gemini-5h", "5h", rem)},
                taken_at="2026-11-01T10:00:00Z",
                provenance="agy_usage",
                raw_json="{}",
            )

    probe = MockProbe()
    slept = []
    s1, s2, leaked = check_idle(
        probe,
        watched_buckets=["gemini-5h"],
        idle_seconds=60,
        sleep_fn=lambda s: slept.append(s),
    )
    assert slept == [60]
    assert leaked == ["gemini-5h"]

    # When idle_seconds == 0, returns immediately without sleeping
    s1_zero, s2_zero, leaked_zero = check_idle(probe, watched_buckets=["gemini-5h"], idle_seconds=0)
    assert leaked_zero == []


def test_manual_quota_probe(tmp_path: Path):
    # From dict
    data = {
        "gemini-5h": {
            "window": "5h",
            "remaining_fraction": 0.75,
            "reset_time": "2026-11-01T15:00:00Z",
        }
    }
    probe = ManualQuotaProbe(data)
    snap = probe.snapshot()
    assert snap.provenance == "manual"
    assert snap.buckets["gemini-5h"].remaining_fraction == 0.75

    # From file
    p = tmp_path / "manual.json"
    p.write_text(json.dumps(data))
    probe_f = ManualQuotaProbe(p)
    snap_f = probe_f.snapshot()
    assert snap_f.provenance == "manual"
    assert snap_f.buckets["gemini-5h"].remaining_fraction == 0.75


def test_save_quota_snapshot_db(tmp_path: Path):
    db_path = tmp_path / "test.db"
    conn = connect(db_path)
    init_db(conn)

    now = "2026-11-01T10:00:00Z"
    cur = conn.execute(
        """
        INSERT INTO experiments (
            name, manifest_json, manifest_digest, suite_version, suite_digest, created_at
        ) VALUES ('test', '{}', 'abc', 'v1', 'def', ?)
        """,
        (now,),
    )
    c_cur = conn.execute(
        "INSERT INTO cycles (experiment_id, started_at, status) VALUES (?, ?, 'running')",
        (cur.lastrowid, now),
    )
    cycle_id = c_cur.lastrowid
    conn.commit()

    snap = QuotaSnapshot(
        buckets={
            "gemini-5h": BucketReading("gemini-5h", "5h", 0.85, "2026-11-01T15:00:00Z"),
            "gemini-weekly": BucketReading("gemini-weekly", "weekly", 0.95, "2026-11-07T00:00:00Z"),
        },
        taken_at="2026-11-01T10:00:00Z",
        provenance="agy_usage",
        raw_json='{"status": "SUCCESS"}',
    )

    save_quota_snapshot(conn, cycle_id=cycle_id, attempt_id=None, phase="idle", snap=snap)

    query = "SELECT bucket_id, window, remaining_fraction, phase FROM quota_snapshots"
    rows = conn.execute(query).fetchall()
    assert len(rows) == 2
    buckets_in_db = {r[0]: r[2] for r in rows}
    assert buckets_in_db["gemini-5h"] == 0.85
    assert buckets_in_db["gemini-weekly"] == 0.95


def test_agy_usage_probe_retries_on_timeout_and_cleans_up_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = USAGE_SCHEMA_PATH.read_text(encoding="utf-8")
    probe = AgyUsageProbe(timeout_seconds=5, retries=1)
    monkeypatch.setattr(probe, "ensure_image", lambda: "subbench-agy:probe")

    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        run_calls = [c for c in calls if c[:2] == ["docker", "run"]]
        if len(run_calls) == 1:
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)
        return subprocess.CompletedProcess(cmd, 0, stdout=raw, stderr="")

    monkeypatch.setattr("subbench.quota.agy_usage.subprocess.run", fake_run)
    monkeypatch.setattr("subbench.quota.base.subprocess.run", fake_run)

    snap = probe.snapshot()
    assert snap.provenance == "agy_usage"

    run_cmds = [c for c in calls if c[:2] == ["docker", "run"]]
    rm_cmds = [c for c in calls if c[:3] == ["docker", "rm", "-f"]]
    assert len(run_cmds) == 2
    assert len(rm_cmds) == 1
    first_cname = run_cmds[0][run_cmds[0].index("--name") + 1]
    assert rm_cmds[0] == ["docker", "rm", "-f", first_cname]


def test_agy_usage_probe_raises_after_exhausting_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = AgyUsageProbe(timeout_seconds=5, retries=1)
    monkeypatch.setattr(probe, "ensure_image", lambda: "subbench-agy:probe")
    removed: list[str] = []

    def fake_run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["docker", "rm", "-f"]:
            removed.append(cmd[3])
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5)

    monkeypatch.setattr("subbench.quota.agy_usage.subprocess.run", fake_run)
    monkeypatch.setattr("subbench.quota.base.subprocess.run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        probe.snapshot()
    assert len(removed) == 2


def test_codex_usage_probe_retries_on_timeout_and_cleans_up_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probe = CodexUsageProbe(host_codex_path="/opt/codex", timeout_seconds=1, retries=1)
    monkeypatch.setattr(probe, "ensure_image", lambda: "subbench-agy:probe")

    removed: list[str] = []
    popened_cmds: list[list[str]] = []

    def fake_rm(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
        if cmd[:3] == ["docker", "rm", "-f"]:
            removed.append(cmd[3])
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("subbench.quota.base.subprocess.run", fake_rm)

    rate_limits_msg = {
        "id": 2,
        "result": {
            "rateLimitsByLimitId": {
                "codex": {
                    "limitId": "codex",
                    "primary": {
                        "usedPercent": 10,
                        "windowDurationMins": 43200,
                        "resetsAt": 1795876811,
                    },
                    "secondary": None,
                }
            }
        },
    }

    class FakeProc:
        def __init__(self, lines: list[str], returncode: int = 0) -> None:
            self.stdin = io.StringIO()
            self.stdout = io.StringIO("".join(lines))
            self.stderr = io.StringIO("")
            self.returncode = returncode

        def terminate(self) -> None:
            pass

        def wait(self, timeout: float | None = None) -> int:
            return self.returncode

        def kill(self) -> None:
            pass

    monotonic_calls = 0

    def fake_monotonic() -> float:
        nonlocal monotonic_calls
        monotonic_calls += 1
        # First Popen attempt: deadline passes immediately while reading EOF
        if len(popened_cmds) == 1 and monotonic_calls >= 2:
            return 100.0
        return 0.0

    def fake_popen(cmd: list[str], **kwargs) -> FakeProc:
        popened_cmds.append(cmd)
        if len(popened_cmds) == 1:
            return FakeProc(lines=[], returncode=-9)
        return FakeProc(
            lines=[
                json.dumps({"id": 1, "result": {}}) + "\n",
                json.dumps(rate_limits_msg) + "\n",
            ],
            returncode=0,
        )

    monkeypatch.setattr("subbench.quota.codex_usage.time.monotonic", fake_monotonic)
    monkeypatch.setattr("subbench.quota.codex_usage.subprocess.Popen", fake_popen)

    snap = probe.snapshot()
    assert snap.provenance == "codex_usage"
    assert abs(snap.buckets["codex-30d"].remaining_fraction - 0.90) < 1e-9
    assert len(popened_cmds) == 2
    assert len(removed) >= 1
    first_cname = popened_cmds[0][popened_cmds[0].index("--name") + 1]
    assert first_cname in removed
