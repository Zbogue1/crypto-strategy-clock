#!/usr/bin/env python3
"""
stock_data.py — Alpaca market data layer for Stock Golem.

Provides the inputs the 5-Pillar screen needs:
  1. Relative volume  → from bars vs 30-day average
  2. % change today   → from screener / snapshot
  3. News catalyst    → Alpaca news API
  4. Price            → snapshot
  5. Float            → NOT available from Alpaca (see FLOAT DATA below)

TICKER FILTERING — why this matters:
A probe of Alpaca's top gainers returned RFAIU (+369%), USDEW (+102%) and
TMCWW (+85%) with 1-6% bar coverage and total daily volume under 15,000 shares.
Those aren't data failures — they're SPAC units and warrants, structurally
illiquid instruments. Genuine common stock in the same list (HOWL, USDE) came
back at 75-92% coverage on hundreds of thousands of shares.

So we filter instrument type BEFORE judging data quality, otherwise the feed
looks broken when it's actually fine for everything we'd trade.

FLOAT DATA:
Alpaca does not expose shares outstanding or float. Pillar 5 needs it. We fetch
it best-effort from yfinance and cache aggressively — float changes rarely.
When unavailable the pillar is scored as unknown rather than failed, so a
missing data point doesn't silently reject good setups.
"""

import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

API_KEY    = os.getenv("ALPACA_API_KEY", "").strip()
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()

DATA_URL  = "https://data.alpaca.markets"
PAPER_URL = "https://paper-api.alpaca.markets"
FEED      = os.getenv("ALPACA_FEED", "iex")   # free tier = iex


# ─── INSTRUMENT FILTERING ─────────────────────────────────────────────────────

# Suffixes that denote non-common-stock instruments on US exchanges.
# These are illiquid by construction and fail the strategy's volume pillars.
_WARRANT_UNIT_RE = re.compile(r"^[A-Z]{2,4}(W|U|R|WS|UN|RT)$")


def is_tradeable_instrument(symbol: str) -> tuple:
    """
    (ok, reason) — reject warrants, units, rights and preferred shares.

    Ross's strategy trades common stock with 5x relative volume. A warrant with
    11,000 shares of daily volume fails every pillar; screening it out early
    saves API calls and stops the data feed looking broken.
    """
    s = (symbol or "").upper().strip()
    if not s:
        return False, "empty symbol"
    if len(s) > 5:
        return False, "unusually long ticker"
    if "." in s or "-" in s:
        return False, "preferred/class share"
    if _WARRANT_UNIT_RE.match(s):
        return False, "warrant/unit/right"
    return True, ""


# ─── HTTP ─────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
        "accept":              "application/json",
    }


