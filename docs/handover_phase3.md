# Worker Handover: Phases 0–2 Completion & Phase 3 Implementation

> [!NOTE]
> **Status: Completed (commits `420e259`, `9617812`, `4e2589b`).** Superseded by [`docs/handover_phases4_6.md`](handover_phases4_6.md), which covers targeted Phase 3 follow-up fixes and Phases 4–6 implementation.

**Date:** 2026-11-01  
**Target scope:** Finish remaining/remediation items from Phases 0–2, implement **Phase 3** (provisioning, egress allowlist proxy, prompt templates, and `AntigravityAdapter`), and run the **SWE-bench calibration** (Phase 0 Item 10) to inform D15.

Read these documents first before writing code:
1. [`README.md`](../README.md) — Benchmark goal and rules (no API-token cost estimates; sequential execution; autonomous completion only).
2. [`docs/implementation_plan.md`](implementation_plan.md) — Revision 3 architecture, phased plan, and resolved decisions (especially **D13a**, **D13c fallback**, and **D14**).
3. [`docs/phase0_findings.md`](phase0_findings.md) — Empirical `agy` CLI, `/usage` bucket, and `stream-json` findings.
4. [`docs/suite_verified-mvp-v1.md`](suite_verified-mvp-v1.md) — Frozen 12-task suite and pre-pulled Docker images.

---

## 1. Current Repository & Environment State

* **Branch:** `main`, clean test suite (`33 passed`), clean `ruff check` and `ruff format --check`.
* **Toolchain quirk:** Non-login shells may not include `~/.local/bin` on `PATH`. Either prepend `export PATH="$HOME/.local/bin:$PATH"` for `uv`, or invoke `.venv/bin/pytest` and `.venv/bin/ruff` directly.
* **`agy` binary:** `/home/john/.local/bin/agy` (`v1.4.12`, dynamically linked x86-64 ELF; runs cleanly on Ubuntu 22.04 `glibc 2.35` used by the SWE-bench images).
* **Confirmed D14 config mapping** (already updated in [`experiments/baseline.yaml`](../experiments/baseline.yaml) and [`experiments/pilot-flash.yaml`](../experiments/pilot-flash.yaml)):
  * `default` → `gemini-4-argon-medium` (`buckets: [gemini-argon-wtu-weekly]`)
  * `efficient` → `gemini-3.8-flash-medium` (`buckets: [gemini-5h, gemini-weekly]`)
  * `capable` → `gemini-4-argon-high` (`buckets: [gemini-argon-wtu-weekly]`)
* **Confirmed D13 execution mode:** **Option (a)** — run `agy` *inside* the SWE-bench task container using a one-time official `agy` OAuth login stored in a named Docker volume (`subbench-agy-auth`). **Option (c)** (host `agy` + bind-mounted `--network none` `testbed` container) is the documented fallback if container keyring persistence fails (see §6).
* **Disk headroom:** `/` has **~37 GB free** (96% used on a 916 GB ext4 partition). All 12 `swebench/sweb.eval.x86_64.*:latest` instance images (~13 GB shared) are already pulled and listed in [`suites/verified-mvp-v1.images.json`](../suites/verified-mvp-v1.images.json).

---

## 2. Hard Guardrails (Do Not Violate)

