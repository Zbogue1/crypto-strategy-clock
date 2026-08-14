#!/usr/bin/env python3
"""
fomo_birdeye.py -- Birdeye API integration for FOMO Golem.

Birdeye provides Solana token analytics that neither DexScreener nor GMGN offer:

1. NEW TOKEN FEED
   Real-time stream of ALL new Solana launches sorted by creation time.
   Catches organic launches before they have a DexScreener marketing profile.
   Used by: fomo_scanner.py new launch radar (augments DexScreener feed).

2. EARLY BUYER DETECTION
   For a token that pumped, find wallets that bought in the first 1-2 hours.
   This is the gold standard for reverse discovery — these wallets had
   conviction BEFORE the crowd validated the token, not after.
   Used by: fomo_gmgn.py reverse_discover_from_winners().

3. TOP TRADERS PER TOKEN
   Wallets ranked by PnL for a specific token.
   Used by: weekly reverse discovery — cross-reference with portfolio winners.

Setup (free tier — 100 req/min):
   1. Go to https://birdeye.so → Sign up → API section → copy key
   2. Add BIRDEYE_API_KEY to Railway Variables tab
   3. Redeploy (Railway picks it up automatically)
"""

import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

BIRDEYE_API_KEY = os.environ.get("BIRDEYE_API_KEY", "").strip()
BIRDEYE_BASE    = "https://public-api.birdeye.so"
CHAIN           = "solana"

# Free tier: 100 req/min — 0.7s delay ≈ 85/min with overhead headroom
_RATE_LIMIT_DELAY = 0.7


def _headers() -> dict:
    return {
        "X-API-KEY": BIRDEYE_API_KEY,
        "x-chain":   CHAIN,
    }


def _get(endpoint: str, params: dict = None, retries: int = 2) -> Optional[dict]:
    """Authenticated GET with retry on transient errors."""
    if not BIRDEYE_API_KEY:
        log.debug("Birdeye: BIRDEYE_API_KEY not set — skipping")
        return None

    url = f"{BIRDEYE_BASE}{endpoint}"
    time.sleep(_RATE_LIMIT_DELAY)

    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params or {}, timeout=15)
            if resp.status_code == 429:
                log.warning("Birdeye: rate limited — waiting 30s")
                time.sleep(30)
                continue
            if resp.status_code == 401:
                log.error("Birdeye: invalid API key — check BIRDEYE_API_KEY in Railway Variables")
                return None
            if resp.status_code == 200:
                body = resp.json()
                if body.get("success"):
                    return body.get("data")
                log.debug(f"Birdeye non-success: {body.get('message', 'unknown')}")
                return None
            log.warning(f"Birdeye: HTTP {resp.status_code} from {endpoint}")
            return None
        except Exception as e:
            log.error(f"Birdeye request error ({endpoint}): {e}")
            if attempt < retries:
                time.sleep(3)
    return None


def is_available() -> bool:
    """Returns True if BIRDEYE_API_KEY is configured."""
    return bool(BIRDEYE_API_KEY)


# ─── 1. NEW TOKEN FEED ────────────────────────────────────────────────────────

def get_new_tokens(limit: int = 50, min_liquidity: float = 5_000) -> list[dict]:
    """
    Pull the most recently created Solana tokens from Birdeye, sorted by
    creation time descending. Returns ALL new launches — not just tokens with
    DexScreener marketing profiles.

    Returns list of normalized dicts with keys expected by fomo_scanner.py.
    """
    data = _get("/defi/v3/token/list", {
        "sort_by":       "creation_time",
        "sort_type":     "desc",
        "offset":        0,
        "limit":         limit,
        "min_liquidity": min_liquidity,
    })
    if not data:
        return []

    tokens = data.get("items") or data.get("tokens") or []
    results = []

    for t in tokens:
        addr = t.get("address", "")
        if not addr:
            continue

        created_ts = t.get("creation_time") or t.get("creationTime")
        age_days   = None
        if created_ts:
            try:
                age_sec  = datetime.now(timezone.utc).timestamp() - float(created_ts)
                age_days = age_sec / 86400
            except Exception:
                pass

        results.append({
            "contract":       addr,
            "symbol":         t.get("symbol", "?"),
            "name":           t.get("name", "?"),
            "token_age_days": age_days,
            "liquidity_usd":  float(t.get("liquidity") or 0),
            "price_usd":      float(t.get("price") or 0),
            "volume_h24":     float(t.get("v24hUSD") or t.get("v24h") or 0),
            "market_cap":     float(t.get("mc") or 0),
            "holder_count":   int(t.get("holder") or 0),
            # Birdeye doesn't give h1 txn data in list — scanner fetches pair data separately
            "buys_h1":        0,
            "sells_h1":       0,
            "volume_h1":      0.0,
            "volume_h6":      0.0,
            "price_change_h1":  0.0,
            "price_change_h6":  0.0,
            "price_change_h24": float(t.get("priceChange24h") or t.get("priceChange24hPercent") or 0),
            "chain":          "solana",
            "source":         "birdeye",
        })

    log.info(f"Birdeye: {len(results)} new tokens fetched")
    return results


# ─── 2. TOKEN OVERVIEW ────────────────────────────────────────────────────────

