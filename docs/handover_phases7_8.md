# Worker Handover: Phases 3–6 Bugfixes, Phase 7 Reporting & Quota-Gated Phase 8

**Date:** 2026-11-01  
**Target scope:**
1. **Milestone 1 (0% model quota):** Fix all critical/high/medium issues uncovered in the Phases 3–6 code review (`runner.py`, `audit.py`, `grading.py`, `quota/base.py`, `antigravity.py`, `pyproject.toml`).
2. **Milestone 2 (0% model quota):** Implement **Phase 7** (`subbench/report/` + `subbench report` CLI + offline tests in `tests/test_report.py`).
3. **Milestone 3 (Quota-gated live execution):** Execute **Phase 8 Stage 1** (`experiments/pilot-flash.yaml`, 3 Flash Medium tasks), run `grade` + `audit` + `report`, and **stop at the operator gate** before running the 36-attempt `baseline.yaml` cycle.

Read these documents first:
1. [`docs/implementation_plan.md`](implementation_plan.md) — Revision 5 architecture, decisions, and quota-conservation policy.
2. [`docs/phase0_findings.md`](phase0_findings.md) — Raw `/usage` schema, bucket reset semantics, `stream-json` schema, and calibration burn rates.
3. [`subbench/store/schema.py`](../subbench/store/schema.py) & [`subbench/config.py`](../subbench/config.py) — SQLite `v2` tables and Pydantic manifest models.

---

## 1. Strict Quota Conservation Policy (CRITICAL)

Subscription quotas on this Google AI Pro account are scarce, shared with interactive development, and weekly pools only reset once a week (`2026-11-07`). **Do not stall development by burning quota:**

1. **Zero LLM inference calls (`0.00%` model quota) during Milestones 1 and 2:**
   * **Do NOT invoke `agy -p <prompt>`** during Milestone 1 bugfixes or Milestone 2 (Phase 7) development and testing.
   * Test all fixes and reporting queries **100% offline** using SQLite fixtures, `FakeAgentAdapter`, `FakeQuotaProbe`, and `tests/fixtures/calibration_sample/`.
   * `agy -p /usage --output-format json` (`subbench quota snapshot`) is confirmed zero-cost (`0` tokens, `0.0000` quota) and may be used once or twice when checking live pre-flight state.
2. **Work efficiently in your own agent turns:**
   * Batch file reads, design edits cleanly, and run targeted `.venv/bin/pytest` checks to minimize your own session's weekly quota footprint.
   * Prepend `export PATH="$HOME/.local/bin:$PATH"` for `uv`, or invoke `.venv/bin/pytest` and `.venv/bin/ruff` directly.
3. **Never auto-launch `experiments/baseline.yaml` inside an interactive coding session:**
   * Running a live cycle while an Antigravity IDE/agent session is open on the same Google account will trigger `external_use` contamination and burn shared weekly Argon/Gemini quota. Follow Milestone 3's two-stage gate strictly.

---

## 2. Milestone 1 — Fix Outstanding Issues from Phases 3–6 (`0.00%` Quota)

Commit Milestone 1 as a clean, atomic commit before starting Phase 7. Every item below was verified in the Phases 3–6 audit and **must** be fixed before any live Phase 8 run:

### 1.1 CRITICAL: Load real SWE-bench `problem_statement` in [`subbench/runner.py`](../subbench/runner.py) & default `suite` group in [`pyproject.toml`](../pyproject.toml)
* **Bug:** `Runner.run` (`subbench/runner.py:533-538`) currently calls `render_prompt(..., problem_statement="Problem placeholder")`! Running Phase 8 as-is would waste real subscription quota sending `"Problem placeholder"` to `agy` on every task.
* **Required fix:**
  1. In [`pyproject.toml`](../pyproject.toml), add:
     ```toml
     [tool.uv]
     default-groups = ["dev", "suite"]
     ```
     so plain `uv sync` and `uv run subbench ...` always include `pyarrow` and `swebench==4.1.0`.
  2. Accept `problem_statements: Mapping[str, str] | None = None` in `Runner.__init__` (so offline unit tests can pass synthetic statements cleanly without touching disk/network).
  3. When `problem_statements` is `None` (and `not dry_run`), load `instance_id -> problem_statement` once at the start of `Runner.run` from the pinned dataset:
     * First check cached parquet via `subbench.tasks.dataset.fetch(self.suite.dataset_revision)` + `load_rows(...)` (falling back to `data/validation/dataset.jsonl` if needed).
     * Verify every planned `task.instance_id` has a non-empty `problem_statement` **before** taking snapshots or provisioning containers; raise a clear `RuntimeError` if any instance is missing.
  4. Add an assertion in [`tests/test_runner.py`](../tests/test_runner.py) verifying `prompt.txt` written in the attempt directory contains the real injected `problem_statement` (never `"Problem placeholder"`).

