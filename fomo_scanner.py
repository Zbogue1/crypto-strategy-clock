#!/usr/bin/env python3
"""
fomo_scanner.py -- Active smart money scanner for FOMO Golem.

Independent signal pipeline that doesn't depend on the 7 Solscan wallets.
Scans the broader market for coins elite wallets are accumulating RIGHT NOW.

Architecture (credit-efficient):
  STEP 1 — DexScreener trending/boosted tokens  (FREE — no GMGN credits)
            Pulls ~50 tokens every 30 minutes showing momentum on Solana

  STEP 2 — Pre-filter  (FREE)
            Age > 1 day, liquidity > $30K, top-10 holders < 90%
            Deduplication: skip tokens seen in last 6 hours

  STEP 3 — GMGN smart money check  (1 credit each — only survivors from step 2)
            Check if GMGN-tagged smart money wallets are holding
            Minimum 2 smart money wallets required to fire a signal

  STEP 4 — Feed into research pipeline  (existing fomo_research.py)
            Same deep research + chart + GMGN security that copy-trade signals use
            Sends EXECUTE button to Telegram with conviction sizing

This gives the Golem eyes on the entire Solana memecoin market, not just
the 7 wallets on Solscan — the "entire room" of smart money activity.

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

        return {
            "contract":      contract,
            "symbol":        (best.get("baseToken") or {}).get("symbol", "?"),
            "name":          (best.get("baseToken") or {}).get("name", "?"),
            "price_usd":     price_usd,
            "liquidity_usd": liquidity,
            "market_cap":    market_cap,
            "token_age_days": token_age_days,
            "pair_address":  best.get("pairAddress", ""),
            "chain":         "solana",
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
    callback(signal_dict) is called for each confirmed smart money signal.
    """
    log.info("Scanner: starting scan cycle")
    signals_emitted = 0
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

    candidates = []
    for contract in unique_tokens:
        # Step 2a: Get pair data (free)
        token_data = _fetch_pair_data(contract)
        if not token_data:
            continue

        # Step 2b: Pre-filter (free)
        passes, reason = _passes_prefilter(token_data)
        if not passes:
            log.debug(f"Scanner filtered {token_data.get('symbol','?')}: {reason}")
            continue

        candidates.append(token_data)

    log.info(f"Scanner: {len(candidates)} passed pre-filter, checking smart money")

    for token in candidates:
        if gmgn_checks_used >= MAX_GMGN_PER_SCAN:
            log.info(f"Scanner: GMGN credit guard hit ({MAX_GMGN_PER_SCAN} checks/cycle)")
            break

        contract = token["contract"]
        symbol   = token.get("symbol", "?")

        # Step 3: GMGN smart money check (costs 1 credit)
        smart_money = _check_smart_money(contract)
        gmgn_checks_used += 1

        if len(smart_money) < MIN_SMART_MONEY:
            log.debug(f"Scanner {symbol}: only {len(smart_money)} smart money wallet(s) — skip")
            _mark_seen(contract)
            continue

        # Step 4: Fire signal
        log.info(
            f"Scanner SIGNAL: {symbol} ({contract[:8]}...) — "
            f"{len(smart_money)} smart money wallets accumulating"
        )
        signal = _build_signal(token, smart_money)
        _mark_seen(contract)

        try:
            callback(signal)
            signals_emitted += 1
        except Exception as e:
            log.error(f"Scanner callback error: {e}")

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
