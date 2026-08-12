#!/usr/bin/env python3
"""
fomo_confidence.py — Golem learns when NOT to trade.

Reads completed trade postmortems and builds a pattern-based confidence score
for every new incoming signal. When enough data exists, suppresses low-confidence
setups rather than letting the human filter them all manually.

───────────────────────────────────────────────────────────────────────────────
DATA GATES (trades with postmortem_done=True in fomo_portfolio.json)
───────────────────────────────────────────────────────────────────────────────
  < 5  completed trades  → BOOTSTRAP:  no suppression, score is informational
  5–19 completed trades  → LEARNING:   suppress only verified failure patterns
                                       (3+ identical setups, 0% win rate)
  ≥ 20 completed trades  → CALIBRATED: full gate — suppress if score < 40

───────────────────────────────────────────────────────────────────────────────
SCORING (0–100, base 50)
───────────────────────────────────────────────────────────────────────────────
  Pattern win rate on similar historical setups  — up to ±30 pts
  Our observed win rate with this wallet          — up to ±15 pts
  Catalyst score from fomo_research               — up to ±10 pts
  Market regime (BULL / BEAR / SIDEWAYS)          — ±5 pts

───────────────────────────────────────────────────────────────────────────────
POSITION MULTIPLIER (always applied, any mode)
───────────────────────────────────────────────────────────────────────────────
  Score 80–100 → 1.00x (full position size)
  Score 60–79  → 0.85x
  Score 40–59  → 0.70x
  Score < 40   → 0.50x (suppressed entirely in CALIBRATED mode)

───────────────────────────────────────────────────────────────────────────────
PATTERN FEATURES used for historical matching
───────────────────────────────────────────────────────────────────────────────
  catalyst_band  : low (0–4) | medium (5–7) | high (8–10)
  mc_band        : micro (<$1M) | small ($1–10M) | mid ($10M+)
  age_band       : new (<3d)   | young (3–7d)    | established (>7d)
  liquidity_ok   : bool — True if liquidity ≥ $100K
"""

import base64
import json
import logging
import os
import threading
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

SUPPRESS_THRESHOLD         = 40   # score below this → suppress in CALIBRATED
BOOTSTRAP_THRESHOLD        = 5    # < this → BOOTSTRAP mode
LEARNING_THRESHOLD         = 20   # < this → LEARNING mode
PATTERN_SUPPRESS_MIN       = 3    # min examples to trigger LEARNING suppression
LIQUIDITY_FLOOR            = 100_000   # $100K

# GitHub data branch
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO        = "Zbogue1/crypto-strategy-clock"
GITHUB_DATA_BRANCH = "data"

PORTFOLIO_FILE   = "fomo_portfolio.json"
PERFORMANCE_FILE = "fomo_wallet_performance.json"

# ─── CACHE ────────────────────────────────────────────────────────────────────
# Pull data from GitHub at most once per 5 minutes to keep signal latency low.

_CACHE_TTL     = 300   # seconds
_cache_lock    = threading.Lock()
_cache: dict   = {}    # filename → {"data": ..., "ts": float}


def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _pull_json(filename: str) -> dict:
    """Return parsed JSON from GitHub data branch, with 5-minute in-process cache.
    Falls back to local filesystem if GitHub is unavailable or token missing."""
    with _cache_lock:
        entry = _cache.get(filename)
        if entry and (time.time() - entry["ts"]) < _CACHE_TTL:
            return entry["data"]

    data = {}

    if GITHUB_TOKEN:
        try:
            r = requests.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/contents/{filename}?ref={GITHUB_DATA_BRANCH}",
                headers=_gh_headers(), timeout=8,
            )
            if r.status_code == 200:
                content = base64.b64decode(r.json()["content"]).decode("utf-8")
                data = json.loads(content)
        except Exception as e:
            log.debug(f"Confidence: GitHub pull failed for {filename}: {e}")

    if not data:
        try:
            if os.path.exists(filename):
                with open(filename) as f:
                    data = json.load(f)
        except Exception:
            pass

    with _cache_lock:
        _cache[filename] = {"data": data, "ts": time.time()}

    return data


# ─── FEATURE EXTRACTION ───────────────────────────────────────────────────────

def _catalyst_band(score) -> str:
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "unknown"
    if s >= 8:
        return "high"
    if s >= 5:
        return "medium"
    return "low"


def _mc_band(mc) -> str:
    try:
        m = float(mc)
    except (TypeError, ValueError):
        return "unknown"
    if m <= 0:
        return "unknown"
    if m < 1_000_000:
        return "micro"
    if m < 10_000_000:
        return "small"
    return "mid"


def _age_band(age_days) -> str:
    try:
        a = float(age_days)
    except (TypeError, ValueError):
        return "unknown"
    if a < 0:
        return "unknown"
    if a < 3:
        return "new"
    if a < 7:
        return "young"
    return "established"


def _extract_features(d: dict) -> dict:
    """Normalize a signal or trade record into comparable feature bands."""
    return {
        "catalyst_band": _catalyst_band(d.get("catalyst_score")),
        "mc_band":       _mc_band(d.get("market_cap")),
        "age_band":      _age_band(d.get("token_age_days")),
        "liquidity_ok":  (float(d.get("liquidity_usd") or 0)) >= LIQUIDITY_FLOOR,
    }


