#!/usr/bin/env python3
"""
kalshi_data.py — Kalshi Perps REST API data layer.

Public endpoints (no auth needed):
  - GET /margin/markets                      → list all perp markets
  - GET /margin/markets/{ticker}             → single market snapshot
  - GET /margin/markets/{ticker}/candlesticks → OHLC history
  - GET /margin/funding_rates/estimate       → live funding rate
  - GET /margin/funding_rates/history        → historical funding rates

Auth endpoints (require KALSHI_API_KEY env var):
  - GET /margin/portfolio/balance            → account balance
  - GET /margin/portfolio/positions          → open positions
  - POST /margin/orders                      → place order (real money — paper only for now)

Candle periods available: 1 min, 60 min, 1440 min (1 day)
We primarily use 60-min candles for the ADX/BBW signal engine.

All prices are fixed-point dollar strings from Kalshi ("0.5600").
We parse them to float for internal use.
"""

import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

# ─── ENDPOINTS ────────────────────────────────────────────────────────────────

PROD_BASE  = "https://external-api.kalshi.com/trade-api/v2"
DEMO_BASE  = "https://external-api.demo.kalshi.co/trade-api/v2"

# Switch to DEMO_BASE for paper trading without real-money risk
_use_demo  = os.getenv("KALSHI_USE_DEMO", "false").lower() == "true"
BASE_URL   = DEMO_BASE if _use_demo else PROD_BASE

API_KEY    = os.getenv("KALSHI_API_KEY", "")

# Market data is public; only portfolio/order endpoints need auth
_AUTH_HEADERS: dict = {"Authorization": f"Bearer {API_KEY}"} if API_KEY else {}

# Cache markets list for 5 minutes
_markets_cache: dict = {"data": None, "fetched_at": 0.0}
MARKETS_TTL = 300

# Candle period options (minutes)
CANDLE_1M    = 1
CANDLE_60M   = 60
CANDLE_1440M = 1440

# How many 60-min candles to fetch for signal analysis (≥80 for ADX warmup)
CANDLE_LOOKBACK_HOURS = 120   # 5 days of hourly candles


# ─── HELPERS ──────────────────────────────────────────────────────────────────

def _fp(val: Optional[str]) -> float:
    """Parse Kalshi's fixed-point dollar string to float. Returns 0.0 on None/error."""
    if val is None:
        return 0.0
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0


