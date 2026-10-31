"""Candidate validation through the official swebench harness.

A task enters the suite only if, over `repeats` independent grading runs:
  * the gold patch resolves it (every FAIL_TO_PASS and PASS_TO_PASS test passes), and
  * a no-op patch does NOT resolve it (the FAIL_TO_PASS tests really fail on base).

The harness silently skips empty patches, so the "empty patch" control is a patch that
only adds an unused file: it applies cleanly, the tests run, and nothing is fixed.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

NOOP_PATCH = (
    "diff --git a/SUBBENCH_NOOP.txt b/SUBBENCH_NOOP.txt\n"
    "new file mode 100644\n"
    "index 0000000..e69de29\n"
    "--- /dev/null\n"
    "+++ b/SUBBENCH_NOOP.txt\n"
)
# e69de29 is the empty blob; an empty new file is a valid, applyable, behaviour-free patch.

MODEL_GOLD = "subbench-gold"
MODEL_NOOP = "subbench-noop"


@dataclass
class InstanceResult:
    gold: list[bool] = field(default_factory=list)
    noop: list[bool] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return (
            bool(self.gold)
            and all(self.gold)
            and bool(self.noop)
            and not any(self.noop)
            and self.error is None
        )

    def to_json(self) -> dict[str, Any]:
        return {"gold": self.gold, "noop": self.noop, "error": self.error, "passed": self.passed}


def write_predictions(
    path: Path, ids: Sequence[str], rows_by_id: Mapping[str, Mapping[str, Any]], kind: str
) -> None:
    model = MODEL_GOLD if kind == "gold" else MODEL_NOOP
    with open(path, "w") as f:
        for iid in ids:
            patch = rows_by_id[iid]["patch"] if kind == "gold" else NOOP_PATCH
            f.write(
                json.dumps({"instance_id": iid, "model_name_or_path": model, "model_patch": patch})
                + "\n"
            )


def parse_report(report: Mapping[str, Any]) -> dict[str, bool]:
    """instance_id -> resolved, for every instance the harness reported on."""
    out = {i: True for i in report.get("resolved_ids", [])}
    for key in ("unresolved_ids", "error_ids", "incomplete_ids"):
        out.update({i: False for i in report.get(key, [])})
    return out


def run_harness(
    dataset_jsonl: Path,
    predictions: Path,
    ids: Sequence[str],
    run_id: str,
    workdir: Path,
    max_workers: int = 2,
    timeout: int = 1800,
) -> subprocess.CompletedProcess[str]:
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "swebench.harness.run_evaluation",
        "--dataset_name",
        str(dataset_jsonl.resolve()),
        "--predictions_path",
        str(predictions.resolve()),
        "--instance_ids",
        *ids,
        "--max_workers",
        str(max_workers),
        "--timeout",
        str(timeout),
        "--run_id",
        run_id,
    ]
    return subprocess.run(cmd, cwd=workdir, capture_output=True, text=True, check=False)


def find_report(workdir: Path, model: str, run_id: str) -> dict[str, Any] | None:
    path = workdir / f"{model}.{run_id}.json"
    return json.loads(path.read_text()) if path.exists() else None


def validate(
    dataset_jsonl: Path,
    rows_by_id: Mapping[str, Mapping[str, Any]],
    ids: Sequence[str],
    workdir: Path,
    repeats: int = 2,
    max_workers: int = 2,
) -> dict[str, InstanceResult]:
    results = {i: InstanceResult() for i in ids}
    for rep in range(repeats):
        for kind, model in (("gold", MODEL_GOLD), ("noop", MODEL_NOOP)):
            run_id = f"val-{kind}-{rep}"
            preds = workdir / f"{run_id}.jsonl"
            write_predictions(preds, ids, rows_by_id, kind)
            proc = run_harness(dataset_jsonl, preds, ids, run_id, workdir, max_workers)
            report = find_report(workdir, model, run_id)
            if report is None:
                tail = (proc.stderr or proc.stdout)[-500:]
                for i in ids:
                    results[i].error = f"{kind} run {rep}: no report (rc={proc.returncode}) {tail}"
                continue
            parsed = parse_report(report)
            for i in ids:
                getattr(results[i], kind).append(parsed.get(i, False))
    return results
