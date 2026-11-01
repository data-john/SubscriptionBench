# Phase 0 Findings: Feasibility Spike

Run date: 2026-10-31. Host: Linux Mint 22.3. Spike scripts: [`scripts/phase0/`](../scripts/phase0). Raw samples: [`docs/phase0_samples/`](phase0_samples).

**Verdict: GO for host-mode execution. D2 (agent inside the task container) is NOT yet confirmed** (see item 8). Several plan hypotheses were wrong or incomplete (marked ⚠).

## Summary of results by plan item

| # | Item | Result |
|---|---|---|
| 1 | Binary, version, headless login | `/home/john/.local/bin/agy`, **v1.4.12**, dynamically linked x86-64 ELF (needs glibc). `agy -p` reuses the cached login ("silent auth succeeded" in `~/.gemini/antigravity-cli/log/`). Works with a scrubbed environment (`env -i HOME=… PATH=/usr/bin:/bin`), so no inherited `ANTIGRAVITY_*` variables are needed. |
| 2 | Models | See [Models](#models). Default is **Gemini 4 Argon (Medium)** (from CLI log "selected model override"). |
| 3 | `/usage` | See [Quota probe](#quota-probe). Full-float precision, idempotent, **does not consume quota**. |
| 4 | `stream-json` schema | See [Event schema](#stream-json-event-schema). Quota-exhaustion behaviour **not verified** (would require draining the account). |
| 5 | AI Credits off | Setting key is **`useG1Credits`** (binary strings; changelog confirms a `UseG1Credits` setting). `--settings '{"useG1Credits": false}'` is accepted per-session and is now passed by `probe_headless.sh`. ⚠ Its *effect* when quota runs out is not verified. The runner must also treat any credit-balance field as a hard stop. |
| 6 | Config isolation | `--settings` is per-session only. Current state is clean: `mcp_config.json` empty, `plugins/` empty, no user rules file. `~/.gemini/config/config.json` holds permission grants (not used by headless `--dangerously-skip-permissions`). **Plan: pre-flight asserts these stay empty and records hashes; do not change HOME**, because the login lives in the system keyring (D-Bus Secret Service), not in the config dir. |
| 7 | Auto-update | `update_status.json` = "Already on the latest version"; the binary has an auto-updater with a 15-min check throttle. ⚠ **No documented flag to disable it was found.** Mitigation: record `agy --version` before and after every attempt and mark attempts where it changed. |
| 8 | In-container execution | **Partially tested.** The binary starts in `python:3.11-slim-bookworm` (glibc OK, `agy --version` → 1.4.12). A fresh container has no login, so `agy -p` prints an OAuth URL and times out after 60 s (`finished_reason: "error"`, process exit code 0 ⚠). Credentials live in the host keyring and **I did not copy or mount them** (the Antigravity additional terms reportedly forbid extracting or reusing OAuth credentials). **Needs your decision (D13)**. |
| 9 | Egress hosts | Not captured yet. Seen in CLI log: `daily-cloudcode-pa.googleapis.com` (`:fetchAvailableModels`), `accounts.google.com` and `antigravity.google` (OAuth), and the auto-updater Cloud Run host. Full list needs a proxy/DNS capture during an agent run (Phase 4 of this plan, provisioning). |
| 10 | Calibration on SWE-bench tasks | **Still pending.** The Phase 2 suite now exists (`verified-mvp-v1`), but calibration runs need the Phase 3 provisioner and agent adapter (and the D13 decision), so they move to the start of Phase 3. Trivial-task costs are below. |
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
| D15 | Argon weekly bucket may be small | **Open until calibration:** Measure per-task `gemini-argon-wtu-weekly` burn at the end of Phase 3 before finalizing `k` / cap for `capable-argon-high`. |
