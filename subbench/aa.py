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
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_AA_ENDPOINT = "https://artificialanalysis.ai/api/v2/language/models/free"
DEFAULT_AA_CACHE_DIR = Path("data/cache")
DEFAULT_AA_LATEST_PATH = DEFAULT_AA_CACHE_DIR / "aa_models_latest.json"
DEFAULT_AA_TOKEN_USAGE_PATH = DEFAULT_AA_CACHE_DIR / "aa_token_usage.json"
DEFAULT_CALIBRATION_PATH = Path("artifacts/reports/quota_weight_calibration.json")
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
    price_1m_cache_write: float | None = None
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
        p_cache_write = _as_float(pricing.get("price_1m_cache_write_tokens"))
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
                price_1m_cache_write=p_cache_write,
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

    # 2. Score overlap on normalized token multisets (e.g. gemini-4-neon + medium)
    base_tokens_seq = tuple(_norm_tokens(base_id))
    base_counts = Counter(base_tokens_seq)
    base_total = len(base_tokens_seq)
    base_nums = [t for t in base_tokens_seq if t.isdigit()]
    best: AAModel | None = None
    best_score = 0.0

    for m in aa_models:
        cand_eff = _extract_effort_from_aa(m)
        slug_base_seq = tuple(_norm_tokens(_base_family_id(m.slug, cand_eff)))
        cand_nums = [t for t in slug_base_seq if t.isdigit()]
        if base_nums and cand_nums != base_nums:
            continue

        cand_slug_counts = Counter(_norm_tokens(m.slug))
        cand_name_counts = Counter(_norm_tokens(m.name))
        cand_tokens = set(cand_slug_counts) | set(cand_name_counts)

        overlap = max(
            sum((base_counts & cand_slug_counts).values()),
            sum((base_counts & cand_name_counts).values()),
        )
        if slug_base_seq == base_tokens_seq:
            score = float(base_total * 10) + 15.0
        elif overlap == base_total:
            score = float(base_total * 10) - abs(len(slug_base_seq) - base_total) * 8.0
        else:
            # Allow partial overlap if at least family + version match
            if overlap < max(2, base_total - 1):
                continue
            score = float(overlap) - abs(len(slug_base_seq) - base_total) * 3.0

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
    cycle_started_at: str | None = None
    cache_write_tokens: int = 0
    implied_pool_se_pct: float | None = None
    implied_pool_ci_low: float | None = None
    implied_pool_ci_high: float | None = None
    tasks_mo_ci_low: float | None = None
    tasks_mo_ci_high: float | None = None


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
    measured_resolve_rate: float | None
    tasks_mo_ci_low: float | None = None
    tasks_mo_ci_high: float | None = None


FIVE_H_TO_WEEKLY_RATIO: dict[str, float] = {
    "gemini-5h": 3.50,
    "3p-5h": 2.00,
}

# Two-sided Student-t 95% critical values by degrees of freedom (df = n - 1)
_T_CRIT_95: dict[int, float] = {
    1: 12.706,
    2: 4.303,
    3: 3.182,
    4: 2.776,
    5: 2.571,
    6: 2.447,
    7: 2.365,
    8: 2.306,
    9: 2.262,
    10: 2.228,
    12: 2.179,
    15: 2.131,
    20: 2.086,
}


def _t_crit_95(df: int) -> float:
    if df <= 0:
        return 1.96
    if df in _T_CRIT_95:
        return _T_CRIT_95[df]
    for k in sorted(_T_CRIT_95):
        if df <= k:
            return _T_CRIT_95[k]
    return 1.96


def _compute_pool_uncertainty(
    *,
    implied_mo_usd: float,
    api_cost_usd: float,
    total_burn_frac: float,
    weeks_per_month: float,
    is_quantized_pct: bool,
    attempt_cost_burn: list[tuple[float, float]],
) -> tuple[float | None, float | None, float | None]:
    """Return (se_pct, ci_low_usd, ci_high_usd) for Implied Pool $/mo."""
    if total_burn_frac <= 0 or implied_mo_usd <= 0:
        return None, None, None

    if is_quantized_pct:
        # Discrete 1pp integer bucket (e.g. codex-30d): start/end uniform [-0.5, +0.5] pp
        # gives RMS = 1/sqrt(6) pp and exact worst-case bounds [B - 0.01, B + 0.01].
        se_pct = 100.0 * (0.01 / math.sqrt(6.0)) / total_burn_frac
        ci_low = (api_cost_usd / (total_burn_frac + 0.01)) * weeks_per_month
        ci_high = (api_cost_usd / max(total_burn_frac - 0.01, 0.002)) * weeks_per_month
        return se_pct, ci_low, ci_high

    n = len(attempt_cost_burn)
    if n < 2:
        return None, None, None

    tot_c = sum(c for c, _ in attempt_cost_burn)
    tot_b = sum(b for _, b in attempt_cost_burn)
    if tot_b <= 0:
        return None, None, None

    jks: list[float] = []
    for c_i, b_i in attempt_cost_burn:
        rem_b = tot_b - b_i
        if rem_b > 0:
            jks.append(((tot_c - c_i) / rem_b) * weeks_per_month)
    if len(jks) < 2:
        return None, None, None

    k = len(jks)
    jk_mean = sum(jks) / k
    jk_var = ((k - 1) / k) * sum((v - jk_mean) ** 2 for v in jks)
    jk_se = math.sqrt(max(0.0, jk_var))
    se_pct = 100.0 * jk_se / implied_mo_usd
    half_w = _t_crit_95(k - 1) * jk_se
    ci_low = max(implied_mo_usd * 0.1, implied_mo_usd - half_w)
    ci_high = implied_mo_usd + half_w
    return se_pct, ci_low, ci_high


