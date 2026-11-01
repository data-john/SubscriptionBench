"""Command-line entry point. Later phases add: suite / run / grade / audit / report / quota."""

from __future__ import annotations

import json
from datetime import UTC
from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import load_experiment, load_suite
from .store import connect, init_db
from .tasks import dataset as ds
from .tasks.curate import build_suite

app = typer.Typer(no_args_is_help=True, help="SubscriptionBench CLI.")
suite_app = typer.Typer(no_args_is_help=True, help="Suite curation and validation.")
app.add_typer(suite_app, name="suite")

quota_app = typer.Typer(no_args_is_help=True, help="Quota probes and snapshots.")
app.add_typer(quota_app, name="quota")

DEFAULT_DB = Path("data/subbench.sqlite")


@app.command()
def version() -> None:
    """Print the subbench version."""
    typer.echo(__version__)


@app.command("init-db")
def init_db_cmd(
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
) -> None:
    """Create or migrate the results database."""
    conn = connect(db)
    typer.echo(f"{db}: schema v{init_db(conn)}")


@app.command()
def validate(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Experiment YAML.")],
) -> None:
    """Validate an experiment manifest (and its suite, if the file exists)."""
    exp = load_experiment(manifest)
    typer.echo(f"experiment {exp.name!r}: {len(exp.configs)} configs, k={exp.passes}")
    typer.echo(f"  digest {exp.digest()[:16]}")
    suite_path = Path(exp.suite)
    if suite_path.exists():
        suite = load_suite(suite_path)
        typer.echo(f"suite {suite.version}: {len(suite.tasks)} tasks, digest {suite.digest()[:16]}")
    else:
        typer.echo(f"suite {exp.suite} not found yet (created in Phase 2)")


@app.command("run")
def run_cmd(
    manifest: Annotated[Path, typer.Argument(exists=True, dir_okay=False, help="Experiment YAML.")],
    cycle_id: Annotated[int | None, typer.Option(help="Cycle ID to resume.")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Print schedule and exit.")] = False,
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
    skip_idle_check: Annotated[
        bool, typer.Option("--skip-idle-check", help="Skip pre-flight idle check.")
    ] = False,
    idle_seconds: Annotated[
        int | None, typer.Option("--idle-seconds", help="Override pre-flight idle check seconds.")
    ] = None,
) -> None:
    """Run an experiment cycle or print a dry run."""
    from .runner import Runner

    runner = Runner(
        manifest_path=manifest,
        db_path=db,
        skip_idle_check=skip_idle_check,
    )
    if idle_seconds is not None:
        runner.manifest.quota.idle_check_seconds = idle_seconds
    c_id = runner.run(cycle_id=cycle_id, dry_run=dry_run)
    if not dry_run:
        typer.echo(f"Cycle {c_id} completed successfully.")


@app.command("grade")
def grade_cmd(
    cycle_id: Annotated[int, typer.Option(help="Cycle ID to grade.")],
    regrade: Annotated[
        bool, typer.Option("--regrade", help="Regrade already graded attempts.")
    ] = False,
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
    max_workers: Annotated[int, typer.Option(help="Max concurrent harness workers.")] = 2,
) -> None:
    """Run offline SWE-bench grading for a cycle."""
    from .grading import grade_cycle
    from .store import connect, init_db

    conn = connect(db)
    init_db(conn)
    res = grade_cycle(conn, cycle_id=cycle_id, regrade=regrade, max_workers=max_workers)
    typer.echo(
        f"Grading complete for cycle {cycle_id}: {res['resolved']}/{res['graded']} resolved."
    )


@app.command("audit")
def audit_cmd(
    cycle_id: Annotated[int, typer.Option(help="Cycle ID to audit.")],
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
) -> None:
    """Audit cycle transcripts and proxy logs for leaks."""
    from .audit import audit_cycle
    from .store import connect, init_db

    conn = connect(db)
    init_db(conn)
    res = audit_cycle(conn, cycle_id=cycle_id)
    n_tot = res["total_findings"]
    n_leak = res["leak"]
    n_warn = res["warn"]
    n_info = res["info"]
    typer.echo(
        f"Audit complete for cycle {cycle_id}: "
        f"{n_tot} findings ({n_leak} leaks, {n_warn} warnings, {n_info} info)."
    )


@app.command("report")
def report_cmd(
    cycle_id: Annotated[int, typer.Option(help="Cycle ID to report on.")],
    compare_cycle_id: Annotated[
        int | None, typer.Option(help="Prior cycle ID to compare against.")
    ] = None,
    out_dir: Annotated[Path | None, typer.Option(help="Output directory for reports.")] = None,
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
) -> None:
    """Generate Markdown summaries, tabular exports (CSV/Parquet), and SVG charts."""
    from .report import generate_report
    from .store import connect, init_db

    conn = connect(db)
    init_db(conn)

    target_dir = out_dir or (Path("artifacts") / "reports" / f"cycle-{cycle_id}")
    outputs = generate_report(
        conn,
        cycle_id=cycle_id,
        out_dir=target_dir,
        compare_cycle_id=compare_cycle_id,
    )
    typer.echo(f"Report generated for cycle {cycle_id} in {target_dir}:")
    for k, p in outputs.items():
        typer.echo(f"  {k}: {p}")


@suite_app.command("build")
def suite_build(
    revision: Annotated[str, typer.Option(help="Exact HF dataset commit sha (40 chars).")],
    out: Annotated[Path, typer.Option(help="Suite JSON to write.")],
    version_label: Annotated[str, typer.Option("--version", help="Suite version label.")],
    seed: Annotated[int, typer.Option()] = 20261031,
    per_stratum: Annotated[int, typer.Option(help="4 -> 12 tasks, 3 -> 9 tasks.")] = 4,
    rejected_file: Annotated[
        Path | None, typer.Option(help="JSON list of rejected instance ids.")
    ] = None,
) -> None:
    """Select a seeded, stratified suite from the pinned SWE-bench Verified revision."""
    rows = ds.load_rows(ds.fetch(revision))
    rejected = json.loads(rejected_file.read_text()) if rejected_file else []
    suite = build_suite(
        rows,
        version=version_label,
        dataset_revision=revision,
        seed=seed,
        per_stratum=per_stratum,
        rejected=rejected,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(suite.model_dump(mode="json"), indent=2) + "\n")
    typer.echo(f"wrote {out}: {len(suite.tasks)} tasks, digest {suite.digest()[:16]}")
    for t in suite.ordered():
        typer.echo(f"  {t.order:2d} {t.difficulty.value:16s} {t.instance_id}")


@suite_app.command("validate")
def suite_validate(
    suite_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    workdir: Annotated[Path, typer.Option(help="Harness scratch dir (git-ignored).")] = Path(
        "data/validation"
    ),
    report: Annotated[Path | None, typer.Option(help="Where to write the JSON report.")] = None,
    repeats: Annotated[int, typer.Option(help="Independent grading runs per control.")] = 2,
    only: Annotated[list[str] | None, typer.Option(help="Restrict to these instance ids.")] = None,
    max_workers: Annotated[int, typer.Option()] = 2,
) -> None:
    """Gold patch must resolve and a no-op patch must not, stable over `repeats` runs."""
    from .tasks.validate import validate as run_validation

    suite = load_suite(suite_path)
    rows = ds.load_rows(ds.fetch(suite.dataset_revision))
    ids = only or [t.instance_id for t in suite.ordered()]
    rows_by_id = {r["instance_id"]: r for r in rows if r["instance_id"] in set(ids)}
    workdir.mkdir(parents=True, exist_ok=True)
    jsonl = workdir / "dataset.jsonl"
    ds.write_jsonl(list(rows_by_id.values()), jsonl)
    results = run_validation(jsonl, rows_by_id, ids, workdir, repeats, max_workers)
    payload = {i: r.to_json() for i, r in results.items()}
    if report:
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(payload, indent=2) + "\n")
    bad = [i for i, r in results.items() if not r.passed]
    for i, r in results.items():
        typer.echo(
            f"{'PASS' if r.passed else 'FAIL'} {i} gold={r.gold} noop={r.noop} {r.error or ''}"
        )
    raise typer.Exit(1 if bad else 0)


