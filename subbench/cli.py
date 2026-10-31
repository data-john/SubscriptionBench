"""Command-line entry point. Later phases add: suite / run / grade / audit / report / quota."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from . import __version__
from .config import load_experiment, load_suite
from .store import connect, init_db

app = typer.Typer(no_args_is_help=True, help="SubscriptionBench CLI.")

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


if __name__ == "__main__":
    app()