def _compute_net_contiguous_burn(
    cycle_rows: list[tuple[int, int, int, float | None, float | None, str | None, str | None]],
    target_config_id: int,
    *,
    external_use_threshold: float = 5e-4,
) -> float:
    """Compute net quota burn across contiguous valid attempts of a config.

    Each contiguous segment sums ``seg_start_pre - seg_end_post``, which nets out
    inter-attempt backend pre-auth recredits (``curr_pre > prev_post``) and
    sub-threshold delayed settling debits while breaking segments on invalid
    attempts, missing snapshots, interleaved configs, bucket resets, or
    above-threshold external drops.
    """
    total_burn = 0.0
    seg_start_pre: float | None = None
    seg_end_post: float | None = None
    prev_post_reset: str | None = None
    prev_attempt_burn = 0.0

    def _flush() -> None:
        nonlocal total_burn, seg_start_pre, seg_end_post, prev_post_reset, prev_attempt_burn
        if seg_start_pre is not None and seg_end_post is not None:
            total_burn += max(0.0, seg_start_pre - seg_end_post)
        seg_start_pre = None
        seg_end_post = None
        prev_post_reset = None
        prev_attempt_burn = 0.0

    for _aid, cfg_id, valid, pre_f, post_f, pre_rst, post_rst in cycle_rows:
        if cfg_id != target_config_id or valid != 1 or pre_f is None or post_f is None:
            _flush()
            continue

        att_burn = max(0.0, pre_f - post_f)
        if seg_start_pre is not None and seg_end_post is not None:
            reset_changed = bool(prev_post_reset and pre_rst and prev_post_reset != pre_rst)
            recredit = pre_f - seg_end_post
            drop = seg_end_post - pre_f
            is_plausible_recredit = (
                recredit > 0
                and not reset_changed
                and pre_f <= seg_start_pre + 1e-9
                and recredit <= prev_attempt_burn + 1e-9
            )
            is_small_settling_drop = (
                drop >= 0 and not reset_changed and drop <= external_use_threshold + 1e-9
            )
            if is_plausible_recredit or is_small_settling_drop:
                seg_end_post = post_f
                prev_post_reset = post_rst
                prev_attempt_burn = att_burn
                continue
            _flush()

        seg_start_pre = pre_f
        seg_end_post = post_f
        prev_post_reset = post_rst
        prev_attempt_burn = att_burn

    _flush()
    return total_burn


