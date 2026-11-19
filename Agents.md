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

Subbench is used to calibrate the token-to-quota conversion rate (`Implied Pool $/mo` = `api_cost_usd / total_burn_frac × weeks_per_month`, pricing `uncached_input`, `cache_read`, and `output` tokens at AA list rates). `Projected Monthly Tasks` divides `Implied Pool $/mo` by AA's full-suite `cost_per_task` (`aa_ii_cost_per_task`), keeping throughput normalized to AA's suite rather than skewed to `verified-core-6`. Outputs are written to `artifacts/reports/aa_intelligence_vs_tasks_per_month.svg`, `aa_projections.{md,csv}`, and `aa_effort_projections.csv`.

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