def _get(base: str, path: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    try:
        r = requests.get(f"{base}{path}", headers=_headers(),
                         params=params or {}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        log.warning(f"Alpaca GET {path} → HTTP {r.status_code}: {r.text[:160]}")
    except Exception as e:
        log.warning(f"Alpaca GET {path} failed: {e}")
    return None


def is_configured() -> bool:
    return bool(API_KEY and SECRET_KEY)


# ─── ACCOUNT ──────────────────────────────────────────────────────────────────

def get_account() -> Optional[dict]:
    d = _get(PAPER_URL, "/v2/account")
    if not d:
        return None
    return {
        "status":        d.get("status"),
        "cash":          float(d.get("cash", 0) or 0),
        "equity":        float(d.get("equity", 0) or 0),
        "buying_power":  float(d.get("buying_power", 0) or 0),
        "daytrade_count": int(d.get("daytrade_count", 0) or 0),
        "pattern_day_trader": bool(d.get("pattern_day_trader")),
    }


# ─── MARKET CLOCK ─────────────────────────────────────────────────────────────

def get_clock() -> Optional[dict]:
    d = _get(PAPER_URL, "/v2/clock")
    if not d:
        return None
    return {
        "is_open":    bool(d.get("is_open")),
        "timestamp":  d.get("timestamp", ""),
        "next_open":  d.get("next_open", ""),
        "next_close": d.get("next_close", ""),
    }


# ─── SCREENER (the gap scanner equivalent) ────────────────────────────────────

def get_movers(top: int = 50, min_pct: float = 10.0) -> list:
    """
    Top percentage gainers, filtered to tradeable common stock.

    Pillar 2 requires the stock to already be up ≥10%, so we apply that here
    rather than fetching data for names that can't qualify.
    """
    d = _get(DATA_URL, "/v1beta1/screener/stocks/movers", {"top": top})
    if not d:
        return []

    out, skipped = [], 0
    for m in d.get("gainers", []):
        sym = m.get("symbol", "")
        ok, why = is_tradeable_instrument(sym)
        if not ok:
            skipped += 1
            log.debug(f"Screener: skip {sym} — {why}")
            continue
        pct = float(m.get("percent_change", 0) or 0)
        if pct < min_pct:
            continue
        out.append({
            "symbol":  sym,
            "pct":     round(pct, 2),
            "price":   float(m.get("price", 0) or 0),
            "change":  float(m.get("change", 0) or 0),
        })

    if skipped:
        log.info(f"Screener: filtered {skipped} warrant/unit/preferred ticker(s)")
    log.info(f"Screener: {len(out)} common-stock gainer(s) ≥{min_pct}%")
    return out


# ─── BARS ─────────────────────────────────────────────────────────────────────

def get_bars(symbol: str, timeframe: str = "1Min",
             lookback_days: int = 1, limit: int = 10000) -> list:
    """
    OHLCV bars, oldest first. timeframe: 1Min / 5Min / 1Day.
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    d = _get(DATA_URL, f"/v2/stocks/{symbol}/bars", {
        "timeframe": timeframe,
        "start":     start.strftime("%Y-%m-%d"),
        "end":       end.strftime("%Y-%m-%d"),
        "limit":     limit,
        "feed":      FEED,
        "adjustment": "raw",
    })
    if not d:
        return []
    bars = d.get("bars") or []
    return [{
        "t": b.get("t", ""),
        "o": float(b.get("o", 0) or 0),
        "h": float(b.get("h", 0) or 0),
        "l": float(b.get("l", 0) or 0),
        "c": float(b.get("c", 0) or 0),
        "v": int(b.get("v", 0) or 0),
        "n": int(b.get("n", 0) or 0),      # trade count
        "vw": float(b.get("vw", 0) or 0),  # VWAP for that bar
    } for b in bars]


def get_snapshot(symbol: str) -> Optional[dict]:
    """Latest quote/trade plus daily and previous-daily bars."""
    d = _get(DATA_URL, f"/v2/stocks/{symbol}/snapshot", {"feed": FEED})
    if not d:
        return None
    daily = d.get("dailyBar") or {}
    prev  = d.get("prevDailyBar") or {}
    trade = d.get("latestTrade") or {}

    prev_close = float(prev.get("c", 0) or 0)
    last       = float(trade.get("p", 0) or 0) or float(daily.get("c", 0) or 0)
    pct = ((last / prev_close - 1) * 100) if prev_close else 0.0

    return {
        "symbol":     symbol,
        "price":      last,
        "prev_close": prev_close,
        "pct_change": round(pct, 2),
        "day_open":   float(daily.get("o", 0) or 0),
        "day_high":   float(daily.get("h", 0) or 0),
        "day_low":    float(daily.get("l", 0) or 0),
        "day_volume": int(daily.get("v", 0) or 0),
    }


# ─── RELATIVE VOLUME (Pillar 1) ───────────────────────────────────────────────

def get_relative_volume(symbol: str, days: int = 30) -> Optional[dict]:
    """
    RVOL = today's volume / average daily volume over `days`.

    Ross requires ≥5x. Note we compare full-day-to-date volume against a full
    day average, so early in the session RVOL understates — a stock at 3x by
    10am is often heading well past 5x. The signal engine accounts for this by
    time-weighting; here we report the raw ratio plus the elapsed fraction.
    """
    daily = get_bars(symbol, "1Day", lookback_days=days + 15, limit=200)
    if len(daily) < 5:
        return None

    today = daily[-1]
    hist  = daily[-(days + 1):-1] or daily[:-1]
    if not hist:
        return None

    avg = sum(b["v"] for b in hist) / len(hist)
    if avg <= 0:
        return None

    return {
        "symbol":      symbol,
        "today_vol":   today["v"],
        "avg_vol":     int(avg),
        "rvol":        round(today["v"] / avg, 2),
        "sample_days": len(hist),
    }


# ─── NEWS (Pillar 3) ──────────────────────────────────────────────────────────

def get_news(symbol: str, hours: int = 48, limit: int = 10) -> list:
    """Recent headlines. Pillar 3 wants a catalyst justifying the move."""
    start = datetime.now(timezone.utc) - timedelta(hours=hours)
    d = _get(DATA_URL, "/v1beta1/news", {
        "symbols": symbol,
        "start":   start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit":   limit,
        "sort":    "desc",
    })
    if not d:
        return []
    return [{
        "headline":   n.get("headline", ""),
        "source":     n.get("source", ""),
        "created_at": n.get("created_at", ""),
        "url":        n.get("url", ""),
    } for n in (d.get("news") or [])]


# ─── FLOAT (Pillar 5) ─────────────────────────────────────────────────────────
# Alpaca doesn't provide float. yfinance does, best-effort. Cached hard because
# float is near-static and the lookup is slow/unofficial.

_float_cache: dict = {}
FLOAT_CACHE_HOURS = 24


def get_float(symbol: str) -> Optional[dict]:
    """
    Shares float, in millions. Returns None when unavailable — callers must
    treat that as UNKNOWN, not as a failed pillar.
    """
    now = time.time()
    hit = _float_cache.get(symbol)
    if hit and (now - hit["ts"]) < FLOAT_CACHE_HOURS * 3600:
        return hit["data"]

    try:
        import yfinance as yf
        info = yf.Ticker(symbol).info or {}
        shares_float = info.get("floatShares") or info.get("sharesOutstanding")
        if not shares_float:
            _float_cache[symbol] = {"ts": now, "data": None}
            return None
        data = {
            "symbol":       symbol,
            "float_shares": int(shares_float),
            "float_m":      round(shares_float / 1_000_000, 2),
            "source":       "yfinance",
        }
        _float_cache[symbol] = {"ts": now, "data": data}
        return data
    except Exception as e:
        log.debug(f"Float lookup failed for {symbol}: {e}")
        _float_cache[symbol] = {"ts": now, "data": None}
        return None


# ─── FULL SNAPSHOT FOR THE SCREEN ─────────────────────────────────────────────

def get_full_snapshot(symbol: str) -> Optional[dict]:
    """Everything the 5-Pillar screen needs for one ticker."""
    ok, why = is_tradeable_instrument(symbol)
    if not ok:
        return None

    snap = get_snapshot(symbol)
    if not snap:
        return None

    rvol   = get_relative_volume(symbol)
    news   = get_news(symbol)
    flt    = get_float(symbol)

    # Catalyst QUALITY, not just presence — a dilutive offering is news that
    # moves the stock for the wrong reason. See stock_catalyst.py.
    catalyst = None
    try:
        from stock_catalyst import analyze_catalyst
        catalyst = analyze_catalyst(
            symbol,
            [n["headline"] for n in news],
            snap.get("pct_change", 0.0),
        )
    except Exception as e:
        log.warning(f"Catalyst analysis failed for {symbol}: {e}")

    return {
        **snap,
        "rvol":        rvol["rvol"] if rvol else None,
        "avg_vol":     rvol["avg_vol"] if rvol else None,
        "news_count":  len(news),
        "headlines":   [n["headline"] for n in news[:4]],
        "catalyst":    catalyst,
        "float_m":     flt["float_m"] if flt else None,
        "fetched_at":  datetime.now(timezone.utc).isoformat(),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json as _json

    print("configured:", is_configured())
    clock = get_clock()
    print("market open:", clock and clock["is_open"])

    movers = get_movers(top=20)
    print(f"\n{len(movers)} tradeable gainers:")
    for m in movers[:5]:
        print(f"  {m['symbol']:6s} +{m['pct']:.1f}%  ${m['price']:.2f}")

    if movers:
        s = get_full_snapshot(movers[0]["symbol"])
        print("\nfull snapshot:")
        print(_json.dumps(s, indent=2))
