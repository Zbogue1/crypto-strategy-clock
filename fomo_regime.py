#!/usr/bin/env python3
"""
fomo_regime.py — Market regime awareness for the FOMO Golem.

v2 — multi-timeframe ADX + BBW regime detection (ported from the BTC Scalper's
regime engine). The old version classified the entire market off a single 24h
% change — one candle. This version reads actual trend structure:

  Per asset (BTC and SOL), on 4H candles from Kraken's public API:
    ADX(14)      — trend STRENGTH  (Wilder smoothing)
    +DI / -DI    — trend DIRECTION
    BBW(20)      — Bollinger Band Width, tiebreak in the ADX 20-25 gray zone
                   (band expansion above its rolling median = trend confirming)

  Per-asset vote:
    ADX ≥ 25            → trending: +1 if +DI > -DI else -1
    20 ≤ ADX < 25       → gray zone: vote counts only if BBW is expanding
    ADX < 20            → ranging: 0

  Combined regime (BTC vote + SOL vote):
    sum ≥ +2  → BULL     (both assets in confirmed uptrends)
    sum ≤ -1  → BEAR     (any confirmed downtrend — asymmetric on purpose;
                          memecoin beta to a downtrending majors market is brutal)
    else      → NEUTRAL

  Crash guard (fast override — a dump moves faster than 4H ADX):
    BTC 24h ≤ -5%  OR  SOL 24h ≤ -8%  → BEAR regardless of trend votes

Fallback chain: Kraken OHLC → CoinGecko 24h logic (v1 behaviour) → cached
value → NEUTRAL default. The bot never crashes because a data source is down.

Position size modifiers (unchanged interface):
  BULL:    +0% (full FOMO mode)
  NEUTRAL: -5% (slight caution)
  BEAR:    -15% (significant reduction — preserve capital)

Cached for 30 minutes to avoid hammering APIs on every signal.
"""

import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

COINGECKO_URL  = "https://api.coingecko.com/api/v3/simple/price"
KRAKEN_OHLC_URL = "https://api.kraken.com/0/public/OHLC"
CACHE_TTL_SEC  = 1800   # 30 minutes

# ADX / BBW parameters — match the BTC Scalper's validated settings
ADX_PERIOD        = 14
ADX_TREND_MIN     = 25.0   # ≥ this → confirmed trend
ADX_GRAY_MIN      = 20.0   # 20-25 → gray zone, BBW breaks the tie
BBW_PERIOD        = 20
BBW_MEDIAN_WINDOW = 50     # BBW compared against its median over this many candles
CANDLE_INTERVAL   = 240    # 4H candles (Kraken interval is in minutes)
MIN_CANDLES       = 80     # enough for ADX warmup + BBW median window

# Crash guard thresholds (v1 behaviour, kept as a fast override)
CRASH_BTC_24H = -5.0
CRASH_SOL_24H = -8.0

# In-memory cache of the last full regime dict
_cache: dict = {}

# Position size adjustments per regime (unchanged)
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


# ─── DATA FETCH ───────────────────────────────────────────────────────────────

def _fetch_kraken_ohlc(pair: str) -> Optional[list]:
    """
    Fetch 4H OHLC candles from Kraken's public API (no key required).
    Returns list of (high, low, close) float tuples, oldest first, or None.
    """
    try:
        r = requests.get(
            KRAKEN_OHLC_URL,
            params={"pair": pair, "interval": CANDLE_INTERVAL},
            timeout=10,
            headers={"Accept": "application/json"},
        )
        if r.status_code != 200:
            log.warning(f"Regime: Kraken HTTP {r.status_code} for {pair}")
            return None
        data = r.json()
        if data.get("error"):
            log.warning(f"Regime: Kraken error for {pair}: {data['error']}")
            return None
        result = data.get("result", {})
        key = next((k for k in result if k != "last"), None)
        if not key:
            return None
        candles = [(float(c[2]), float(c[3]), float(c[4])) for c in result[key]]
        return candles if len(candles) >= MIN_CANDLES else None
    except Exception as e:
        log.warning(f"Regime: Kraken fetch failed for {pair}: {e}")
        return None


