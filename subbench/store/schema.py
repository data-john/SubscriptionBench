"""SQLite schema, versioned with `PRAGMA user_version` migrations.

Design notes
- SQLite is the source of truth; CSV/Parquet are exports.
- `quota_snapshots` stores one row *per bucket* per snapshot (Phase 0: configs draw from
  different buckets, so allowance is accounted per bucket).
- Raw agent NDJSON is kept on disk under artifacts/; `agent_events` indexes the parsed
  events, and `attempts.transcript_path` points to the raw file.
- Attempts are never deleted: discarded or invalid attempts keep their cost.
"""

from __future__ import annotations

MIGRATIONS: list[str] = [
    # --- v1 ---
    """
    CREATE TABLE experiments (
        id              INTEGER PRIMARY KEY,
        name            TEXT NOT NULL,
        manifest_json   TEXT NOT NULL,
        manifest_digest TEXT NOT NULL,
        suite_version   TEXT NOT NULL,
        suite_digest    TEXT NOT NULL,
        created_at      TEXT NOT NULL,
        UNIQUE (name, manifest_digest)
    );

    CREATE TABLE environment (
        id               INTEGER PRIMARY KEY,
        experiment_id    INTEGER NOT NULL REFERENCES experiments(id),
        agy_path         TEXT NOT NULL,
        agy_version      TEXT NOT NULL,
        os               TEXT NOT NULL,
        kernel           TEXT,
        docker_version   TEXT,
        swebench_version TEXT,
        dataset          TEXT,
        dataset_revision TEXT,
        prompt_hash      TEXT,
        config_state_hash TEXT,
        recorded_at      TEXT NOT NULL
    );

    CREATE TABLE configs (
        id            INTEGER PRIMARY KEY,
        experiment_id INTEGER NOT NULL REFERENCES experiments(id),
        label         TEXT NOT NULL,
        role          TEXT NOT NULL CHECK (role IN ('default','efficient','capable')),
        model_id      TEXT NOT NULL,
        effort        TEXT,
        buckets_json  TEXT NOT NULL,
        UNIQUE (experiment_id, label)
    );

    -- One cycle = one scheduled run of an experiment (bi-weekly cadence).
    CREATE TABLE cycles (
        id            INTEGER PRIMARY KEY,
        experiment_id INTEGER NOT NULL REFERENCES experiments(id),
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL DEFAULT 'running'
            CHECK (status IN ('running','completed','aborted')),
        flags_json    TEXT NOT NULL DEFAULT '[]'
    );

    CREATE TABLE attempts (
        id               INTEGER PRIMARY KEY,
        cycle_id         INTEGER NOT NULL REFERENCES cycles(id),
        config_id        INTEGER NOT NULL REFERENCES configs(id),
        instance_id      TEXT NOT NULL,
        pass_index       INTEGER NOT NULL,
        seq              INTEGER NOT NULL,            -- global execution order in the cycle
        retry_of         INTEGER REFERENCES attempts(id),
        state            TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (state IN ('PENDING','PROVISIONED','QUOTA_PRE','RUNNING','HALTED',
                             'CAPTURED','QUOTA_POST','GRADED')),
        halt_reason      TEXT
            CHECK (halt_reason IS NULL OR halt_reason IN
                   ('completed','timeout','agent_error','infra_error','quota_restricted')),
        valid            INTEGER NOT NULL DEFAULT 1,  -- 0 = excluded from quota analysis
        invalid_reason   TEXT,                        -- e.g. invalid_reset, external_use
        prompt_hash      TEXT,
        agy_version      TEXT,
        exit_code        INTEGER,
        finished_reason  TEXT,                        -- agy result.finished_reason
        started_at       TEXT,
        ended_at         TEXT,
        wall_seconds     REAL,
        num_turns        INTEGER,
        input_tokens     INTEGER,
        output_tokens    INTEGER,
        thinking_tokens  INTEGER,
        cache_read_tokens INTEGER,
        tool_calls       INTEGER,
        patch_path       TEXT,
        patch_bytes      INTEGER,
        transcript_path  TEXT,
        stdout_path      TEXT,
        stderr_path      TEXT,
        error_text       TEXT
    );
    -- At most one original attempt per (cycle, config, task, pass); retries reference it.
    CREATE UNIQUE INDEX attempts_original
        ON attempts (cycle_id, config_id, instance_id, pass_index) WHERE retry_of IS NULL;
    CREATE INDEX attempts_by_cycle ON attempts (cycle_id, seq);

    -- One row per bucket per snapshot. `phase` ties it to the attempt boundary.
    CREATE TABLE quota_snapshots (
        id                 INTEGER PRIMARY KEY,
        cycle_id           INTEGER NOT NULL REFERENCES cycles(id),
        attempt_id         INTEGER REFERENCES attempts(id),
        phase              TEXT NOT NULL CHECK (phase IN ('pre','post','idle','manual')),
        bucket_id          TEXT NOT NULL,
        window             TEXT NOT NULL,
        remaining_fraction REAL NOT NULL,
        reset_time         TEXT,
        taken_at           TEXT NOT NULL,
        provenance         TEXT NOT NULL CHECK (provenance IN ('agy_usage','manual')),
        raw_json           TEXT
    );
    CREATE INDEX quota_by_attempt ON quota_snapshots (attempt_id, phase, bucket_id);

    CREATE TABLE agent_events (
        id          INTEGER PRIMARY KEY,
        attempt_id  INTEGER NOT NULL REFERENCES attempts(id),
        idx         INTEGER NOT NULL,                 -- line number in the raw NDJSON
        event       TEXT NOT NULL,                    -- init | step_update | result
        step_type   TEXT,
        state       TEXT,
        tool_name   TEXT,
        duration_seconds REAL,
        total_tokens INTEGER,
        UNIQUE (attempt_id, idx)
    );

    CREATE TABLE grades (
        id               INTEGER PRIMARY KEY,
        attempt_id       INTEGER NOT NULL REFERENCES attempts(id),
        graded_at        TEXT NOT NULL,
        swebench_version TEXT,
        resolved         INTEGER NOT NULL,
        patch_applied    INTEGER NOT NULL,
        fail_to_pass_json TEXT,
        pass_to_pass_json TEXT,
        report_path      TEXT,
        log_path         TEXT,
        error_text       TEXT
    );
    CREATE INDEX grades_by_attempt ON grades (attempt_id);

    CREATE TABLE audit_findings (
        id          INTEGER PRIMARY KEY,
        attempt_id  INTEGER NOT NULL REFERENCES attempts(id),
        kind        TEXT NOT NULL,                    -- upstream_url | git_history_probe | ...
        severity    TEXT NOT NULL CHECK (severity IN ('info','warn','leak')),
        evidence    TEXT NOT NULL,
        event_idx   INTEGER,
        created_at  TEXT NOT NULL
    );
    """,
    # --- v2 ---
    """
    ALTER TABLE environment
        ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'container'
        CHECK (execution_mode IN ('container', 'hybrid_host'));
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)

EXPECTED_TABLES = (
    "experiments",
    "environment",
    "configs",
    "cycles",
    "attempts",
    "quota_snapshots",
    "agent_events",
    "grades",
    "audit_findings",
)
