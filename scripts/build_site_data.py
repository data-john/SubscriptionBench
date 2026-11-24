#!/usr/bin/env python3
"""Build static JSON data bundle for the SubscriptionBench GitHub Pages site.

Reads the latest CSV reports from artifacts/reports/ and outputs
site/data/benchmark_data.json so the static web app can run with zero backend.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORTS_DIR = ROOT / "artifacts" / "reports"
OUT_PATH = ROOT / "site" / "data" / "benchmark_data.json"

NOV7_FAMILIES = {
    "gemini-3.8-flash@gemini-weekly",
    "gemini-4-argon@gemini-argon-wtu-weekly",
}

POOL_META = {
    "gemini-weekly": {
        "label": "Gemini Weekly (Primary)",
        "short_label": "gemini-weekly",
        "subscription": "Google AI Pro ($19.99/mo)",
        "subscription_price_usd": 19.99,
        "provider": "Google Antigravity",
        "description": "Shared weekly quota pool for Gemini 4 Neon, Gemini 4 Carbon, Gemini 4 Hydrogen, and legacy Gemini 3.8 Flash.",
    },
    "gemini-argon-wtu-weekly": {
        "label": "Gemini Argon Weekly (Legacy)",
        "short_label": "argon-weekly",
        "subscription": "Google AI Pro ($19.99/mo)",
        "subscription_price_usd": 19.99,
        "provider": "Google Antigravity",
        "description": "Separate weekly pool used by Gemini 4 Argon during the Nov 7 baseline.",
    },
    "3p-weekly": {
        "label": "3rd-Party Weekly (Claude Bonus)",
        "short_label": "3p-weekly",
        "subscription": "Google AI Pro ($19.99/mo)",
        "subscription_price_usd": 19.99,
        "provider": "Google Antigravity (3P)",
        "description": "Separate weekly bonus pool for Anthropic Claude Sonnet 5.5 and Opus 5.5 (~$20/mo implied API value).",
    },
    "codex-30d": {
        "label": "Codex 30-Day (Free Tier)",
        "short_label": "codex-30d",
        "subscription": "Codex Free ($0/mo)",
        "subscription_price_usd": 0.0,
        "provider": "OpenAI Codex (Free)",
        "description": "30-day free-tier baseline for GPT-6 Luna (1% integer quota increments).",
    },
}

FAMILY_COLORS = {
    "gemini-4-neon@gemini-weekly": "#2563eb",
    "gemini-4-carbon@gemini-weekly": "#e11d48",
    "gemini-4-hydrogen@gemini-weekly": "#0891b2",
    "gemini-4-argon@gemini-argon-wtu-weekly": "#7c3aed",
    "gemini-3.8-flash@gemini-weekly": "#64748b",
    "claude-opus-5-5@3p-weekly": "#d97706",
    "claude-sonnet-5-5@3p-weekly": "#ea580c",
    "gpt-6-luna@codex-30d": "#16a34a",
}

EFFORT_ORDER = {
    "non-reasoning": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "xhigh": 4,
    "max": 5,
}


def _float(val: str | None) -> float | None:
    if val is None or val == "":
        return None
    return float(val)


def _int(val: str | None) -> int | None:
    if val is None or val == "":
        return None
    return int(float(val))


def _bool(val: str | None) -> bool:
    return str(val).strip().lower() in {"true", "1", "yes"}


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    measured_rows = read_csv(REPORTS_DIR / "aa_projections.csv")
    effort_rows = read_csv(REPORTS_DIR / "aa_effort_projections.csv")
    token_rows = read_csv(REPORTS_DIR / "aa_token_weighted_projections.csv")

    measured_by_cycle_config: dict[tuple[int, str], dict] = {}
    measured_clean: list[dict] = []

    for r in measured_rows:
        cycle_id = int(r["cycle_id"])
        pool = r["weekly_bucket"]
        implied_mo = float(r["implied_pool_usd_per_month"])
        sub_price = POOL_META.get(pool, {}).get("subscription_price_usd", 0.0)
        multiplier = (implied_mo / sub_price) if sub_price > 0 else None
        era = "nov7" if cycle_id <= 4 else "current"
        item = {
            "cycle_id": cycle_id,
            "date": r["cycle_started_at"][:10],
            "cycle_started_at": r["cycle_started_at"],
            "config_label": r["config_label"],
            "model_id": r["model_id"],
            "effort": r["effort"],
            "agent": r["agent"],
            "weekly_bucket": pool,
            "era": era,
            "resolved_valid": _int(r["resolved_valid"]),
            "attempted_valid": _int(r["attempted_valid"]),
            "weekly_burn_pp": _float(r["weekly_burn_pp"]),
            "uncached_input_tokens": _int(r["uncached_input_tokens"]),
            "cache_read_tokens": _int(r["cache_read_tokens"]),
            "cache_write_tokens": _int(r["cache_write_tokens"]),
            "output_tokens": _int(r["output_tokens"]),
            "aa_slug": r["aa_slug"],
            "aa_name": r["aa_name"],
            "aa_intelligence": _float(r["aa_intelligence"]),
            "aa_ii_cost_per_task": _float(r["aa_ii_cost_per_task"]),
            "aa_price_1m_in": _float(r["aa_price_1m_in"]),
            "aa_price_1m_out": _float(r["aa_price_1m_out"]),
            "aa_price_1m_cache": _float(r["aa_price_1m_cache"]),
            "api_cost_usd": _float(r["api_cost_usd"]),
            "implied_pool_usd_per_week": _float(r["implied_pool_usd_per_week"]),
            "implied_pool_usd_per_month": implied_mo,
            "implied_pool_se_pct": _float(r["implied_pool_se_pct"]),
            "implied_pool_ci_low": _float(r["implied_pool_ci_low"]),
            "implied_pool_ci_high": _float(r["implied_pool_ci_high"]),
            "api_multiplier": round(multiplier, 2) if multiplier is not None else None,
            "tasks_per_month_attempted": _float(r["tasks_per_month_attempted"]),
            "tasks_mo_ci_low": _float(r["tasks_mo_ci_low"]),
            "tasks_mo_ci_high": _float(r["tasks_mo_ci_high"]),
        }
        measured_clean.append(item)
        measured_by_cycle_config[(cycle_id, r["config_label"])] = item

    token_by_key: dict[tuple[str, str], dict] = {}
    for r in token_rows:
        token_by_key[(r["family_key"], r["effort"])] = {
            "weight_source": r["weight_source"],
            "w_in_pp_per_1m": _float(r["w_in_pp_per_1m"]),
            "w_cache_pp_per_1m": _float(r["w_cache_pp_per_1m"]),
            "w_out_pp_per_1m": _float(r["w_out_pp_per_1m"]),
            "uncached_in_tokens_per_task": _float(r["uncached_in_tokens_per_task"]),
            "cache_read_tokens_per_task": _float(r["cache_read_tokens_per_task"]),
            "output_tokens_per_task": _float(r["output_tokens_per_task"]),
            "burn_per_task_wk_pp": _float(r["burn_per_task_wk_pp"]),
            "token_weighted_tasks_per_month": _float(r["token_weighted_tasks_per_month"]),
            "harness_ratio_subbench_over_token": _float(r["harness_ratio_subbench_over_token"]),
        }

    projections: list[dict] = []
    for r in effort_rows:
        fk = r["family_key"]
        effort = r["effort"]
        source_cycle = int(r["source_cycle_id"])
        source_cfg = r["source_config_label"]
        source_m = measured_by_cycle_config.get((source_cycle, source_cfg), {})
        tok = token_by_key.get((fk, effort), {})
        era = "nov7" if fk in NOV7_FAMILIES else "current"
        implied_mo = source_m.get("implied_pool_usd_per_month")
        sub_price = POOL_META.get(r["weekly_bucket"], {}).get("subscription_price_usd", 0.0)
        multiplier = (implied_mo / sub_price) if (implied_mo and sub_price > 0) else None

        projections.append(
            {
                "id": f"{fk}:{effort}",
                "family_key": fk,
                "family_label": r["family_label"],
                "effort": effort,
                "effort_order": EFFORT_ORDER.get(effort, 99),
                "display_name": f"{r['family_label']} ({effort})",
                "aa_slug": r["aa_slug"],
                "aa_name": r["aa_name"],
                "weekly_bucket": r["weekly_bucket"],
                "era": era,
                "color": FAMILY_COLORS.get(fk, "#475569"),
                "measured": _bool(r["measured"]),
                "source_cycle_id": source_cycle,
                "source_config_label": source_cfg,
                "source_date": source_m.get("date"),
                "aa_intelligence": _float(r["aa_intelligence"]),
                "aa_ii_cost_per_task": _float(r["aa_ii_cost_per_task"]),
                "tasks_per_month_primary": _float(r["tasks_per_month_attempted"]),
                "tasks_mo_ci_low": _float(r["tasks_mo_ci_low"]),
                "tasks_mo_ci_high": _float(r["tasks_mo_ci_high"]),
                "measured_resolve_rate": _float(r["measured_resolve_rate"]),
                "implied_pool_usd_per_month": implied_mo,
                "implied_pool_ci_low": source_m.get("implied_pool_ci_low"),
                "implied_pool_ci_high": source_m.get("implied_pool_ci_high"),
                "api_multiplier": round(multiplier, 2) if multiplier is not None else None,
                "aa_price_1m_in": source_m.get("aa_price_1m_in"),
                "aa_price_1m_out": source_m.get("aa_price_1m_out"),
                "aa_price_1m_cache": source_m.get("aa_price_1m_cache"),
                "token_weighted_tasks_per_month": tok.get("token_weighted_tasks_per_month"),
                "weight_source": tok.get("weight_source"),
                "burn_per_task_wk_pp": tok.get("burn_per_task_wk_pp"),
                "harness_ratio": tok.get("harness_ratio_subbench_over_token"),
                "uncached_in_tokens_per_task": tok.get("uncached_in_tokens_per_task"),
                "cache_read_tokens_per_task": tok.get("cache_read_tokens_per_task"),
                "output_tokens_per_task": tok.get("output_tokens_per_task"),
            }
        )

    projections.sort(key=lambda x: (-x["aa_intelligence"], -x["tasks_per_month_primary"]))

    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "aa_snapshot": "2026-11-19T11:00:37+00:00",
            "aa_token_snapshot": "2026-11-19T15:19:35+00:00",
            "aa_index_version": "Artificial Analysis Intelligence Index v5",
            "weeks_per_month": 4.33,
            "google_ai_pro_monthly_usd": 19.99,
            "measured_cycle_count": len(measured_clean),
            "projection_count": len(projections),
        },
        "pools": POOL_META,
        "family_colors": FAMILY_COLORS,
        "measured_cycles": measured_clean,
        "projections": projections,
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {OUT_PATH} ({len(measured_clean)} measured cycles, {len(projections)} projections)")


if __name__ == "__main__":
    main()
