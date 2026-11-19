# Agents.md

Guidance for coding agents working in **SubscriptionBench**.

## Running Experiments (`subbench`)

`subbench run` is manifest-driven: model configurations, suites, timeouts, and quota policies are defined in YAML files under `experiments/` rather than passed as CLI model flags.

### Standard Workflow

1. **Create or select a manifest** in `experiments/` (default suite for single-model runs is `suites/verified-core-6.json`).
2. **Validate** the manifest against the suite and schema:
   ```bash
   .venv/bin/subbench validate experiments/<manifest>.yaml
   ```
3. **Dry-run** to verify the planned attempt schedule (zero quota cost):
   ```bash
   .venv/bin/subbench run experiments/<manifest>.yaml --dry-run
   ```
4. **Execute** from a plain host terminal with no concurrent `agy` / IDE / Codex sessions sharing the target quota pool:
   ```bash
   .venv/bin/subbench run experiments/<manifest>.yaml
   ```
5. **Post-process** a completed cycle:
   ```bash
   .venv/bin/subbench grade --cycle-id <id>
   .venv/bin/subbench audit --cycle-id <id>
   .venv/bin/subbench report --cycle-id <id>
   .venv/bin/subbench aa project
   ```

### AA Monthly Throughput Projections (`subbench aa project`)

SubscriptionBench produces **two complementary monthly AA Intelligence Index task projections**:

1. **Primary — Subbench-Calibrated Projection (`aa_projections.{md,csv}`, `aa_effort_projections.csv`, `aa_intelligence_vs_tasks_per_month.svg`)**:
   - Does **not** depend on SWE-bench task difficulty; instead, full multi-turn Subbench runs measure the **actual end-to-end quota usage in `agy`/`codex`** (including real agent-harness/multi-turn overhead and cache behavior) relative to AA API list cost (`Implied Pool $/mo = api_cost_usd / total_burn_frac × weeks_per_month`, using net contiguous burn across valid attempts to net out inter-attempt recredits).
   - `Projected Monthly AA Tasks` divides `Implied Pool $/mo` by AA's Intelligence Index v5 `cost_per_task` (`aa_ii_cost_per_task`).
   - Visual error bars are omitted from the SVG by default (opt-in via `--show-ci`) because historical Cycles 4–10 pre-date post-attempt quota settling; numerical 95% CIs remain in `aa_projections.{md,csv}`.
   - **Caveats:** `agy` does not separate `cache_write_tokens` from uncached input (~10–13% sensitivity on Anthropic models where `cache_write = 1.25×` input); weekly scaling (`× 4.33`) assumes full weekly pool utilization without 5h-cap waste; `codex-30d` quantizes remaining quota in 1% integer increments.

2. **Alternative — Token-Weighted AA Projection (`aa_token_weighted_projections.{md,csv}`, `aa_token_weighted_vs_tasks_per_month.svg`)**:
   - Uses synthetic 3-prompt calibrated quota weights `(w_in, w_cache, w_out)` from `subbench calibrate` (`artifacts/reports/quota_weight_calibration.json`) multiplied directly by AA's exact benchmark-weighted per-task token breakdown (`nonCacheInput + cacheWrite`, `cacheRead`, `output`) cached via `.venv/bin/subbench aa sync-tokens` (`data/cache/aa_token_usage.json`).
   - Represents **theoretical token-only quota throughput** directly against the provider's meter (before `agy` multi-turn harness overhead).
   - **Caveats:** AA per-task token breakdowns are parsed from AA's public model pages (not included in free AA API v2); models whose tiny synthetic `input_heavy` probe was masked by a background recredit (`efficient-flash-medium`, `hydrogen-medium`) fall back to `joint-fallback` weights; on Anthropic `sonnet-5-5-medium`, synthetic `w_cache` absorbs Anthropic's unseparated `1.25×` cache-write premium; `codex-30d` weights remain quantization-limited. The gap between Subbench (primary) and Token-Weighted (alternative) isolates real `agy` multi-turn harness overhead.

```bash
.venv/bin/subbench calibrate          # synthetic 3-prompt (w_in, w_cache, w_out) calibration
.venv/bin/subbench aa sync-tokens     # fetch AA per-task token breakdowns into data/cache/aa_token_usage.json
.venv/bin/subbench aa project         # regenerate primary Subbench + alternative Token-Weighted reports & SVGs
```

### Antigravity (`agy`) Model & Quota Bucket Conventions

All Antigravity manifests set `agy_path: /home/john/.local/bin/agy` (default `agent: antigravity`) and map each `config` to its `KNOWN_BUCKETS` in `subbench/config.py`:

| Model family | Example `model_id` | `buckets` | `primary_bucket_by_config` | `weekly_bucket_by_config` |
|---|---|---|---|---|
| Gemini Flash / Hydrogen / Neon | `gemini-3.8-flash-medium`, `gemini-4-hydrogen-medium`, `gemini-4-neon-low`, `gemini-4-neon-medium` | `[gemini-5h, gemini-weekly]` | `gemini-5h` | `gemini-weekly` |
| Gemini Argon | `gemini-4-argon-medium`, `gemini-4-argon-high` | `[gemini-argon-wtu-weekly]` | `gemini-argon-wtu-weekly` | `gemini-argon-wtu-weekly` |
| Claude (3p pool in `agy`) | `claude-sonnet-5-5-medium`, `claude-opus-5-5-medium` | `[3p-5h, 3p-weekly]` | `3p-5h` | `3p-weekly` |

Current single-model `agy` manifests on `suites/verified-core-6.json`:
- `experiments/neon-low.yaml` (`gemini-4-neon-low`)
- `experiments/neon-medium.yaml` (`gemini-4-neon-medium`)
- `experiments/hydrogen-medium.yaml` (`gemini-4-hydrogen-medium`)
- `experiments/sonnet-5-5-medium.yaml` (`claude-sonnet-5-5-medium`)
- `experiments/opus-5-5-medium.yaml` (`claude-opus-5-5-medium`)

## Development Checks

```bash
.venv/bin/pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```