def _fetch_coingecko_24h() -> Optional[dict]:
    """v1 fallback: BTC and SOL 24h % change from CoinGecko free tier."""
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
        log.warning(f"Regime: CoinGecko fetch failed: {e}")
        return None


# ─── INDICATORS (pure python — no numpy dependency) ──────────────────────────

def _adx_di(candles: list, period: int = ADX_PERIOD) -> Optional[tuple]:
    """
    Wilder-smoothed ADX with directional indicators.
    candles: list of (high, low, close), oldest first.
    Returns (adx, plus_di, minus_di) for the latest candle, or None.
    """
    n = len(candles)
    if n < period * 2 + 1:
        return None

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, n):
        hi, lo, _   = candles[i]
        phi, plo, pc = candles[i - 1]
        tr = max(hi - lo, abs(hi - pc), abs(lo - pc))
        up_move   = hi - phi
        down_move = plo - lo
        plus_dm   = up_move if (up_move > down_move and up_move > 0) else 0.0
        minus_dm  = down_move if (down_move > up_move and down_move > 0) else 0.0
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    # Wilder smoothing seeds
    atr      = sum(trs[:period])
    plus_s   = sum(plus_dms[:period])
    minus_s  = sum(minus_dms[:period])

    dxs = []
    plus_di = minus_di = 0.0
    for i in range(period, len(trs)):
        atr     = atr - atr / period + trs[i]
        plus_s  = plus_s - plus_s / period + plus_dms[i]
        minus_s = minus_s - minus_s / period + minus_dms[i]
        if atr <= 0:
            continue
        plus_di  = 100.0 * plus_s / atr
        minus_di = 100.0 * minus_s / atr
        di_sum = plus_di + minus_di
        if di_sum > 0:
            dxs.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dxs) < period:
        return None

    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx, plus_di, minus_di


def _bbw_series(closes: list, period: int = BBW_PERIOD) -> list:
    """Bollinger Band Width series: (upper - lower) / middle, per candle."""
    out = []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        mean = sum(window) / period
        var  = sum((c - mean) ** 2 for c in window) / period
        sd   = var ** 0.5
        out.append((4.0 * sd / mean) if mean > 0 else 0.0)
    return out


def _median(vals: list) -> float:
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


# ─── PER-ASSET TREND VOTE ─────────────────────────────────────────────────────

