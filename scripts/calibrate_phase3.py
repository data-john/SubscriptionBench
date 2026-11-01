"""End-to-end calibration script for Milestone 4 (SWE-bench calibration & quota burn).

Measures:
1. Pre-attempt quota snapshot across all buckets.
2. Container provisioning with .git stripping to <initial_sha>.
3. Autonomous agent execution via AntigravityAdapter over egress allowlist proxy.
4. Patch & artifact capture against <initial_sha>.
5. Post-attempt quota snapshot & exact delta computation per bucket.
6. Offline grading via swebench harness (--cache_level instance).
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Any

from subbench.agents.antigravity import AntigravityAdapter
from subbench.agents.prompts import render_prompt
from subbench.provision.capture import capture_patch_in_container, save_attempt_artifacts
from subbench.provision.images import build_overlay_image
from subbench.provision.network import (
    DEFAULT_EGRESS_ALLOWLIST,
    EgressProxyManager,
    launch_task_container,
)
from subbench.provision.workspace import prepare_workspace
from subbench.quota import AgyUsageProbe
from subbench.tasks.validate import find_report, run_harness

REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_JSONL = REPO_ROOT / "data" / "validation" / "dataset.jsonl"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("calibrate")


def get_usage_snapshot() -> dict[str, Any]:
    """Capture raw /usage JSON snapshot from container with persistent silent auth."""
    probe = AgyUsageProbe()
    snap = probe.snapshot()
    buckets = {b_id: b.remaining_fraction for b_id, b in snap.buckets.items()}
    return {"raw": json.loads(snap.raw_json), "buckets": buckets, "ts": time.time()}


def load_task_data(instance_id: str) -> dict[str, Any]:
    """Load task row from dataset.jsonl."""
    if not DATASET_JSONL.exists():
        raise FileNotFoundError(f"Dataset not found at {DATASET_JSONL}")
    for line in DATASET_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("instance_id") == instance_id:
            return row
    raise ValueError(f"Instance {instance_id} not found in {DATASET_JSONL}")


def run_calibration_attempt(
    instance_id: str,
    model_id: str,
    timeout_minutes: int = 30,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    out_dir = output_dir or (REPO_ROOT / "artifacts" / "calibration" / f"{instance_id}_{model_id}")
    out_dir.mkdir(parents=True, exist_ok=True)

    task_data = load_task_data(instance_id)
    repo = task_data["repo"]
    base_commit = task_data["base_commit"]
    problem_statement = task_data["problem_statement"]

    logger.info("Starting calibration attempt for %s on model %s", instance_id, model_id)

    # 1. Pre-attempt quota snapshot
    pre_usage = get_usage_snapshot()
    (out_dir / "usage_pre.json").write_text(json.dumps(pre_usage, indent=2))
    logger.info("Pre-attempt bucket levels: %s", pre_usage["buckets"])

    # 2. Start egress proxy
    proxy_log = out_dir / "proxy.log"
    proxy_mgr = EgressProxyManager(allowlist=DEFAULT_EGRESS_ALLOWLIST, log_path=proxy_log)
    proxy_env = proxy_mgr.start()

    container_name = f"subbench-calib-{instance_id.replace('__', '-').replace('_', '-')}"
    try:
        # 3. Build overlay image & launch task container
        image_tag = build_overlay_image(instance_id)
        cid = launch_task_container(
            image=image_tag,
            container_name=container_name,
            proxy_env=proxy_env,
        )

        # 4. Prepare workspace (.git stripping to <initial_sha>)
        initial_sha = prepare_workspace(container_name, base_commit=base_commit)

        # 5. Render prompt
        rendered = render_prompt(
            "minimal-v1",
            instance_id=instance_id,
            repo=repo,
            workspace="/testbed",
            problem_statement=problem_statement,
        )
        (out_dir / "prompt.txt").write_text(rendered.text)

        # 6. Run agent adapter (uses DEFAULT_SETTINGS with useG1Credits: False and tool permissions)
        adapter = AntigravityAdapter(
            model_id=model_id,
        )
        raw_ndjson = out_dir / "stream_json.ndjson"
        outcome = adapter.run_attempt(
            instance_id=instance_id,
            prompt=rendered,
            container_id=cid,
            timeout_seconds=timeout_minutes * 60,
            raw_output_path=raw_ndjson,
        )
        logger.info(
            "Attempt completed: halt_reason=%s, finished_reason=%s, wall_seconds=%.1fs, turns=%s",
            outcome.halt_reason,
            outcome.finished_reason,
            outcome.wall_seconds,
            outcome.num_turns,
        )

        # 7. Capture patch against <initial_sha>
        patch_text, patch_bytes = capture_patch_in_container(cid, initial_sha=initial_sha)
        save_attempt_artifacts(out_dir, patch_text=patch_text)
        logger.info("Captured patch: %d bytes", patch_bytes)

        # Copy in-container logs if any
        subprocess.run(
            ["docker", "cp", f"{cid}:/tmp/agy-cli.log", str(out_dir / "agy-cli.log")],
            check=False,
            capture_output=True,
        )

    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, check=False)
        proxy_mgr.stop()

    # 8. Post-attempt quota snapshot
    post_usage = get_usage_snapshot()
    (out_dir / "usage_post.json").write_text(json.dumps(post_usage, indent=2))
    logger.info("Post-attempt bucket levels: %s", post_usage["buckets"])

    bucket_deltas = {}
    for b_id, pre_val in pre_usage["buckets"].items():
        post_val = post_usage["buckets"].get(b_id, pre_val)
        bucket_deltas[b_id] = {
            "pre": pre_val,
            "post": post_val,
            "burned_fraction": pre_val - post_val,
            "burned_percentage_points": (pre_val - post_val) * 100.0,
        }
    logger.info("Burned quota per bucket: %s", bucket_deltas)

    # 9. Offline grading via swebench harness
    logger.info("Grading patch offline via swebench...")
    preds_path = out_dir / "predictions.jsonl"
    with open(preds_path, "w") as f:
        f.write(
            json.dumps(
                {
                    "instance_id": instance_id,
                    "model_name_or_path": model_id,
                    "model_patch": patch_text,
                }
            )
            + "\n"
        )

    grading_dir = out_dir / "grading"
    run_id = f"eval-{int(time.time())}"
    run_harness(
        dataset_jsonl=DATASET_JSONL,
        predictions=preds_path,
        ids=[instance_id],
        run_id=run_id,
        workdir=grading_dir,
        max_workers=1,
    )
    report = find_report(grading_dir, model_id, run_id)
    resolved = bool(report and instance_id in report.get("resolved_ids", []))
    logger.info("Grading result: resolved=%s (report=%s)", resolved, report)

    summary = {
        "instance_id": instance_id,
        "model_id": model_id,
        "resolved": resolved,
        "patch_bytes": patch_bytes,
        "halt_reason": outcome.halt_reason,
        "finished_reason": outcome.finished_reason,
        "wall_seconds": outcome.wall_seconds,
        "num_turns": outcome.num_turns,
        "input_tokens": outcome.input_tokens,
        "output_tokens": outcome.output_tokens,
        "thinking_tokens": outcome.thinking_tokens,
        "cache_read_tokens": outcome.cache_read_tokens,
        "tool_calls": outcome.tool_calls,
        "bucket_deltas": bucket_deltas,
        "report": report,
    }
    (out_dir / "calibration_summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description="SubscriptionBench Phase 3 Calibration")
    parser.add_argument("--instance-id", default="pytest-dev__pytest-5809", help="Task instance id")
    parser.add_argument("--model-id", default="gemini-3.8-flash-medium", help="Model id")
    parser.add_argument(
        "--timeout-minutes", type=int, default=25, help="Attempt timeout in minutes"
    )
    parser.add_argument("--output-dir", help="Directory to save calibration artifacts")
    args = parser.parse_args()

    out_dir = Path(args.output_dir) if args.output_dir else None
    res = run_calibration_attempt(
        instance_id=args.instance_id,
        model_id=args.model_id,
        timeout_minutes=args.timeout_minutes,
        output_dir=out_dir,
    )
    print("\n" + "=" * 60)
    print("CALIBRATION RESULT SUMMARY:")
    print(json.dumps(res, indent=2))
    print("=" * 60)


if __name__ == "__main__":
    main()
