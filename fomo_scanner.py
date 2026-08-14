#!/usr/bin/env python3
"""
fomo_scanner.py -- Active smart money scanner for FOMO Golem.

Two independent signal paths — neither requires a Solscan wallet to trigger:

  PATH A — Smart money clustering
            DexScreener trending → pre-filter → GMGN holder check
            Fires when ≥2 GMGN-tagged smart money wallets are holding.
            Cost: 1 GMGN credit per token checked.

  PATH B — Golem momentum signal  (NEW — Task #17)
            DexScreener data only, zero GMGN credits.
            Golem scores each token on buy pressure, volume acceleration,
            and price momentum relative to token age. Score ≥ 60 fires.
            This fires BEFORE smart money shows up in GMGN holder data —
            the Golem is reading the market itself, not following others.

Architecture (credit-efficient):
  STEP 1 — DexScreener trending/boosted tokens  (FREE)
  STEP 2 — Pre-filter  (FREE)
  STEP 3 — Golem momentum score  (FREE — Path B fires here if ≥ 60)
  STEP 4 — GMGN smart money check  (credit, only if Path B didn't fire)
            Minimum 2 smart money wallets required for Path A signal.
  STEP 5 — Feed into research pipeline  (existing fomo_research.py)

Credit budget: ~1-3 GMGN credits per 30-min scan cycle.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

SCAN_INTERVAL_SEC   = 1800    # 30 minutes
STARTUP_DELAY_SEC   = 180     # 3 min after Flask starts
MIN_SMART_MONEY     = 2       # minimum smart money wallets to fire signal
SEEN_CACHE_HOURS    = 6       # don't re-signal same token within 6 hours
MAX_GMGN_PER_SCAN   = 5       # max GMGN holder checks per scan cycle (credit guard)

# Pre-filter thresholds (same as our hard vetos in fomo_research.py)
MIN_LIQUIDITY_USD   = 30_000
MIN_TOKEN_AGE_DAYS  = 1.0
MAX_TOP10_HOLDER    = 0.90

# In-memory cache: contract → last_seen datetime
_seen_cache: dict[str, datetime] = {}
_cache_lock = threading.Lock()


# ─── DEXSCREENER DISCOVERY (FREE) ────────────────────────────────────────────

def _fetch_dexscreener_trending() -> list[dict]:
    """
    Pull trending and boosted Solana tokens from DexScreener.
    Returns list of raw token dicts with address, liquidity, age info.
    Zero GMGN credits used.
    """
    tokens = []
    endpoints = [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ]
    for url in endpoints:
        try:
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                continue
            data = resp.json()
            # Both endpoints return a list at top level
            items = data if isinstance(data, list) else data.get("pairs", [])
            for item in items:
                chain = (item.get("chainId") or item.get("chain", "")).lower()
                if chain not in ("solana", "sol"):
                    continue
                addr = item.get("tokenAddress") or item.get("address", "")
                if addr:
                    tokens.append({"contract": addr, "raw": item})
        except Exception as e:
            log.debug(f"Scanner DexScreener fetch error ({url}): {e}")
    return tokens


def _fetch_pair_data(contract: str) -> Optional[dict]:
    """Get liquidity, age, and holder data for a specific token from DexScreener."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return None
        # Pick most liquid Solana pair
        sol_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
        if not sol_pairs:
            return None
        best = max(sol_pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))

        liquidity   = float((best.get("liquidity") or {}).get("usd", 0) or 0)
        price_usd   = float(best.get("priceUsd", 0) or 0)
        market_cap  = float((best.get("marketCap") or 0))
        created_at  = best.get("pairCreatedAt")   # ms timestamp
        token_age_days = None
        if created_at:
            age_ms = datetime.now(timezone.utc).timestamp() * 1000 - float(created_at)
            token_age_days = age_ms / (1000 * 86400)

        # Momentum data — used by Golem momentum scorer (Path B)
        txns      = best.get("txns") or {}
        volume    = best.get("volume") or {}
        price_chg = best.get("priceChange") or {}

        txns_h1  = txns.get("h1") or {}
        txns_h6  = txns.get("h6") or {}
        txns_h24 = txns.get("h24") or {}

        return {
            "contract":        contract,
            "symbol":          (best.get("baseToken") or {}).get("symbol", "?"),
            "name":            (best.get("baseToken") or {}).get("name", "?"),
            "price_usd":       price_usd,
            "liquidity_usd":   liquidity,
            "market_cap":      market_cap,
            "token_age_days":  token_age_days,
            "pair_address":    best.get("pairAddress", ""),
            "chain":           "solana",
            # Momentum fields
            "buys_h1":         int(txns_h1.get("buys") or 0),
            "sells_h1":        int(txns_h1.get("sells") or 0),
            "buys_h6":         int(txns_h6.get("buys") or 0),
            "sells_h6":        int(txns_h6.get("sells") or 0),
            "buys_h24":        int(txns_h24.get("buys") or 0),
            "sells_h24":       int(txns_h24.get("sells") or 0),
            "volume_h1":       float(volume.get("h1") or 0),
            "volume_h6":       float(volume.get("h6") or 0),
            "volume_h24":      float(volume.get("h24") or 0),
            "price_change_h1":  float(price_chg.get("h1") or 0),
            "price_change_h6":  float(price_chg.get("h6") or 0),
            "price_change_h24": float(price_chg.get("h24") or 0),
        }
    except Exception as e:
        log.debug(f"Scanner pair fetch error ({contract[:8]}): {e}")
        return None


