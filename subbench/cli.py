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

aa_app = typer.Typer(no_args_is_help=True, help="Artificial Analysis API sync and projections.")
app.add_typer(aa_app, name="aa")

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
    artifacts_dir: Annotated[
        Path, typer.Option("--artifacts-dir", help="Base directory for cycle artifacts.")
    ] = Path("artifacts"),
) -> None:
    """Run an experiment cycle or print a dry run."""
    from .runner import Runner

    runner = Runner(
        manifest_path=manifest,
        db_path=db,
        skip_idle_check=skip_idle_check,
        artifacts_dir=artifacts_dir,
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
    workdir: Annotated[
        Path | None, typer.Option("--workdir", help="Grading working directory.")
    ] = None,
) -> None:
    """Run offline SWE-bench grading for a cycle."""
    from .grading import grade_cycle
    from .store import connect, init_db

    conn = connect(db)
    init_db(conn)
    res = grade_cycle(
        conn, cycle_id=cycle_id, regrade=regrade, max_workers=max_workers, workdir=workdir
    )
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
    agent: Annotated[
        str,
        typer.Option(
            help="Agent quota probe to query when manual-file is omitted (antigravity|codex)."
        ),
    ] = "antigravity",
    db: Annotated[
        Path | None, typer.Option(help="SQLite database path to persist snapshot.")
    ] = None,
    cycle_id: Annotated[
        int | None, typer.Option(help="Cycle ID if persisting to database.")
    ] = None,
) -> None:
    """Capture a quota snapshot from agy /usage, codex rateLimits, or a manual JSON file."""
    from .quota import (
        AgyUsageProbe,
        CodexUsageProbe,
        ManualQuotaProbe,
        QuotaProbe,
        save_quota_snapshot,
    )

    probe: QuotaProbe
    if manual_file:
        probe = ManualQuotaProbe(manual_file)
    elif agent == "codex":
        probe = CodexUsageProbe()
    elif agent == "antigravity":
        probe = AgyUsageProbe()
    else:
        raise typer.BadParameter(f"Unknown agent '{agent}'. Must be 'antigravity' or 'codex'.")
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


@aa_app.command("set-key")
def aa_set_key_cmd(
    storage: Annotated[
        str,
        typer.Option(help="Where to store the key (file|keyring)."),
    ] = "file",
) -> None:
    """Securely store AA_API_KEY via hidden prompt or stdin (never CLI args)."""
    import getpass
    import sys

    from .aa import store_aa_api_key_file, store_aa_api_key_keyring

    if not sys.stdin.isatty():
        raw_key = sys.stdin.read().strip()
    else:
        raw_key = getpass.getpass("Enter Artificial Analysis API key (hidden): ").strip()
    if not raw_key:
        raise typer.BadParameter("Empty API key.")

    if storage == "file":
        dest = store_aa_api_key_file(raw_key)
        typer.echo(f"Stored AA API key in {dest} (mode 0600).")
    elif storage == "keyring":
        store_aa_api_key_keyring(raw_key)
        typer.echo("Stored AA API key in system keyring (service=subbench, key=aa_api_key).")
    else:
        raise typer.BadParameter("storage must be 'file' or 'keyring'.")


@aa_app.command("sync")
def aa_sync_cmd(
    cache_dir: Annotated[
        Path, typer.Option(help="Directory to store cached AA JSON snapshots.")
    ] = Path("data/cache"),
    endpoint: Annotated[
        str, typer.Option(help="Artificial Analysis API endpoint.")
    ] = "https://artificialanalysis.ai/api/v2/language/models/free",
) -> None:
    """Fetch and cache the latest model catalog from Artificial Analysis API v2."""
    from .aa import fetch_aa_models

    res = fetch_aa_models(endpoint=endpoint, cache_dir=cache_dir)
    typer.echo(
        f"Fetched {res.model_count} models from AA at {res.fetched_at} -> {res.cache_path} "
        f"(archive: {res.archive_path})"
    )
    if res.rate_limit_headers:
        hdrs = ", ".join(f"{k}={v}" for k, v in sorted(res.rate_limit_headers.items()))
        typer.echo(f"Rate limit headers: {hdrs}")