1. **Never extract, copy, dump, or bind-mount raw OAuth tokens from the host keyring.** Container authentication under D13(a) must use `agy`'s own official OAuth sign-in flow into a dedicated Docker volume (`subbench-agy-auth`).
2. **Never use `GEMINI_API_KEY` or GCP ADC (`AGY_ADC_AUTH`).** Those bill API/GCP credits rather than the consumer Google AI Pro subscription (F1).
3. **Never intentionally drain the subscription account** to test 0%-quota errors. Keep probe and calibration runs minimal and purposeful.
4. **Be aware of quota contamination:** If this worker session itself runs on an Antigravity Gemini model, its own turns draw from the corresponding Gemini pool (whereas Claude/GPT sessions draw from `3p-*`). Always take `/usage` snapshots immediately before and after each `agy` subprocess invocation so only that subprocess's delta is measured.
5. **Never edit [`suites/verified-mvp-v1.json`](../suites/verified-mvp-v1.json) in place.** It is frozen.
6. **Protect `/` disk space (~37 GB free):**
   * Never run `cp -r /testbed` or mutate `/testbed` inside a `Dockerfile` `RUN` step (that duplicates ~0.5–1 GB per image × 12 images). Strip `.git` history **at container runtime**.
   * Always pass `--cache_level instance` to `swebench.harness.run_evaluation` so grading does not delete pre-pulled instance images.

---

## 3. Milestone 1 — Phase 1 & 2 Remediation (Offline Code + Unit Tests)

Complete these fixes first and commit them as a clean unit before touching Docker/auth:

### 1.1 Fix disjoint-bucket policy in [`subbench/config.py`](../subbench/config.py) & manifests
* **Problem:** `QuotaPolicy.common_unit_bucket` is hardcoded to `"gemini-weekly"`, which Argon models never touch (see [`usage_before_argon_probe.json`](phase0_samples/usage_before_argon_probe.json) vs [`usage_after_argon_probe.json`](phase0_samples/usage_after_argon_probe.json): Argon drains **only** `gemini-argon-wtu-weekly`). Also, `KNOWN_BUCKETS` is unused, and `max_windows_per_config_cycle = 2` makes no sense for 7-day buckets.
* **Required changes:**
  1. Validate bucket IDs in `ConfigSpec.buckets`, `QuotaPolicy.primary_bucket_by_config`, and `QuotaPolicy.weekly_bucket_by_config` against `KNOWN_BUCKETS`.
  2. Replace `common_unit_bucket: NonEmpty = "gemini-weekly"` with `weekly_bucket_by_config: dict[NonEmpty, NonEmpty] = Field(default_factory=dict)`.
  3. In `ExperimentManifest._validate`, verify that `primary_bucket_by_config` and `weekly_bucket_by_config` only reference configured labels **and** that each mapped bucket appears in that `ConfigSpec.buckets` list.
  4. Replace `max_windows_per_config_cycle` with bucket-aware fields:
     * `max_5h_windows_per_config_cycle: int = Field(default=2, ge=1)`
     * `on_weekly_exhaustion: Literal["abort_config", "wait"] = "abort_config"`
     * `max_weekly_fraction_per_config_cycle: float | None = Field(default=None, gt=0, le=1.0)`
  5. Update [`experiments/baseline.yaml`](../experiments/baseline.yaml), [`experiments/pilot-flash.yaml`](../experiments/pilot-flash.yaml), and [`tests/test_config.py`](../tests/test_config.py) accordingly (`weekly_bucket_by_config`: `efficient-flash-medium: gemini-weekly`, `default-argon-medium: gemini-argon-wtu-weekly`, `capable-argon-high: gemini-argon-wtu-weekly`).

### 1.2 Track `execution_mode` in [`subbench/store/schema.py`](../subbench/store/schema.py)
* Add `execution_mode TEXT NOT NULL DEFAULT 'container' CHECK (execution_mode IN ('container', 'hybrid_host'))` to `environment` (via a `v2` migration in `MIGRATIONS`, or updating `v1` since no `data/subbench.sqlite` exists yet—prefer adding `MIGRATIONS[1]` as `v2` so migration progression itself gets a unit test in [`tests/test_store.py`](../tests/test_store.py)!).

### 1.3 Phase 2 minor fixes
* **[`subbench/tasks/validate.py`](../subbench/tasks/validate.py):** Pass `"--cache_level", "instance"` in `run_harness()` so validation never prunes pulled instance images. Update [`tests/test_validate.py`](../tests/test_validate.py) to assert `--cache_level instance` is constructed.
* **[`subbench/tasks/dataset.py`](../subbench/tasks/dataset.py):** `sha256()` is currently dead code despite `fetch()`'s docstring (`cached; sha256 recorded`). Write/verify a `.sha256` sidecar file next to `verified-<rev12>.parquet` in `fetch()` (and add a unit test).