def _features_match(a: dict, b: dict, strict: bool = False) -> bool:
    """
    True if feature dicts describe a similar setup.
    strict=True: all 4 features must match (for LEARNING failure detection).
    strict=False: 3 of 4 must match (for CALIBRATED pattern scoring).
    'unknown' values are excluded from match counting — don't penalise missing data.
    """
    fields = ["catalyst_band", "mc_band", "age_band", "liquidity_ok"]
    known_fields = [f for f in fields
                    if a.get(f) != "unknown" and b.get(f) != "unknown"]
    if not known_fields:
        return False
    matches = sum(1 for f in known_fields if a.get(f) == b.get(f))
    required = len(known_fields) if strict else max(1, len(known_fields) - 1)
    return matches >= required


# ─── HISTORICAL ANALYSIS ──────────────────────────────────────────────────────

def _completed_trades(portfolio: dict) -> list:
    return [
        t for t in portfolio.get("trade_history", [])
        if t.get("postmortem_done")
    ]


def _pattern_win_rate(signal_features: dict, trades: list) -> Optional[tuple]:
    """
    Return (win_rate, sample_size) for historical trades that match this signal
    loosely (3-of-4 features). Returns None if no matches found.
    """
    matching = [
        t for t in trades
        if _features_match(signal_features, _extract_features(t), strict=False)
    ]
    if not matching:
        return None
    wins = sum(1 for t in matching if (t.get("profit_pct") or 0) > 0)
    return wins / len(matching), len(matching)


def _failure_pattern_exists(signal_features: dict, trades: list) -> Optional[int]:
    """
    Return the sample count if this exact setup (4-of-4 match) has
    PATTERN_SUPPRESS_MIN or more trades and zero wins. None otherwise.
    Used for LEARNING mode suppression.
    """
    matching = [
        t for t in trades
        if _features_match(signal_features, _extract_features(t), strict=True)
    ]
    if len(matching) < PATTERN_SUPPRESS_MIN:
        return None
    wins = sum(1 for t in matching if (t.get("profit_pct") or 0) > 0)
    return len(matching) if wins == 0 else None


# ─── MAIN SCORER ──────────────────────────────────────────────────────────────

