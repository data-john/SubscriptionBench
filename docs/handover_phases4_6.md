# Worker Handover: Phase 3 Remediation & Phases 4–6 Implementation

**Date:** 2026-11-01  
**Target scope:** Fix outstanding issues found in the Phase 3 review, then implement **Phase 4** (`subbench/quota/`), **Phase 5** (`subbench/runner.py`), and **Phase 6** (`subbench/grading.py`, `subbench/audit.py`), plus their CLI subcommands and offline tests.

Read these documents first:
1. [`docs/implementation_plan.md`](implementation_plan.md) — Revision 4 architecture, state machine, decisions, and quota-conservation policy.
2. [`docs/phase0_findings.md`](phase0_findings.md) — Raw `/usage` schema, bucket reset semantics, `stream-json` schema, and calibration findings.
3. [`subbench/store/schema.py`](../subbench/store/schema.py) & [`subbench/config.py`](../subbench/config.py) — Exact SQLite `v2` tables/constraints and Pydantic manifest models.

---

## 1. Strict Quota Conservation Policy (CRITICAL)

Weekly subscription quotas on this account are tight (`3p-weekly` is ~12.6% remaining until 2026-11-07; `gemini-weekly` is ~89.1%; `gemini-argon-wtu-weekly` is ~91.3%). **Do not stall development by burning quota:**

1. **Zero LLM inference calls (`0.00%` model quota) during this entire batch:**
   * **Do NOT run `agy -p <task_prompt>`** or `scripts/calibrate_phase3.py` during Phases 4–6 development or tests. Calibration is already complete!
   * Test `QuotaProbe`, `Runner`, `Grader`, and `Audit` **100% offline** using `FakeAgentAdapter`, `FakeQuotaProbe`, and existing recorded JSON/NDJSON/patch fixtures on disk.
   * For a live `AgyUsageProbe` Docker check, you may run `agy -p /usage --output-format json` **at most 1–2 times** (confirmed 0 tokens / 0.0000 quota cost).
2. **Work efficiently in your own agent turns:**
   * Batch file reads, plan edits cleanly before writing, and run targeted `pytest` invocations rather than noisy trial-and-error loops so your own session consumes minimal weekly allowance.
3. **Environment note:** Prepend `export PATH="$HOME/.local/bin:$PATH"` for `uv`, or invoke `.venv/bin/pytest` and `.venv/bin/ruff` directly.

---

## 2. Milestone 1 — Fix Outstanding Phase 3 Issues & Repo Hygiene

Commit these fixes as an atomic first milestone before starting Phase 4:

### 1.1 Fix non-functioning timeout & orphaned container processes in [`subbench/agents/antigravity.py`](../subbench/agents/antigravity.py)
* **Bugs:**
  1. `for line in proc.stdout:` blocks until EOF *before* `proc.wait(timeout=timeout_seconds)` is reached—so a hung command inside the container **never times out**.
  2. `proc.kill()` only kills the host-side `docker exec` CLI client, leaving `agy` and child test processes running inside `container_id` (continuing to burn quota and mutate `/testbed` during patch capture).
* **Required fix:**
  * Stream `proc.stdout` and `proc.stderr` via daemon reader threads (writing lines incrementally to `raw_output_path` and collecting `stdout_lines`/`stderr`), while the main thread calls `proc.wait(timeout=timeout_seconds)`.
  * On `subprocess.TimeoutExpired`:
    1. Kill the in-container process tree first (e.g. `docker exec <container_id> pkill -KILL -f /usr/local/bin/agy` and any non-PID1 children in `/testbed`, or pass an `exec_id`/unique marker env var `SUBBENCH_ATTEMPT_RUN=1` and kill processes matching it inside `<container_id>` without killing PID 1 `tail -f /dev/null`).
    2. Kill and reap the host `proc` (`proc.kill(); proc.wait(timeout=5)`), then join reader threads briefly.
  * Add an offline unit test in [`tests/test_antigravity_parser.py`](../tests/test_antigravity_parser.py) (mocking/subprocessing a command like `python3 -c "import time; print('{\"event\":\"init\"}', flush=True); time.sleep(30)"`) verifying `run_attempt(..., timeout_seconds=1)` returns within ~2s with `halt_reason == "timeout"` and preserves the partial line!