---

## 4. Milestone 2 — Remaining Phase 0 Spikes (Container Auth, Egress, Tool Deny)

### 2.1 In-container `agy` & D13(a) auth bootstrap (Phase 0 Item 8)
* **Technical context:** Linux `agy` uses `libsecret` over D-Bus (`org.freedesktop.secrets`). SWE-bench instance images are Ubuntu 22.04 (`jammy`, `glibc 2.35`) without a running D-Bus session or keyring daemon.
* **What to build (`docker/` + `scripts/bootstrap_container_auth.sh`):**
  1. Create a thin base/overlay setup (e.g. `docker/Dockerfile.agy-overlay` parameterized by `ARG BASE_IMAGE` + `docker/entrypoint-agy.sh`) that:
     * Installs minimal runtime packages if absent (`dbus-x11`, `gnome-keyring`, `libsecret-1-0`, `ca-certificates`) and cleans `/var/lib/apt/lists/*` in the same `RUN` layer. **Do not touch `/testbed` or `/opt/miniconda3/envs/testbed` in the Dockerfile.**
     * Mounts `/home/john/.local/bin/agy` read-only at `/usr/local/bin/agy` (so no 100 MB binary is duplicated across 12 image tags) and mounts named volume `subbench-agy-auth` at `/agy-state`.
     * Uses `docker/entrypoint-agy.sh` to set `HOME=/agy-state` (or symlink `~/.gemini` and `XDG_DATA_HOME=/agy-state/local/share`), start `dbus-run-session`, unlock `gnome-keyring-daemon` (`printf '' | gnome-keyring-daemon --unlock --components=secrets`), and `exec` the target command.
  2. Provide `scripts/bootstrap_container_auth.sh` for the **one-time interactive operator login**:
     * Runs an interactive container (`docker run --rm -it --network host -v subbench-agy-auth:/agy-state ...`) **with no task repo mounted** so the operator can open the printed OAuth URL in their host browser and let the `localhost:<callback_port>` redirect reach `agy` inside the container. *(Note: `--network host` is ONLY for this one-time auth bootstrap container, never for task containers!)*
     * Ask the operator to complete that one-time login if `subbench-agy-auth` is not yet authenticated.
  3. **Go/No-Go Verification Gate:**
     * Run **two separate, non-interactive `docker run --rm`** invocations mounting `subbench-agy-auth` (without `--network host`):
       1. `agy -p /usage --output-format json` → verify valid bucket JSON and full-precision floats.
       2. A 1-turn non-SWE-bench smoke prompt (`--model gemini-3.8-flash-medium --settings '{"useG1Credits": false}'`) → verify `result.status == "SUCCESS"`.
     * **If token persistence fails across separate `docker run --rm` containers** despite `dbus-run-session` + `gnome-keyring-daemon`: **STOP and report the exact failure to the operator** before switching to **Option (c)** (§6).

### 2.2 Raw `/usage` inspection (Phase 0 Item 5 carry-over)
* Save one **complete, unstripped** JSON response from `agy -p /usage --output-format json` (redacting any email/account identifier if present) under `docs/phase0_samples/usage_raw_schema.json` and document in [`docs/phase0_findings.md`](phase0_findings.md) whether any AI Credits / G1 balance fields exist in the payload so Phase 4's probe can assert they never decrease.

### 2.3 Egress host capture & `permissions.deny` verification (Phase 0 Item 9)
* Run the container through a logging HTTP/HTTPS `CONNECT` proxy (see Milestone 3.2) during a smoke prompt and record every hostname contacted:
  * Expected required hosts: `daily-cloudcode-pa.googleapis.com` and OAuth token refresh endpoints (`oauth2.googleapis.com` / `accounts.google.com` if refreshed).
  * **Auto-updater check (Phase 0 Item 7):** Test keeping the Cloud Run auto-updater host **blocked** by the proxy. Confirm `agy` logs at most a harmless warning and still completes `result.status == "SUCCESS"`—this prevents mid-benchmark `agy` version drift!