def _asset_vote(candles: list) -> dict:
    """
    Classify one asset's 4H trend structure.
    Returns {vote: -1|0|+1, adx, plus_di, minus_di, bbw_expanding, label, chg_24h}.
    """
    closes = [c[2] for c in candles]

    # 24h change = last close vs close 6 4H-candles ago
    chg_24h = 0.0
    if len(closes) >= 7 and closes[-7] > 0:
        chg_24h = (closes[-1] - closes[-7]) / closes[-7] * 100

    result = {
        "vote": 0, "adx": None, "plus_di": None, "minus_di": None,
        "bbw_expanding": None, "label": "RANGING", "chg_24h": round(chg_24h, 2),
        "price": closes[-1],
    }

    adx_out = _adx_di(candles)
    if adx_out is None:
        return result
    adx, plus_di, minus_di = adx_out
    result.update({
        "adx":      round(adx, 1),
        "plus_di":  round(plus_di, 1),
        "minus_di": round(minus_di, 1),
    })

    bbw = _bbw_series(closes)
    bbw_expanding = None
    if len(bbw) >= BBW_MEDIAN_WINDOW:
        bbw_expanding = bbw[-1] > _median(bbw[-BBW_MEDIAN_WINDOW:])
        result["bbw_expanding"] = bbw_expanding

    direction = 1 if plus_di > minus_di else -1

    if adx >= ADX_TREND_MIN:
        result["vote"]  = direction
        result["label"] = "UPTREND" if direction > 0 else "DOWNTREND"
    elif adx >= ADX_GRAY_MIN and bbw_expanding:
        # Gray zone + expanding bands → early trend, count the vote
        result["vote"]  = direction
        result["label"] = ("EARLY UPTREND" if direction > 0 else "EARLY DOWNTREND")
    else:
        result["label"] = "RANGING"

    return result


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def get_market_regime() -> dict:
    """
    Return current market regime with context for signal processing.

    Returns (same keys as v1, plus trend detail):
      {
        "regime":       "BULL" | "NEUTRAL" | "BEAR",
        "icon":         str,
        "modifier_pct": float,         # add to position size
        "btc_24h":      float,
        "sol_24h":      float,
        "btc_price":    float,
        "sol_price":    float,
        "summary":      str,           # one-line for Telegram
        "cached":       bool,
        "engine":       "adx_bbw" | "coingecko_24h" | "default",
        "btc_trend":    dict | None,   # per-asset ADX/DI/BBW detail
        "sol_trend":    dict | None,
      }
    """
    global _cache

    now = time.time()
    if _cache and (now - _cache.get("fetched_at", 0)) < CACHE_TTL_SEC:
        return {**_cache, "cached": True}

    regime = None
    btc_trend = sol_trend = None
    btc_24h = sol_24h = 0.0
    btc_price = sol_price = 0.0
    engine = "adx_bbw"

    # ── Primary: Kraken 4H ADX + BBW ─────────────────────────────────────────
    btc_candles = _fetch_kraken_ohlc("XBTUSD")
    sol_candles = _fetch_kraken_ohlc("SOLUSD")

    if btc_candles and sol_candles:
        btc_trend = _asset_vote(btc_candles)
        sol_trend = _asset_vote(sol_candles)
        btc_24h, sol_24h   = btc_trend["chg_24h"], sol_trend["chg_24h"]
        btc_price, sol_price = btc_trend["price"], sol_trend["price"]

        vote_sum = btc_trend["vote"] + sol_trend["vote"]
        if vote_sum >= 2:
            regime = "BULL"
        elif vote_sum <= -1:
            regime = "BEAR"
        else:
            regime = "NEUTRAL"

    # ── Fallback: CoinGecko 24h logic (v1) ───────────────────────────────────
    if regime is None:
        engine = "coingecko_24h"
        prices = _fetch_coingecko_24h()
        if prices:
            btc_24h, sol_24h     = prices["btc_24h"], prices["sol_24h"]
            btc_price, sol_price = prices["btc_price"], prices["sol_price"]
            if btc_24h <= CRASH_BTC_24H or sol_24h <= CRASH_SOL_24H:
                regime = "BEAR"
            elif btc_24h >= 3.0 and sol_24h >= 2.0:
                regime = "BULL"
            else:
                regime = "NEUTRAL"

    # ── Fallback: last cached value, then safe default ───────────────────────
    if regime is None:
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
            "engine":       "default",
            "btc_trend":    None,
            "sol_trend":    None,
        }

    # ── Crash guard: a fast dump overrides slow trend structure ──────────────
    if regime != "BEAR" and (btc_24h <= CRASH_BTC_24H or sol_24h <= CRASH_SOL_24H):
        regime = "BEAR"
        crash_note = " (crash guard)"
    else:
        crash_note = ""

    icon     = REGIME_ICONS[regime]
    modifier = REGIME_MODIFIERS[regime]

    if btc_trend and sol_trend:
        detail = (f"BTC {btc_trend['label']} ADX {btc_trend['adx']} / "
                  f"SOL {sol_trend['label']} ADX {sol_trend['adx']}")
    else:
        detail = f"BTC {btc_24h:+.1f}% / SOL {sol_24h:+.1f}% 24h"

    if regime == "BEAR":
        summary = (f"{icon} BEAR market{crash_note} — {detail} "
                   f"| Position sizes reduced {abs(modifier):.0f}%")
    elif regime == "BULL":
        summary = f"{icon} BULL market — {detail} | Full FOMO mode"
    else:
        summary = f"{icon} NEUTRAL — {detail} | Slight caution ({modifier:+.0f}% size)"

    _cache = {
        "regime":       regime,
        "icon":         icon,
        "modifier_pct": modifier,
        "btc_24h":      round(btc_24h, 2),
        "sol_24h":      round(sol_24h, 2),
        "btc_price":    btc_price,
        "sol_price":    sol_price,
        "summary":      summary,
        "engine":       engine,
        "btc_trend":    btc_trend,
        "sol_trend":    sol_trend,
        "fetched_at":   now,
    }

    log.info(f"Regime: {regime} [{engine}] | {detail}")
    return {**_cache, "cached": False}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json as _json
    print(_json.dumps(get_market_regime(), indent=2, default=str))
