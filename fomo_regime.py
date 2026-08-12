#!/usr/bin/env python3
"""
fomo_regime.py — Market regime awareness for the FOMO Golem.

Checks BTC and SOL price action to determine whether the broader market
is in a bull, neutral, or bear regime. Used to scale position sizes and
add context warnings to signals so the Golem doesn't FOMO full-size into
a bear market.

Regime logic (checked in order):
  BEAR   — BTC 24h change <= -5%  OR  SOL 24h change <= -8%
  BULL   — BTC 24h change >= +3%  AND SOL 24h change >= +2%
  NEUTRAL — everything else

Position size modifiers:
  BULL:    +0% (no change — full FOMO mode)
  NEUTRAL: -5% (slight caution)
  BEAR:    -15% (significant reduction — preserve capital)

Price data from CoinGecko free API (no key required).
Cached for 30 minutes to avoid hammering the API on every signal.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
CACHE_TTL_SEC = 1800   # 30 minutes

# In-memory cache: { "regime": str, "btc_24h": float, "sol_24h": float, "fetched_at": float }
_cache: dict = {}

# Position size adjustments per regime
REGIME_MODIFIERS = {
    "BULL":    0.0,
    "NEUTRAL": -5.0,
    "BEAR":    -15.0,
}

REGIME_ICONS = {
    "BULL":    "\U0001f7e2",   # 🟢
    "NEUTRAL": "\U0001f7e1",   # 🟡
    "BEAR":    "\U0001f534",   # 🔴
}


def _fetch_prices() -> Optional[dict]:
    """Fetch BTC and SOL 24h % change from CoinGecko free tier."""
    try:
        r = requests.get(
            COINGECKO_URL,
            params={
                "ids":            "bitcoin,solana",
                "vs_currencies":  "usd",
                "include_24hr_change": "true",
            },
            timeout=10,
            headers={"Accept": "application/json"},
        )
        if r.status_code == 429:
            log.warning("Regime: CoinGecko rate limited — using cached regime")
            return None
        if r.status_code != 200:
            log.warning(f"Regime: CoinGecko HTTP {r.status_code}")
            return None
        data = r.json()
        return {
            "btc_price": data.get("bitcoin", {}).get("usd", 0),
            "btc_24h":   data.get("bitcoin", {}).get("usd_24h_change", 0) or 0,
            "sol_price": data.get("solana", {}).get("usd", 0),
            "sol_24h":   data.get("solana", {}).get("usd_24h_change", 0) or 0,
        }
    except Exception as e:
        log.warning(f"Regime: price fetch failed: {e}")
        return None


def get_market_regime() -> dict:
    """
    Return current market regime with context for signal processing.

    Returns:
      {
        "regime":       "BULL" | "NEUTRAL" | "BEAR",
        "icon":         str,
        "modifier_pct": float,         # add to position size
        "btc_24h":      float,         # BTC 24h % change
        "sol_24h":      float,         # SOL 24h % change
        "btc_price":    float,
        "sol_price":    float,
        "summary":      str,           # one-line for Telegram
        "cached":       bool,
      }
    """
    global _cache

    # Return cached value if fresh
    now = time.time()
    if _cache and (now - _cache.get("fetched_at", 0)) < CACHE_TTL_SEC:
        return {**_cache, "cached": True}

    prices = _fetch_prices()

    if not prices:
        # Return last known regime or safe NEUTRAL default
        if _cache:
            return {**_cache, "cached": True}
        return {
            "regime":       "NEUTRAL",
            "icon":         REGIME_ICONS["NEUTRAL"],
            "modifier_pct": REGIME_MODIFIERS["NEUTRAL"],
            "btc_24h":      0.0,
            "sol_24h":      0.0,
            "btc_price":    0.0,
            "sol_price":    0.0,
            "summary":      "Market data unavailable — using NEUTRAL default",
            "cached":       False,
        }

    btc_24h = prices["btc_24h"]
    sol_24h = prices["sol_24h"]

    # Determine regime
    if btc_24h <= -5.0 or sol_24h <= -8.0:
        regime = "BEAR"
    elif btc_24h >= 3.0 and sol_24h >= 2.0:
        regime = "BULL"
    else:
        regime = "NEUTRAL"

    icon     = REGIME_ICONS[regime]
    modifier = REGIME_MODIFIERS[regime]

    if regime == "BEAR":
        summary = (f"{icon} BEAR market — BTC {btc_24h:+.1f}% / SOL {sol_24h:+.1f}% 24h "
                   f"| Position sizes reduced {abs(modifier):.0f}%")
    elif regime == "BULL":
        summary = (f"{icon} BULL market — BTC {btc_24h:+.1f}% / SOL {sol_24h:+.1f}% 24h "
                   f"| Full FOMO mode")
    else:
        summary = (f"{icon} NEUTRAL — BTC {btc_24h:+.1f}% / SOL {sol_24h:+.1f}% 24h "
                   f"| Slight caution ({modifier:+.0f}% size)")

    _cache = {
        "regime":       regime,
        "icon":         icon,
        "modifier_pct": modifier,
        "btc_24h":      round(btc_24h, 2),
        "sol_24h":      round(sol_24h, 2),
        "btc_price":    prices["btc_price"],
        "sol_price":    prices["sol_price"],
        "summary":      summary,
        "fetched_at":   now,
    }

    log.info(f"Regime: {regime} | BTC {btc_24h:+.1f}% | SOL {sol_24h:+.1f}%")
    return {**_cache, "cached": False}