### 1.2 CRITICAL: Fix real `agy` `stream-json` schema paths & deduplication in [`subbench/audit.py`](../subbench/audit.py)
* **Bugs:**
  1. Real `agy` v1.4.12 `step_update` tool events store parameters and output under `step["tool_info"]["parameters"]["CommandLine"]` and `step["tool_info"]["output"]` (see [`tests/fixtures/calibration_sample/stream_json.ndjson`](../tests/fixtures/calibration_sample/stream_json.ndjson) lines 9–10), **not** `step["args"]` / `step["output"]`. `audit_ndjson_events` currently extracts `cmd_text = ""` on every real `run_command` event!
  2. Each tool invocation emits **two** `step_update` lines (`state="ACTIVE"` followed by `state="DONE"`), which would double-count `web_tool_call` and `run_command` findings unless `state == "ACTIVE"` is skipped (while still catching a truncated final `ACTIVE` event if no `DONE` event ever arrives for that `step_index`).
  3. `git_history_probe` (`line 160`) counts `r"\b[0-9a-f]{40}\b"` across the entire diff output of `git log -p`, which can false-positive if source files in the diff contain SHA-1 test constants. Count actual git commit lines (`r"(?m)^commit\s+[0-9a-f]{40}\b"`, or non-header commit lines for `git log --oneline`) instead.
  4. `audit_cycle` (`line 333`) passes `task_repo=""` instead of deriving `<owner>/<repo>` from `instance_id` (e.g. `inst_id.split("__", 1)[0] + "/" + inst_id.split("__", 1)[1].rsplit("-", 1)[0]` when `"__"` is in `inst_id`).