# ─── PRE-FILTER (FREE) ───────────────────────────────────────────────────────

def _passes_prefilter(token: dict) -> tuple[bool, str]:
    """
    Basic quality gates before spending any GMGN credits.
    Returns (passes, reason_if_failed).
    """
    liq = token.get("liquidity_usd", 0) or 0
    if liq < MIN_LIQUIDITY_USD:
        return False, f"liquidity ${liq:,.0f} < ${MIN_LIQUIDITY_USD:,.0f}"

    age = token.get("token_age_days")
    if age is not None and age < MIN_TOKEN_AGE_DAYS:
        return False, f"token only {age:.1f} days old"

    contract = token.get("contract", "")
    with _cache_lock:
        last_seen = _seen_cache.get(contract)
        if last_seen:
            hours_ago = (datetime.now(timezone.utc) - last_seen).total_seconds() / 3600
            if hours_ago < SEEN_CACHE_HOURS:
                return False, f"seen {hours_ago:.1f}h ago (dedup)"

    return True, ""


def _mark_seen(contract: str):
    with _cache_lock:
        _seen_cache[contract] = datetime.now(timezone.utc)
        # Prune old entries
        cutoff = datetime.now(timezone.utc) - timedelta(hours=SEEN_CACHE_HOURS * 2)
        stale  = [k for k, v in _seen_cache.items() if v < cutoff]
        for k in stale:
            del _seen_cache[k]


# ─── GMGN SMART MONEY CHECK (COSTS CREDITS) ──────────────────────────────────

def _check_smart_money(contract: str) -> list[dict]:
    """
    Check GMGN for smart money wallets holding this token.
    Costs 1 GMGN credit. Returns list of smart money holders.
    """
    try:
        from fomo_gmgn import get_smart_money_in_token
        return get_smart_money_in_token(contract)
    except Exception as e:
        log.debug(f"Scanner GMGN check error ({contract[:8]}): {e}")
        return []


# ─── PATH B: GOLEM MOMENTUM SCORER (FREE — no GMGN credits) ─────────────────

GOLEM_SIGNAL_THRESHOLD = 60   # minimum score to fire an independent signal