* **Server-side web tool block:** Because `search_web` rides over `daily-cloudcode-pa.googleapis.com`, test `--settings` `permissions.deny` rules in a cheap probe prompt ("Use search_web to look up example.com and tell me if the tool was denied"):
  * Check whether `--dangerously-skip-permissions` still enforces `permissions.deny` (`Deny > Ask > Allow`), **or** whether headless container runs should omit `--dangerously-skip-permissions` and instead pass `--settings` with explicit `permissions.allow` (`command(*)`, file tools) + `permissions.deny` (`search_web`, `read_url`, browser tools).
  * Record the exact working CLI flag + `--settings` JSON combination in [`docs/phase0_findings.md`](phase0_findings.md).

---

## 5. Milestone 3 — Phase 3 Implementation (`provision/`, `agents/`, `docker/`)

### 3.1 Egress allowlist proxy (`docker/egress_proxy.py` + `subbench/provision/network.py`)
* Implement a zero-dependency Python `asyncio` HTTP `CONNECT` + HTTP proxy (`docker/egress_proxy.py`, unit-testable directly in Python!) enforcing an exact hostname allowlist (captured in Milestone 2.3):
  * Rejects raw IP literals, non-443 ports, and any host outside the allowlist with `403 Forbidden` (never resolving/connecting to disallowed hosts).
  * Writes structured JSONL lines (`ts`, `client`, `method`, `host`, `port`, `allowed`) to a per-attempt `proxy.log`.
* Network topology in `subbench/provision/network.py`:
  * `subbench-internal`: Docker `--internal` bridge network (no external gateway). Task containers attach **only** to `subbench-internal`.
  * `subbench-egress`: Standard Docker bridge network with internet egress. The proxy container attaches to **both** `subbench-egress` and `subbench-internal`.
  * Task containers receive `HTTP_PROXY` / `HTTPS_PROXY` / `http_proxy` / `https_proxy` pointing to the proxy container on `subbench-internal`, plus `NO_PROXY=localhost,127.0.0.1`.

### 3.2 Task container provisioner & `.git` stripping (`subbench/provision/`)
* `subbench/provision/images.py`: Builds/tags (if not already cached) the thin `subbench-agy:<instance_key>` overlay image on top of `swebench/sweb.eval.x86_64.<instance_key>:latest`.
* `subbench/provision/workspace.py`: Starts the task container on `subbench-internal` and prepares `/testbed`:
  1. Assert `git -C /testbed rev-parse HEAD` equals `task.base_commit`.
  2. Strip all history, tags, branches, stashes, and remotes: `rm -rf /testbed/.git` followed by `git init`, `git config user.name subbench`, `git config user.email subbench@local`, `git add -A`, `git commit -m "base"`, and capture `<initial_sha>`.
  3. Assert `git rev-list --count HEAD == 1`, `git remote` == empty, `git tag` == empty.
  4. **Editable-install sanity check:** Verify `/opt/miniconda3/envs/testbed/bin/python` can still import the repo package after `.git` replacement (watch `setuptools_scm` repos in our suite: `pytest-dev__pytest-*` and `astropy__astropy-13579`; if `setuptools_scm` needs `SETUPTOOLS_SCM_PRETEND_VERSION` only during re-install, verify normal `pytest` invocation works).