def get_token_overview(contract: str) -> Optional[dict]:
    """
    Fetch comprehensive token analytics from Birdeye.
    Richer than DexScreener: includes holder count, unique wallet count,
    buy/sell counts.
    """
    data = _get("/defi/token_overview", {"address": contract})
    if not data:
        return None

    return {
        "contract":          contract,
        "symbol":            data.get("symbol", "?"),
        "name":              data.get("name", "?"),
        "price_usd":         float(data.get("price") or 0),
        "liquidity_usd":     float(data.get("liquidity") or 0),
        "volume_h24":        float(data.get("v24hUSD") or data.get("v24h") or 0),
        "market_cap":        float(data.get("mc") or 0),
        "holder_count":      int(data.get("holder") or 0),
        "unique_wallet_24h": int(data.get("uniqueWallet24h") or 0),
        "trade_24h":         int(data.get("trade24h") or 0),
        "buy_24h":           int(data.get("buy24h") or 0),
        "sell_24h":          int(data.get("sell24h") or 0),
        "price_change_h1":   float(data.get("priceChange1hPercent") or data.get("priceChange1h") or 0),
        "price_change_h24":  float(data.get("priceChange24hPercent") or data.get("priceChange24h") or 0),
        "raw":               data,
    }


# ─── 3. EARLY BUYER DETECTION ─────────────────────────────────────────────────

def get_early_buyers(
    contract:        str,
    max_age_minutes: int = 120,
    limit:           int = 100,
) -> list[dict]:
    """
    Find wallets that bought a token within its first {max_age_minutes} minutes.

    Why this beats GMGN current-holder data for reverse discovery:
    - Current holders may have bought AFTER a token pumped (chasing, not leading)
    - Early buyers made a bet before the crowd validated the token
    - A wallet appearing as early buyer across 3+ of your winners is
      a genuinely rare signal — they consistently position before crowds

    Returns list of {wallet, amount_usd, timestamp, minutes_in} sorted earliest first.
    """
    data = _get("/defi/txs/token", {
        "address":   contract,
        "tx_type":   "buy",
        "sort_type": "asc",   # earliest first
        "offset":    0,
        "limit":     limit,
    })
    if not data:
        return []

    txs = (
        data.get("items")
        or data.get("data")
        or (data if isinstance(data, list) else [])
    )
    if not txs:
        return []

    # Determine launch time from the first buy transaction
    first_ts = None
    for tx in txs:
        ts = tx.get("blockUnixTime") or tx.get("blockTime")
        if ts:
            try:
                first_ts = float(ts)
                break
            except (TypeError, ValueError):
                pass

    if first_ts is None:
        # Can't pin launch time — use now minus window as fallback
        first_ts = datetime.now(timezone.utc).timestamp() - (max_age_minutes * 60)

    cutoff_ts    = first_ts + (max_age_minutes * 60)
    early        = []
    seen_wallets: set = set()

    for tx in txs:
        ts = tx.get("blockUnixTime") or tx.get("blockTime")
        if ts is None:
            continue
        try:
            tx_ts = float(ts)
        except (TypeError, ValueError):
            continue

        # Transactions are sorted ascending — past our window, stop
        if tx_ts > cutoff_ts:
            break

        wallet = (
            tx.get("owner")
            or tx.get("source")
            or tx.get("from")
            or ""
        )
        if not wallet or wallet in seen_wallets:
            continue
        seen_wallets.add(wallet)

        amount_usd = float(
            tx.get("volume")
            or tx.get("volumeUSD")
            or tx.get("amount_usd")
            or tx.get("amount")
            or 0
        )

        early.append({
            "wallet":     wallet,
            "amount_usd": amount_usd,
            "timestamp":  datetime.fromtimestamp(tx_ts, tz=timezone.utc).isoformat(),
            "minutes_in": round((tx_ts - first_ts) / 60, 1),
        })

    log.info(
        f"Birdeye: {len(early)} unique early buyers for {contract[:8]}… "
        f"(first {max_age_minutes}min)"
    )
    return early


# ─── 4. TOP TRADERS PER TOKEN ─────────────────────────────────────────────────

def get_top_traders(
    contract:   str,
    time_frame: str = "1W",
    limit:      int = 20,
) -> list[dict]:
    """
    Find wallets with the best PnL on a specific token.

    Run this on your portfolio winners — the top-PnL wallets for tokens you
    profited on positioned correctly, may have been earlier, and are worth
    tracking or adding to the watchlist.

    time_frame: '24h' | '1W' | '1M'
    Returns list of {wallet, pnl, trade_count, volume}
    """
    data = _get(f"/defi/v2/tokens/{contract}/top_traders", {
        "time_frame": time_frame,
        "sort_by":    "pnl",
        "sort_type":  "desc",
        "limit":      limit,
    })
    if not data:
        return []

    traders = (
        data.get("items")
        or data.get("traders")
        or (data if isinstance(data, list) else [])
    )
    results = []
    for t in traders:
        wallet = (
            t.get("address")
            or t.get("wallet")
            or t.get("owner")
            or ""
        )
        if not wallet:
            continue
        results.append({
            "wallet":      wallet,
            "pnl":         float(t.get("pnl") or t.get("realizedPnl") or 0),
            "trade_count": int(t.get("tradeCount") or t.get("trade") or 0),
            "volume":      float(t.get("volume") or t.get("volumeUSD") or 0),
        })

    log.info(f"Birdeye: {len(results)} top traders for {contract[:8]}…")
    return results