def _golem_momentum_score(token: dict) -> tuple[int, list[str]]:
    """
    Score a token purely on DexScreener market data — no wallet or smart money
    confirmation required. Returns (score 0-100, list of positive factors).

    Scoring rubric:
      Buy pressure  (h1 buys / total txns h1)          up to 25 pts
      Volume accel  (h1 volume vs h6/6 average)         up to 25 pts
      Price momentum (h1 gain vs h24 — early in move)   up to 20 pts
      Token age     (sweet spot 1-10 days)               up to 15 pts
      Liquidity     (healthy range $50K-$2M)             up to 15 pts
    """
    score   = 0
    factors = []

    # ── Buy pressure ──────────────────────────────────────────────────────────
    buys_h1  = token.get("buys_h1", 0) or 0
    sells_h1 = token.get("sells_h1", 0) or 0
    total_h1 = buys_h1 + sells_h1
    if total_h1 >= 10:   # need meaningful sample
        buy_ratio = buys_h1 / total_h1
        if buy_ratio >= 0.72:
            score += 25
            factors.append(f"Strong buy pressure: {buy_ratio*100:.0f}% buys h1 ({buys_h1}/{total_h1})")
        elif buy_ratio >= 0.62:
            score += 15
            factors.append(f"Buy pressure: {buy_ratio*100:.0f}% buys h1")
        elif buy_ratio >= 0.55:
            score += 8

    # ── Volume acceleration ───────────────────────────────────────────────────
    vol_h1 = token.get("volume_h1", 0) or 0
    vol_h6 = token.get("volume_h6", 0) or 0
    avg_h6_per_hour = vol_h6 / 6 if vol_h6 > 0 else 0
    if avg_h6_per_hour > 0:
        accel = vol_h1 / avg_h6_per_hour
        if accel >= 3.0:
            score += 25
            factors.append(f"Volume surging: {accel:.1f}x recent average (${vol_h1:,.0f}/hr)")
        elif accel >= 2.0:
            score += 18
            factors.append(f"Volume building: {accel:.1f}x recent average")
        elif accel >= 1.5:
            score += 10

    # ── Price momentum (early in the move, not already 5x'd) ─────────────────
    pc_h1  = token.get("price_change_h1", 0) or 0
    pc_h24 = token.get("price_change_h24", 0) or 0
    if pc_h1 >= 8 and pc_h24 < 300:
        score += 20
        factors.append(f"Price moving: +{pc_h1:.0f}% h1, {pc_h24:.0f}% h24 (still early)")
    elif pc_h1 >= 4 and pc_h24 < 200:
        score += 12
        factors.append(f"Price building: +{pc_h1:.0f}% h1")
    elif pc_h1 < 0:
        score -= 10   # selling off — penalise

    # ── Token age sweet spot ──────────────────────────────────────────────────
    age = token.get("token_age_days")
    if age is not None:
        if 1.0 <= age <= 7.0:
            score += 15
            factors.append(f"Age sweet spot: {age:.1f} days old")
        elif 7.0 < age <= 21.0:
            score += 8
        elif age > 60:
            score -= 5   # old token suddenly pumping — more suspicious

    # ── Liquidity health ─────────────────────────────────────────────────────
    liq = token.get("liquidity_usd", 0) or 0
    if 50_000 <= liq <= 2_000_000:
        score += 15
        factors.append(f"Healthy liquidity: ${liq:,.0f}")
    elif 20_000 <= liq < 50_000:
        score += 5
    elif liq > 5_000_000:
        score -= 5   # already very large — less upside

    return max(0, min(score, 100)), factors


def _build_golem_signal(token: dict, score: int, factors: list[str]) -> dict:
    """Build a signal dict for a Golem-generated momentum signal (Path B)."""
    symbol = token.get("symbol", "?")
    return {
        "alias":            f"Golem (momentum score {score}/100)",
        "tier":             "A",
        "chain":            "solana",
        "bankroll_usd":     None,
        "copy_trade":       True,
        "action":           "BUY",
        "token_symbol":     symbol,
        "contract_address": token.get("contract"),
        "confidence":       "high" if score >= 75 else "medium",
        "signal_text":      (
            f"Golem spotted ${symbol} independently — score {score}/100\n"
            + "\n".join(f"• {f}" for f in factors)
        ),
        "source":            "golem_momentum",
        "timestamp":         datetime.now(timezone.utc).isoformat(),
        "original_text":     f"Golem momentum: {score}/100 — {'; '.join(factors[:2])}",
        "golem_score":       score,
        "golem_factors":     factors,
    }


# ─── SIGNAL GENERATION ───────────────────────────────────────────────────────

def _build_signal(token: dict, smart_money: list[dict]) -> dict:
    """
    Build a signal dict compatible with fomo_tracker.process_social_signal().
    Appears to the research pipeline as a scanner signal, not a copy-trade signal.
    """
    sm_count   = len(smart_money)
    sm_tags    = list({tag for h in smart_money for tag in h.get("tags", [])})
    confidence = "high" if sm_count >= 3 else "medium"

    return {
        "alias":            f"GMGN Smart Money ({sm_count} wallets)",
        "tier":             "A",
        "chain":            "solana",
        "bankroll_usd":     None,
        "copy_trade":       True,
        "action":           "BUY",
        "token_symbol":     token.get("symbol"),
        "contract_address": token.get("contract"),
        "confidence":       confidence,
        "signal_text":      (
            f"{sm_count} GMGN smart money wallet(s) accumulating "
            f"{token.get('symbol','?')} | Tags: {', '.join(sm_tags) or 'smart_money'}"
        ),
        "source":           "scanner",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "original_text":    f"GMGN scanner: {sm_count} smart money holders detected",
        "smart_money_count": sm_count,
        "smart_money_tags":  sm_tags,
    }