@aa_app.command("list")
def aa_list_cmd(
    search: Annotated[str | None, typer.Option(help="Filter by slug or name substring.")] = None,
    cache_path: Annotated[Path, typer.Option(help="Cached AA JSON path.")] = Path(
        "data/cache/aa_models_latest.json"
    ),
) -> None:
    """List cached Artificial Analysis models without making network calls."""
    from .aa import load_aa_cache

    fetched_at, models = load_aa_cache(cache_path)
    needle = search.lower() if search else None
    matched = [
        m for m in models if not needle or needle in m.slug.lower() or needle in m.name.lower()
    ]
    typer.echo(f"Cached AA models ({len(matched)}/{len(models)}, fetched_at={fetched_at}):")
    for m in matched[:50]:
        intel = f"{m.intelligence_index:.1f}" if m.intelligence_index is not None else "N/A"
        p_in = f"${m.price_1m_input:.2f}" if m.price_1m_input is not None else "N/A"
        p_out = f"${m.price_1m_output:.2f}" if m.price_1m_output is not None else "N/A"
        typer.echo(f"  {m.slug:<34} {m.name:<34} intel={intel:<6} in={p_in:<7} out={p_out}")


@aa_app.command("sync-tokens")
def aa_sync_tokens_cmd(
    suite: Annotated[
        str | None, typer.Option(help="Filter Subbench suite_version to pick target slugs.")
    ] = "verified-core-6",
    slugs: Annotated[
        str | None,
        typer.Option(help="Optional comma-separated AA slugs (default: all projected variants)."),
    ] = None,
    cache_path: Annotated[Path, typer.Option(help="Cached AA JSON path.")] = Path(
        "data/cache/aa_models_latest.json"
    ),
    token_usage_path: Annotated[
        Path, typer.Option(help="Output cache path for AA per-task token breakdowns.")
    ] = Path("data/cache/aa_token_usage.json"),
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
) -> None:
    """Fetch per-task token/cost breakdowns from AA public model pages."""
    from .aa import (
        compute_aa_projections,
        fetch_aa_token_breakdowns,
        load_aa_cache,
        project_effort_variants,
    )

    _, models = load_aa_cache(cache_path)
    if slugs:
        target_slugs = [s.strip() for s in slugs.split(",") if s.strip()]
    else:
        conn = connect(db)
        init_db(conn)
        summaries = compute_aa_projections(conn, models, suite_version=suite or None)
        effort_pts = project_effort_variants(summaries, models)
        target_slugs = sorted({p.aa_slug for p in effort_pts if p.aa_slug})

    fetched_at, breakdowns = fetch_aa_token_breakdowns(
        target_slugs, aa_models=models, cache_path=token_usage_path
    )
    matched = sum(1 for s in target_slugs if s.lower() in breakdowns)
    typer.echo(
        f"Synced AA per-task token breakdowns ({matched}/{len(target_slugs)} target slugs, "
        f"{len(breakdowns)} total cached) at {fetched_at} -> {token_usage_path}"
    )