def _get(path: str, params: dict = None, auth: bool = False, timeout: int = 12) -> Optional[dict]:
    """GET request with error handling. Returns parsed JSON or None."""
    url = f"{BASE_URL}{path}"
    headers = {"Accept": "application/json"}
    if auth:
        headers.update(_AUTH_HEADERS)
    try:
        r = requests.get(url, params=params, headers=headers, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        log.warning(f"Kalshi GET {path} → HTTP {r.status_code}: {r.text[:200]}")
        return None
    except Exception as e:
        log.warning(f"Kalshi GET {path} failed: {e}")
        return None


# ─── MARKETS ──────────────────────────────────────────────────────────────────

def get_all_markets(use_cache: bool = True) -> list[dict]:
    """
    Fetch all active Kalshi perp markets.

    Returns list of dicts with keys:
      ticker, title, price, bid, ask, open_interest, volume_24h,
      leverage_estimate, status
    """
    now = time.time()
    if use_cache and _markets_cache["data"] and (now - _markets_cache["fetched_at"]) < MARKETS_TTL:
        return _markets_cache["data"]

    data = _get("/margin/markets", params={"status": "active"})
    if not data:
        return _markets_cache["data"] or []

    markets = []
    for m in data.get("markets", []):
        markets.append({
            "ticker":            m.get("ticker", ""),
            "title":             m.get("title", ""),
            "price":             _fp(m.get("price")),
            "bid":               _fp(m.get("bid")),
            "ask":               _fp(m.get("ask")),
            "open_interest":     _fp(m.get("open_interest")),
            "open_interest_usd": _fp(m.get("open_interest_notional_value_dollars")),
            "volume_24h":        _fp(m.get("volume_24h")),
            "volume_24h_usd":    _fp(m.get("volume_24h_notional_value_dollars")),
            "leverage_estimate": m.get("leverage_estimate"),
            "status":            m.get("status", ""),
            "contract_size":     m.get("contract_size", "1.000000"),
        })

    _markets_cache["data"] = markets
    _markets_cache["fetched_at"] = now
    log.info(f"Kalshi: loaded {len(markets)} active markets")
    return markets


def get_market(ticker: str) -> Optional[dict]:
    """Fetch a single market snapshot."""
    data = _get(f"/margin/markets/{ticker}")
    if not data:
        return None
    m = data.get("market") or data  # API may wrap or return directly
    if not m.get("ticker"):
        m = data  # fallback: response IS the market object
    return {
        "ticker":            m.get("ticker", ticker),
        "title":             m.get("title", ""),
        "price":             _fp(m.get("price")),
        "bid":               _fp(m.get("bid")),
        "ask":               _fp(m.get("ask")),
        "open_interest":     _fp(m.get("open_interest")),
        "open_interest_usd": _fp(m.get("open_interest_notional_value_dollars")),
        "volume_24h":        _fp(m.get("volume_24h")),
        "volume_24h_usd":    _fp(m.get("volume_24h_notional_value_dollars")),
        "leverage_estimate": m.get("leverage_estimate"),
        "status":            m.get("status", ""),
        "settlement_mark_price": _fp(
            (m.get("settlement_mark_price") or {}).get("price")
        ),
    }


# ─── CANDLESTICKS ─────────────────────────────────────────────────────────────

def get_candlesticks(
    ticker: str,
    period_interval: int = CANDLE_60M,
    lookback_hours: int = CANDLE_LOOKBACK_HOURS,
) -> list[dict]:
    """
    Fetch OHLC candlesticks for a market.

    period_interval: 1, 60, or 1440 (minutes)
    lookback_hours:  how many hours of history to request

    Returns list of dicts:
      {end_ts, open, high, low, close, volume, open_interest}
    Sorted oldest-first. close may be None if no trades in that candle.
    """
    end_ts   = int(time.time())
    start_ts = end_ts - lookback_hours * 3600

    data = _get(
        f"/margin/markets/{ticker}/candlesticks",
        params={
            "start_ts":        start_ts,
            "end_ts":          end_ts,
            "period_interval": period_interval,
        },
    )
    if not data:
        return []

    candles = []
    for c in data.get("candlesticks", []):
        price = c.get("price", {})
        open_  = _fp(price.get("open"))
        high   = _fp(price.get("high"))
        low    = _fp(price.get("low"))
        close  = _fp(price.get("close"))
        prev   = _fp(price.get("previous"))

        # Use previous-close as fallback if this candle had no trades
        if close == 0.0 and prev > 0:
            close = prev
        if open_ == 0.0:
            open_ = close
        if high == 0.0:
            high = close
        if low == 0.0:
            low = close

        if close == 0.0:
            continue  # skip fully empty candles

        candles.append({
            "end_ts":        c.get("end_period_ts"),
            "open":          open_,
            "high":          high,
            "low":           low,
            "close":         close,
            "volume":        _fp(c.get("volume")),
            "open_interest": _fp(c.get("open_interest")),
        })

    # Sort oldest-first
    candles.sort(key=lambda x: x["end_ts"] or 0)
    return candles


def candles_to_hlc(candles: list[dict]) -> list[tuple]:
    """Convert candle list to (high, low, close) tuples for the ADX/BBW engine."""
    return [(c["high"], c["low"], c["close"]) for c in candles]


# ─── FUNDING RATES ────────────────────────────────────────────────────────────

def get_funding_rate_estimate(ticker: str) -> Optional[dict]:
    """
    Fetch live funding rate estimate for a market.

    Returns:
      {
        ticker, funding_rate (float, e.g. 0.0012 = 0.12% per 8H),
        mark_price (float),
        next_funding_time (str ISO),
        annualized_pct (float),
        daily_pct (float),
        sentiment (str: "crowded_longs" | "crowded_shorts" | "balanced"),
      }
    or None on error.
    """
    data = _get("/margin/funding_rates/estimate", params={"ticker": ticker})
    if not data:
        return None

    rate = data.get("funding_rate")
    if rate is None:
        return None

    rate = float(rate)
    # Kalshi funds every 8H → 3 payments per day, ~1095 per year
    daily_pct = rate * 3 * 100
    annualized = rate * 1095 * 100

    if rate > 0.001:
        sentiment = "crowded_longs"    # longs pay shorts → bearish lean
    elif rate < -0.001:
        sentiment = "crowded_shorts"   # shorts pay longs → bullish lean
    else:
        sentiment = "balanced"

    return {
        "ticker":            ticker,
        "funding_rate":      rate,
        "mark_price":        _fp(data.get("mark_price")),
        "next_funding_time": data.get("next_funding_time", ""),
        "daily_pct":         round(daily_pct, 4),
        "annualized_pct":    round(annualized, 2),
        "sentiment":         sentiment,
    }


def get_historical_funding_rates(ticker: str, limit: int = 10) -> list[dict]:
    """
    Fetch recent historical funding rate payments for a market.
    Returns list of {rate, ts} dicts, newest first.
    """
    import time as _time
    end_ts   = int(_time.time())
    start_ts = end_ts - 7 * 24 * 3600  # last 7 days

    data = _get(
        "/margin/funding_rates/historical",
        params={"ticker": ticker, "start_ts": start_ts, "end_ts": end_ts},
    )
    if not data:
        return []

    rates = []
    for item in data.get("funding_rates", [])[:limit]:
        rates.append({
            "rate":      float(item.get("funding_rate", 0)),
            "ts":        item.get("funding_time", ""),
            "mark_price": _fp(item.get("mark_price")),
        })
    return rates


# ─── PORTFOLIO (auth required) ────────────────────────────────────────────────

def get_balance() -> Optional[dict]:
    """Fetch account balance. Requires KALSHI_API_KEY."""
    if not API_KEY:
        log.warning("Kalshi: no API key — balance unavailable")
        return None
    data = _get("/margin/portfolio/balance", auth=True)
    if not data:
        return None
    return {
        "cash":               _fp(data.get("cash")),
        "position_value":     _fp(data.get("position_value")),
        "total_balance":      _fp(data.get("total_balance")),
        "maintenance_margin": _fp(data.get("maintenance_margin")),
    }


def get_positions() -> list[dict]:
    """Fetch open perp positions. Requires KALSHI_API_KEY."""
    if not API_KEY:
        return []
    data = _get("/margin/portfolio/positions", auth=True)
    if not data:
        return []
    positions = []
    for p in data.get("market_positions", []):
        positions.append({
            "ticker":       p.get("market_ticker", ""),
            "position":     _fp(p.get("position")),        # contracts (+ long, - short)
            "total_cost":   _fp(p.get("total_traded_cost")),
            "avg_price":    _fp(p.get("fees_paid")),        # placeholder — real avg from fills
        })
    return positions


# ─── MARKET SUMMARY (for research agent) ─────────────────────────────────────

def get_full_market_snapshot(ticker: str) -> Optional[dict]:
    """
    Build a comprehensive data snapshot for one market.
    Used by the research agent as its input.

    Returns dict with: market info, 60H of hourly candles, funding rate.
    """
    market   = get_market(ticker)
    if not market:
        log.warning(f"Kalshi: market {ticker} not found")
        return None

    candles  = get_candlesticks(ticker, period_interval=CANDLE_60M, lookback_hours=120)
    funding  = get_funding_rate_estimate(ticker)
    hist_funding = get_historical_funding_rates(ticker, limit=6)

    # 24h price change from candles
    price_24h_pct = 0.0
    if len(candles) >= 24:
        price_24_ago = candles[-24]["close"]
        price_now    = candles[-1]["close"]
        if price_24_ago > 0:
            price_24h_pct = (price_now - price_24_ago) / price_24_ago * 100

    # OI trend: compare last candle OI to 24h ago
    oi_trend = "unknown"
    if len(candles) >= 24:
        oi_now    = candles[-1]["open_interest"]
        oi_24ago  = candles[-24]["open_interest"]
        if oi_now > oi_24ago * 1.03:
            oi_trend = "rising"
        elif oi_now < oi_24ago * 0.97:
            oi_trend = "falling"
        else:
            oi_trend = "flat"

    return {
        "ticker":           ticker,
        "title":            market["title"],
        "price":            market["price"],
        "bid":              market["bid"],
        "ask":              market["ask"],
        "leverage_estimate": market["leverage_estimate"],
        "price_24h_pct":    round(price_24h_pct, 2),
        "open_interest":    market["open_interest"],
        "open_interest_usd": market["open_interest_usd"],
        "oi_trend":         oi_trend,
        "volume_24h_usd":   market["volume_24h_usd"],
        "candles":          candles,        # raw for signal engine
        "funding":          funding,        # live rate + sentiment
        "hist_funding":     hist_funding,   # last 6 payments
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json as _json

    print("=== Active Kalshi Perp Markets ===")
    markets = get_all_markets()
    for m in markets:
        print(f"  {m['ticker']:30s}  price={m['price']:.4f}  OI_usd=${m['open_interest_usd']:,.0f}")

    if markets:
        ticker = markets[0]["ticker"]
        print(f"\n=== Snapshot: {ticker} ===")
        snap = get_full_market_snapshot(ticker)
        if snap:
            safe = {k: v for k, v in snap.items() if k != "candles"}
            safe["candle_count"] = len(snap["candles"])
            print(_json.dumps(safe, indent=2, default=str))