# ─── NEW PAIRS RADAR (tokens 0–6 hours old) ──────────────────────────────────
# Elite traders watch new launches, not things already on the trending list.
# DexScreener's token-profiles/latest endpoint returns the most recently
# listed tokens — far newer than the trending/boosted feed.

NEW_LAUNCH_MAX_AGE_HOURS  = 6       # only tokens younger than this
NEW_LAUNCH_MIN_LIQUIDITY  = 10_000  # lower bar — new tokens start small
NEW_LAUNCH_MIN_BUYS_H1    = 12      # at least 12 buys in last hour
NEW_LAUNCH_SCORE_THRESHOLD = 55     # slightly lower than momentum threshold


def _fetch_new_pairs_solana() -> list:
    """
    Pull newest Solana token listings from DexScreener profiles endpoint.
    These are tokens that just got DexScreener entries — minutes to hours old.
    """
    try:
        resp = requests.get(
            "https://api.dexscreener.com/token-profiles/latest/v1",
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        if resp.status_code != 200:
            return []
        items = resp.json()
        if not isinstance(items, list):
            items = []
        return [
            {"contract": t.get("tokenAddress", ""), "raw": t}
            for t in items
            if (t.get("chainId") or "").lower() in ("solana", "sol")
            and t.get("tokenAddress")
        ]
    except Exception as e:
        log.debug(f"New pairs fetch error: {e}")
        return []


def _new_launch_score(token: dict) -> tuple[int, list[str]]:
    """
    Specialized scoring for brand-new tokens (0–6 hours old).
    Weights early buy volume and buy ratio higher than the momentum scorer
    because volume acceleration vs. 6h average is meaningless at this age.
    """
    score   = 0
    factors = []

    # Early buy pressure — most important signal for a new launch
    buys_h1  = token.get("buys_h1", 0) or 0
    sells_h1 = token.get("sells_h1", 0) or 0
    total_h1 = buys_h1 + sells_h1
    if total_h1 >= 12:
        buy_ratio = buys_h1 / total_h1
        if buy_ratio >= 0.75:
            score += 30
            factors.append(f"Hot launch: {buy_ratio*100:.0f}% buys ({buys_h1}/{total_h1} txns h1)")
        elif buy_ratio >= 0.65:
            score += 20
            factors.append(f"Strong launch buying: {buy_ratio*100:.0f}% buys h1")
        elif buy_ratio >= 0.55:
            score += 10
    elif total_h1 >= 5:
        score += 5

    # h1 volume in real dollars — filters out bots with 1000 micro-transactions
    vol_h1 = token.get("volume_h1", 0) or 0
    if vol_h1 >= 50_000:
        score += 25
        factors.append(f"Strong launch volume: ${vol_h1:,.0f} in h1")
    elif vol_h1 >= 20_000:
        score += 18
        factors.append(f"Good launch volume: ${vol_h1:,.0f} in h1")
    elif vol_h1 >= 5_000:
        score += 10
        factors.append(f"Early volume: ${vol_h1:,.0f} in h1")

    # Price action — should be climbing, not dumping
    pc_h1 = token.get("price_change_h1", 0) or 0
    if pc_h1 >= 20:
        score += 20
        factors.append(f"Launching hot: +{pc_h1:.0f}% h1")
    elif pc_h1 >= 5:
        score += 12
        factors.append(f"Positive launch: +{pc_h1:.0f}% h1")
    elif pc_h1 < -10:
        score -= 15   # early dump — dev selling or botched launch

    # Liquidity — new token specific ranges
    liq = token.get("liquidity_usd", 0) or 0
    if liq >= 50_000:
        score += 15
        factors.append(f"Good launch liquidity: ${liq:,.0f}")
    elif liq >= 15_000:
        score += 8
    elif liq < 5_000:
        score -= 10

    # Age bonus — sooner we catch it, the better
    age_hours = (token.get("token_age_days") or 99) * 24
    if age_hours <= 1:
        score += 10
        factors.append(f"Brand new: {age_hours:.1f}hr old")
    elif age_hours <= 3:
        score += 5
        factors.append(f"Very new: {age_hours:.1f}hr old")

    return max(0, min(score, 100)), factors


def _build_launch_signal(token: dict, score: int, factors: list[str]) -> dict:
    """Build signal dict for a new launch detection."""
    symbol    = token.get("symbol", "?")
    age_hours = (token.get("token_age_days") or 0) * 24
    return {
        "alias":            f"Golem (new launch, score {score}/100)",
        "tier":             "A",
        "chain":            "solana",
        "bankroll_usd":     None,
        "copy_trade":       True,
        "action":           "BUY",
        "token_symbol":     symbol,
        "contract_address": token.get("contract"),
        "confidence":       "high" if score >= 70 else "medium",
        "signal_text":      (
            f"🚀 New launch detected: ${symbol} ({age_hours:.1f}hr old) — score {score}/100\n"
            + "\n".join(f"• {f}" for f in factors)
        ),
        "source":           "new_launch",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "original_text":    f"New launch radar: {score}/100 — {'; '.join(factors[:2])}",
        "launch_score":     score,
        "launch_factors":   factors,
        "token_age_hours":  age_hours,
    }


def run_new_launch_scan(callback) -> int:
    """
    Scan for brand-new Solana token launches (0–6 hours old).
    Runs every 15 min — much faster than the main 30-min trending scan.
    """
    log.info("New launch radar: scanning for fresh Solana launches")
    signals_emitted = 0

    raw = _fetch_new_pairs_solana()
    log.info(f"New launch radar: {len(raw)} new Solana profiles")

    tried: set = set()
    for item in raw:
        contract = item.get("contract", "")
        if not contract or contract in tried:
            continue
        tried.add(contract)

        # Skip if already signalled recently
        with _cache_lock:
            if contract in _seen_cache:
                continue

        time.sleep(0.75)
        token_data = _fetch_pair_data(contract)
        if not token_data:
            continue

        # Age gate — new pairs only
        age_hours = (token_data.get("token_age_days") or 999) * 24
        if age_hours > NEW_LAUNCH_MAX_AGE_HOURS:
            continue

        liq = token_data.get("liquidity_usd", 0) or 0
        if liq < NEW_LAUNCH_MIN_LIQUIDITY:
            log.debug(f"New launch filtered {token_data.get('symbol','?')}: liq ${liq:,.0f}")
            continue

        buys_h1 = token_data.get("buys_h1", 0) or 0
        if buys_h1 < NEW_LAUNCH_MIN_BUYS_H1:
            log.debug(f"New launch filtered {token_data.get('symbol','?')}: {buys_h1} buys h1")
            continue

        score, factors = _new_launch_score(token_data)
        log.debug(f"New launch {token_data.get('symbol','?')}: score {score}/100")

        if score >= NEW_LAUNCH_SCORE_THRESHOLD:
            log.info(
                f"NEW LAUNCH SIGNAL: {token_data.get('symbol','?')} ({contract[:8]}…) "
                f"— {age_hours:.1f}hr old, score {score}/100"
            )
            signal = _build_launch_signal(token_data, score, factors)
            _mark_seen(contract)
            try:
                callback(signal)
                signals_emitted += 1
            except Exception as e:
                log.error(f"New launch callback error: {e}")

    log.info(f"New launch radar: {signals_emitted} signal(s) emitted")
    return signals_emitted


# ─── MAIN SCAN CYCLE ─────────────────────────────────────────────────────────

def run_scan(callback) -> int:
    """
    Single scan cycle. Returns count of signals emitted.
    callback(signal_dict) is called for each confirmed signal (either path).

    Path A: smart money clustering (GMGN, costs credits)
    Path B: Golem momentum (DexScreener only, free) — fires BEFORE Path A check,
            meaning the Golem can catch tokens before smart money shows up in GMGN.
    """
    log.info("Scanner: starting scan cycle")
    signals_emitted  = 0
    gmgn_checks_used = 0

    # Step 1: Discover trending tokens (free)
    raw_tokens = _fetch_dexscreener_trending()
    log.info(f"Scanner: {len(raw_tokens)} tokens from DexScreener")

    # Deduplicate by contract
    seen_contracts = set()
    unique_tokens  = []
    for t in raw_tokens:
        c = t.get("contract", "")
        if c and c not in seen_contracts:
            seen_contracts.add(c)
            unique_tokens.append(c)

    # Step 2: Fetch pair data + pre-filter
    candidates = []
    for contract in unique_tokens[:30]:   # cap to avoid DexScreener 429
        time.sleep(0.75)
        token_data = _fetch_pair_data(contract)
        if not token_data:
            continue
        passes, reason = _passes_prefilter(token_data)
        if not passes:
            log.debug(f"Scanner filtered {token_data.get('symbol','?')}: {reason}")
            continue
        candidates.append(token_data)

    log.info(f"Scanner: {len(candidates)} passed pre-filter")

    for token in candidates:
        contract = token["contract"]
        symbol   = token.get("symbol", "?")
        fired    = False

        # ── Path B: Golem momentum signal (free, no GMGN credit) ─────────────
        golem_score, factors = _golem_momentum_score(token)
        log.debug(f"Scanner {symbol}: Golem score {golem_score}/100")

        if golem_score >= GOLEM_SIGNAL_THRESHOLD:
            log.info(
                f"Scanner GOLEM SIGNAL: {symbol} ({contract[:8]}…) "
                f"— momentum score {golem_score}/100"
            )
            signal = _build_golem_signal(token, golem_score, factors)
            _mark_seen(contract)
            try:
                callback(signal)
                signals_emitted += 1
                fired = True
            except Exception as e:
                log.error(f"Scanner Golem callback error: {e}")

        # ── Path A: Smart money clustering (costs 1 GMGN credit) ─────────────
        # Only check if Path B didn't fire AND we have credit budget remaining.
        # This way smart money check covers tokens the Golem wasn't confident on.
        if not fired:
            if gmgn_checks_used >= MAX_GMGN_PER_SCAN:
                log.info(f"Scanner: GMGN credit guard hit ({MAX_GMGN_PER_SCAN} checks/cycle)")
                continue

            smart_money = _check_smart_money(contract)
            gmgn_checks_used += 1

            if len(smart_money) < MIN_SMART_MONEY:
                log.debug(f"Scanner {symbol}: {len(smart_money)} smart money — skip")
                _mark_seen(contract)
                continue

            log.info(
                f"Scanner SMART MONEY SIGNAL: {symbol} ({contract[:8]}…) "
                f"— {len(smart_money)} wallets accumulating"
            )
            signal = _build_signal(token, smart_money)
            _mark_seen(contract)
            try:
                callback(signal)
                signals_emitted += 1
            except Exception as e:
                log.error(f"Scanner smart money callback error: {e}")

    log.info(
        f"Scanner: cycle complete — "
        f"{signals_emitted} signal(s) emitted, "
        f"{gmgn_checks_used} GMGN credit(s) used"
    )
    return signals_emitted


# ─── BACKGROUND THREAD ───────────────────────────────────────────────────────

def start_scanner(callback) -> threading.Thread:
    """
    Background thread running three scan modes on staggered cadences:

      Every 15 min  — New launch radar (tokens 0-6 hrs old)
      Every 30 min  — Main scan (momentum scoring + smart money clustering)
      Every 2 hours — Narrative import (ETH/BASE trends → Solana equivalents)

    The 15-min base cycle means new launches are caught fast; the main scan
    runs every other cycle; narrative import runs every 8th cycle.
    """
    CYCLE_SEC       = 15 * 60   # base tick: 15 min
    MAIN_EVERY_N    = 2         # main scan every 2 ticks = 30 min
    NARRATIVE_EVERY_N = 8       # narrative scan every 8 ticks = 2 hours

    def _loop():
        log.info("Active scanner started — 3 modes: new-launch(15m) / main(30m) / narrative(2h)")
        time.sleep(STARTUP_DELAY_SEC)
        cycle = 0
        while True:
            try:
                # Mode 1: New launch radar — every cycle
                run_new_launch_scan(callback)

                # Mode 2: Momentum + smart money — every 30 min
                if cycle % MAIN_EVERY_N == 0:
                    run_scan(callback)

                # Mode 3: Cross-chain narrative import — every 2 hours
                if cycle % NARRATIVE_EVERY_N == 0:
                    try:
                        from fomo_narrative import run_narrative_scan
                        with _cache_lock:
                            already_seen = set(_seen_cache.keys())
                        run_narrative_scan(callback, already_seen)
                    except Exception as e:
                        log.error(f"Narrative scan error: {e}")

                cycle += 1
            except Exception as e:
                log.error(f"Scanner loop error: {e}")
            time.sleep(CYCLE_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-active-scanner")
    t.start()
    return t