@aa_app.command("project")
def aa_project_cmd(
    suite: Annotated[
        str | None, typer.Option(help="Filter by suite_version (or empty for all).")
    ] = "verified-core-6",
    cache_discount: Annotated[
        float, typer.Option(help="Fallback cached-input multiplier if AA omits cache price.")
    ] = 0.10,
    cache_path: Annotated[Path, typer.Option(help="Cached AA JSON path.")] = Path(
        "data/cache/aa_models_latest.json"
    ),
    token_usage_path: Annotated[
        Path, typer.Option(help="Optional AA per-task token breakdown cache.")
    ] = Path("data/cache/aa_token_usage.json"),
    calibration_path: Annotated[
        Path, typer.Option(help="Optional synthetic 3-prompt quota-weight calibration JSON.")
    ] = Path("artifacts/reports/quota_weight_calibration.json"),
    out_dir: Annotated[
        Path, typer.Option(help="Output directory for AA projection reports.")
    ] = Path("artifacts/reports"),
    use_5h_calibration: Annotated[
        bool,
        typer.Option(
            "--5h-calibration/--no-5h-calibration",
            help="Derive weekly burn from 5h / ratio (3.50 gemini, 2.00 3p) to avoid weekly lag.",
        ),
    ] = True,
    show_ci: Annotated[
        bool,
        typer.Option(
            "--show-ci/--no-show-ci",
            help="Draw horizontal 95% CI error whiskers on measured points in the primary SVG.",
        ),
    ] = False,
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
) -> None:
    """Combine Subbench results with cached AA data to project monthly subscription capacity."""
    from .aa import (
        compute_aa_projections,
        compute_token_weighted_projections,
        load_aa_cache,
        load_aa_token_breakdowns,
        project_effort_variants,
        write_aa_projection_reports,
        write_token_weighted_reports,
    )

    fetched_at, models = load_aa_cache(cache_path)
    conn = connect(db)
    init_db(conn)
    suite_filter = suite if suite else None
    summaries = compute_aa_projections(
        conn,
        models,
        suite_version=suite_filter,
        cache_discount=cache_discount,
        use_5h_calibration=use_5h_calibration,
    )
    effort_points = project_effort_variants(summaries, models)
    outputs = write_aa_projection_reports(
        summaries,
        effort_points=effort_points,
        out_dir=out_dir,
        aa_fetched_at=fetched_at,
        show_ci=show_ci,
    )
    typer.echo(
        f"Computed AA projections for {len(summaries)} configs "
        f"({len(effort_points)} effort points):"
    )
    for s in summaries:
        if s.implied_pool_usd_per_month is not None:
            pool_s = (
                f"${s.implied_pool_usd_per_month:.2f}/mo"
                if s.implied_pool_usd_per_month < 10.0
                else f"${s.implied_pool_usd_per_month:.0f}/mo"
            )
        else:
            pool_s = "N/A"
        tasks_s = (
            f"{s.tasks_per_month_attempted:.0f} AA tasks/mo"
            if s.tasks_per_month_attempted is not None
            else "N/A"
        )
        typer.echo(
            f"  c{s.cycle_id} {s.config_label:<22} -> aa={s.aa_slug or 'NONE':<26} "
            f"pool={pool_s:<10} ({tasks_s})"
        )
    for k, p in outputs.items():
        typer.echo(f"  {k}: {p}")

    # Alternative Token-Weighted projection when both AA token breakdowns & calibration exist
    if token_usage_path.exists() and calibration_path.exists():
        tok_fetched_at, breakdowns = load_aa_token_breakdowns(token_usage_path)
        cal_payload = json.loads(calibration_path.read_text(encoding="utf-8"))
        tw_points = compute_token_weighted_projections(effort_points, breakdowns, cal_payload)
        if tw_points:
            tw_out = write_token_weighted_reports(
                tw_points,
                out_dir=out_dir,
                aa_fetched_at=fetched_at,
                tokens_fetched_at=tok_fetched_at,
            )
            typer.echo(
                f"Computed alternative Token-Weighted projections ({len(tw_points)} points):"
            )
            for k, p in tw_out.items():
                typer.echo(f"  token_weighted_{k}: {p}")