### 3.3 Patch & artifact capture (`subbench/provision/capture.py`)
* **Handle agent commits properly:** Autonomous agents often run `git commit` even when told not to. Diffing only against `HEAD` loses committed edits!
* Capture patch inside `/testbed` against `<initial_sha>`:
  1. Ensure `agy` writes any CLI log outside `/testbed` (`--log-file /tmp/agy-cli.log`) so `/testbed` stays clean of harness files.
  2. Run `git -C /testbed add -A && git -C /testbed diff --cached --no-binary <initial_sha>` to produce `model.patch`.
  3. Record `patch_bytes` and copy `/tmp/agy-cli.log` and `proxy.log` into the attempt's `artifacts/` directory.

### 3.4 Versioned prompt templates (`subbench/agents/prompts.py`)
* Implement `render_prompt(template_id: str, *, instance_id: str, repo: str, workspace: str, problem_statement: str) -> RenderedPrompt` returning `(text, template_id, sha256_hash)`.
* Define `minimal-v1` (matching `experiments/*.yaml`):
  * Gives the repository name, `/testbed` working directory (noting its Python/test environment is pre-activated on `PATH`), and exact `problem_statement`.
  * Instructs the agent to investigate, reproduce, implement the fix, run relevant tests autonomously without asking questions, leave changes in `/testbed` without pushing or searching the web for upstream issues/PRs. No hints about specific test filenames or `FAIL_TO_PASS` targets.

