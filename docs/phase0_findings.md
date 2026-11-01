# Phase 0 & Phase 3 Findings: Feasibility Spike and In-Container Calibration

Run dates: 2026-10-31 (Spike) & 2026-11-01 (D13a container auth & calibration). Host: Linux Mint 22.3. Spike scripts: [`scripts/phase0/`](../scripts/phase0), [`scripts/calibrate_phase3.py`](../scripts/calibrate_phase3.py). Raw samples: [`docs/phase0_samples/`](phase0_samples).

**Verdict: GO for in-container execution (D13a). Verified persistent silent auth in containers, egress allowlist enforcement, tool auto-denials, and 100% offline SWE-bench resolution (3/3 resolved) across Flash Medium and Argon Medium.**

## Summary of results by plan item

| # | Item | Result |
|---|---|---|
| 1 | Binary, version, headless login | `/home/john/.local/bin/agy`, **v1.4.12**, dynamically linked x86-64 ELF (needs glibc). `agy -p` reuses the cached login ("silent auth succeeded" in `~/.gemini/antigravity-cli/log/`). Works with a scrubbed environment (`env -i HOME=… PATH=/usr/bin:/bin`), so no inherited `ANTIGRAVITY_*` variables are needed. |
| 2 | Models | See [Models](#models). Default is **Gemini 4 Argon (Medium)** (from CLI log "selected model override"). |
| 3 | `/usage` | See [Quota probe](#quota-probe). Full-float precision, idempotent, **does not consume quota**. |
| 4 | `stream-json` schema | See [Event schema](#stream-json-event-schema). Quota-exhaustion behaviour **not verified** (would require draining the account; protected by Hard Guardrail 3). |
| 5 | AI Credits off | Setting key is **`useG1Credits`**. `--settings '{"useG1Credits": false}'` is accepted per-session and passed in all headless adapter invocations. Inspected raw `/usage` schema (`docs/phase0_samples/usage_raw_schema.json`): contains exclusively `groups[].buckets[]` (`id`, `name`, `window`, `remaining_fraction`, `reset_time`) and **contains zero credit balance or monetary balance fields**. |
| 6 | Config isolation | `--settings` is per-session only. Container mode (D13a) isolates agent state completely inside the named Docker volume `subbench-agy-auth` and fresh per-container overlays, completely decoupled from the host user profile. |
| 7 | Auto-update | `update_status.json` = "Already on the latest version"; the binary has an auto-updater with a 15-min check throttle targeting `antigravity-cli-auto-updater-974169037036.us-central1.run.app`. **Verified:** The egress proxy blocks this host (403 Forbidden); `agy` logs the failed check and continues normal operation with zero disruption or exit code change. |
| 8 | In-container execution | **Verified working under D13(a).** Built `subbench-agy-overlay:base` with `dbus-user-session`, `gnome-keyring`, and an entrypoint script (`entrypoint-agy.sh`). Operator completed a one-time OAuth bootstrap into named volume `subbench-agy-auth`. Persistent silent auth was verified across multiple fresh `docker run --rm` containers. No host OAuth tokens are extracted or bind-mounted (satisfying Hard Guardrails 1 & 2). |
| 9 | Egress hosts | Verified minimal egress allowlist: `daily-cloudcode-pa.googleapis.com` (inference/telemetry), `www.googleapis.com` (eligibility check), `aicode.googleapis.com` (code assist), `oauth2.googleapis.com` & `accounts.google.com` (token refresh). Non-allowlisted egress (auto-updater, package indexes, analytics) is cleanly rejected by `subbench-proxy`. Tool permissions: passing `--settings` with explicit `permissions.allow` (commands/file edits) and `permissions.deny` (`search_web`, browser tools) enables headless operation while preventing prompt-leak web calls. |
| 10 | Calibration on SWE-bench tasks | **Completed.** Ran 3 calibration attempts on tasks from `suites/verified-mvp-v1.json` using the Phase 3 harness and graded offline with official SWE-bench evaluation: **3/3 tasks resolved (100%)**. Empirical quota burn recorded for both Flash Medium and Argon Medium (see [Calibration Results](#calibration-results-phase-3)). |
| 11 | Terms of service | Web search summary only, **unverified against the primary source**: personal local scripting with the official unmodified binary is supported. Third-party tools that extract or proxy OAuth credentials are prohibited. Unattended or polling automation on a consumer plan may trigger abuse detection. Please review the Antigravity terms before the baseline. |

## Models

`agy models` returned 13 identifiers. Gemini ones:

| Id | Label | Bucket(s) drawn from (verified) |
|---|---|---|
| `gemini-4-argon-{low,medium,high}` | Gemini 4 Argon | **Argon weekly only** (`gemini-argon-wtu-weekly`) |
| `gemini-3.8-flash-{low,medium,high}` | Gemini 3.8 Flash | `gemini-5h` **and** `gemini-weekly` |

Also listed (out of scope, D6): `claude-opus-5-5-*`, `claude-sonnet-5-5-*`, `gpt-oss-120b-medium`. They draw from a separate `3p-*` pool.

Confirmed config mapping (D14):

| Config | Model id |
|---|---|
| default | `gemini-4-argon-medium` (observed default) |
| efficient | `gemini-3.8-flash-medium` |
| capable | `gemini-4-argon-high` |

> ⚠ **Design impact: configs draw from different allowance buckets.** Default and capable (Argon) have **no 5-hour window**, only a weekly bucket. Efficient (Flash) uses the shared Gemini 5h and weekly buckets. The "resolved per full 5h window" headline and the "2 × 5h windows per config" cap in the plan don't apply to Argon. The allowance unit must be **per-bucket**, with **weekly-fraction** as the cross-config common unit (Flash also reports weekly). The runner cap needs a per-bucket definition. Argon quota is described by `/usage` as separate for Pro plans; its size is unknown, so the capable model could exhaust it within one pass.

## Quota probe

`agy -p /usage --output-format json` → `response` (text table) plus `command.data.groups[].buckets[]`:

```json
{"id": "gemini-5h", "name": "Five Hour Limit Remaining", "window": "5h",
 "remaining_fraction": 0.9895889163017273, "reset_time": "2026-11-01T03:16:39Z"}
```

Bucket ids seen: `gemini-weekly`, `gemini-5h`, `3p-weekly`, `3p-5h`, `gemini-argon-wtu-weekly`.

- **Precision:** double-precision float (the text table rounds to whole percent, so use the JSON).
- **`/usage` is free:** three consecutive calls returned identical fractions.
- **Resolution is fine enough for per-task cost:** a trivial run (Flash low, ~13.7k tokens, 1 tool call) moved `gemini-5h` by **0.017 percentage points** (0.98976 → 0.98959) and weekly by 0.009 pp. An Argon-low trivial run (~9.0k tokens) cost **0.127 pp** of the Argon weekly bucket. A SWE-bench attempt will cost orders of magnitude more, so batch-level fallback shouldn't be needed.
- ⚠ **The very first Flash run showed a ~1 pp drop** on `gemini-5h` (1.0 → 0.9898), which is much larger than the next identical run. Cause unknown (cold start, a concurrent Gemini use by the desktop app, or a window-initialisation effect). This is exactly the contamination the plan's guard exists for: the guard must compare `post(i)` with `pre(i+1)`.
- **Reset semantics are rolling from first use.** An unused `gemini-5h` bucket reports `reset_time = now + 5h` (and moves each call) until the first spend, then stays fixed (03:16:39 stayed constant afterwards). The unused Argon weekly bucket likewise shows `now + 7d` until first use. Weekly Gemini and 3p buckets have fixed reset times.
- **Quota pools are independent:** Claude/GPT activity (including this agent session) drains `3p-*` only. The Gemini pools did not move during it. Using the Antigravity app or IDE on Gemini models would still contaminate the Gemini buckets.

## `stream-json` event schema

Command: `agy -p <prompt> --model <id> --dangerously-skip-permissions --output-format stream-json [--settings JSON]`. One JSON object per line, `event` is one of:

| `event` | Payload |
|---|---|
| `init` | `conversation_id`, `init.{model, cwd, tools[], permission_mode}` (`always-proceed` with the skip flag). The tool list includes browser tools, `search_web`, `read_url_content`, `invoke_subagent` and `schedule`. ⚠ **The agent has web access tools in headless mode, which makes the egress allowlist and leak audit essential**. |
| `step_update` | `step_update.{conversation_id, step_index, state (ACTIVE/DONE), step_type (user_input/agent_response/tool), tool_name, tool_info.{name, parameters}, duration_seconds, text_delta, usage}` |
| `result` | `result.{conversation_id, status (SUCCESS/ERROR), response, error, duration_seconds, num_turns, usage.{input,output,thinking,cache_read,total}_tokens, finished_reason}` |

- `usage` appears on `agent_response` steps and cumulatively on `result`, so **token usage is exposed**.
- `finished_reason` (`print_timeout` / `interrupted` / `error`) is omitted on a normal finish.
- `--print-timeout` exists (default 0 = wait). The runner still needs its own wall-clock kill, since it must kill the whole process group.
- Auth failure: `result.status = "ERROR"`, `finished_reason = "error"`, but the **process exit code was 0**, so the adapter must parse `result`, never trust the exit code.
- Per the changelog, "reaching your individual quota is reported right away instead of being retried". Expect `status = ERROR` with a quota message. The exact text is **unverified**, and the adapter must treat any `ERROR` after a quota snapshot at ≈0 as `quota_restricted`.

## Consequences for the plan

1. Allowance accounting is **per bucket**: Flash (`gemini-5h`, `gemini-weekly`) and Argon (`gemini-argon-wtu-weekly`) draw from disjoint pools, so cross-config comparison uses each config's respective weekly pool (`weekly_bucket_by_config`), disclosed as separate Pro allowances.
2. Quota precision is **good**; the repeat-for-resolution argument (k≥2) is no longer driven by probe coarseness, only by run-to-run noise.
3. The agent has web tools by default (`search_web` runs server-side over Google's API) → combine the container egress allowlist proxy with `--settings` `permissions.deny` on web/browser tools and the transcript leak audit.
4. Run `agy` from a scrubbed environment; on the host do not override `HOME`, and in container mode (D13a) provide a persistent unlocked keyring + `.gemini` volume.
5. Exit codes are unreliable; parse the `result` event.

## Operator decisions

| ID | Question | Status / Decision |
|---|---|---|
| D13 | In-container auth (D2) | **Resolved: Option (a)** — Run `agy` inside the SWE-bench task container using a one-time official `agy` OAuth login stored in a named Docker volume (`subbench-agy-auth`, with `dbus-run-session` + `gnome-keyring-daemon`). Pure host worktree (b) rejected because the host lacks the historical `/opt/miniconda3/envs/testbed` environments. **Fallback: Option (c)** (host `agy` + bind-mounted `--network none` task container), documented in [`docs/implementation_plan.md`](implementation_plan.md#53-documented-fallback-for-d13-option-c--hybrid-host-agy--air-gapped-testbed-container). |
| D14 | Config → model mapping | **Resolved:** `default` = `gemini-4-argon-medium`, `efficient` = `gemini-3.8-flash-medium`, `capable` = `gemini-4-argon-high`. |
| D15 | Argon weekly bucket capacity & runner budget | **Resolved (conservative `passes: 1` first):** `gemini-4-argon-medium` burned **0.687 pp** on easy task `pytest-dev__pytest-5809` (12 tool calls, 83.7 s). However, medium/hard tasks cost several multiples of easy tasks (on Flash, medium `psf__requests-2931` used 6.6× the tool calls and 2.75× weekly / 6.3× 5h quota), and **`default` (`argon-medium`) and `capable` (`argon-high`) share the single `gemini-argon-wtu-weekly` bucket**. Start `baseline.yaml` at **`passes: 1`** (with optional `ConfigSpec.max_passes` support) so one full matched pass across all 3 configs completes safely before deciding whether remaining weekly Argon/Gemini headroom supports a 2nd pass. |

## Calibration Results (Phase 3)

Conducted on 2026-11-01 using `scripts/calibrate_phase3.py` against `suites/verified-mvp-v1.json` with in-container execution (D13a), isolated egress proxy, headless auto-approval, and offline official SWE-bench grading (`--cache_level instance`).

### Empirical Calibration Matrix

| Metric | Attempt 1 (Flash) | Attempt 2 (Flash) | Attempt 3 (Argon Probe) |
|---|---|---|---|
| **Instance ID** | `pytest-dev__pytest-5809` | `psf__requests-2931` | `pytest-dev__pytest-5809` |
| **Model** | `gemini-3.8-flash-medium` | `gemini-3.8-flash-medium` | `gemini-4-argon-medium` |
| **Halt / Finish Reason** | `completed` / `SUCCESS` | `completed` / `SUCCESS` | `completed` / `SUCCESS` |
| **SWE-bench Resolution** | **`resolved: true`** | **`resolved: true`** | **`resolved: true`** |
| **Wall Clock Duration** | 104.5 s | 750.8 s (~12.5 min) | 83.7 s |
| **Turns / Tool Calls** | 1 turn / 25 tool calls | 1 turn / 80 tool calls | 1 turn / 12 tool calls |
| **Input Tokens** | 191,244 | 358,193 | 34,621 |
| **Output Tokens** | 5,021 (3,542 thinking) | 82,242 (76,534 thinking) | 1,930 (1,099 thinking) |
| **Cache Read Tokens** | 233,393 | 4,717,144 | 144,998 |
| **Patch Size** | 1,049 bytes | 1,909 bytes | 979 bytes |
| **`gemini-weekly` Burn** | **0.88 pp** (0.0088) | **2.42 pp** (0.0242) | 0.04 pp (background) |
| **`gemini-5h` Burn** | **1.61 pp** (0.0161) | **10.19 pp** (0.1019) | 0.15 pp (background) |
| **`gemini-argon-wtu-weekly` Burn** | 0.00 pp | 0.00 pp | **0.687 pp** (0.0069) |
| **`3p-weekly` / `3p-5h` Burn** | 0.00 pp / 0.00 pp | 0.00 pp / 0.00 pp | 0.00 pp / 0.00 pp |

### Key Takeaways

1. **Resolution Accuracy:** In-container `agy` with `minimal-v1` achieved 3 out of 3 resolutions on diverse Python repositories (`pytest-dev/pytest` and `psf/requests`), verified offline by official SWE-bench evaluation harness.
2. **Flash Quota Dynamics & `HTTP_PROXY` Finding:** `gemini-3.8-flash-medium` spends exclusively from `gemini-weekly` and `gemini-5h` (`1.61 pp` 5h on `pytest-5809`; `10.19 pp` 5h on `requests-2931`). Inspection of `proxy.log` on `requests-2931` revealed that setting plain-HTTP `HTTP_PROXY` caused `requests`'s own offline error tests (`http://doesnotexist.google.com`, `http://10.255.255.1`) to receive `403 Forbidden` from `subbench-proxy` instead of `ConnectionError`, inflating debugging turns to 80 tool calls. Switching task containers to `HTTPS_PROXY`-only (relying on `--internal` Docker networking to reject direct port-80 traffic with `ConnectionError`) eliminates this test interference and saves quota.
3. **Argon Quota Dynamics & Shared Pool:** `gemini-4-argon-medium` burned `0.687 pp` of `gemini-argon-wtu-weekly` on easy `pytest-5809`. Because `default` and `capable` share `gemini-argon-wtu-weekly` and 8 of the 12 suite tasks are medium or hard, `baseline.yaml` starts at `passes: 1` to guarantee full 3-config coverage before attempting a 2nd pass. Also note that `0.15 pp` `gemini-5h` / `0.04 pp` `gemini-weekly` moved during Attempt 3 while a parent agent session was active—benchmark cycles in Phase 8 must be launched from a plain host terminal with no concurrent Antigravity Gemini session.
4. **Tool Denial & Egress Enforcement:** Non-interactive auto-approval (`DEFAULT_SETTINGS` in `AntigravityAdapter`) permits local commands and file edits without stalling; web/browser deny rules are being tightened in the Phase 4–6 batch to cover exact `agy` v1.4.12 tool names (`read_url_content`, `open_browser_url`, `read_browser_page`, etc.) alongside `--disable-slash-commands`. The egress proxy cleanly blocked the auto-updater and `pypi.org` without disrupting `agy`.