* **Required fix:**
  * Support `tool_info = step.get("tool_info") or {}` (`tool_info.get("parameters")` and `tool_info.get("output")`) as the primary schema while retaining fallback support for legacy test dicts if needed—and update [`tests/test_audit.py`](../tests/test_audit.py) synthetic events to use the real `agy` `tool_info.parameters.CommandLine` / `tool_info.output` structure (including `ACTIVE` + `DONE` pairs)!
  * Verify `test_audit_real_calibration_sample_fixture_has_zero_leaks` asserts **both** zero `'leak'` findings **and** at least 1 `'info'` `git_history_probe` finding (proving line 10's `git log -n 5 -p ...` in `stream_json.ndjson` was genuinely parsed!).

### 1.3 HIGH: Preserve exact unified diff whitespace in [`subbench/grading.py`](../subbench/grading.py) & auto-materialize `dataset.jsonl`
* **Bug:** `subbench/grading.py:229` reads `Path(patch_path).read_text(...).strip()`. Calling `.strip()` on a unified git diff removes the required trailing `\n` **and** strips the leading space from any trailing empty context line (`" \n"` → `""`), corrupting hunk line counts so `git apply` rejects valid patches!
* **Required fix:**
  * Read `raw_patch = Path(patch_path).read_text(encoding="utf-8", errors="replace")` **without** `.strip()` (ensuring `raw_patch` ends with `"\n"` if non-empty), and test emptiness via `if not raw_patch.strip() or patch_bytes == 0:`.
  * If `dataset_jsonl` (`data/validation/dataset.jsonl`) does not exist at runtime in `grade_cycle`, materialize it automatically into `grading_dir / "dataset.jsonl"` from `subbench.tasks.dataset.fetch(dataset_revision)` + `load_rows` + `write_jsonl` (querying `environment.dataset_revision` for the cycle's experiment).
  * Add a regression test in [`tests/test_grading.py`](../tests/test_grading.py) with a patch ending in an empty context line (`" \n"`) verifying `predictions.jsonl` preserves `" \n"` verbatim!

### 1.4 HIGH: Scope `invalid_reset` to `watched_buckets` in [`subbench/quota/base.py`](../subbench/quota/base.py)
* **Bug:** `compute_attempt_deltas(pre, post)` checks all 5 buckets in `/usage` for `invalid_reset`. If `3p-5h` or `gemini-5h` resets while running an Argon config (`buckets: [gemini-argon-wtu-weekly]`), the Argon attempt is falsely invalidated (`valid = 0`).
* **Required fix:**
  * Add `watched_buckets: Iterable[str] | None = None` to `compute_attempt_deltas(pre, post, watched_buckets=None)`. Compute `BucketDelta` for all buckets, set `BucketDelta.reset_detected: bool` per bucket, and set `AttemptQuotaResult.invalid_reset = True` **only** if a bucket in `watched_buckets` (or any bucket when `watched_buckets is None`) resets invalidly.
  * Pass `watched_buckets=config.buckets` in [`Runner.run`](../subbench/runner.py). Add a unit test in [`tests/test_quota.py`](../tests/test_quota.py) where `3p-5h` resets mid-attempt while `gemini-argon-wtu-weekly` drops cleanly, asserting `invalid_reset is False`.

### 1.5 HIGH: Guarantee container & proxy cleanup via `try / finally` in [`subbench/runner.py`](../subbench/runner.py)
* **Bug:** `task_env = self.provisioner.provision(...)` (`runner.py:575`) is only cleaned up on line 690 before post-snapshot/SQLite operations; an exception in `save_quota_snapshot`, `capture_patch`, SQLite, or `KeyboardInterrupt` leaks `subbench-task-*` and `subbench-proxy` containers.
* **Required fix:** Wrap everything after `task_env = self.provisioner.provision(...)` through patch/artifact capture in `try: ... finally: task_env.cleanup()`, and add a test in [`tests/test_runner.py`](../tests/test_runner.py) verifying `cleanup()` still runs if `adapter.run_attempt` raises an unexpected exception.

### 1.6 MEDIUM: Complete quota-policy enforcement, `stderr.txt` capture & `environment` metadata
* **Quota policy gaps in [`subbench/runner.py`](../subbench/runner.py):**
  1. Enforce `manifest.quota.max_weekly_fraction_per_config_cycle`: track cumulative `valid=1` consumed fraction on `weekly_bucket` per `(cycle_id, config.label)` across the cycle (including resumed attempts); if `>= max_weekly_fraction_per_config_cycle`, append `"weekly_cap_reached:<config.label>"` to `cycles.flags_json` and abort remaining tasks for that config.
  2. Pre-attempt `5h` bucket check: before provisioning/running a task, if any `5h` bucket in `config.buckets` already has `remaining_fraction <= 1e-3`, trigger the `5h` window sleep/cap logic immediately instead of launching a doomed container attempt.
  3. Post-attempt `quota_restricted` handling (`runner.py:734-775`): check `b.window == "5h"` across `config.buckets` (rather than hardcoding `"gemini-5h"`). If `outcome.halt_reason == "quota_restricted"` even though `remaining_fraction > 1e-3` (e.g. request rejected near bucket floor or transient 429): if the config has a `5h` bucket, apply the `5h` window wait/retry rule; otherwise apply `on_weekly_exhaustion` (`abort_config` + `"quota_restricted_abort:<config.label>"` flag) rather than silently dropping the task.
* **`stderr.txt` persistence in [`subbench/agents/antigravity.py`](../subbench/agents/antigravity.py) & [`subbench/runner.py`](../subbench/runner.py):**
  * Add optional `stderr_output_path: Path | None = None` to `AntigravityAdapter.run_attempt` (and `AgentAdapter` protocol / `FakeAgentAdapter`), passing `attempt_dir / "stderr.txt"` from `Runner.run` so `attempts.stderr_path` always points to an actual file on disk.
* **Real metadata & pre-flight checks in [`subbench/runner.py`](../subbench/runner.py):**
  * In `record_environment`, record real `agy --version` output (when binary exists), `docker --version`, `importlib.metadata.version("swebench")` (fallback `"unknown"`), and the SHA-256 hex digest of `TEMPLATES[manifest.prompt_template]` (not the template name!).
  * In `run_preflight`, when `isinstance(self.provisioner, DockerProvisioner)`, verify `Path(manifest.agy_path).is_file()`, `docker info` succeeds, and `verify_base_image_exists(t.instance_id)` is `True` for all scheduled tasks.

---

## 3. Milestone 2 — Phase 7: Metrics, Exports & Reporting (`subbench/report/` & CLI)

Implement `subbench/report/` with **zero new heavy dependencies** (use stdlib `math`, `csv`, `statistics`, `json` + existing `pyarrow` for Parquet and pure-Python SVG generation for charts):

### 3.1 Package layout
* `subbench/report/__init__.py`
* `subbench/report/metrics.py` — SQLite queries, retry/validity resolution rules, Wilson 95% CIs, per-config & paired task matrix dataclasses, longitudinal cycle comparison.
* `subbench/report/render.py` — Self-contained GitHub-Flavored Markdown generator (`summary.md`).
* `subbench/report/export.py` — `attempts.csv`, `attempts.parquet`, `config_metrics.csv`, `config_metrics.parquet` writers.
* `subbench/report/charts.py` — Clean, deterministic pure-Python SVG generators (`efficiency_vs_accuracy.svg` and `task_matrix.svg`).

### 3.2 Exact statistical & accounting formulas (`subbench/report/metrics.py`)
1. **Slot resolution (handling D9 `retry_of` retries):**
   * A scheduled task slot is `(config_id, instance_id, pass_index)`.
   * `planned_slots` per config = `min(len(suite.tasks), manifest.task_limit or len(suite.tasks)) * effective_passes`, where `effective_passes = min(manifest.passes, config.max_passes or manifest.passes)`.
   * For each attempted `(config_id, instance_id, pass_index)` slot, select the **final attempt** (`ORDER BY seq DESC, id DESC LIMIT 1`) and join its `grades` row (`resolved = 1` vs `0`).
   * Report **both**:
     * `resolve_rate_planned = resolved_slots / planned_slots` (penalizes early weekly-quota aborts transparently; **primary accuracy metric**), alongside **Wilson 95% CI** `[ci_low, ci_high]`:
       $$\text{Wilson}_{95\%}(k, n) = \frac{\hat{p} + \frac{z^2}{2n} \pm z\sqrt{\frac{\hat{p}(1-\hat{p})}{n} + \frac{z^2}{4n^2}}}{1 + \frac{z^2}{n}}, \quad z = 1.959964$$
     * `resolve_rate_attempted = resolved_slots / attempted_slots`.
2. **Allowance consumption & paired numerator/denominator rule (CRITICAL):**
   * Per D9, **every `valid = 1` attempt** for `config_id` in `cycle_id`—including interrupted/retried attempts whose replacement ran via `retry_of`—contributes `max(0.0, pre.remaining_fraction - post.remaining_fraction)` to `consumed_fraction[bucket_id]`.
   * Attempts with `valid = 0` (e.g. `invalid_reset`) have unmeasurable quota deltas and are excluded from `consumed_fraction` (`invalid_quota_attempts` count reported prominently).
   * To prevent numerator/denominator mismatch if a final attempt had `valid = 0`: define `resolved_valid_slots` = count of resolved slots whose contributing attempt chain had `valid = 1` throughout. Compute efficiency using `resolved_valid_slots`:
     * `tasks_per_full_bucket = resolved_valid_slots / consumed_fraction[b]` (`None` if `consumed_fraction[b] <= 1e-9`) — i.e., resolved tasks per `100%` (`1.0`) of bucket `b`.
     * `pct_points_per_resolved_task = (consumed_fraction[b] * 100.0) / resolved_valid_slots` (`None` if `resolved_valid_slots == 0`).
   * Compute and surface these metrics for **both**:
     * `primary_bucket` (`manifest.quota.primary_bucket_by_config[label]`: `gemini-5h` for Flash, `gemini-argon-wtu-weekly` for Argon)
     * `weekly_bucket` (`manifest.quota.weekly_bucket_by_config[label]`: `gemini-weekly` for Flash, `gemini-argon-wtu-weekly` for Argon).
3. **Runtime, tokens, failure taxonomy & audit integrity:**
   * `median_wall_seconds` and `max_wall_seconds` (over final attempts, plus `median_resolved_wall_seconds`), `median_tool_calls`, sum of `input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_tokens` across all attempts.
   * Halt/failure breakdown: counts of `completed`, `timeout`, `quota_restricted`, `agent_error`, `infra_error`, `retried_attempts` (`retry_of IS NOT NULL`), `empty_patch` (`patch_bytes == 0`), and `patch_apply_failed` (`patch_bytes > 0 AND patch_applied == 0`).
   * Integrity summary: cycle `status` & `flags_json`, `invalid_quota_attempts` (`valid = 0`), and `audit_findings` counts by severity (`leak`, `warn`, `info`). If `leak > 0`, render a prominent `[!WARNING]` banner at the top of `summary.md` listing the affected `attempt_id`s!
4. **Paired Task × Config Outcome Matrix:**
   * Rows in canonical `suite.ordered()` order (`seq_in_suite`, `instance_id`, `repo`, `difficulty`); columns for each config showing `PASS`/`FAIL`/`SKIP`, `wall_seconds`, `weekly_pp` (`consumed * 100`), and `tool_calls` (for `passes > 1`, show `k/passes` resolved + mean wall/pp).
   * Include paired win/tie/loss summary across tasks attempted by multiple configs.
5. **Longitudinal comparison (`--compare-cycle-id ID`, or auto-detect most recent prior `completed` cycle with matching `suite_digest`):**
   * Compute deltas (`Δ resolve_rate_planned`, `Δ weekly_pp_per_resolved_task`, `Δ total_weekly_pp`, `Δ median_wall_seconds`) per config label/role, plus an environment drift block (`agy_version`, `model_id`, `swebench_version`, `prompt_hash`).

### 3.3 CLI & outputs
* Add `subbench report` in [`subbench/cli.py`](../subbench/cli.py):
  ```bash
  uv run subbench report --cycle-id <ID> [--compare-cycle-id <PRIOR_ID>] [--out-dir <DIR>] [--db data/subbench.sqlite]
  ```
* Default `--out-dir`: `artifacts/reports/cycle-<cycle_id>/`, writing:
  * `summary.md` (embedding `./efficiency_vs_accuracy.svg` and `./task_matrix.svg`, with mandatory F3 disclosure that `default` and `capable` share `gemini-argon-wtu-weekly` while `efficient` uses disjoint `gemini-5h` + `gemini-weekly` pools, and `n=12` directional CI framing)
  * `attempts.csv` & `attempts.parquet`
  * `config_metrics.csv` & `config_metrics.parquet`
  * `efficiency_vs_accuracy.svg` & `task_matrix.svg`
* Add thorough offline unit tests in [`tests/test_report.py`](../tests/test_report.py) covering:
  * Multi-config cycle with a `retry_of` attempt (asserting aborted attempt's quota burn is summed into `consumed_fraction` while resolution is scored once on the retry),
  * `valid = 0` (`invalid_reset`) attempt exclusion from quota efficiency without distorting raw accuracy,
  * Early `weekly_exhausted` abort (`attempted_slots < planned_slots`),
  * Wilson 95% CI edge cases (`0/12`, `6/12`, `12/12`, `0/0`),
  * CSV + Parquet round-trip via `pyarrow.parquet.read_table`, valid `<svg` output, Markdown disclosure & leak-warning banners, and `--compare-cycle-id` longitudinal deltas!

---

## 4. Milestone 3 — Phase 8: Quota-Gated Pilot & Baseline Protocol

> [!CAUTION]
> **Read before spending ANY model quota!**
> Do **NOT** run `experiments/baseline.yaml` inside your worker session! Even `pilot-flash.yaml` should only be run **after** Milestones 1 and 2 are 100% committed, all offline unit tests pass, and live `/usage` headroom is verified.

### Step 1: Pre-Pilot Verification (`0.00%` model quota)
1. Confirm `.venv/bin/pytest` and `.venv/bin/ruff check . && .venv/bin/ruff format --check .` are 100% green and Milestones 1 & 2 are committed.
2. Run dry-run schedule checks for both manifests:
   ```bash
   uv run subbench init-db
   uv run subbench run experiments/pilot-flash.yaml --dry-run
   uv run subbench run experiments/baseline.yaml --dry-run
   ```
3. Take a zero-cost live quota snapshot to inspect current bucket levels and reset timestamps:
   ```bash
   uv run subbench quota snapshot
   ```
   * Verify `gemini-5h` has `>= 25%` remaining and `gemini-weekly` has `>= 15%` remaining before considering Stage 1 (`pilot-flash.yaml` only touches `gemini-5h` and `gemini-weekly`, burning ~3–12 pp of `gemini-5h` and ~1–3 pp of `gemini-weekly`, and **`0.00%` of `gemini-argon-wtu-weekly`**).

### Step 2: Stage 1 — Pilot Execution (`experiments/pilot-flash.yaml`, 3 Flash Medium tasks)
* **Contamination note:** If you are running inside an active Antigravity agent session powered by an Argon or 3P model, note that `pilot-flash.yaml` only watches `[gemini-5h, gemini-weekly]`—however, **ask/confirm or run cleanly** with no concurrent Flash usage so `check_idle` and `detect_external_use` stay clean (`--idle-seconds 15` or default). If another Gemini session is actively drawing `gemini-5h`/`gemini-weekly`, **do not force `--skip-idle-check`**; instead stop and hand the exact terminal commands below to the operator!
* Execute Stage 1 end-to-end:
  ```bash
  uv run subbench run experiments/pilot-flash.yaml
  uv run subbench grade --cycle-id <PILOT_CYCLE_ID>
  uv run subbench audit --cycle-id <PILOT_CYCLE_ID>
  uv run subbench report --cycle-id <PILOT_CYCLE_ID>
  ```
* **Mandatory Pilot Gate Checklist (verify all 6 before considering Stage 2):**
  1. `artifacts/cycles/cycle-<id>/attempt-*/prompt.txt` contains the full SWE-bench issue text (no `"Problem placeholder"`).
  2. `quota_snapshots` has valid `pre` and `post` rows (`valid = 1`, positive `gemini-5h` and `gemini-weekly` deltas, `0.00` Argon burn).
  3. `grades` has 3 rows (`state = 'GRADED'`) and `swebench` preserved all 12 instance images (`docker images | grep sweb.eval | wc -l` == 12).
  4. `audit_findings` has `leak == 0` (and confirms `git_history_probe` / `proxy_blocked_host` `'info'` events parse properly).
  5. `artifacts/reports/cycle-<id>/summary.md`, `attempts.parquet`, `config_metrics.parquet`, and both `.svg` charts exist and look clean.
  6. Free disk space on `/` remains `>= 30 GB`.

### Step 3: STOP for Operator Gate Before Stage 2 (`experiments/baseline.yaml`)
**Stop after Stage 1 (pilot) and report pilot results + live bucket balances to the operator.** Stage 2 (`baseline.yaml`, 36 attempts across `default-argon-medium`, `efficient-flash-medium`, and `capable-argon-high`) **must** be launched by the operator from a **plain host terminal (`tmux`)** with all Antigravity IDE/agent windows closed, after:
1. Operator completes Phase 0 #11 (primary Antigravity Terms of Service check).
2. Operator checks `subbench quota snapshot` and confirms sufficient `gemini-argon-wtu-weekly` and `gemini-weekly` headroom (or waits for the weekly reset on `2026-11-07`, or optionally sets `max_weekly_fraction_per_config_cycle` in `experiments/baseline.yaml` to reserve interactive weekly quota).
3. Operator runs in `tmux`:
   ```bash
   tmux new -s subbench-baseline
   export PATH="$HOME/.local/bin:$PATH"
   cd /home/john/Documents/Codex/SubscriptionBench
   uv run subbench quota snapshot
   uv run subbench run experiments/baseline.yaml
   uv run subbench grade --cycle-id <BASELINE_CYCLE_ID>
   uv run subbench audit --cycle-id <BASELINE_CYCLE_ID>
   uv run subbench report --cycle-id <BASELINE_CYCLE_ID>
   ```