def _tokens_api_cost_usd(
    *,
    raw_in: int,
    raw_out: int,
    raw_cch: int,
    raw_cw: int,
    agent: str,
    p_in: float | None,
    p_out: float | None,
    p_cch: float | None,
    p_cw: float | None,
) -> float | None:
    if p_in is None or p_out is None or p_cch is None or p_cw is None:
        return None
    u_in = max(0, raw_in - raw_cch - raw_cw) if agent == "codex" else raw_in
    return (
        (u_in / 1e6) * p_in
        + (raw_cw / 1e6) * p_cw
        + (raw_cch / 1e6) * p_cch
        + (raw_out / 1e6) * p_out
    )


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
        SELECT c.id as cycle_id, c.started_at, e.manifest_json, cf.id as config_id, cf.label,
               cf.model_id, cf.effort
        FROM cycles c
        JOIN experiments e ON c.experiment_id = e.id
        JOIN configs cf ON cf.experiment_id = e.id
        WHERE c.status = 'completed' {where_suite}
        ORDER BY c.id ASC, cf.id ASC
        """,
        params,
    ).fetchall()

    attempt_cols = {r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()}
    cw_expr = "COALESCE(a.cache_write_tokens, 0)" if "cache_write_tokens" in attempt_cols else "0"

    summaries: list[SubbenchConfigCostSummary] = []
    for cycle_id, cycle_started_at, manifest_json_str, config_id, label, model_id, effort in rows:
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

        ext_thresh = float(quota_obj.get("external_use_threshold", 5e-4))
        cycle_quota_rows = conn.execute(
            """
            SELECT a.id, a.config_id, a.valid,
                   q_pre.remaining_fraction, q_post.remaining_fraction,
                   q_pre.reset_time, q_post.reset_time
            FROM attempts a
            LEFT JOIN quota_snapshots q_pre
                ON q_pre.attempt_id = a.id AND q_pre.phase = 'pre' AND q_pre.bucket_id = ?
            LEFT JOIN quota_snapshots q_post
                ON q_post.attempt_id = a.id AND q_post.phase = 'post' AND q_post.bucket_id = ?
            WHERE a.cycle_id = ?
            ORDER BY a.id ASC
            """,
            (measure_bucket, measure_bucket, cycle_id),
        ).fetchall()

        attempts = conn.execute(
            f"""
            SELECT a.id, a.instance_id, a.pass_index, a.seq,
                   COALESCE(a.input_tokens, 0), COALESCE(a.output_tokens, 0),
                   COALESCE(a.cache_read_tokens, 0),
                   {cw_expr} as cache_write_tokens,
                   q_pre.remaining_fraction, q_post.remaining_fraction,
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

        raw_burn_frac = _compute_net_contiguous_burn(
            cycle_quota_rows, config_id, external_use_threshold=ext_thresh
        )
        total_burn_frac = raw_burn_frac / ratio_5h if ratio_5h else raw_burn_frac
        weekly_burn_pp = total_burn_frac * 100.0
        attempted_valid = len({(r[1], r[2]) for r in attempts})

        final_by_slot: dict[tuple[str, int], int] = {}
        for r in attempts:
            final_by_slot[(r[1], r[2])] = int(r[10])
        resolved_valid = sum(final_by_slot.values())

        raw_in = sum(int(r[4]) for r in attempts)
        raw_out = sum(int(r[5]) for r in attempts)
        raw_cch = sum(int(r[6]) for r in attempts)
        raw_cw = sum(int(r[7]) for r in attempts)

        # Codex reports input_tokens inclusive of cached_input_tokens; agy reports them separately
        if agent == "codex":
            uncached_in = max(0, raw_in - raw_cch - raw_cw)
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
        p_cw = (
            aa_match.price_1m_cache_write
            if (aa_match and aa_match.price_1m_cache_write is not None)
            else p_in
        )

        api_cost_usd = _tokens_api_cost_usd(
            raw_in=raw_in,
            raw_out=raw_out,
            raw_cch=raw_cch,
            raw_cw=raw_cw,
            agent=agent,
            p_in=p_in,
            p_out=p_out,
            p_cch=p_cch,
            p_cw=p_cw,
        )

        is_30d = weekly_bucket.endswith("-30d")
        weeks_per_month = 1.0 if is_30d else 4.33
        implied_wk_usd: float | None = None
        implied_mo_usd: float | None = None
        pool_se_pct: float | None = None
        pool_ci_lo: float | None = None
        pool_ci_hi: float | None = None
        if api_cost_usd is not None and total_burn_frac > 0:
            implied_wk_usd = api_cost_usd / total_burn_frac
            implied_mo_usd = implied_wk_usd * weeks_per_month

            # Build recredit-adjusted per-attempt (cost, burn_wk) pairs for jackknife SE
            next_pre_by_aid: dict[int, float] = {}
            for idx_q in range(len(cycle_quota_rows) - 1):
                cur_q = cycle_quota_rows[idx_q]
                nxt_q = cycle_quota_rows[idx_q + 1]
                if (
                    cur_q[1] == config_id
                    and nxt_q[1] == config_id
                    and cur_q[2] == 1
                    and nxt_q[2] == 1
                    and cur_q[4] is not None
                    and nxt_q[3] is not None
                    and not (cur_q[6] and nxt_q[5] and cur_q[6] != nxt_q[5])
                ):
                    next_pre_by_aid[int(cur_q[0])] = float(nxt_q[3])

            att_pairs: list[tuple[float, float]] = []
            for r in attempts:
                c_i = _tokens_api_cost_usd(
                    raw_in=int(r[4]),
                    raw_out=int(r[5]),
                    raw_cch=int(r[6]),
                    raw_cw=int(r[7]),
                    agent=agent,
                    p_in=p_in,
                    p_out=p_out,
                    p_cch=p_cch,
                    p_cw=p_cw,
                )
                if c_i is None:
                    continue
                pre_f, post_f = float(r[8]), float(r[9])
                d_i = max(0.0, pre_f - post_f)
                nxt_pre = next_pre_by_aid.get(int(r[0]))
                if nxt_pre is not None:
                    rec = nxt_pre - post_f
                    if 0.0 < rec <= d_i + 1e-9 and nxt_pre <= pre_f + 1e-9:
                        d_i = max(0.0, d_i - rec)
                b_wk_i = d_i / ratio_5h if ratio_5h else d_i
                att_pairs.append((c_i, b_wk_i))

            pool_se_pct, pool_ci_lo, pool_ci_hi = _compute_pool_uncertainty(
                implied_mo_usd=implied_mo_usd,
                api_cost_usd=api_cost_usd,
                total_burn_frac=total_burn_frac,
                weeks_per_month=weeks_per_month,
                is_quantized_pct=is_30d,
                attempt_cost_burn=att_pairs,
            )

        aa_cpt = aa_match.ii_cost_per_task if aa_match else None
        tasks_ci_lo: float | None = None
        tasks_ci_hi: float | None = None
        if implied_mo_usd is not None and aa_cpt is not None and aa_cpt > 0:
            # Convert Subbench-calibrated monthly pool $ into AA Intelligence Index tasks/month
            tasks_mo_att: float | None = implied_mo_usd / aa_cpt
            if pool_ci_lo is not None and pool_ci_hi is not None:
                tasks_ci_lo = pool_ci_lo / aa_cpt
                tasks_ci_hi = pool_ci_hi / aa_cpt
        else:
            tasks_mo_att = None

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
                cycle_started_at=cycle_started_at,
                cache_write_tokens=raw_cw,
                implied_pool_se_pct=pool_se_pct,
                implied_pool_ci_low=pool_ci_lo,
                implied_pool_ci_high=pool_ci_hi,
                tasks_mo_ci_low=tasks_ci_lo,
                tasks_mo_ci_high=tasks_ci_hi,
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
                    measured_resolve_rate=rr,
                    tasks_mo_ci_low=s.tasks_mo_ci_low,
                    tasks_mo_ci_high=s.tasks_mo_ci_high,
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
            proj_lo = (
                nearest.tasks_mo_ci_low * ratio if nearest.tasks_mo_ci_low is not None else None
            )
            proj_hi = (
                nearest.tasks_mo_ci_high * ratio if nearest.tasks_mo_ci_high is not None else None
            )
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
                    measured_resolve_rate=None,
                    tasks_mo_ci_low=proj_lo,
                    tasks_mo_ci_high=proj_hi,
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
    "Claude Opus 5.5": "Opus 5.5",
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
    show_ci: bool = False,
    title: str = "AA Intelligence Index vs. Subscription Throughput (AA Tasks / Month)",
    subtitle: str | None = None,
    x_label: str = "Projected Monthly AA Intelligence Index Tasks (Log Scale)",
    measured_legend: str = "Measured (Subbench)",
    projected_legend: str = "Projected (sibling pool)",
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

    min_x = min(
        (
            p.tasks_mo_ci_low
            if (show_ci and p.measured and p.tasks_mo_ci_low is not None and p.tasks_mo_ci_low > 0)
            else p.tasks_per_month_attempted
        )
        for p in valid
    )
    max_x = max(
        (
            p.tasks_mo_ci_high
            if (
                show_ci and p.measured and p.tasks_mo_ci_high is not None and p.tasks_mo_ci_high > 0
            )
            else p.tasks_per_month_attempted
        )
        for p in valid
    )
    log_min = math.floor(math.log10(max(1e-3, min_x * 0.75)) * 2) / 2
    log_max = math.ceil(math.log10(max_x * 1.35) * 2) / 2
    if log_max <= log_min:
        log_max = log_min + 1.0

    y_min = 0.0
    max_y = max(p.aa_intelligence for p in valid)
    y_max = float(math.ceil(max(65.0, max_y + 5.0) / 10.0) * 10)

    def x_px(val: float) -> float:
        lx = math.log10(max(1e-3, val))
        return pad_left + ((lx - log_min) / (log_max - log_min)) * plot_w

    def y_px(val: float) -> float:
        return pad_top + plot_h - ((val - y_min) / (y_max - y_min)) * plot_h

    date_note = f"  |  AA snapshot {aa_fetched_at[:10]}" if aa_fetched_at else ""
    sub_text = (
        subtitle
        if subtitle is not None
        else (
            "Actual agy/codex quota calibration (Implied Pool $/mo ÷ AA Cost/Task)  |  "
            f"Filled = Subbench-calibrated, Hollow = sibling pool{date_note}"
        )
    )
    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
        f'width="{width}" height="{height}" '
        'style="background-color:#f8fafc; font-family:Inter,system-ui,sans-serif;">',
        f'  <rect x="{pad_left}" y="{pad_top}" width="{plot_w}" height="{plot_h}" '
        'fill="#ffffff" stroke="#cbd5e1" stroke-width="1" />',
        f'  <text x="{pad_left}" y="32" font-size="19" font-weight="700" fill="#0f172a">'
        f"{html.escape(title)}</text>",
        f'  <text x="{pad_left}" y="52" font-size="11.5" fill="#475569">'
        f"{html.escape(sub_text)}</text>",
    ]

    cand_ticks: list[float] = [
        0.1,
        0.2,
        0.5,
        1,
        2,
        5,
        10,
        20,
        50,
        100,
        200,
        500,
        1000,
        2000,
        5000,
        10000,
        20000,
    ]
    p_bot = pad_top + plot_h
    for tx in cand_ticks:
        lx = math.log10(tx)
        if log_min - 1e-6 <= lx <= log_max + 1e-6:
            xp = x_px(float(tx))
            tx_lbl = f"{int(tx):,}" if tx >= 1 else f"{tx:g}"
            lines.append(
                f'  <line x1="{xp:.1f}" y1="{pad_top}" x2="{xp:.1f}" y2="{p_bot}" '
                'stroke="#e2e8f0" stroke-dasharray="3,3" stroke-width="1" />'
            )
            lines.append(
                f'  <text x="{xp:.1f}" y="{p_bot + 20}" font-size="11" '
                f'text-anchor="middle" fill="#64748b">{tx_lbl}</text>'
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
        f"{html.escape(x_label)}</text>"
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
                if (
                    show_ci
                    and p.tasks_mo_ci_low is not None
                    and p.tasks_mo_ci_high is not None
                    and p.tasks_mo_ci_high > p.tasks_mo_ci_low > 0
                ):
                    x_lo = x_px(p.tasks_mo_ci_low)
                    x_hi = x_px(p.tasks_mo_ci_high)
                    lines.append(
                        f'  <line x1="{x_lo:.1f}" y1="{yp:.1f}" x2="{x_hi:.1f}" y2="{yp:.1f}" '
                        f'stroke="{color}" stroke-width="1.8" stroke-opacity="0.55" />'
                    )
                    lines.append(
                        f'  <line x1="{x_lo:.1f}" y1="{yp - 4:.1f}" x2="{x_lo:.1f}" '
                        f'y2="{yp + 4:.1f}" stroke="{color}" stroke-width="1.5" '
                        'stroke-opacity="0.65" />'
                    )
                    lines.append(
                        f'  <line x1="{x_hi:.1f}" y1="{yp - 4:.1f}" x2="{x_hi:.1f}" '
                        f'y2="{yp + 4:.1f}" stroke="{color}" stroke-width="1.5" '
                        'stroke-opacity="0.65" />'
                    )
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
    leg_h = len(legend_entries) * 30 + (120 if show_ci else 102)
    lines.append(
        f'  <rect x="{leg_x - 8}" y="{leg_y - 14}" width="206" '
        f'height="{leg_h}" rx="6" fill="#ffffff" '
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
        f"{html.escape(measured_legend)}</text>"
    )
    cy += 18
    lines.append(
        f'  <circle cx="{leg_x + 6}" cy="{cy - 4}" r="4.2" fill="#ffffff" '
        'stroke="#475569" stroke-width="1.8" />'
    )
    lines.append(
        f'  <text x="{leg_x + 17}" y="{cy}" font-size="10" fill="#475569">'
        f"{html.escape(projected_legend)}</text>"
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
    if show_ci:
        cy += 18
        lines.append(
            f'  <line x1="{leg_x}" y1="{cy}" x2="{leg_x + 18}" y2="{cy}" '
            'stroke="#475569" stroke-width="1.8" stroke-opacity="0.65" />'
        )
        lines.append(
            f'  <line x1="{leg_x}" y1="{cy - 3}" x2="{leg_x}" y2="{cy + 3}" '
            'stroke="#475569" stroke-width="1.5" />'
        )
        lines.append(
            f'  <line x1="{leg_x + 18}" y1="{cy - 3}" x2="{leg_x + 18}" y2="{cy + 3}" '
            'stroke="#475569" stroke-width="1.5" />'
        )
        lines.append(
            f'  <text x="{leg_x + 24}" y="{cy + 4}" font-size="10" fill="#475569">'
            "95% CI (measured)</text>"
        )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def write_aa_projection_reports(
    summaries: list[SubbenchConfigCostSummary],
    *,
    effort_points: list[EffortProjectionPoint] | None = None,
    out_dir: Path = Path("artifacts/reports"),
    aa_fetched_at: str | None = None,
    show_ci: bool = False,
) -> dict[str, Path]:
    """Write Markdown, CSV, and SVG AA × Subbench monthly projection reports."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "aa_projections.csv"
    md_path = out_dir / "aa_projections.md"
    effort_csv_path = out_dir / "aa_effort_projections.csv"
    svg_path = out_dir / "aa_intelligence_vs_tasks_per_month.svg"

    fieldnames = [
        "cycle_id",
        "cycle_started_at",
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
        "cache_write_tokens",
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
        "implied_pool_se_pct",
        "implied_pool_ci_low",
        "implied_pool_ci_high",
        "tasks_per_month_attempted",
        "tasks_mo_ci_low",
        "tasks_mo_ci_high",
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
            "tasks_mo_ci_low",
            "tasks_mo_ci_high",
            "measured_resolve_rate",
        ]
        with open(effort_csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=eff_fields)
            writer.writeheader()
            for p in effort_points:
                writer.writerow({k: getattr(p, k) for k in eff_fields})

        svg_path.write_text(
            render_aa_intelligence_vs_tasks_svg(
                effort_points, aa_fetched_at=aa_fetched_at, show_ci=show_ci
            ),
            encoding="utf-8",
        )

    lines = [
        "# Artificial Analysis × SubscriptionBench Monthly Projections (Primary)",
        "",
        f"**AA Snapshot:** `{aa_fetched_at or 'unknown'}`  ",
        "**Methodology (Primary — Subbench-Calibrated):** This is the primary real-world "
        "throughput projection. It does **not** depend on SWE-bench task difficulty; rather, "
        "Subbench runs measure the **actual end-to-end quota usage in `agy`/`codex` multi-turn "
        "agent sessions** (including real harness/multi-turn overhead and cache behavior) "
        "relative to API list cost (`Implied Pool $/mo = api_cost_usd / total_burn_frac × "
        "weeks_per_month`), then divide by AA's Intelligence Index v5 `cost_per_task` "
        "(`aa_ii_cost_per_task`). Burn is measured as net contiguous burn across valid "
        "attempts (netting inter-attempt backend recredits), using `5h / ratio` (`3.50` for "
        "`gemini-5h`, `2.00` for `3p-5h`) when a `5h` bucket is present. Numerical 95% CIs "
        "(leave-one-out Student-t jackknife for continuous buckets; exact `±1 pp` quantization "
        "bounds for `codex-30d`) are reported below; visual error bars are omitted from the "
        "SVG by default because old Cycles 4–10 pre-date post-attempt settling. For the "
        "alternative **theoretical token-only** projection (synthetic 3-prompt quota weights "
        "× AA per-task tokens, excluding `agy` multi-turn harness overhead), see "
        "`aa_token_weighted_projections.md`.",
        "",
        "**Caveats:** `agy` does not report `cache_write_tokens` separately from uncached "
        "input (a ~10–13% upper-bound shift on Anthropic models where `cache_write = 1.25×` "
        "input); weekly projections (`× 4.33`) assume full weekly quota utilization without "
        "5h-cap waste; `codex-30d` uses 1% integer quota increments.",
        "",
        "## 1. Empirically Measured Subbench Configs",
        "",
        "| Cycle | Date | Config | Pool | SWE Resolved | AA Match | AA Intel | "
        "API $/1M (In/Out) | Run API $ | Burn (pp) | Implied Pool $/mo (95% CI) | "
        "AA Tasks/mo (95% CI) |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for s in summaries:
        date_s = (s.cycle_started_at or "")[:10] or "N/A"
        intel_s = f"{s.aa_intelligence:.1f}" if s.aa_intelligence is not None else "N/A"
        price_s = (
            f"${s.aa_price_1m_in:.2f}/${s.aa_price_1m_out:.2f}"
            if (s.aa_price_1m_in is not None and s.aa_price_1m_out is not None)
            else "N/A"
        )
        cost_s = f"${s.api_cost_usd:.3f}" if s.api_cost_usd is not None else "N/A"

        def _fmt_usd(v: float) -> str:
            return f"${v:.2f}" if v < 10.0 else f"${v:.0f}"

        if s.implied_pool_usd_per_month is not None:
            pool_mo_s = _fmt_usd(s.implied_pool_usd_per_month)
            if s.implied_pool_ci_low is not None and s.implied_pool_ci_high is not None:
                pool_mo_s += (
                    f" ({_fmt_usd(s.implied_pool_ci_low)}–{_fmt_usd(s.implied_pool_ci_high)})"
                )
        else:
            pool_mo_s = "N/A"
        if s.tasks_per_month_attempted is not None:
            t_att = f"{s.tasks_per_month_attempted:.0f}"
            if s.tasks_mo_ci_low is not None and s.tasks_mo_ci_high is not None:
                t_att += f" ({s.tasks_mo_ci_low:.0f}–{s.tasks_mo_ci_high:.0f})"
        else:
            t_att = "N/A"
        aa_lbl = f"`{s.aa_slug}`" if s.aa_slug else "*unmatched*"
        lines.append(
            f"| {s.cycle_id} | {date_s} | `{s.config_label}` | `{s.weekly_bucket}` | "
            f"{s.resolved_valid}/{s.attempted_valid} | {aa_lbl} | {intel_s} | {price_s} | "
            f"{cost_s} | {s.weekly_burn_pp:.2f} | {pool_mo_s} | {t_att} |"
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


# ---------------------------------------------------------------------------
# Alternative Projection: Token-Weighted AA Projection (Synthetic 3-Prompt Weights)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AATaskTokenBreakdown:
    """Exact benchmark-weighted per-task cost and token breakdown from AA model pages."""

    slug: str
    non_cache_input_tokens_per_task: float
    cache_write_tokens_per_task: float
    cache_read_tokens_per_task: float
    output_tokens_per_task: float
    reasoning_tokens_per_task: float
    answer_tokens_per_task: float
    cost_non_cache_input: float
    cost_cache_write: float
    cost_cache_read: float
    cost_output: float
    cost_total: float
    price_1m_input: float
    price_1m_cache_write: float
    price_1m_cache_read: float
    price_1m_output: float
    canonical_input_tokens: int | None = None
    canonical_output_tokens: int | None = None


def parse_aa_model_page_token_breakdowns(
    html_text: str,
    aa_models_by_slug: dict[str, AAModel] | None = None,
) -> dict[str, AATaskTokenBreakdown]:
    """Extract per-task token/cost breakdowns embedded in an AA model page HTML."""
    unescaped = html_text.replace('\\"', '"')
    decoder = json.JSONDecoder()
    found: dict[str, AATaskTokenBreakdown] = {}

    for m in re.finditer(r'"intelligenceIndexCostPerTask":\{"cost":\{', unescaped):
        pos = m.start()
        # Walk back up to 6,000 chars to locate the enclosing model object's `"id":` or `"slug":`
        window_start = max(0, pos - 6000)
        window = unescaped[window_start:pos]
        # Model records carry `"slug":"...","name":"...","shortName":"..."` before pricing
        # (unlike nested `"modelFamily":{"slug":"...","name":"..."}` objects).
        slug_matches = list(
            re.finditer(r'"slug":"([a-z0-9-]+)","name":"[^"]+","shortName":"', window)
        )
        if not slug_matches:
            slug_matches = list(re.finditer(r'"slug":"([a-z0-9-]+)","name":"', window))
        if not slug_matches:
            continue
        slug = slug_matches[-1].group(1)

        # Decode the `cost` dict directly
        cost_start = unescaped.find("{", m.start() + len('"intelligenceIndexCostPerTask":{"cost":'))
        if cost_start < 0:
            continue
        try:
            cost_obj, cost_end = decoder.raw_decode(unescaped, cost_start)
        except json.JSONDecodeError:
            continue
        if not isinstance(cost_obj, dict) or cost_obj.get("total") is None:
            continue

        # Parse pricing fields from the window immediately before `intelligenceIndexCostPerTask`
        seg_before = unescaped[window_start + slug_matches[-1].start() : pos]
        seg_after = unescaped[cost_end : min(len(unescaped), cost_end + 1500)]

        def _extract_num(pattern: str, text: str) -> float | None:
            mm = re.search(pattern, text)
            if not mm:
                return None
            return _as_float(mm.group(1))

        aa_m = (aa_models_by_slug or {}).get(slug)
        p_in = _extract_num(r'"price1mInputTokens":([0-9.eE+-]+)', seg_before) or (
            aa_m.price_1m_input if aa_m else None
        )
        p_out = _extract_num(r'"price1mOutputTokens":([0-9.eE+-]+)', seg_before) or (
            aa_m.price_1m_output if aa_m else None
        )
        p_cch = _extract_num(r'"cacheHitPrice":([0-9.eE+-]+)', seg_before)
        if p_cch is None and aa_m is not None:
            p_cch = aa_m.price_1m_cache_read
        if p_cch is None and p_in is not None:
            p_cch = p_in * 0.10
        p_cw = (
            aa_m.price_1m_cache_write
            if (aa_m is not None and aa_m.price_1m_cache_write is not None)
            else p_in
        )
        if not (p_in and p_out and p_cch and p_cw):
            continue

        c_tot = float(cost_obj.get("total") or 0.0)
        c_nci = float(cost_obj.get("nonCacheInput") or 0.0)
        c_cw = float(cost_obj.get("cacheWrite") or 0.0)
        c_cr = float(cost_obj.get("cacheRead") or 0.0)
        c_out = float(cost_obj.get("output") or 0.0)
        c_reas = float(cost_obj.get("reasoning") or 0.0)
        c_ans = float(cost_obj.get("answer") or 0.0)

        t_nci = (c_nci / p_in) * 1e6
        t_cw = (c_cw / p_cw) * 1e6
        t_cr = (c_cr / p_cch) * 1e6
        t_out = (c_out / p_out) * 1e6
        t_reas = (c_reas / p_out) * 1e6
        t_ans = (c_ans / p_out) * 1e6

        can_in_m = re.search(r'"canonicalIntelligenceIndexTokenCount":\{"input":(\d+)', seg_after)
        can_out_m = re.search(
            r'"canonicalIntelligenceIndexTokenCount":\{[^}]*"output":(\d+)', seg_after
        )
        can_in = int(can_in_m.group(1)) if can_in_m else None
        can_out = int(can_out_m.group(1)) if can_out_m else None

        found[slug] = AATaskTokenBreakdown(
            slug=slug,
            non_cache_input_tokens_per_task=t_nci,
            cache_write_tokens_per_task=t_cw,
            cache_read_tokens_per_task=t_cr,
            output_tokens_per_task=t_out,
            reasoning_tokens_per_task=t_reas,
            answer_tokens_per_task=t_ans,
            cost_non_cache_input=c_nci,
            cost_cache_write=c_cw,
            cost_cache_read=c_cr,
            cost_output=c_out,
            cost_total=c_tot,
            price_1m_input=p_in,
            price_1m_cache_write=p_cw,
            price_1m_cache_read=p_cch,
            price_1m_output=p_out,
            canonical_input_tokens=can_in,
            canonical_output_tokens=can_out,
        )

    return found


def fetch_aa_token_breakdowns(
    slugs: list[str],
    *,
    aa_models: list[AAModel] | None = None,
    cache_path: Path = DEFAULT_AA_TOKEN_USAGE_PATH,
    timeout_seconds: int = 20,
) -> tuple[str, dict[str, AATaskTokenBreakdown]]:
    """Fetch per-task token/cost breakdowns from AA public model pages and cache locally."""
    by_slug_aa = {m.slug.lower(): m for m in (aa_models or []) if m.slug}
    collected: dict[str, AATaskTokenBreakdown] = {}
    if cache_path.exists():
        try:
            _, existing = load_aa_token_breakdowns(cache_path)
            collected.update(existing)
        except Exception:
            pass

    for slug in slugs:
        s_low = slug.lower()
        if s_low in collected:
            continue
        url = f"https://artificialanalysis.ai/models/{s_low}"
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (SubscriptionBench/0.1)"},
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
                page_html = resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning("Failed to fetch AA model page %s: %s", url, e)
            continue
        parsed = parse_aa_model_page_token_breakdowns(page_html, aa_models_by_slug=by_slug_aa)
        collected.update(parsed)

    fetched_at = datetime.now(UTC).isoformat()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    envelope = {
        "fetched_at": fetched_at,
        "source": "https://artificialanalysis.ai/models/<slug>",
        "models": {
            k: {
                "slug": v.slug,
                "non_cache_input_tokens_per_task": v.non_cache_input_tokens_per_task,
                "cache_write_tokens_per_task": v.cache_write_tokens_per_task,
                "cache_read_tokens_per_task": v.cache_read_tokens_per_task,
                "output_tokens_per_task": v.output_tokens_per_task,
                "reasoning_tokens_per_task": v.reasoning_tokens_per_task,
                "answer_tokens_per_task": v.answer_tokens_per_task,
                "cost_non_cache_input": v.cost_non_cache_input,
                "cost_cache_write": v.cost_cache_write,
                "cost_cache_read": v.cost_cache_read,
                "cost_output": v.cost_output,
                "cost_total": v.cost_total,
                "price_1m_input": v.price_1m_input,
                "price_1m_cache_write": v.price_1m_cache_write,
                "price_1m_cache_read": v.price_1m_cache_read,
                "price_1m_output": v.price_1m_output,
                "canonical_input_tokens": v.canonical_input_tokens,
                "canonical_output_tokens": v.canonical_output_tokens,
            }
            for k, v in sorted(collected.items())
        },
    }
    cache_path.write_text(json.dumps(envelope, indent=2) + "\n", encoding="utf-8")
    return fetched_at, collected


def load_aa_token_breakdowns(
    cache_path: Path = DEFAULT_AA_TOKEN_USAGE_PATH,
) -> tuple[str | None, dict[str, AATaskTokenBreakdown]]:
    """Load cached AA per-task token/cost breakdowns."""
    if not cache_path.exists():
        raise FileNotFoundError(
            f"AA token breakdown cache not found at {cache_path}. Run `subbench aa sync-tokens`."
        )
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    fetched_at = payload.get("fetched_at") if isinstance(payload, dict) else None
    raw_models = payload.get("models", {}) if isinstance(payload, dict) else {}
    out: dict[str, AATaskTokenBreakdown] = {}
    for slug, obj in raw_models.items():
        if not isinstance(obj, dict):
            continue
        out[slug.lower()] = AATaskTokenBreakdown(
            slug=str(obj.get("slug") or slug),
            non_cache_input_tokens_per_task=float(obj["non_cache_input_tokens_per_task"]),
            cache_write_tokens_per_task=float(obj["cache_write_tokens_per_task"]),
            cache_read_tokens_per_task=float(obj["cache_read_tokens_per_task"]),
            output_tokens_per_task=float(obj["output_tokens_per_task"]),
            reasoning_tokens_per_task=float(obj.get("reasoning_tokens_per_task") or 0.0),
            answer_tokens_per_task=float(obj.get("answer_tokens_per_task") or 0.0),
            cost_non_cache_input=float(obj["cost_non_cache_input"]),
            cost_cache_write=float(obj["cost_cache_write"]),
            cost_cache_read=float(obj["cost_cache_read"]),
            cost_output=float(obj["cost_output"]),
            cost_total=float(obj["cost_total"]),
            price_1m_input=float(obj["price_1m_input"]),
            price_1m_cache_write=float(obj["price_1m_cache_write"]),
            price_1m_cache_read=float(obj["price_1m_cache_read"]),
            price_1m_output=float(obj["price_1m_output"]),
            canonical_input_tokens=obj.get("canonical_input_tokens"),
            canonical_output_tokens=obj.get("canonical_output_tokens"),
        )
    return fetched_at, out


@dataclass(frozen=True)
class TokenWeightedProjectionPoint:
    """Alternative theoretical token-only monthly AA task projection for one model variant."""

    family_key: str
    family_label: str
    effort: str
    aa_slug: str
    aa_name: str
    weekly_bucket: str
    calibrated_anchor: bool
    weight_source: str
    w_in_pp_per_1m: float
    w_cache_pp_per_1m: float
    w_out_pp_per_1m: float
    aa_intelligence: float
    uncached_in_tokens_per_task: float
    cache_read_tokens_per_task: float
    output_tokens_per_task: float
    burn_per_task_wk_pp: float
    token_weighted_tasks_per_month: float
    subbench_tasks_per_month: float | None
    harness_ratio_subbench_over_token: float | None


def compute_token_weighted_projections(
    effort_points: list[EffortProjectionPoint],
    token_breakdowns: dict[str, AATaskTokenBreakdown],
    calibration_payload: dict[str, Any],
) -> list[TokenWeightedProjectionPoint]:
    """Compute theoretical token-only monthly AA tasks using synthetic 3-prompt quota weights.

    Prefers the pure 3-prompt synthetic NNLS fit when all 3 probes produced non-zero
    burn and ``w_cache > 0``; falls back to the joint settled fit otherwise (e.g. when a
    tiny ``input_heavy`` probe was masked by a background recredit).
    """
    weights_by_family: dict[tuple[str, str], tuple[str, str, float, float, float]] = {}
    for entry in calibration_payload.get("models", []):
        tgt = entry.get("target") or {}
        model_id = str(tgt.get("model_id") or "")
        effort = tgt.get("effort")
        bucket = str(tgt.get("weekly_bucket") or "")
        if not model_id or not bucket:
            continue
        fam = _base_family_id(model_id, effort)
        syn = entry.get("synthetic_fit")
        jnt = entry.get("joint_settled_fit") or entry.get("joint_fit")
        if (
            isinstance(syn, dict)
            and int(syn.get("n_samples") or 0) >= 3
            and float(syn.get("w_in_pp_per_1m") or 0.0) > 0
            and float(syn.get("w_cache_pp_per_1m") or 0.0) > 0
            and float(syn.get("w_out_pp_per_1m") or 0.0) > 0
        ):
            fit_used = syn
            src_lbl = "synthetic-3p"
        elif isinstance(jnt, dict):
            fit_used = jnt
            src_lbl = "joint-fallback"
        else:
            continue

        anchor_slug = str(entry.get("aa_slug") or "").lower()
        weights_by_family[(fam, bucket)] = (
            anchor_slug,
            src_lbl,
            float(fit_used["w_in_pp_per_1m"]),
            float(fit_used["w_cache_pp_per_1m"]),
            float(fit_used["w_out_pp_per_1m"]),
        )

    out: list[TokenWeightedProjectionPoint] = []
    for p in effort_points:
        if "@" not in p.family_key:
            continue
        fam, bucket = p.family_key.split("@", 1)
        w_info = weights_by_family.get((fam, bucket))
        tb = token_breakdowns.get(p.aa_slug.lower())
        if w_info is None or tb is None:
            continue
        anchor_slug, weight_src, w_in, w_cch, w_out = w_info
        unc_in_t = tb.non_cache_input_tokens_per_task + tb.cache_write_tokens_per_task
        cch_t = tb.cache_read_tokens_per_task
        out_t = tb.output_tokens_per_task

        burn_per_task_pp = (unc_in_t / 1e6) * w_in + (cch_t / 1e6) * w_cch + (out_t / 1e6) * w_out
        if burn_per_task_pp <= 0:
            continue

        wpm = 1.0 if bucket.endswith("-30d") else 4.33
        tok_pmt = (100.0 * wpm) / burn_per_task_pp
        sub_pmt = p.tasks_per_month_attempted if p.tasks_per_month_attempted > 0 else None
        ratio = (sub_pmt / tok_pmt) if (sub_pmt is not None and tok_pmt > 0) else None

        out.append(
            TokenWeightedProjectionPoint(
                family_key=p.family_key,
                family_label=p.family_label,
                effort=p.effort,
                aa_slug=p.aa_slug,
                aa_name=p.aa_name,
                weekly_bucket=bucket,
                calibrated_anchor=(p.aa_slug.lower() == anchor_slug),
                weight_source=weight_src,
                w_in_pp_per_1m=w_in,
                w_cache_pp_per_1m=w_cch,
                w_out_pp_per_1m=w_out,
                aa_intelligence=p.aa_intelligence,
                uncached_in_tokens_per_task=unc_in_t,
                cache_read_tokens_per_task=cch_t,
                output_tokens_per_task=out_t,
                burn_per_task_wk_pp=burn_per_task_pp,
                token_weighted_tasks_per_month=tok_pmt,
                subbench_tasks_per_month=sub_pmt,
                harness_ratio_subbench_over_token=ratio,
            )
        )
    return out


def write_token_weighted_reports(
    tw_points: list[TokenWeightedProjectionPoint],
    *,
    out_dir: Path = Path("artifacts/reports"),
    aa_fetched_at: str | None = None,
    tokens_fetched_at: str | None = None,
) -> dict[str, Path]:
    """Write Markdown, CSV, and SVG for the alternative Token-Weighted AA projection."""
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "aa_token_weighted_projections.csv"
    md_path = out_dir / "aa_token_weighted_projections.md"
    svg_path = out_dir / "aa_token_weighted_vs_tasks_per_month.svg"

    fieldnames = [
        "family_key",
        "family_label",
        "effort",
        "aa_slug",
        "aa_name",
        "weekly_bucket",
        "calibrated_anchor",
        "weight_source",
        "w_in_pp_per_1m",
        "w_cache_pp_per_1m",
        "w_out_pp_per_1m",
        "aa_intelligence",
        "uncached_in_tokens_per_task",
        "cache_read_tokens_per_task",
        "output_tokens_per_task",
        "burn_per_task_wk_pp",
        "token_weighted_tasks_per_month",
        "subbench_tasks_per_month",
        "harness_ratio_subbench_over_token",
    ]
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in tw_points:
            writer.writerow({k: getattr(p, k) for k in fieldnames})

    # Convert to EffortProjectionPoint so the same clean SVG layout renders the alternative chart
    svg_pts = [
        EffortProjectionPoint(
            family_key=p.family_key,
            family_label=p.family_label,
            effort=p.effort,
            aa_slug=p.aa_slug,
            aa_name=p.aa_name,
            weekly_bucket=p.weekly_bucket,
            measured=p.calibrated_anchor,
            source_cycle_id=0,
            source_config_label=p.weight_source,
            aa_intelligence=p.aa_intelligence,
            aa_ii_cost_per_task=None,
            tasks_per_month_attempted=p.token_weighted_tasks_per_month,
            measured_resolve_rate=None,
        )
        for p in tw_points
    ]
    date_note = f"  |  AA snapshot {aa_fetched_at[:10]}" if aa_fetched_at else ""
    svg_path.write_text(
        render_aa_intelligence_vs_tasks_svg(
            svg_pts,
            aa_fetched_at=aa_fetched_at,
            show_ci=False,
            title="AA Intelligence Index vs. Token-Weighted Throughput (Alternative)",
            subtitle=(
                "Theoretical token-only ceiling: synthetic 3-prompt quota weights × AA per-task "
                f"tokens (excludes agy multi-turn overhead){date_note}"
            ),
            x_label="Token-Weighted Monthly AA Intelligence Index Tasks (Log Scale)",
            measured_legend="Calibrated anchor model",
            projected_legend="Sibling effort (AA tokens)",
        ),
        encoding="utf-8",
    )

    lines = [
        "# Token-Weighted Monthly AA Projections (Alternative — Theoretical Token-Only)",
        "",
        f"**AA Snapshot:** `{aa_fetched_at or 'unknown'}`  ",
        f"**AA Token Breakdown Snapshot:** `{tokens_fetched_at or 'unknown'}`  ",
        "",
        "> **Primary vs. Alternative:** The **primary** projection in "
        "[`aa_projections.md`](./aa_projections.md) reflects **actual end-to-end quota usage "
        "in `agy`/`codex` multi-turn agent sessions** (not SWE-bench task difficulty). This "
        "**alternative** projection isolates the **theoretical token-only quota throughput** "
        "by multiplying AA's exact benchmark-weighted per-task tokens "
        "(`nonCacheInput + cacheWrite`, `cacheRead`, `output`) directly by each model's "
        "synthetic 3-prompt calibrated quota weights `(w_in, w_cache, w_out)`.",
        "",
        "## Methodology & Caveats",
        "",
        "1. **Formula:** `burn_per_task_pp = w_in·T_unc_M + w_cache·T_cache_M + w_out·T_out_M`, "
        "`Token-Weighted Tasks/mo = (100 × weeks_per_month) / burn_per_task_pp`. "
        "Every effort level uses its *own* AA per-task token mix rather than scaling by a "
        "single scalar cost ratio.",
        "2. **AA token source:** Free AA API v2 omits per-token-type breakdowns; per-task "
        "`nonCacheInput`, `cacheWrite`, `cacheRead`, and `output` costs are parsed from AA's "
        "public model pages (`data/cache/aa_token_usage.json`) and divided by AA list prices.",
        "3. **Harness overhead gap (`Subbench / Token-Weighted`):** On Gemini 4 Neon & "
        "Hydrogen, real multi-turn `agy` sessions burn ~15–35% more quota than synthetic "
        "3-prompt token weights alone predict; treat Token-Weighted as an upper-bound "
        "provider-meter baseline and Subbench (`aa_projections.md`) as actual `agy` throughput.",
        "4. **Weight fallback & caveats:** Models whose synthetic `input_heavy` probe was "
        "masked by a background recredit (`efficient-flash-medium`, `hydrogen-medium`) fall "
        "back to `joint-fallback` weights. On Anthropic `sonnet-5-5-medium`, the synthetic "
        "3-prompt `w_cache` absorbs Anthropic's unseparated `1.25×` cache-write premium. "
        "`codex-30d` (`gpt-6-luna`) weights remain limited by 1% integer quota quantization.",
        "",
        "![AA Intelligence vs Token-Weighted Tasks Per Month]"
        "(./aa_token_weighted_vs_tasks_per_month.svg)",
        "",
        "| Model | Effort | Pool | Weights | AA Intel | AA Tokens/Task (Unc / Cache / Out) | "
        "Token-Weighted Tasks/mo | Subbench Primary Tasks/mo | Subbench / Token-Weighted |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for p in sorted(
        tw_points,
        key=lambda x: (-x.aa_intelligence, -x.token_weighted_tasks_per_month),
    ):
        tok_s = (
            f"{p.uncached_in_tokens_per_task / 1e3:.0f}k / "
            f"{p.cache_read_tokens_per_task / 1e6:.2f}M / "
            f"{p.output_tokens_per_task / 1e3:.0f}k"
        )
        sub_s = (
            f"{p.subbench_tasks_per_month:.0f}" if p.subbench_tasks_per_month is not None else "N/A"
        )
        rat_s = (
            f"{p.harness_ratio_subbench_over_token:.2f}×"
            if p.harness_ratio_subbench_over_token is not None
            else "N/A"
        )
        lines.append(
            f"| {p.family_label} | {p.effort} | `{p.weekly_bucket}` | `{p.weight_source}` | "
            f"{p.aa_intelligence:.1f} | {tok_s} | **{p.token_weighted_tasks_per_month:.0f}** | "
            f"{sub_s} | {rat_s} |"
        )

    lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"csv": csv_path, "md": md_path, "svg": svg_path}