### 3.5 `AgentAdapter` protocol & `AntigravityAdapter` (`subbench/agents/base.py`, `subbench/agents/antigravity.py`)
* Define `AgentAdapter` protocol + structured `AttemptOutcome` dataclass (`halt_reason`, `exit_code`, `finished_reason`, `agy_version_pre`, `agy_version_post`, `wall_seconds`, `num_turns`, token fields, `tool_calls`, `events: list[ParsedEvent]`, `error_text`).
* Implement `parse_stream_json(lines: Iterable[str]) -> StreamSummary`:
  * Tolerates partial/truncated final lines on timeout without crashing.
  * Extracts `agent_events` index rows (`idx`, `event`, `step_type`, `state`, `tool_name`, `duration_seconds`, `total_tokens`) matching [`schema.py`](../subbench/store/schema.py#L123-L134).
  * Classifies outcome strictly from `result` (never `exit_code == 0` alone!):
    * Missing `result` after wall-clock kill → `timeout`
    * `result.status == "SUCCESS"` → `completed`
    * `result.status == "ERROR"` with quota/limit/exhausted indicators → `quota_restricted`
    * Other `result.status == "ERROR"` → `agent_error` (or `infra_error` on auth/container failure)
* Ensure `/opt/miniconda3/envs/testbed/bin` is prepended to `PATH` inside the `agy` execution environment so bare `pytest` / `python` commands hit `testbed`.
* Enforce wall-clock timeout (`timeout_minutes * 60`) by terminating/killing the container process group cleanly and still running artifact/patch capture.

### 3.6 Unit & integration tests
* Fast offline unit tests (`uv run pytest`, < 1 s):
  * `tests/test_prompts.py`: determinism and hash stability of `minimal-v1`.
  * `tests/test_antigravity_parser.py`: recorded NDJSON fixtures covering normal `SUCCESS`, `exit_code=0` with `result.status="ERROR"`, quota error classification, and truncated timeout stream.
  * `tests/test_egress_proxy.py`: allowlisted HTTPS `CONNECT`, blocked `github.com` (`403`), blocked raw IP literal, JSONL audit log output.
  * `tests/test_capture.py`: patch capture on temporary local git repos covering unstaged edits, new untracked files, deleted files, binary-file exclusion, and **agent-created `git commit`s** on top of `<initial_sha>`.
* Docker integration tests (`@pytest.mark.docker`, deselected by default in `pyproject.toml` `addopts = "-m 'not docker'"` so `uv run pytest` stays fast offline; run explicitly with `uv run pytest -m docker`):
  * Provision `pytest-dev__pytest-5809`, verify 1-commit stripped `.git`, verify `curl https://github.com` fails via proxy (`403`) while `pytest --version` uses `/opt/miniconda3/envs/testbed`.

---

## 6. Documented Fallback — D13 Option (c): Hybrid Host `agy` + Air-Gapped `testbed` Container

> **Rule:** Only implement Option (c) if D13(a) fails the Milestone 2.1 Go/No-Go gate **and** the operator approves switching to Option (c). Do not fall back to a bare host checkout without a container (Option b), because historical repos (`astropy`, 2013 `requests`, etc.) cannot run tests on the host's Python 3.12.

If activated:
1. Extract `/testbed` from the SWE-bench instance image into `/tmp/subbench-attempt-<id>/testbed`, strip `.git` to a single `<initial_sha>` commit, and bind-mount `/tmp/subbench-attempt-<id>/testbed:/testbed` into a running SWE-bench container started with **`--network none`**.
2. Run `agy` on the host (`cwd=/tmp/subbench-attempt-<id>/testbed`, reusing the host keyring) with `--sandbox`, `--disable-slash-commands`, and `--settings` denying `search_web`, `read_url`, browser tools, and file access outside `/tmp/subbench-attempt-<id>/testbed` (explicitly denying `/home/john/Documents/Codex/SubscriptionBench`).
3. Provide a single-purpose wrapper script `/tmp/subbench-attempt-<id>/bin/testbed-exec` that runs commands inside the `--network none` container's `/opt/miniconda3/envs/testbed`, paired with template `minimal-hybrid-v1` and `environment.execution_mode = 'hybrid_host'`.

---

## 7. Milestone 4 — SWE-bench Calibration (Phase 0 Item 10 & D15)

Once Milestone 3 passes offline and `@pytest.mark.docker` tests:
1. Add a lightweight CLI command or script (e.g. `scripts/calibrate_phase3.py` or `subbench smoke-attempt`) that runs a single `(instance_id, model_id)` attempt end-to-end:
   * Pre-attempt raw `/usage` snapshot → Phase 3 provision + `AntigravityAdapter` → patch capture → post-attempt raw `/usage` snapshot → offline grade via `swebench` (`--cache_level instance`).
2. Run **2–3 calibration attempts** from `suites/verified-mvp-v1.json`:
   * 1–2 easy/medium tasks on `gemini-3.8-flash-medium` (e.g. order `0` `pytest-dev__pytest-5809` and order `1` `psf__requests-2931`) to verify end-to-end resolution, patch application, proxy logs, wall-clock duration, and `gemini-5h` / `gemini-weekly` consumption.
   * **1 task on `gemini-4-argon-medium` (or `gemini-4-argon-high` on easy task `0`)** to measure real SWE-bench consumption on `gemini-argon-wtu-weekly` and calculate projected full-pass (`12` tasks) and `k=2` (`24` tasks) Argon weekly burn for **D15**.
3. Record the empirical numbers in [`docs/phase0_findings.md`](phase0_findings.md) (updating Items 8, 9, 10 and providing concrete numbers for the operator's **D15** decision).

---

## 8. Definition of Done for This Batch

- [ ] Milestone 1 fixes committed; all existing + new unit tests pass (`uv run pytest`) and `ruff check` / `ruff format --check` are clean.
- [ ] D13(a) container auth volume (`subbench-agy-auth`) verified across two fresh `docker run --rm` runs (or escalated cleanly to operator if blocked).
- [ ] Egress allowlist hosts, updater-blocking behaviour, raw `/usage` credit fields, and `permissions.deny` web-tool blocking verified and documented in [`docs/phase0_findings.md`](phase0_findings.md).
- [ ] `subbench/provision/`, `subbench/agents/`, and `docker/` implemented with unit tests + passing `uv run pytest -m docker` integration checks; `/` still has healthy free disk (~30+ GB) and all 12 pre-pulled SWE-bench images remain intact.
- [ ] 2–3 SWE-bench calibration runs completed, graded, and summarized in [`docs/phase0_findings.md`](phase0_findings.md) with a concrete recommendation on **D15** (`k=2` vs `k=1` / cap for Argon).