### 1.2 Switch task containers to `HTTPS_PROXY`-only & centralize allowlist in [`subbench/provision/network.py`](../subbench/provision/network.py)
* **Bug:** Setting plain-HTTP `HTTP_PROXY` / `http_proxy` caused `psf/requests` offline unit tests (`http://doesnotexist.google.com`, `http://10.255.255.1`) to receive `403 Forbidden` from `subbench-proxy` instead of `ConnectionError`, inflating Flash debugging to 80 tool calls. `agy` only uses HTTPS `CONNECT` on port 443, and the `--internal` Docker network already rejects direct port-80 traffic with `ConnectionError`.
* **Required fix:**
  * Export `DEFAULT_EGRESS_ALLOWLIST: frozenset[str]` (`daily-cloudcode-pa.googleapis.com`, `www.googleapis.com`, `aicode.googleapis.com`, `oauth2.googleapis.com`, `accounts.google.com`) from `subbench/provision/network.py`.
  * In `EgressProxyManager.start()`, return **only** `HTTPS_PROXY`, `https_proxy`, and `NO_PROXY="localhost,127.0.0.1,::1"` (omit `HTTP_PROXY` and `http_proxy`). Update `tests/test_provision_docker.py` if needed.

### 1.3 Add `--disable-slash-commands` & complete web/browser tool deny list in [`subbench/agents/antigravity.py`](../subbench/agents/antigravity.py)
* In `AntigravityAdapter.build_exec_command`, pass `"--disable-slash-commands"` so `/...` tokens in GitHub issue text never trigger slash-command or skill expansion.
* In `DEFAULT_SETTINGS["permissions"]["deny"]`, cover the actual `agy` v1.4.12 tool names seen in `init.tools`:
  * `"search_web"`, `"search_web(*)"`, `"read_url"`, `"read_url(*)"`, `"read_url_content"`, `"read_url_content(*)"`, `"open_browser_url"`, `"open_browser_url(*)"`, `"read_browser_page"`, `"read_browser_page(*)"`, `"capture_browser_screenshot"`, `"capture_browser_screenshot(*)"`, `"capture_browser_console_logs"`, `"click_browser_pixel"`, `"execute_browser_javascript"`, `"list_browser_pages"`, `"browser_subagent"`, `"browser_*"`.

### 1.4 Binary-safe patch capture in [`subbench/provision/capture.py`](../subbench/provision/capture.py)
* **Bug:** `git add -A && git diff --cached --no-color <initial_sha>` emits `Binary files /dev/null and b/foo.db differ` whenever tests leave an untracked binary file (`.db`, `.pkl`, `.pyc`, `.png`), which causes `git apply` in `swebench` to fail the entire patch.
* **Required fix:**
  * After `git add -A`, run `git diff --cached --numstat -z <initial_sha>` (or `--numstat`) to identify binary entries (`added == "-"` and `deleted == "-"`).
  * Unstage or exclude binary paths (`git reset -q <initial_sha> -- <binary_paths>` or pathspec exclusion) before running `git diff --cached --no-color <initial_sha>`.
  * Add a unit test in [`tests/test_capture.py`](../tests/test_capture.py) creating both a valid `.py` edit and an untracked binary file (`b"\x00\x89PNG..."`), asserting the captured patch excludes the binary file and cleanly passes `git apply --check` on a fresh checkout of `initial_sha`!

