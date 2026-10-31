"""Command-line entry point. Later phases add: suite / run / grade / audit / report / quota."""

from __future__ import annotations

import json
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


if __name__ == "__main__":
    app()
