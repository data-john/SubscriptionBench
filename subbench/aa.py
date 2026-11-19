"""Artificial Analysis (AA) Data API v2 client, secure key loader, and projection engine."""

from __future__ import annotations

import csv
import html
import json
import logging
import math
import os
import re
import shutil
import sqlite3
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_AA_ENDPOINT = "https://artificialanalysis.ai/api/v2/language/models/free"
DEFAULT_AA_CACHE_DIR = Path("data/cache")
DEFAULT_AA_LATEST_PATH = DEFAULT_AA_CACHE_DIR / "aa_models_latest.json"
DEFAULT_KEY_FILE = Path.home() / ".config" / "subbench" / "aa_api_key"
SECRET_SERVICE = "subbench"
SECRET_KEY_NAME = "aa_api_key"


def _parse_dotenv_key(path: Path, var_name: str = "AA_API_KEY") -> str | None:
    if not path.is_file():
        return None
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export ") :].strip()
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() != var_name:
                continue
            val = v.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            if val:
                return val
    except Exception as e:
        logger.debug("Could not read dotenv file %s: %s", path, e)
    return None


def resolve_aa_api_key(
    explicit_key: str | None = None,
    *,
    repo_root: Path | None = None,
    key_file: Path = DEFAULT_KEY_FILE,
) -> str | None:
    """Resolve AA_API_KEY from explicit arg, env, .env.local/.env, key file, or secret-tool."""
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()

    env_val = os.environ.get("AA_API_KEY", "").strip()
    if env_val:
        return env_val

    root = repo_root or Path.cwd()
    for fname in (".env.local", ".env"):
        val = _parse_dotenv_key(root / fname)
        if val:
            return val

    if key_file.is_file():
        try:
            txt = key_file.read_text(encoding="utf-8").strip()
            if txt:
                return txt
        except Exception as e:
            logger.debug("Could not read key file %s: %s", key_file, e)

    if shutil.which("secret-tool"):
        try:
            res = subprocess.run(
                ["secret-tool", "lookup", "service", SECRET_SERVICE, "key", SECRET_KEY_NAME],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception as e:
            logger.debug("secret-tool lookup failed: %s", e)

    return None


def store_aa_api_key_file(api_key: str, key_file: Path = DEFAULT_KEY_FILE) -> Path:
    """Persist AA API key to user-private file with 0600 permissions."""
    cleaned = api_key.strip()
    if not cleaned:
        raise ValueError("API key must not be empty")
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(key_file.parent, 0o700)
    except OSError:
        pass
    key_file.write_text(cleaned + "\n", encoding="utf-8")
    os.chmod(key_file, 0o600)
    return key_file


def store_aa_api_key_keyring(api_key: str) -> None:
    """Persist AA API key into libsecret/GNOME Keyring via secret-tool."""
    cleaned = api_key.strip()
    if not cleaned:
        raise ValueError("API key must not be empty")
    if not shutil.which("secret-tool"):
        raise RuntimeError("secret-tool is not installed")
    res = subprocess.run(
        [
            "secret-tool",
            "store",
            "--label=SubscriptionBench AA API Key",
            "service",
            SECRET_SERVICE,
            "key",
            SECRET_KEY_NAME,
        ],
        input=cleaned,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"secret-tool store failed: {res.stderr.strip()}")


@dataclass(frozen=True)
class AAFetchResult:
    """Metadata and payload from an AA Data API sync."""

    fetched_at: str
    endpoint: str
    model_count: int
    cache_path: Path
    archive_path: Path
    rate_limit_headers: dict[str, str]
    payload: dict[str, Any]


def fetch_aa_models(
    api_key: str | None = None,
    *,
    endpoint: str = DEFAULT_AA_ENDPOINT,
    cache_dir: Path = DEFAULT_AA_CACHE_DIR,
    timeout_seconds: int = 30,
) -> AAFetchResult:
    """Fetch language models from Artificial Analysis API v2 and cache locally."""
    key = resolve_aa_api_key(api_key)
    if not key:
        raise RuntimeError(
            "AA_API_KEY not found. Set AA_API_KEY, run `subbench aa set-key`, "
            "or store it in ~/.config/subbench/aa_api_key (chmod 600)."
        )

    all_models: list[dict[str, Any]] = []
    headers: dict[str, str] = {}
    meta: dict[str, Any] = {}
    page = 1
    while True:
        sep = "&" if "?" in endpoint else "?"
        page_url = endpoint if page == 1 else f"{endpoint}{sep}page={page}"
        req = urllib.request.Request(
            page_url,
            headers={
                "x-api-key": key,
                "Accept": "application/json",
                "User-Agent": "SubscriptionBench/0.1",
            },
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                raw_bytes = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:400]
            raise RuntimeError(f"AA API HTTP {e.code} {e.reason}: {body}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"AA API network error: {e.reason}") from e

        payload = json.loads(raw_bytes.decode("utf-8"))
        models = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(models, list):
            raise ValueError("Unexpected AA API payload shape (expected list in 'data')")
        all_models.extend(models)

        if isinstance(payload, dict):
            if "tier" in payload:
                meta["tier"] = payload["tier"]
            if "intelligence_index_version" in payload:
                meta["intelligence_index_version"] = payload["intelligence_index_version"]
            pag = payload.get("pagination")
            if isinstance(pag, dict) and pag.get("has_more"):
                page += 1
                continue
        break

    now = datetime.now(UTC)
    fetched_at = now.isoformat()
    rl_headers = {
        k: headers[k]
        for k in ("x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset", "x-aa-tier")
        if k in headers
    }

    cache_dir.mkdir(parents=True, exist_ok=True)
    archive_path = cache_dir / f"aa_models_{now.strftime('%Y-%m-%d')}.json"
    latest_path = cache_dir / "aa_models_latest.json"

    envelope = {
        "fetched_at": fetched_at,
        "endpoint": endpoint,
        **meta,
        "rate_limit_headers": rl_headers,
        "data": all_models,
    }
    encoded = json.dumps(envelope, indent=2) + "\n"
    archive_path.write_text(encoded, encoding="utf-8")
    latest_path.write_text(encoded, encoding="utf-8")

    return AAFetchResult(
        fetched_at=fetched_at,
        endpoint=endpoint,
        model_count=len(all_models),
        cache_path=latest_path,
        archive_path=archive_path,
        rate_limit_headers=rl_headers,
        payload=envelope,
    )


@dataclass(frozen=True)
class AAModel:
    """Normalized Artificial Analysis model entry."""

    slug: str
    name: str
    creator: str
    intelligence_index: float | None = None
    coding_index: float | None = None
    engineering_index: float | None = None
    ii_total_cost: float | None = None
    ii_cost_per_task: float | None = None
    price_1m_input: float | None = None
    price_1m_output: float | None = None
    price_1m_cache_read: float | None = None
    price_1m_blended: float | None = None
    evaluations: dict[str, Any] = field(default_factory=dict)
    performance: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def _as_float(val: Any) -> float | None:
    if val is None or isinstance(val, bool):
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def parse_aa_models(payload: dict[str, Any] | list[dict[str, Any]]) -> list[AAModel]:
    """Parse raw AA API JSON (or cached envelope) into normalized AAModel objects."""
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("Invalid AA cache payload: expected list of models")

    models: list[AAModel] = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        evals = item.get("evaluations") if isinstance(item.get("evaluations"), dict) else {}
        pricing = item.get("pricing") if isinstance(item.get("pricing"), dict) else {}
        ii_cost_obj = (
            item.get("artificial_analysis_intelligence_index_cost")
            if isinstance(item.get("artificial_analysis_intelligence_index_cost"), dict)
            else {}
        )
        ii_cpt_obj = (
            ii_cost_obj.get("cost_per_task")
            if isinstance(ii_cost_obj.get("cost_per_task"), dict)
            else {}
        )
        perf = (
            item.get("performance_v3")
            if isinstance(item.get("performance_v3"), dict)
            else (item.get("performance") if isinstance(item.get("performance"), dict) else {})
        )
        creator_obj = item.get("model_creator")
        creator = (
            str(creator_obj.get("name") or "")
            if isinstance(creator_obj, dict)
            else str(creator_obj or "")
        )

        intel = _as_float(evals.get("artificial_analysis_intelligence_index"))
        coding = _as_float(evals.get("artificial_analysis_coding_index"))
        eng = _as_float(evals.get("artificial_analysis_engineering_index"))
        ii_tot_cost = _as_float(ii_cost_obj.get("total_cost"))
        ii_cpt = _as_float(ii_cpt_obj.get("total_cost"))
        p_in = _as_float(pricing.get("price_1m_input_tokens"))
        p_out = _as_float(pricing.get("price_1m_output_tokens"))
        p_cache = _as_float(
            pricing.get("price_1m_cache_hit_tokens")
            if pricing.get("price_1m_cache_hit_tokens") is not None
            else (pricing.get("price_1m_cache_read_tokens") or pricing.get("cache_read_price"))
        )
        p_blend = _as_float(pricing.get("price_1m_blended_3_to_1"))

        models.append(
            AAModel(
                slug=str(item.get("slug") or item.get("id") or ""),
                name=str(item.get("name") or item.get("slug") or ""),
                creator=creator,
                intelligence_index=intel,
                coding_index=coding,
                engineering_index=eng,
                ii_total_cost=ii_tot_cost,
                ii_cost_per_task=ii_cpt,
                price_1m_input=p_in,
                price_1m_output=p_out,
                price_1m_cache_read=p_cache,
                price_1m_blended=p_blend,
                evaluations=dict(evals),
                performance=dict(perf),
                raw=dict(item),
            )
        )
    return models


def load_aa_cache(cache_path: Path = DEFAULT_AA_LATEST_PATH) -> tuple[str | None, list[AAModel]]:
    """Load cached AA models from JSON."""
    if not cache_path.exists():
        raise FileNotFoundError(
            f"AA cache not found at {cache_path}. Run `subbench aa sync` first."
        )
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    fetched_at = payload.get("fetched_at") if isinstance(payload, dict) else None
    return fetched_at, parse_aa_models(payload)


def _norm_tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if t]


def match_aa_model(
    model_id: str,
    effort: str | None,
    aa_models: list[AAModel],
) -> AAModel | None:
    """Match a Subbench (model_id, effort) pair to the closest AA model entry."""
    if not aa_models:
        return None

    m_lower = model_id.lower()
    effort_val = effort.lower() if effort else None
    if effort_val is None:
        for e in ("xhigh", "high", "medium", "low", "max", "ultra"):
            if m_lower.endswith(f"-{e}"):
                effort_val = e
                break

    base_id = m_lower
    if effort_val and base_id.endswith(f"-{effort_val}"):
        base_id = base_id[: -len(f"-{effort_val}")]

    # 1. Exact slug match (prefer effort-qualified slug before bare base_id)
    by_slug = {m.slug.lower(): m for m in aa_models if m.slug}
    cands = [f"{base_id}-{effort_val}", m_lower, base_id] if effort_val else [m_lower, base_id]
    for cand in cands:
        if cand and cand in by_slug:
            return by_slug[cand]

    # 2. Score overlap on normalized tokens (e.g. gemini-4-neon + medium)
    base_tokens = set(_norm_tokens(base_id))
    best: AAModel | None = None
    best_score = -1.0

    for m in aa_models:
        cand_tokens = set(_norm_tokens(f"{m.slug} {m.name}"))
        if not base_tokens.issubset(cand_tokens):
            # Allow partial overlap if at least family + version match
            overlap = len(base_tokens & cand_tokens)
            if overlap < max(2, len(base_tokens) - 1):
                continue
            score = float(overlap)
        else:
            score = float(len(base_tokens) * 10)

        if effort_val:
            if effort_val in cand_tokens:
                score += 5.0
            elif any(
                other in cand_tokens
                for other in ("low", "medium", "high", "xhigh", "max", "ultra")
                if other != effort_val
            ):
                score -= 2.0

        if m.price_1m_input is not None and m.price_1m_output is not None:
            score += 0.5

        if score > best_score:
            best_score = score
            best = m

    return best


@dataclass(frozen=True)
class SubbenchConfigCostSummary:
    """Empirical token, quota, and implied dollar summary for one Subbench config."""

    cycle_id: int
    config_label: str
    model_id: str
    effort: str | None
    agent: str
    weekly_bucket: str
    resolved_valid: int
    attempted_valid: int
    weekly_burn_pp: float
    uncached_input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    aa_slug: str | None
    aa_name: str | None
    aa_intelligence: float | None
    aa_ii_cost_per_task: float | None
    aa_price_1m_in: float | None
    aa_price_1m_out: float | None
    aa_price_1m_cache: float | None
    api_cost_usd: float | None
    implied_pool_usd_per_week: float | None
    implied_pool_usd_per_month: float | None
    tasks_per_month_attempted: float | None
    tasks_per_month_resolved: float | None


@dataclass(frozen=True)
class EffortProjectionPoint:
    """Measured or AA-cost-scaled monthly task throughput point at an AA Intelligence score."""

    family_key: str
    family_label: str
    effort: str
    aa_slug: str
    aa_name: str
    weekly_bucket: str
    measured: bool
    source_cycle_id: int
    source_config_label: str
    aa_intelligence: float
    aa_ii_cost_per_task: float | None
    tasks_per_month_attempted: float
    tasks_per_month_resolved: float | None
    measured_resolve_rate: float | None


FIVE_H_TO_WEEKLY_RATIO: dict[str, float] = {
    "gemini-5h": 3.50,
    "3p-5h": 2.00,
}


def compute_aa_projections(
    conn: sqlite3.Connection,
    aa_models: list[AAModel],
    *,
    suite_version: str | None = "verified-core-6",
    cache_discount: float = 0.10,
    use_5h_calibration: bool = True,
) -> list[SubbenchConfigCostSummary]:
    """Combine Subbench SQLite attempts/quotas with AA pricing & intelligence data."""
    where_suite = "AND e.suite_version = ?" if suite_version else ""
    params: tuple[Any, ...] = (suite_version,) if suite_version else ()

    rows = conn.execute(
        f"""
        SELECT c.id as cycle_id, e.manifest_json, cf.id as config_id, cf.label, cf.model_id,
               cf.effort
        FROM cycles c
        JOIN experiments e ON c.experiment_id = e.id
        JOIN configs cf ON cf.experiment_id = e.id
        WHERE c.status = 'completed' {where_suite}
        ORDER BY c.id ASC, cf.id ASC
        """,
        params,
    ).fetchall()

    summaries: list[SubbenchConfigCostSummary] = []
    for cycle_id, manifest_json_str, config_id, label, model_id, effort in rows:
        try:
            manifest_obj = json.loads(manifest_json_str or "{}")
        except Exception:
            manifest_obj = {}

        agent = str(manifest_obj.get("agent") or "antigravity")
        quota_obj = manifest_obj.get("quota") or {}
        weekly_bucket = (quota_obj.get("weekly_bucket_by_config") or {}).get(label)
        primary_bucket = (quota_obj.get("primary_bucket_by_config") or {}).get(label)
        if not weekly_bucket:
            continue

        # Prefer clean 5h bucket divided by verified 5h:weekly factor when available
        # to eliminate weekly backend settling/recrediting jitter.
        ratio_5h = (
            FIVE_H_TO_WEEKLY_RATIO.get(primary_bucket)
            if (use_5h_calibration and primary_bucket)
            else None
        )
        measure_bucket = primary_bucket if ratio_5h else weekly_bucket

        attempts = conn.execute(
            """
            SELECT a.id, a.instance_id, a.pass_index, a.seq,
                   COALESCE(a.input_tokens, 0), COALESCE(a.output_tokens, 0),
                   COALESCE(a.cache_read_tokens, 0),
                   q_pre.remaining_fraction - q_post.remaining_fraction as burned,
                   COALESCE(g.resolved, 0)
            FROM attempts a
            JOIN quota_snapshots q_pre
                ON q_pre.attempt_id = a.id AND q_pre.phase = 'pre' AND q_pre.bucket_id = ?
            JOIN quota_snapshots q_post
                ON q_post.attempt_id = a.id AND q_post.phase = 'post' AND q_post.bucket_id = ?
            LEFT JOIN grades g ON g.attempt_id = a.id
            WHERE a.cycle_id = ? AND a.config_id = ? AND a.valid = 1
            ORDER BY a.seq ASC, a.id ASC
            """,
            (measure_bucket, measure_bucket, cycle_id, config_id),
        ).fetchall()
        if not attempts:
            continue

        raw_burn_frac = sum(max(0.0, float(r[7])) for r in attempts)
        total_burn_frac = raw_burn_frac / ratio_5h if ratio_5h else raw_burn_frac
        weekly_burn_pp = total_burn_frac * 100.0
        attempted_valid = len({(r[1], r[2]) for r in attempts})

        final_by_slot: dict[tuple[str, int], int] = {}
        for _, inst_id, p_idx, _, _, _, _, _, resolved in attempts:
            final_by_slot[(inst_id, p_idx)] = int(resolved)
        resolved_valid = sum(final_by_slot.values())

        raw_in = sum(int(r[4]) for r in attempts)
        raw_out = sum(int(r[5]) for r in attempts)
        raw_cch = sum(int(r[6]) for r in attempts)

        # Codex reports input_tokens inclusive of cached_input_tokens; agy reports them separately
        if agent == "codex":
            uncached_in = max(0, raw_in - raw_cch)
        else:
            uncached_in = raw_in

        aa_match = match_aa_model(model_id, effort, aa_models)
        p_in = aa_match.price_1m_input if aa_match else None
        p_out = aa_match.price_1m_output if aa_match else None
        p_cch = (
            aa_match.price_1m_cache_read
            if (aa_match and aa_match.price_1m_cache_read is not None)
            else (p_in * cache_discount if p_in is not None else None)
        )

        api_cost_usd: float | None = None
        if p_in is not None and p_out is not None and p_cch is not None:
            api_cost_usd = (
                (uncached_in / 1e6) * p_in + (raw_cch / 1e6) * p_cch + (raw_out / 1e6) * p_out
            )

        is_30d = weekly_bucket.endswith("-30d")
        weeks_per_month = 1.0 if is_30d else 4.33
        implied_wk_usd: float | None = None
        implied_mo_usd: float | None = None
        if api_cost_usd is not None and total_burn_frac > 0:
            implied_wk_usd = api_cost_usd / total_burn_frac
            implied_mo_usd = implied_wk_usd * weeks_per_month

        aa_cpt = aa_match.ii_cost_per_task if aa_match else None
        if implied_mo_usd is not None and aa_cpt is not None and aa_cpt > 0:
            # Convert Subbench-calibrated monthly pool $ into AA full-suite tasks/month
            tasks_mo_att = implied_mo_usd / aa_cpt
            tasks_mo_res = (
                tasks_mo_att * (resolved_valid / attempted_valid) if attempted_valid > 0 else 0.0
            )
        elif total_burn_frac > 0:
            tasks_mo_att = (attempted_valid / total_burn_frac) * weeks_per_month
            tasks_mo_res = (resolved_valid / total_burn_frac) * weeks_per_month
        else:
            tasks_mo_att = None
            tasks_mo_res = None

        summaries.append(
            SubbenchConfigCostSummary(
                cycle_id=cycle_id,
                config_label=label,
                model_id=model_id,
                effort=effort,
                agent=agent,
                weekly_bucket=weekly_bucket,
                resolved_valid=resolved_valid,
                attempted_valid=attempted_valid,
                weekly_burn_pp=weekly_burn_pp,
                uncached_input_tokens=uncached_in,
                cache_read_tokens=raw_cch,
                output_tokens=raw_out,
                aa_slug=aa_match.slug if aa_match else None,
                aa_name=aa_match.name if aa_match else None,
                aa_intelligence=aa_match.intelligence_index if aa_match else None,
                aa_ii_cost_per_task=aa_match.ii_cost_per_task if aa_match else None,
                aa_price_1m_in=p_in,
                aa_price_1m_out=p_out,
                aa_price_1m_cache=p_cch,
                api_cost_usd=api_cost_usd,
                implied_pool_usd_per_week=implied_wk_usd,
                implied_pool_usd_per_month=implied_mo_usd,
                tasks_per_month_attempted=tasks_mo_att,
                tasks_per_month_resolved=tasks_mo_res,
            )
        )

    return summaries


EFFORT_LABELS = ("non-reasoning", "low", "medium", "high", "xhigh", "max", "ultra")


def _base_family_id(model_id: str, effort: str | None) -> str:
    low = model_id.lower()
    eff = effort.lower() if effort else None
    if eff and low.endswith(f"-{eff}"):
        return low[: -len(f"-{eff}")]
    for e in ("xhigh", "high", "medium", "low", "max", "ultra"):
        if low.endswith(f"-{e}"):
            return low[: -len(f"-{e}")]
    return low


def _extract_effort_from_aa(model: AAModel) -> str:
    name_low = model.name.lower()
    slug_low = model.slug.lower()
    for e in ("non-reasoning", "xhigh", "high", "medium", "low", "max", "ultra"):
        if re.search(rf"\b{re.escape(e)}\b", name_low) or slug_low.endswith(f"-{e}"):
            return e
    return "default"


def _clean_family_label(aa_name: str) -> str:
    return re.sub(r"\s*\([^)]*\)\s*$", "", aa_name).strip()


def project_effort_variants(
    summaries: list[SubbenchConfigCostSummary],
    aa_models: list[AAModel],
) -> list[EffortProjectionPoint]:
    """Build measured points plus AA-cost-scaled sibling effort projections per model family."""
    points: list[EffortProjectionPoint] = []
    by_slug = {m.slug.lower(): m for m in aa_models if m.slug}

    by_family: dict[tuple[str, str], list[SubbenchConfigCostSummary]] = {}
    for s in summaries:
        if s.aa_slug is None or s.aa_intelligence is None or s.tasks_per_month_attempted is None:
            continue
        fam = _base_family_id(s.model_id, s.effort)
        by_family.setdefault((fam, s.weekly_bucket), []).append(s)

    for (fam, bucket), fam_summaries in sorted(by_family.items()):
        latest_by_slug: dict[str, SubbenchConfigCostSummary] = {}
        for s in fam_summaries:
            assert s.aa_slug is not None
            latest_by_slug[s.aa_slug.lower()] = s

        measured_slugs = set(latest_by_slug.keys())
        anchor_candidates = sorted(
            latest_by_slug.values(),
            key=lambda x: (
                0 if "medium" in x.config_label else (1 if x.effort == "high" else 2),
                -x.cycle_id,
            ),
        )
        measured_with_cpt = [
            c for c in anchor_candidates if c.aa_ii_cost_per_task and c.aa_ii_cost_per_task > 0
        ]
        anchor = measured_with_cpt[0] if measured_with_cpt else anchor_candidates[0]
        fam_label = _clean_family_label(anchor.aa_name or anchor.model_id)
        fam_key = f"{fam}@{bucket}"

        for s in latest_by_slug.values():
            assert s.aa_slug is not None and s.aa_intelligence is not None
            assert s.tasks_per_month_attempted is not None
            aa_m = by_slug.get(s.aa_slug.lower())
            eff = _extract_effort_from_aa(aa_m) if aa_m else (s.effort or "default")
            rr = (s.resolved_valid / s.attempted_valid) if s.attempted_valid > 0 else None
            points.append(
                EffortProjectionPoint(
                    family_key=fam_key,
                    family_label=fam_label,
                    effort=eff,
                    aa_slug=s.aa_slug,
                    aa_name=s.aa_name or s.aa_slug,
                    weekly_bucket=bucket,
                    measured=True,
                    source_cycle_id=s.cycle_id,
                    source_config_label=s.config_label,
                    aa_intelligence=s.aa_intelligence,
                    aa_ii_cost_per_task=s.aa_ii_cost_per_task,
                    tasks_per_month_attempted=s.tasks_per_month_attempted,
                    tasks_per_month_resolved=s.tasks_per_month_resolved,
                    measured_resolve_rate=rr,
                )
            )

        if (
            anchor.aa_ii_cost_per_task is None
            or anchor.aa_ii_cost_per_task <= 0
            or anchor.tasks_per_month_attempted is None
        ):
            continue

        base_tokens_seq = tuple(_norm_tokens(fam))
        base_tokens = set(base_tokens_seq)
        for m in aa_models:
            if not m.slug or m.slug.lower() in measured_slugs:
                continue
            if (
                m.intelligence_index is None
                or m.ii_cost_per_task is None
                or m.ii_cost_per_task <= 0
            ):
                continue
            cand_tokens = set(_norm_tokens(f"{m.slug} {m.name}"))
            if not base_tokens.issubset(cand_tokens):
                continue
            eff = _extract_effort_from_aa(m)
            slug_base = _base_family_id(m.slug, eff)
            if tuple(_norm_tokens(slug_base)) != base_tokens_seq:
                continue

            # Pick the measured anchor closest in AA cost_per_task to target
            nearest = min(
                measured_with_cpt,
                key=lambda c: abs(math.log((c.aa_ii_cost_per_task or 1.0) / m.ii_cost_per_task)),
            )
            assert nearest.aa_ii_cost_per_task is not None
            assert nearest.tasks_per_month_attempted is not None
            ratio = nearest.aa_ii_cost_per_task / m.ii_cost_per_task
            proj_att = nearest.tasks_per_month_attempted * ratio
            points.append(
                EffortProjectionPoint(
                    family_key=fam_key,
                    family_label=fam_label,
                    effort=eff,
                    aa_slug=m.slug,
                    aa_name=m.name,
                    weekly_bucket=bucket,
                    measured=False,
                    source_cycle_id=nearest.cycle_id,
                    source_config_label=nearest.config_label,
                    aa_intelligence=m.intelligence_index,
                    aa_ii_cost_per_task=m.ii_cost_per_task,
                    tasks_per_month_attempted=proj_att,
                    tasks_per_month_resolved=None,
                    measured_resolve_rate=None,
                )
            )

    return points


AA_FAMILY_PALETTE = [
    "#0284c7",
    "#4f46e5",
    "#059669",
    "#9333ea",
    "#ea580c",
    "#e11d48",
    "#0d9488",
]


SHORT_FAMILY_LABELS = {
    "Claude Sonnet 5.5": "Sonnet 5.5",
    "Gemini 3.8 Flash": "Flash 3.8",
    "Gemini 4 Argon": "Argon 4",
    "Gemini 4 Hydrogen": "Hydrogen 4",
    "Gemini 4 Neon": "Neon 4",
    "GPT-6 Luna": "Luna 6",
}


def _place_label(
    xp: float,
    yp: float,
    text: str,
    *,
    is_measured: bool,
    plot_left: float,
    plot_right: float,
    plot_top: float,
    plot_bottom: float,
    placed_boxes: list[tuple[float, float, float, float]],
) -> tuple[float, float, str]:
    """Choose a non-overlapping (x, y, text-anchor) placement for a point label."""
    est_w = max(22.0, len(text) * (6.2 if is_measured else 5.6))
    est_h = 12.0
    candidates = [
        (9.0, -7.0, "start"),
        (-9.0, -7.0, "end"),
        (9.0, 12.0, "start"),
        (-9.0, 12.0, "end"),
        (0.0, -11.0, "middle"),
        (0.0, 16.0, "middle"),
        (9.0, -18.0, "start"),
        (-9.0, -18.0, "end"),
        (9.0, 22.0, "start"),
        (-9.0, 22.0, "end"),
    ]
    best = (xp + 9.0, yp - 7.0, "start")
    best_penalty = float("inf")

    for dx, dy, anchor in candidates:
        lx = xp + dx
        ly = yp + dy
        if anchor == "start":
            bx0, bx1 = lx, lx + est_w
        elif anchor == "end":
            bx0, bx1 = lx - est_w, lx
        else:
            bx0, bx1 = lx - est_w / 2.0, lx + est_w / 2.0
        by0, by1 = ly - est_h + 2.0, ly + 2.0

        penalty = 0.0
        if bx0 < plot_left + 2 or bx1 > plot_right - 2:
            penalty += 500.0
        if by0 < plot_top + 2 or by1 > plot_bottom - 2:
            penalty += 500.0
        for ox0, oy0, ox1, oy1 in placed_boxes:
            iw = max(0.0, min(bx1, ox1) - max(bx0, ox0))
            ih = max(0.0, min(by1, oy1) - max(by0, oy0))
            if iw > 0 and ih > 0:
                penalty += 100.0 + iw * ih
        if penalty < best_penalty:
            best_penalty = penalty
            best = (lx, ly, anchor)
            if penalty == 0.0:
                break

    lx, ly, anchor = best
    if anchor == "start":
        placed_boxes.append((lx, ly - est_h + 2.0, lx + est_w, ly + 2.0))
    elif anchor == "end":
        placed_boxes.append((lx - est_w, ly - est_h + 2.0, lx, ly + 2.0))
    else:
        placed_boxes.append((lx - est_w / 2.0, ly - est_h + 2.0, lx + est_w / 2.0, ly + 2.0))
    return best


def render_aa_intelligence_vs_tasks_svg(
    points: list[EffortProjectionPoint],
    *,
    aa_fetched_at: str | None = None,
) -> str:
    """Render an Artificial Analysis-style SVG scatter plot: AA Intelligence vs Tasks/Month."""
    width = 980
    height = 600
    pad_left = 80
    pad_right = 230
    pad_top = 70
    pad_bottom = 75
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    valid = [p for p in points if p.tasks_per_month_attempted > 0]
    if not valid:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
            f'width="{width}" height="{height}">'
            '<text x="50%" y="50%" text-anchor="middle" fill="#64748b">'
            "No AA-matched Subbench data available</text></svg>"
        )

    min_x = min(p.tasks_per_month_attempted for p in valid)
    max_x = max(p.tasks_per_month_attempted for p in valid)
    log_min = math.floor(math.log10(max(1.0, min_x * 0.75)) * 2) / 2
    log_max = math.ceil(math.log10(max_x * 1.35) * 2) / 2
    if log_max <= log_min:
        log_max = log_min + 1.0

    y_min = 0.0
    max_y = max(p.aa_intelligence for p in valid)
    y_max = float(math.ceil(max(65.0, max_y + 5.0) / 10.0) * 10)

    def x_px(val: float) -> float:
        lx = math.log10(max(1.0, val))
        return pad_left + ((lx - log_min) / (log_max - log_min)) * plot_w

    def y_px(val: float) -> float:
        return pad_top + plot_h - ((val - y_min) / (y_max - y_min)) * plot_h

    date_note = f"  |  AA snapshot {aa_fetched_at[:10]}" if aa_fetched_at else ""
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        'style="background-color:#f8fafc; font-family:Inter,system-ui,sans-serif;">',
        f'  <rect x="{pad_left}" y="{pad_top}" width="{plot_w}" height="{plot_h}" '
        'fill="#ffffff" stroke="#cbd5e1" stroke-width="1" />',
        f'  <text x="{pad_left}" y="32" font-size="19" font-weight="700" fill="#0f172a">'
        "AA Intelligence Index vs. Subscription Throughput (Tasks / Month)</text>",
        f'  <text x="{pad_left}" y="52" font-size="12" fill="#475569">'
        "Projected Monthly Tasks = Implied Pool $/mo ÷ AA Cost/Task  |  "
        f"Filled = quota-calibrated in Subbench, Hollow = sibling pool{date_note}</text>",
    ]

    cand_ticks = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000]
    p_bot = pad_top + plot_h
    for tx in cand_ticks:
        lx = math.log10(tx)
        if log_min - 1e-6 <= lx <= log_max + 1e-6:
            xp = x_px(float(tx))
            lines.append(
                f'  <line x1="{xp:.1f}" y1="{pad_top}" x2="{xp:.1f}" y2="{p_bot}" '
                'stroke="#e2e8f0" stroke-dasharray="3,3" stroke-width="1" />'
            )
            lines.append(
                f'  <text x="{xp:.1f}" y="{p_bot + 20}" font-size="11" '
                f'text-anchor="middle" fill="#64748b">{tx:,}</text>'
            )

    y_val = 0.0
    while y_val <= y_max + 1e-6:
        yp = y_px(y_val)
        lines.append(
            f'  <line x1="{pad_left}" y1="{yp:.1f}" x2="{pad_left + plot_w}" y2="{yp:.1f}" '
            'stroke="#e2e8f0" stroke-dasharray="3,3" stroke-width="1" />'
        )
        lines.append(
            f'  <text x="{pad_left - 10}" y="{yp + 4:.1f}" font-size="11" '
            f'text-anchor="end" fill="#64748b">{int(y_val)}</text>'
        )
        y_val += 10.0

    lines.append(
        f'  <text x="{pad_left + plot_w / 2:.1f}" y="{p_bot + 46}" font-size="13" '
        'font-weight="600" text-anchor="middle" fill="#1e293b">'
        "Projected Monthly Tasks (Attempted, Log Scale)</text>"
    )
    mid_y = pad_top + plot_h / 2
    lines.append(
        f'  <text x="24" y="{mid_y:.1f}" font-size="13" font-weight="600" '
        f'text-anchor="middle" fill="#1e293b" transform="rotate(-90, 24, {mid_y:.1f})">'
        "Artificial Analysis Intelligence Index (v5)</text>"
    )

    families: dict[str, list[EffortProjectionPoint]] = {}
    for p in valid:
        families.setdefault(p.family_key, []).append(p)

    sorted_desc_x = sorted(valid, key=lambda p: (-p.tasks_per_month_attempted, -p.aa_intelligence))
    pareto: list[EffortProjectionPoint] = []
    best_ii = -1.0
    for p in sorted_desc_x:
        if p.aa_intelligence > best_ii:
            best_ii = p.aa_intelligence
            pareto.append(p)
    if len(pareto) >= 2:
        pareto_pts = " ".join(
            f"{x_px(p.tasks_per_month_attempted):.1f},{y_px(p.aa_intelligence):.1f}" for p in pareto
        )
        lines.append(
            f'  <polyline points="{pareto_pts}" fill="none" stroke="#94a3b8" '
            'stroke-width="1.5" stroke-dasharray="6,4" />'
        )

    legend_entries: list[tuple[str, str, str]] = []
    placed_boxes: list[tuple[float, float, float, float]] = []
    # Reserve circle hitboxes so labels stay clear of dots
    for p in valid:
        xp = x_px(p.tasks_per_month_attempted)
        yp = y_px(p.aa_intelligence)
        placed_boxes.append((xp - 6.0, yp - 6.0, xp + 6.0, yp + 6.0))

    label_svg: list[str] = []
    for idx, (_fam_key, fam_pts) in enumerate(sorted(families.items())):
        color = AA_FAMILY_PALETTE[idx % len(AA_FAMILY_PALETTE)]
        fam_sorted = sorted(fam_pts, key=lambda p: p.aa_intelligence)
        if len(fam_sorted) >= 2:
            poly = " ".join(
                f"{x_px(p.tasks_per_month_attempted):.1f},{y_px(p.aa_intelligence):.1f}"
                for p in fam_sorted
            )
            lines.append(
                f'  <polyline points="{poly}" fill="none" stroke="{color}" '
                'stroke-width="2" stroke-opacity="0.65" />'
            )

        legend_entries.append((fam_sorted[0].family_label, fam_sorted[0].weekly_bucket, color))

        for p in fam_sorted:
            xp = x_px(p.tasks_per_month_attempted)
            yp = y_px(p.aa_intelligence)
            if p.measured:
                lines.append(
                    f'  <circle cx="{xp:.1f}" cy="{yp:.1f}" r="6" fill="{color}" '
                    'stroke="#ffffff" stroke-width="1.5" />'
                )
            else:
                lines.append(
                    f'  <circle cx="{xp:.1f}" cy="{yp:.1f}" r="4.8" fill="#ffffff" '
                    f'stroke="{color}" stroke-width="2" />'
                )

            eff_txt = p.effort.capitalize()
            short_fam = SHORT_FAMILY_LABELS.get(p.family_label, p.family_label)
            lbl_txt = f"{short_fam} ({eff_txt})" if p.measured else eff_txt
            fw = "600" if p.measured else "400"
            lx, ly, anchor = _place_label(
                xp,
                yp,
                lbl_txt,
                is_measured=p.measured,
                plot_left=pad_left,
                plot_right=pad_left + plot_w,
                plot_top=pad_top,
                plot_bottom=p_bot,
                placed_boxes=placed_boxes,
            )
            label_svg.append(
                f'  <text x="{lx:.1f}" y="{ly:.1f}" font-size="10.5" '
                f'font-weight="{fw}" text-anchor="{anchor}" fill="#1e293b">'
                f"{html.escape(lbl_txt)}</text>"
            )

    lines.extend(label_svg)

    leg_x = pad_left + plot_w + 18
    leg_y = pad_top + 10
    lines.append(
        f'  <rect x="{leg_x - 8}" y="{leg_y - 14}" width="206" '
        f'height="{len(legend_entries) * 30 + 102}" rx="6" fill="#ffffff" '
        'stroke="#e2e8f0" stroke-width="1" />'
    )
    lines.append(
        f'  <text x="{leg_x}" y="{leg_y + 2}" font-size="12" font-weight="700" '
        'fill="#0f172a">Model &amp; Pool</text>'
    )
    cy = leg_y + 24
    for fam_lbl, bkt, col in legend_entries:
        lines.append(f'  <circle cx="{leg_x + 6}" cy="{cy - 4}" r="5" fill="{col}" />')
        lines.append(
            f'  <text x="{leg_x + 17}" y="{cy - 1}" font-size="11" font-weight="600" '
            f'fill="#1e293b">{html.escape(fam_lbl)}</text>'
        )
        lines.append(
            f'  <text x="{leg_x + 17}" y="{cy + 11}" font-size="9.5" fill="#64748b">'
            f"{html.escape(bkt)}</text>"
        )
        cy += 30

    lines.append(f'  <circle cx="{leg_x + 6}" cy="{cy - 4}" r="4.5" fill="#475569" />')
    lines.append(
        f'  <text x="{leg_x + 17}" y="{cy}" font-size="10" fill="#475569">'
        "Measured (Subbench)</text>"
    )
    cy += 18
    lines.append(
        f'  <circle cx="{leg_x + 6}" cy="{cy - 4}" r="4.2" fill="#ffffff" '
        'stroke="#475569" stroke-width="1.8" />'
    )
    lines.append(
        f'  <text x="{leg_x + 17}" y="{cy}" font-size="10" fill="#475569">'
        "Projected (AA ratio)</text>"
    )
    cy += 18
    lines.append(
        f'  <line x1="{leg_x}" y1="{cy}" x2="{leg_x + 18}" y2="{cy}" '
        'stroke="#94a3b8" stroke-width="1.5" stroke-dasharray="4,3" />'
    )
    lines.append(
        f'  <text x="{leg_x + 24}" y="{cy + 4}" font-size="10" fill="#475569">'
        "Pareto frontier</text>"
    )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def write_aa_projection_reports(
    summaries: list[SubbenchConfigCostSummary],
    *,
    effort_points: list[EffortProjectionPoint] | None = None,
    out_dir: Path = Path("artifacts/reports"),
    aa_fetched_at: str | None = None,
) -> dict[str, Path]:
    """Write Markdown, CSV, and SVG AA × Subbench monthly projection reports."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "aa_projections.csv"
    md_path = out_dir / "aa_projections.md"
    effort_csv_path = out_dir / "aa_effort_projections.csv"
    svg_path = out_dir / "aa_intelligence_vs_tasks_per_month.svg"

    fieldnames = [
        "cycle_id",
        "config_label",
        "model_id",
        "effort",
        "agent",
        "weekly_bucket",
        "resolved_valid",
        "attempted_valid",
        "weekly_burn_pp",
        "uncached_input_tokens",
        "cache_read_tokens",
        "output_tokens",
        "aa_slug",
        "aa_name",
        "aa_intelligence",
        "aa_ii_cost_per_task",
        "aa_price_1m_in",
        "aa_price_1m_out",
        "aa_price_1m_cache",
        "api_cost_usd",
        "implied_pool_usd_per_week",
        "implied_pool_usd_per_month",
        "tasks_per_month_attempted",
        "tasks_per_month_resolved",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in summaries:
            writer.writerow({k: getattr(s, k) for k in fieldnames})

    if effort_points is not None:
        eff_fields = [
            "family_key",
            "family_label",
            "effort",
            "aa_slug",
            "aa_name",
            "weekly_bucket",
            "measured",
            "source_cycle_id",
            "source_config_label",
            "aa_intelligence",
            "aa_ii_cost_per_task",
            "tasks_per_month_attempted",
            "tasks_per_month_resolved",
            "measured_resolve_rate",
        ]
        with open(effort_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=eff_fields)
            writer.writeheader()
            for p in effort_points:
                writer.writerow({k: getattr(p, k) for k in eff_fields})

        svg_path.write_text(
            render_aa_intelligence_vs_tasks_svg(effort_points, aa_fetched_at=aa_fetched_at),
            encoding="utf-8",
        )

    lines = [
        "# Artificial Analysis × SubscriptionBench Monthly Projections",
        "",
        f"**AA Snapshot:** `{aa_fetched_at or 'unknown'}`  ",
        "**Methodology:** Subbench is used to calibrate the token-to-quota conversion rate "
        "(`Implied Pool $/mo` = `api_cost_usd / total_burn_frac × weeks_per_month`, where "
        "`api_cost_usd` prices Subbench `uncached_input`, `cache_read`, and `output` tokens "
        "using AA list rates with `0.10×` cache-read fallback). When a `5h` bucket is present, "
        "weekly burn is calibrated from `5h / ratio` (`3.50` for `gemini-5h`, `2.00` for "
        "`3p-5h`) to eliminate weekly backend settling jitter. `Tasks/mo` divides "
        "`Implied Pool $/mo` by AA's full-suite `cost_per_task` (`aa_ii_cost_per_task`).",
        "",
        "## 1. Empirically Measured Subbench Configs",
        "",
        "| Cycle | Config | Pool | Resolved | AA Match | AA Intel | API $/1M (In/Out) | "
        "Run API $ | Burn (pp) | Implied Pool $/mo | Tasks/mo (Att / Res) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        intel_s = f"{s.aa_intelligence:.1f}" if s.aa_intelligence is not None else "N/A"
        price_s = (
            f"${s.aa_price_1m_in:.2f}/${s.aa_price_1m_out:.2f}"
            if (s.aa_price_1m_in is not None and s.aa_price_1m_out is not None)
            else "N/A"
        )
        cost_s = f"${s.api_cost_usd:.3f}" if s.api_cost_usd is not None else "N/A"
        pool_mo_s = (
            f"${s.implied_pool_usd_per_month:.0f}"
            if s.implied_pool_usd_per_month is not None
            else "N/A"
        )
        t_att = (
            f"{s.tasks_per_month_attempted:.0f}"
            if s.tasks_per_month_attempted is not None
            else "N/A"
        )
        t_res = (
            f"{s.tasks_per_month_resolved:.0f}" if s.tasks_per_month_resolved is not None else "N/A"
        )
        aa_lbl = f"`{s.aa_slug}`" if s.aa_slug else "*unmatched*"
        lines.append(
            f"| {s.cycle_id} | `{s.config_label}` | `{s.weekly_bucket}` | "
            f"{s.resolved_valid}/{s.attempted_valid} | {aa_lbl} | {intel_s} | {price_s} | "
            f"{cost_s} | {s.weekly_burn_pp:.2f} | {pool_mo_s} | {t_att} / {t_res} |"
        )

    if effort_points is not None:
        lines.extend(
            [
                "",
                "## 2. Effort-Scaled Monthly Projections (AA Cost-Per-Task Ratio)",
                "",
                "![AA Intelligence vs Tasks Per Month](./aa_intelligence_vs_tasks_per_month.svg)",
                "",
                "| Model | Effort | Pool | Source | AA Intel | AA Cost/Task | "
                "Est. Tasks/mo (Att) |",
                "|---|---|---|---|---|---|---|",
            ]
        )
        for p in sorted(
            effort_points,
            key=lambda x: (-x.aa_intelligence, -x.tasks_per_month_attempted),
        ):
            src = (
                f"Measured (c{p.source_cycle_id})"
                if p.measured
                else f"Scaled from `{p.source_config_label}`"
            )
            cpt = f"${p.aa_ii_cost_per_task:.4f}" if p.aa_ii_cost_per_task is not None else "N/A"
            lines.append(
                f"| {p.family_label} | {p.effort} | `{p.weekly_bucket}` | {src} | "
                f"{p.aa_intelligence:.1f} | {cpt} | **{p.tasks_per_month_attempted:.0f}** |"
            )

    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    out: dict[str, Path] = {"csv": csv_path, "md": md_path}
    if effort_points is not None:
        out["effort_csv"] = effort_csv_path
        out["svg"] = svg_path
    return out