@quota_app.command("snapshot")
def quota_snapshot_cmd(
    manual_file: Annotated[
        Path | None, typer.Option(help="Path to operator-supplied JSON snapshot.")
    ] = None,
    db: Annotated[
        Path | None, typer.Option(help="SQLite database path to persist snapshot.")
    ] = None,
    cycle_id: Annotated[
        int | None, typer.Option(help="Cycle ID if persisting to database.")
    ] = None,
) -> None:
    """Capture a quota snapshot from agy /usage or a manual JSON file."""
    from .quota import AgyUsageProbe, ManualQuotaProbe, QuotaProbe, save_quota_snapshot

    probe: QuotaProbe
    if manual_file:
        probe = ManualQuotaProbe(manual_file)
    else:
        probe = AgyUsageProbe()
    snap = probe.snapshot()
    typer.echo(f"Quota snapshot taken at {snap.taken_at} (provenance: {snap.provenance}):")
    for b_id, b in sorted(snap.buckets.items()):
        rem_pct = b.remaining_fraction * 100
        typer.echo(f"  {b_id:<25} {b.window:<8} {rem_pct:6.2f}% (reset: {b.reset_time or 'none'})")
    if db:
        from datetime import datetime

        conn = connect(db)
        init_db(conn)
        if cycle_id is not None:
            c_id = cycle_id
        else:
            row = conn.execute("SELECT id FROM cycles ORDER BY id DESC LIMIT 1").fetchone()
            if row:
                c_id = row[0]
            else:
                now = datetime.now(UTC).isoformat()
                cur = conn.execute(
                    """
                    INSERT INTO experiments (
                        name, manifest_json, manifest_digest,
                        suite_version, suite_digest, created_at
                    ) VALUES ('manual', '{}', 'manual', 'none', 'none', ?)
                    """,
                    (now,),
                )
                c_cur = conn.execute(
                    """
                    INSERT INTO cycles (experiment_id, started_at, status)
                    VALUES (?, ?, 'running')
                    """,
                    (cur.lastrowid, now),
                )
                c_id = c_cur.lastrowid
                conn.commit()

        save_quota_snapshot(conn, cycle_id=c_id, attempt_id=None, phase="manual", snap=snap)
        typer.echo(f"Saved snapshot to {db} (cycle_id={c_id})")


if __name__ == "__main__":
    app()
