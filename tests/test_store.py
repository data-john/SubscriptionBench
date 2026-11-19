import sqlite3

import pytest

from subbench.store import connect, init_db
from subbench.store.db import schema_version
from subbench.store.schema import EXPECTED_TABLES, SCHEMA_VERSION


@pytest.fixture
def conn():
    c = connect(":memory:")
    init_db(c)
    return c


def test_all_tables_created(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert set(EXPECTED_TABLES) <= names
    assert schema_version(conn) == SCHEMA_VERSION


def test_init_is_idempotent(conn):
    assert init_db(conn) == SCHEMA_VERSION


def _seed(conn):
    conn.execute(
        "INSERT INTO experiments (name, manifest_json, manifest_digest, suite_version,"
        " suite_digest, created_at) VALUES ('e','{}','d','v','s','now')"
    )
    conn.execute(
        "INSERT INTO configs (experiment_id,label,role,model_id,buckets_json)"
        " VALUES (1,'c','default','m','[]')"
    )
    conn.execute("INSERT INTO cycles (experiment_id, started_at) VALUES (1,'now')")


def _attempt(conn, retry_of=None, state="PENDING", seq=0):
    return conn.execute(
        "INSERT INTO attempts (cycle_id,config_id,instance_id,pass_index,seq,retry_of,state)"
        " VALUES (1,1,'i',0,?,?,?)",
        (seq, retry_of, state),
    ).lastrowid


def test_one_original_attempt_but_retries_allowed(conn):
    _seed(conn)
    first = _attempt(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, seq=1)
    assert _attempt(conn, retry_of=first, seq=2)


def test_invalid_state_and_halt_reason_rejected(conn):
    _seed(conn)
    with pytest.raises(sqlite3.IntegrityError):
        _attempt(conn, state="BOGUS")
    aid = _attempt(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE attempts SET halt_reason='nope' WHERE id=?", (aid,))


def test_foreign_keys_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO cycles (experiment_id, started_at) VALUES (99,'now')")


def test_quota_snapshot_is_per_bucket(conn):
    _seed(conn)
    aid = _attempt(conn)
    for bucket, frac in (("gemini-5h", 0.9), ("gemini-weekly", 0.99)):
        conn.execute(
            "INSERT INTO quota_snapshots (cycle_id,attempt_id,phase,bucket_id,window,"
            "remaining_fraction,taken_at,provenance) VALUES (1,?,?,?,?,?,'now','agy_usage')",
            (aid, "pre", bucket, "5h", frac),
        )
    n = conn.execute("SELECT COUNT(*) FROM quota_snapshots WHERE attempt_id=?", (aid,)).fetchone()
    assert n[0] == 2


def test_migration_progression_and_execution_mode():
    from subbench.store.schema import MIGRATIONS

    raw_conn = connect(":memory:")
    # Manually apply only v1
    raw_conn.executescript("BEGIN;\n" + MIGRATIONS[0] + "\nPRAGMA user_version = 1;\nCOMMIT;")
    assert schema_version(raw_conn) == 1
    # execution_mode should not exist yet in v1
    cols_v1 = {r[1] for r in raw_conn.execute("PRAGMA table_info(environment)")}
    assert "execution_mode" not in cols_v1

    # Insert environment row in v1
    _seed(raw_conn)
    raw_conn.execute(
        "INSERT INTO environment (experiment_id, agy_path, agy_version, os, recorded_at)"
        " VALUES (1, '/path', '1.0', 'linux', 'now')"
    )

    # Run init_db to migrate to latest (v3)
    new_version = init_db(raw_conn)
    assert new_version == SCHEMA_VERSION == 3

    # Check execution_mode added with default 'container'
    row = raw_conn.execute("SELECT execution_mode FROM environment WHERE id=1").fetchone()
    assert row["execution_mode"] == "container"

    # Verify codex_usage provenance is accepted after v3 migration
    aid = _attempt(raw_conn)
    raw_conn.execute(
        "INSERT INTO quota_snapshots (cycle_id,attempt_id,phase,bucket_id,window,"
        "remaining_fraction,taken_at,provenance) VALUES (1,?,?,?,?,?,'now','codex_usage')",
        (aid, "pre", "codex-30d", "30d", 0.78),
    )
    with pytest.raises(sqlite3.IntegrityError):
        raw_conn.execute(
            "INSERT INTO quota_snapshots (cycle_id,attempt_id,phase,bucket_id,window,"
            "remaining_fraction,taken_at,provenance) VALUES (1,?,?,?,?,?,'now','bogus')",
            (aid, "pre", "codex-30d", "30d", 0.78),
        )

    # Verify check constraint
    raw_conn.execute(
        "INSERT INTO environment (experiment_id, agy_path, agy_version, os,"
        " recorded_at, execution_mode)"
        " VALUES (1, '/path', '1.0', 'linux', 'now', 'hybrid_host')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        raw_conn.execute(
            "INSERT INTO environment (experiment_id, agy_path, agy_version, os,"
            " recorded_at, execution_mode)"
            " VALUES (1, '/path', '1.0', 'linux', 'now', 'invalid_mode')"
        )