@app.command("calibrate")
def calibrate_cmd(
    models: Annotated[
        str | None,
        typer.Option(
            help="Comma-separated calibration target labels (default: all measured models)."
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print planned probes and prompt sizes without running."),
    ] = False,
    settle_seconds: Annotated[
        int, typer.Option(help="Max post-probe settling wait seconds for 5h/weekly reconciliation.")
    ] = 25,
    timeout_seconds: Annotated[
        int, typer.Option(help="Timeout per individual probe execution in seconds.")
    ] = 300,
    db: Annotated[Path, typer.Option(help="SQLite database path.")] = DEFAULT_DB,
    cache_path: Annotated[Path, typer.Option(help="Cached AA JSON path.")] = Path(
        "data/cache/aa_models_latest.json"
    ),
    out_dir: Annotated[Path, typer.Option(help="Output directory for calibration reports.")] = Path(
        "artifacts/reports"
    ),
) -> None:
    """Run synthetic 3-prompt quota-weight calibration and regress (w_in, w_cache, w_out)."""
    from .aa import load_aa_cache
    from .calibrate import (
        DEFAULT_CALIBRATION_TARGETS,
        build_calibration_prompt,
        build_model_calibration_report,
        run_calibration_for_target,
        write_calibration_reports,
    )

    wanted = {m.strip() for m in models.split(",") if m.strip()} if models else None
    targets = [t for t in DEFAULT_CALIBRATION_TARGETS if wanted is None or t.label in wanted]
    if not targets:
        avail = ", ".join(t.label for t in DEFAULT_CALIBRATION_TARGETS)
        raise typer.BadParameter(f"No matching targets. Available: {avail}")

    if dry_run:
        typer.echo(f"Calibration dry-run ({len(targets)} targets):")
        for t in targets:
            sz_in = len(build_calibration_prompt("input_heavy", t, nonce="dryrun000001"))
            sz_out = len(build_calibration_prompt("output_heavy", t, nonce="dryrun000001"))
            sz_cch = len(build_calibration_prompt("cache_heavy", t, nonce="dryrun000001"))
            typer.echo(
                f"  {t.label:<24} agent={t.agent:<11} model={t.model_id} "
                f"bucket={t.primary_bucket}->{t.weekly_bucket} "
                f"prompt_bytes=({sz_in:,}/{sz_out:,}/{sz_cch:,}) "
                f"repeats=({t.input_repeats}/{t.output_repeats}/{t.cache_repeats})"
            )
        return

    aa_models = []
    if cache_path.exists():
        _, aa_models = load_aa_cache(cache_path)
    conn = connect(db)
    init_db(conn)

    reports = []
    for t in targets:
        typer.echo(f"Calibrating {t.label} ({t.agent}: {t.model_id})...")
        samples = run_calibration_for_target(
            t, settle_max_seconds=settle_seconds, timeout_seconds=timeout_seconds
        )
        rep = build_model_calibration_report(t, samples, conn=conn, aa_models=aa_models)
        reports.append(rep)
        for s in samples:
            typer.echo(
                f"  [{s.probe_kind:<12}] in={s.uncached_input_tokens:>7,} "
                f"cch={s.cache_read_tokens:>8,} out={s.output_tokens:>6,} "
                f"burn={s.weekly_eq_burn_pp:.3f}pp(wk-eq) settled={s.settled} "
                f"({s.wall_seconds:.1f}s)"
            )
        fit = rep.synthetic_fit
        if fit:
            typer.echo(
                f"  -> syn fit: w_in={fit.w_in_pp_per_1m:.3f}, "
                f"w_cache={fit.w_cache_pp_per_1m:.4f}, w_out={fit.w_out_pp_per_1m:.3f} pp/1M "
                f"(out/in={fit.ratio_out_to_in or 0:.2f}x, "
                f"cch/in={fit.ratio_cache_to_in or 0:.3f}x)"
            )
        # Checkpoint report after each model so partial progress is preserved
        write_calibration_reports(reports, out_dir=out_dir)

    outputs = write_calibration_reports(reports, out_dir=out_dir)
    for k, p in outputs.items():
        typer.echo(f"  {k}: {p}")


if __name__ == "__main__":
    app()