### 1.5 Add optional `ConfigSpec.max_passes` in [`subbench/config.py`](../subbench/config.py)
* Add `max_passes: int | None = Field(default=None, ge=1)` on `ConfigSpec` (validated `<= ExperimentManifest.passes` when set, or capping that config's executed passes in the runner) so multi-pass manifests can run `k=2` on cheaper configs while keeping `capable-argon-high` at `1` pass. Add unit test in [`tests/test_config.py`](../tests/test_config.py).

### 1.6 Clean up `experiments/calibration/` repo hygiene
* Move the local `experiments/calibration/` directory to git-ignored `artifacts/calibration/` (preserving local files on disk for reference) and remove `experiments/calibration/` from git tracking (`git rm -r --cached experiments/calibration`).
* Copy **one small, PII-free fixture set** (e.g. `model.patch`, `proxy.log`, and `stream_json.ndjson` from `pytest-dev__pytest-5809_gemini-4-argon-medium`—**do NOT include `agy-cli.log`**, which contains user email) into `tests/fixtures/calibration_sample/` for offline Phase 6 tests.
* Update [`scripts/calibrate_phase3.py`](../scripts/calibrate_phase3.py) default `output_dir` to `REPO_ROOT / "artifacts" / "calibration" / ...`, import `DEFAULT_EGRESS_ALLOWLIST` from `subbench.provision.network`, and delegate `/usage` snapshots to Phase 4's `AgyUsageProbe` (removing the hardcoded `"subbench-agy-test"` image tag).

---

## 3. Milestone 2 — Phase 4: Quota Probe (`subbench/quota/` & CLI)

Implement `subbench/quota/`:

### 2.1 Core types & guard logic (`subbench/quota/base.py`)
* `BucketReading` (frozen dataclass / Pydantic): `bucket_id: str`, `window: str` (`"5h"` | `"weekly"`), `remaining_fraction: float`, `reset_time: str | None`.
* `QuotaSnapshot`: `buckets: dict[str, BucketReading]`, `taken_at: str` (ISO-8601 UTC), `provenance: Literal["agy_usage", "manual"]`, `raw_json: str`.
* `QuotaProbe` protocol: `snapshot() -> QuotaSnapshot`.
* Pure, deterministic guard functions (100% unit-tested offline):
  1. `parse_usage_json(raw_stdout: str, taken_at: str | None = None) -> QuotaSnapshot`:
     * Parses `docs/phase0_samples/usage_raw_schema.json` structure; asserts `status == "SUCCESS"` (if present) and non-empty `buckets`; preserves full `raw_stdout` in `raw_json`.
     * Defensive check: scan top-level / `command.data` keys for unexpected `credit` / `overage` fields and raise or surface a warning if non-zero spend appears.
  2. `compute_attempt_deltas(pre: QuotaSnapshot, post: QuotaSnapshot) -> AttemptQuotaResult`:
     * Computes `consumed = pre.remaining_fraction - post.remaining_fraction` per bucket.
     * Detects window reset mid-attempt: if `pre.reset_time != post.reset_time` (when `pre.remaining_fraction < 1.0`, accounting for Phase 0's rolling-until-first-spend behaviour when `pre == 1.0`!) **or** `post.remaining_fraction > pre.remaining_fraction + 1e-6`, sets `invalid_reset = True`.
     * *Important nuance from Phase 0:* Remember an **unused** (`remaining_fraction == 1.0`) rolling bucket shifts `reset_time = now + window` on every call until first spend! If `pre.remaining_fraction == 1.0` and `post.remaining_fraction < 1.0`, `reset_time` locking in on first spend is **normal first use**, NOT an `invalid_reset`! Only treat `reset_time` change as `invalid_reset` when `pre.remaining_fraction < 1.0` or `post.remaining_fraction > pre.remaining_fraction + 1e-6`. Test this exact case in `tests/test_quota.py`!
  3. `detect_external_use(prev_post: QuotaSnapshot, next_pre: QuotaSnapshot, watched_buckets: Iterable[str], threshold: float = 1e-4) -> list[str]`:
     * Returns bucket IDs where `prev_post.remaining_fraction - next_pre.remaining_fraction > threshold` (ignoring buckets whose window reset upward between attempts).
  4. `check_idle(probe: QuotaProbe, watched_buckets: Iterable[str], idle_seconds: int, threshold: float = 1e-4, sleep_fn=time.sleep) -> tuple[QuotaSnapshot, QuotaSnapshot, list[str]]`:
     * Takes two snapshots separated by `idle_seconds` (if `idle_seconds == 0`, returns `(snap, snap, [])` with a single snapshot) and checks `detect_external_use`.
  5. SQLite persistence helper `save_quota_snapshot(conn, *, cycle_id: int, attempt_id: int | None, phase: str, snap: QuotaSnapshot) -> None` writing one row per bucket into `quota_snapshots` matching [`schema.py`](../subbench/store/schema.py#L108-L121).

### 2.2 `AgyUsageProbe` (`subbench/quota/agy_usage.py`) & `ManualQuotaProbe` (`subbench/quota/manual.py`)
* `AgyUsageProbe`:
  * Ensures a thin probe image exists (`subbench-agy:probe` built from `docker/Dockerfile.agy-overlay` on top of any local suite base image, e.g. `get_base_image_for_instance("pytest-dev__pytest-5809")`, so zero new base layers are pulled!) and runs `docker run --rm -v subbench-agy-auth:/agy-state -v <agy_path>:/usr/local/bin/agy:ro subbench-agy:probe agy -p /usage --output-format json`.
* `ManualQuotaProbe`:
  * Loads operator-supplied JSON file (`{bucket_id: {"window": ..., "remaining_fraction": ..., "reset_time": ...}}`) with `provenance="manual"`.
* Wire CLI `subbench quota snapshot [--manual-file PATH] [--db PATH]` in [`subbench/cli.py`](../subbench/cli.py).

---

## 4. Milestone 3 — Phase 5: Sequential Resumable Runner (`subbench/runner.py` & CLI)

Implement `subbench/runner.py` and `subbench run`:

### 3.1 Execution order & state machine
* **Pass-interleaved schedule:**
  ```text
  for pass_index in range(manifest.passes):
      for config in manifest.configs:
          if config.max_passes is not None and pass_index >= config.max_passes:
              continue
          for task in suite.ordered()[:manifest.task_limit]:
              execute_or_skip(...)
  ```
* **Attempt states** (matching `attempts.state` `CHECK` constraint in [`schema.py`](../subbench/store/schema.py#L74-L80)):
  `PENDING → PROVISIONED → QUOTA_PRE → RUNNING → HALTED → CAPTURED → QUOTA_POST`
  *(Offline grading in Phase 6 transitions `QUOTA_POST → GRADED`.)*
* Persist every transition immediately to SQLite so Ctrl-C / power loss leaves consistent state.
* Store artifacts per attempt under `artifacts/cycles/cycle-<cycle_id>/attempt-<attempt_id>/` (`prompt.txt`, `stream_json.ndjson`, `model.patch`, `proxy.log`, `agy-cli.log`, `stderr.txt`) and record relative/canonical paths in `attempts` plus parsed event index rows in `agent_events`.

### 3.2 Pre-flight, contamination guards & bucket-aware exhaustion
1. **Pre-flight (`run_preflight`):**
   * Assert `manifest.quota.use_g1_credits is False`.
   * Verify `agy_path` exists, Docker is reachable, all required suite instance images exist locally, and free disk space on workspace partition ≥ `min_free_gb` (default `15.0` GB).
   * Run `check_idle` on union of `config.buckets` across `manifest.configs`; abort pre-flight (before creating/advancing attempts) if external use is detected (unless `--skip-idle-check` is explicitly passed).
   * Record `experiments`, `configs`, `environment` (`agy_version`, OS, kernel, Docker version, `swebench` version, dataset + revision, `prompt_hash`, `execution_mode='container'`), and `cycles` row.
2. **Inter-attempt contamination guard:**
   * Compare previous attempt's `QUOTA_POST` snapshot with current attempt's `QUOTA_PRE` snapshot on `config.buckets`. If `detect_external_use` triggers, record `"external_use:<bucket>"` in `cycles.flags_json` and annotate `attempts.invalid_reason` if overlapping.
3. **Mid-attempt window reset:**
   * If `compute_attempt_deltas` reports `invalid_reset`, set `attempts.valid = 0` and `attempts.invalid_reason = "invalid_reset"` (keeping patch/transcript for grading, excluded from efficiency denominator).
4. **Quota exhaustion & retry rules (D9 + Milestone 1 `QuotaPolicy`):**
   * An attempt halts with `halt_reason == "quota_restricted"` (or `QUOTA_PRE` already shows `remaining_fraction <= 1e-4` on a required bucket):
     * Check which bucket in `config.buckets` is exhausted (`remaining_fraction <= 1e-3`, or fallback to primary bucket if `halt_reason == "quota_restricted"`):
     * **Weekly bucket exhausted (`window == "weekly"`)** or cumulative cycle burn on `weekly_bucket` ≥ `max_weekly_fraction_per_config_cycle`:
       * Under `on_weekly_exhaustion == "abort_config"`, mark the config exhausted for this cycle, append flag `"weekly_exhausted:<config.label>"` to `cycles.flags_json`, and skip remaining tasks for this config without sleeping!
     * **5h bucket exhausted (`window == "5h"`):**
       * Keep the interrupted attempt row in SQLite (its consumed quota still counts!).
       * Increment `windows_used` for `(cycle_id, config.label)` (starts at `1`; each `5h` reset wait consumes another window).
       * If `windows_used < manifest.quota.max_5h_windows_per_config_cycle`: sleep until `reset_time` + 30s buffer (using an injectable `sleep_fn` and `now_fn` so unit tests run in milliseconds!), then create a new attempt row with `retry_of = original_attempt_id` and run the task from scratch.
       * If `windows_used >= max_5h_windows_per_config_cycle`: append `"max_5h_windows_reached:<config.label>"` to `cycles.flags_json` and stop attempting further tasks for this `(config, pass)`.
5. **Resume semantics (`subbench run <manifest.yaml> [--cycle-id ID]`):**
   * If resuming an existing `running` cycle: skip `(config_id, instance_id, pass_index)` tuples that already have a terminal attempt (`state IN ('QUOTA_POST', 'GRADED')` with `halt_reason != 'quota_restricted'` and not crashed mid-run). Any stale mid-flight attempt (`state IN ('PENDING', 'PROVISIONED', 'QUOTA_PRE', 'RUNNING', 'HALTED', 'CAPTURED')`) is finalized as `state='QUOTA_POST'`, `halt_reason='infra_error'`, `error_text='interrupted_before_completion'`, and retried via a new `retry_of` row.
   * Support `--dry-run` (validates manifest, suite, DB, and prints exact planned `(seq, pass_index, config.label, instance_id)` schedule with zero Docker/quota calls).

---

## 5. Milestone 4 — Phase 6: Offline Grading & Leak Audit (`subbench/grading.py`, `subbench/audit.py`)

### 4.1 Offline batch grader (`subbench/grading.py` & `subbench grade`)
* Queries ungraded attempts for a cycle (`state = 'QUOTA_POST'` — plus optional `--regrade` to re-run even `'GRADED'` attempts, since grading is offline and repeatable!).
* **Empty-patch & non-completed handling:**
  * If `patch_bytes == 0` or `patch_path` is empty (e.g. `timeout` / `agent_error` / no edits): insert `grades` row directly (`resolved=0`, `patch_applied=0`, `error_text='empty_patch'`) and mark `attempts.state = 'GRADED'` **without** launching a SWE-bench Docker container (`swebench` skips empty patches anyway).
* **Unique `instance_id` batching (CRITICAL `swebench` constraint):**
  * A single `predictions.jsonl` passed to `swebench.harness.run_evaluation` cannot contain duplicate `instance_id`s! Because a cycle tests multiple configs (and potentially multiple passes/retries) on the **same** 12 `instance_id`s, group ungraded non-empty attempts into batches where every `instance_id` is unique (e.g. batch by `(config_id, pass_index, retry_ordinal)` with `model_name_or_path = f"subbench-c{cycle_id}-cfg{config_id}-p{pass_index}-r{retry_ord}"`).
* Run `swebench.harness.run_evaluation` with `--cache_level instance` and `--max_workers` (default `2`).
* Parse both the run-level summary JSON and per-instance `logs/run_evaluation/<run_id>/<model>/<instance_id>/report.json` (when created by `swebench`) to extract:
  * `resolved` (`0`/`1`), `patch_applied` (`0`/`1`), `fail_to_pass_json`, `pass_to_pass_json`, `report_path`, `log_path`, `swebench_version`.
* Insert into `grades` and update `attempts.state = 'GRADED'`.

### 4.2 Transcript & proxy leak audit (`subbench/audit.py` & `subbench audit`)
* Scans every attempt in a cycle idempotently (`DELETE FROM audit_findings WHERE attempt_id = ?` before inserting fresh findings so `subbench audit --cycle-id ID` is safely repeatable).
* **Avoid false positives on passive repository content!** Many SWE-bench repos contain `https://github.com/<owner>/<repo>/issues/...` inside their own `CHANGELOG.rst` or docstrings (e.g. `read_file` / `git grep` output in `pytest-5809`). Scope checks to **agent actions and network traffic**:
  1. **`web_tool_call` (`severity='leak'` if completed with output, `'warn'` if denied/errored):** Any `step_update` where `tool_name` is a web/browser tool (`search_web`, `read_url_content`, `read_url`, `open_browser_url`, `read_browser_page`, `browser_*`).
  2. **`outbound_url_command` (`severity='leak'` or `'warn'`):** `run_command` parameters invoking network clients (`curl`, `wget`, `git clone`, `git fetch`, `git remote add`, `pip install`, `urllib`, `requests.get`, `http.client`) targeting external URLs—especially `github.com`, `gitlab.com`, `huggingface.co`, `stackoverflow.com`, or raw commit/PR URLs (`severity='leak'` if referencing the task's repo/issue/PR or `SWE-bench`). *(Ignore localhost/`127.0.0.1` test commands and inline unit-test mocks.)*
  3. **`git_history_probe` (`severity='info'` or `'warn'`):** `run_command` probing `git log --all`, `git reflog`, `git branch -a`, `git tag`, `git remote -v` (`'info'` when output confirms only the single `subbench` `base` commit; `'leak'` if >1 historical commits prior to `subbench` ever appear, catching any workspace-stripping regression!).
  4. **`proxy_blocked_host` (`proxy.log` scan):**
     * Ignore benign background Google CLI/OS noise (`play.googleapis.com`, `lh3.googleusercontent.com`, `antigravity-cli-auto-updater-*.run.app`) and RFC5737 / `doesnotexist.google.com` offline test probes (or aggregate as a single `'info'` summary).
     * Flag blocked attempts to code/Q&A hosts (`github.com`, `raw.githubusercontent.com`, `api.github.com`, `stackoverflow.com`, `huggingface.co`) as `severity='warn'` (attempted leak blocked by proxy) and any **allowed** non-Google-API host (`allowed == True` outside `DEFAULT_EGRESS_ALLOWLIST`) as `severity='leak'`!

---

## 6. Required Tests & Definition of Done

All new tests must run offline in `< 2s` under `.venv/bin/pytest` with **zero `agy` model calls**:
- [ ] `tests/test_antigravity_parser.py` & `tests/test_capture.py`: Updated for real 1s subprocess timeout test, `--disable-slash-commands`, expanded web-tool `deny` list, and binary-file patch exclusion (`git apply --check`).
- [ ] `tests/test_quota.py`: Tests `parse_usage_json` on `docs/phase0_samples/usage_raw_schema.json`, normal first-spend rolling `reset_time` lock-in vs real mid-attempt `invalid_reset`, `detect_external_use`, `check_idle`, `ManualQuotaProbe`, and `quota_snapshots` DB persistence.
- [ ] `tests/test_runner.py`: End-to-end SQLite state-machine tests using `FakeAgentAdapter` + `FakeQuotaProbe` + mocked provisioner covering:
  * Pass-interleaved ordering (`passes=2` + `ConfigSpec.max_passes=1` on `capable`),
  * `gemini-5h` exhaustion → simulated sleep (`sleep_fn`) → `retry_of` attempt → `max_5h_windows_per_config_cycle` cap,
  * `gemini-argon-wtu-weekly` exhaustion → immediate `abort_config` without sleeping,
  * `invalid_reset` (`valid=0`) and inter-attempt `external_use` flagging,
  * Mid-run crash recovery / resume (`subbench run --cycle-id ...`) without re-running completed tasks.
- [ ] `tests/test_grading.py`: Tests multi-config unique-`instance_id` batching, empty-patch short-circuiting, report parsing (`resolved`, `patch_applied`, `FAIL_TO_PASS` / `PASS_TO_PASS`), and `QUOTA_POST → GRADED` transition.
- [ ] `tests/test_audit.py`: Tests `audit.py` on the real fixture in `tests/fixtures/calibration_sample/` (confirming zero false-positive `'leak'` findings on `CHANGELOG.rst` / `pastebin.py` file reads and `play.googleapis.com` proxy noise) plus synthetic leak transcripts (`search_web`, `curl https://github.com/pytest-dev/pytest/pull/5809`, unstripped multi-commit `git log`).
- [ ] CLI smoke tests in `tests/test_cli.py` for `subbench run --dry-run experiments/baseline.yaml`, `subbench quota snapshot --manual-file ...`, `subbench grade`, and `subbench audit`.
- [ ] Clean repo: `experiments/calibration/` removed from git (only YAML manifests in `experiments/`), `.venv/bin/pytest` green, `.venv/bin/ruff check .` and `.venv/bin/ruff format --check .` clean.
