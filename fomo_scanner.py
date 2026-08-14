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
    Start background scanner thread. Polls every 30 minutes.
    callback(signal_dict) is called for each confirmed smart money signal.
    """
    def _loop():
        log.info(f"Active scanner started (DexScreener + GMGN every {SCAN_INTERVAL_SEC//60} min)")
        time.sleep(STARTUP_DELAY_SEC)
        while True:
            try:
                n = run_scan(callback)
                if n:
                    log.info(f"Scanner: emitted {n} signal(s) this cycle")
            except Exception as e:
                log.error(f"Scanner loop error: {e}")
            time.sleep(SCAN_INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-active-scanner")
    t.start()
    return t