def get_confidence(signal: dict) -> dict:
    """
    Compute a confidence score and suppression recommendation for a new signal.

    Input keys (all optional — missing fields treated as unknown):
        alias           str    wallet alias (to look up our observed performance)
        catalyst_score  float  from fomo_research verdict.final_score (0–10)
        market_cap      float  USD market cap at entry
        liquidity_usd   float  USD liquidity at entry
        token_age_days  float  token age in days
        volume_spike_pct float % volume spike (informational, not used in score yet)
        regime          str    "BULL" | "BEAR" | "SIDEWAYS" | "UNKNOWN"

    Returns dict:
        score               int (0–100)
        mode                str ("BOOTSTRAP" | "LEARNING" | "CALIBRATED")
        suppress            bool
        position_multiplier float (0.50 – 1.00)
        suppress_reason     str | None
        reasoning           str  (human-readable one-liner for Telegram)
        factors             dict (all intermediate values for debugging)
    """
    # ── Load data (cached) ────────────────────────────────────────────────────
    portfolio   = _pull_json(PORTFOLIO_FILE)
    performance = _pull_json(PERFORMANCE_FILE)

    trades  = _completed_trades(portfolio)
    n_pm    = len(trades)

    # ── Mode ─────────────────────────────────────────────────────────────────
    if n_pm < BOOTSTRAP_THRESHOLD:
        mode = "BOOTSTRAP"
    elif n_pm < LEARNING_THRESHOLD:
        mode = "LEARNING"
    else:
        mode = "CALIBRATED"

    # ── Signal features ───────────────────────────────────────────────────────
    signal_features = _extract_features(signal)
    alias           = (signal.get("alias") or "").strip()
    catalyst_score  = float(signal.get("catalyst_score") or 0)
    regime          = (signal.get("regime") or "UNKNOWN").upper()

    factors = {
        "mode":            mode,
        "n_postmortems":   n_pm,
        "signal_features": signal_features,
        "catalyst_score":  catalyst_score,
        "regime":          regime,
    }

    # ── Base score ────────────────────────────────────────────────────────────
    score = 50

    # ── Pattern win rate (±30) ────────────────────────────────────────────────
    pattern_result = _pattern_win_rate(signal_features, trades)
    if pattern_result:
        pattern_wr, pattern_n = pattern_result
        # 0% WR → −30, 50% WR → 0, 100% WR → +30
        score += round((pattern_wr - 0.5) * 60)
        factors["pattern_wr"] = round(pattern_wr, 3)
        factors["pattern_n"]  = pattern_n
    else:
        factors["pattern_wr"] = None
        factors["pattern_n"]  = 0

    # ── Wallet observed performance (±15) ─────────────────────────────────────
    # Uses the rolling 10-trade win rate recorded by fomo_wallet_stats.py
    wallet_perf = performance.get(alias, {})
    wallet_wr   = wallet_perf.get("win_rate")      # float 0-1, rolling 10-trade
    wallet_n    = int(wallet_perf.get("trades_followed") or 0)

    if wallet_wr is not None and wallet_n >= 3:
        # 0% → −15, 50% → 0, 100% → +15
        score += round((wallet_wr - 0.5) * 30)
        factors["wallet_wr"] = round(wallet_wr, 3)
        factors["wallet_n"]  = wallet_n
    else:
        factors["wallet_wr"] = None
        factors["wallet_n"]  = wallet_n

    # ── Catalyst score (±10) ──────────────────────────────────────────────────
    # Research already gates on catalyst quality; this adds a smaller weight to
    # the confidence score so high-quality signals get a full-size position.
    if catalyst_score >= 8:
        cat_adj = 10
    elif catalyst_score >= 6:
        cat_adj = 5
    elif catalyst_score >= 4:
        cat_adj = 0
    else:
        cat_adj = -10
    score += cat_adj
    factors["catalyst_adj"] = cat_adj

    # ── Regime (±5) ──────────────────────────────────────────────────────────
    if regime == "BULL":
        regime_adj = 5
    elif regime == "BEAR":
        regime_adj = -5
    else:
        regime_adj = 0
    score += regime_adj
    factors["regime_adj"] = regime_adj

    # Clamp to 0–100
    score = max(0, min(100, score))
    factors["final_score"] = score

    # ── Position multiplier ───────────────────────────────────────────────────
    if score >= 80:
        position_multiplier = 1.00
    elif score >= 60:
        position_multiplier = 0.85
    elif score >= 40:
        position_multiplier = 0.70
    else:
        position_multiplier = 0.50
    factors["position_multiplier"] = position_multiplier

    # ── Suppression decision ──────────────────────────────────────────────────
    suppress        = False
    suppress_reason = None

    if mode == "BOOTSTRAP":
        pass  # never suppress — not enough data yet

    elif mode == "LEARNING":
        # Only suppress if this exact setup has PATTERN_SUPPRESS_MIN+ all-loss examples
        failure_n = _failure_pattern_exists(signal_features, trades)
        if failure_n is not None:
            suppress = True
            suppress_reason = (
                f"Verified failure pattern: {failure_n} identical setups, "
                f"0% win rate over {n_pm} total trades"
            )
            factors["failure_pattern_n"] = failure_n

    else:  # CALIBRATED
        if score < SUPPRESS_THRESHOLD:
            suppress = True
            suppress_reason = (
                f"Confidence {score}/100 below suppression threshold "
                f"({SUPPRESS_THRESHOLD}) — {n_pm} postmortems analysed"
            )

    # ── Reasoning string (Telegram-friendly) ─────────────────────────────────
    parts = [f"{mode} ({n_pm} postmortems)"]

    if factors["pattern_wr"] is not None:
        parts.append(
            f"Similar setups: {factors['pattern_wr']*100:.0f}% WR "
            f"({factors['pattern_n']} trades)"
        )
    else:
        parts.append("No matching historical setups yet")

    if factors["wallet_wr"] is not None:
        parts.append(
            f"Wallet observed: {factors['wallet_wr']*100:.0f}% WR "
            f"({factors['wallet_n']} followed)"
        )

    parts.append(f"Catalyst {catalyst_score:.0f}/10 · {regime} regime")

    reasoning = " | ".join(parts)

    return {
        "score":               score,
        "mode":                mode,
        "suppress":            suppress,
        "position_multiplier": position_multiplier,
        "suppress_reason":     suppress_reason,
        "reasoning":           reasoning,
        "factors":             factors,
    }


# ─── TELEGRAM HELPERS ─────────────────────────────────────────────────────────

def format_confidence_line(conf: dict) -> str:
    """
    One-line confidence badge for BUY alert Telegram messages.
    Returns empty string in BOOTSTRAP mode (not enough data to show as meaningful).
    """
    mode  = conf["mode"]
    score = conf["score"]
    mult  = conf["position_multiplier"]

    if mode == "BOOTSTRAP":
        n = conf["factors"]["n_postmortems"]
        return f"\n🌱 Confidence: learning ({n}/{LEARNING_THRESHOLD} postmortems)"

    icon = "🟢" if score >= 70 else "🟡" if score >= 50 else "🔴"
    mult_str = f" · {mult:.0%} size" if mult < 1.0 else ""
    return f"\n🧠 Confidence: {icon} {score}/100{mult_str}"


def format_suppression_telegram(alias: str, symbol: str, conf: dict,
                                 verdict_summary: str = "") -> str:
    """Full Telegram message for a suppressed signal."""
    lines = [
        f"🧠 <b>Signal suppressed — {alias} on ${symbol}</b>",
        f"Confidence: {conf['score']}/100 ({conf['mode']})",
        f"<i>{conf['suppress_reason']}</i>",
        f"Reasoning: {conf['reasoning']}",
    ]
    if verdict_summary:
        lines.append(verdict_summary)
    return "\n".join(lines)
