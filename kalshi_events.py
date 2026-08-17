#!/usr/bin/env python3
"""
kalshi_events.py — Kalshi EVENT market client (the yes/no prediction markets,
not the perps). Used by kalshi_analyst.py to answer /ask questions.

Public endpoints (no auth needed):
  GET /markets            → paginated list of open markets
  GET /markets/{ticker}   → single market detail
  GET /events/{ticker}    → event detail (groups related markets)
  GET /series/{ticker}    → series metadata

Key market fields:
  yes_bid / yes_ask       → current yes prices in cents (1-99)
  last_price              → last trade price in cents = market-implied probability %
  close_time              → when the market stops trading
  expiration_time         → when the outcome is determined
  volume / open_interest  → activity measures
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Event markets live on the elections API host (covers ALL categories, not just politics)
EVENTS_BASE = os.getenv("KALSHI_EVENTS_BASE", "https://api.elections.kalshi.com/trade-api/v2")

_HEADERS = {"Accept": "application/json"}

MAX_SEARCH_PAGES = 8      # up to ~1600 open markets scanned per search
PAGE_LIMIT       = 200


def _get(path: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    try:
        r = requests.get(f"{EVENTS_BASE}{path}", params=params, headers=_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        log.warning(f"Kalshi events GET {path} → HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Kalshi events GET {path} failed: {e}")
    return None


# ─── SEARCH ───────────────────────────────────────────────────────────────────

_STOPWORDS = {
    "will", "the", "a", "an", "be", "is", "are", "at", "on", "in", "by", "of",
    "to", "for", "than", "above", "below", "over", "under", "before", "after",
    "or", "and", "do", "does", "did", "what", "who", "when", "how", "much",
}


def _tokenize(text: str) -> set:
    words = re.findall(r"[a-z0-9$%.,]+", text.lower())
    toks = set()
    for w in words:
        w = w.strip(".,")
        if w and w not in _STOPWORDS:
            toks.add(w)
            # numeric variants: $66,000 → 66000, 66k
            digits = re.sub(r"[^0-9]", "", w)
            if digits:
                toks.add(digits)
                if len(digits) > 3 and digits.endswith("000"):
                    toks.add(digits[:-3] + "k")
    return toks


def _score_match(question_tokens: set, market: dict) -> float:
    """Overlap score between the question and a market's title/subtitle/ticker."""
    text = " ".join([
        market.get("title", ""),
        market.get("subtitle", ""),
        market.get("yes_sub_title", ""),
        market.get("ticker", ""),
        market.get("event_ticker", ""),
    ])
    market_tokens = _tokenize(text)
    if not question_tokens or not market_tokens:
        return 0.0
    overlap = question_tokens & market_tokens
    return len(overlap) / len(question_tokens)


def search_markets(question: str, max_results: int = 5) -> list[dict]:
    """
    Fuzzy-search open Kalshi event markets matching a free-text question.
    Returns up to max_results parsed market dicts, best match first.
    """
    q_tokens = _tokenize(question)
    if not q_tokens:
        return []

    scored: list[tuple] = []
    cursor = None
    for _ in range(MAX_SEARCH_PAGES):
        params = {"status": "open", "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params=params)
        if not data:
            break
        for m in data.get("markets", []):
            s = _score_match(q_tokens, m)
            if s > 0.2:
                scored.append((s, m))
        cursor = data.get("cursor")
        if not cursor:
            break
        time.sleep(0.15)

    scored.sort(key=lambda x: -x[0])
    return [_parse_market(m, score) for score, m in scored[:max_results]]


def get_market(ticker: str) -> Optional[dict]:
    data = _get(f"/markets/{ticker}")
    if not data:
        return None
    m = data.get("market") or data
    return _parse_market(m)


# ─── PARSING ──────────────────────────────────────────────────────────────────

def _parse_market(m: dict, match_score: float = 1.0) -> dict:
    """Normalize a raw market payload into what the analyst needs."""
    yes_bid  = m.get("yes_bid") or 0
    yes_ask  = m.get("yes_ask") or 0
    last     = m.get("last_price") or 0

    # Market-implied probability (cents = %). Prefer bid/ask mid, fall back to last.
    if yes_bid and yes_ask:
        implied = (yes_bid + yes_ask) / 2
    else:
        implied = last

    close_time = m.get("close_time") or m.get("expiration_time") or ""
    hours_left = None
    if close_time:
        try:
            dt = datetime.fromisoformat(close_time.replace("Z", "+00:00"))
            hours_left = max(0.0, (dt - datetime.now(timezone.utc)).total_seconds() / 3600)
        except Exception:
            pass

    return {
        "ticker":        m.get("ticker", ""),
        "event_ticker":  m.get("event_ticker", ""),
        "title":         m.get("title", ""),
        "subtitle":      m.get("subtitle", "") or m.get("yes_sub_title", ""),
        "category":      m.get("category", ""),
        "yes_bid":       yes_bid,
        "yes_ask":       yes_ask,
        "last_price":    last,
        "implied_prob":  round(implied, 1),        # % chance the market says YES
        "volume":        m.get("volume", 0),
        "volume_24h":    m.get("volume_24h", 0),
        "open_interest": m.get("open_interest", 0),
        "liquidity":     m.get("liquidity", 0),
        "close_time":    close_time,
        "hours_left":    round(hours_left, 1) if hours_left is not None else None,
        "rules":         (m.get("rules_primary", "") or "")[:500],
        "match_score":   round(match_score, 2),
    }


# ─── CRYPTO SPOT HELPERS (for numeric threshold bets) ─────────────────────────

_COINBASE = "https://api.exchange.coinbase.com"

_SYMBOL_MAP = {
    "bitcoin": "BTC", "btc": "BTC",
    "ethereum": "ETH", "eth": "ETH",
    "solana": "SOL", "sol": "SOL",
    "xrp": "XRP", "ripple": "XRP",
    "dogecoin": "DOGE", "doge": "DOGE",
    "litecoin": "LTC", "ltc": "LTC",
    "chainlink": "LINK", "link": "LINK",
    "cardano": "ADA", "ada": "ADA",
}


def detect_crypto_symbol(text: str) -> Optional[str]:
    """Return e.g. 'BTC' if the question mentions a known crypto asset."""
    lower = text.lower()
    for name, sym in _SYMBOL_MAP.items():
        if re.search(rf"\b{name}\b", lower):
            return sym
    return None


def extract_threshold(text: str) -> Optional[float]:
    """Extract a numeric price threshold like $66,000 or 66k from the question."""
    m = re.search(r"\$?\s*([0-9][0-9,]*\.?[0-9]*)\s*([km])?", text.replace(",", ""), re.I)
    candidates = re.findall(r"\$\s*([0-9][0-9,]*\.?[0-9]*)\s*([kKmM])?|([0-9][0-9,]*\.?[0-9]*)\s*([kK])\b", text)
    # Simplest robust approach: find all $X or Xk numbers, take the largest
    nums = []
    for m2 in re.finditer(r"\$?([0-9][0-9,]*(?:\.[0-9]+)?)\s*([kKmM])?", text):
        raw, suffix = m2.group(1), (m2.group(2) or "").lower()
        try:
            val = float(raw.replace(",", ""))
        except ValueError:
            continue
        if suffix == "k":
            val *= 1_000
        elif suffix == "m":
            val *= 1_000_000
        if val >= 0.01:
            nums.append(val)
    if not nums:
        return None
    return max(nums)


def get_spot_and_vol(symbol: str) -> Optional[dict]:
    """
    Fetch current spot price + realized volatility from Coinbase hourly candles.
    Returns {spot, hourly_vol, daily_vol, candles_24h_pct}
    """
    try:
        r = requests.get(
            f"{_COINBASE}/products/{symbol}-USD/candles",
            params={"granularity": 3600},   # 1H, returns 300 candles
            headers={"User-Agent": "kalshi-analyst"},
            timeout=12,
        )
        if r.status_code != 200:
            return None
        candles = r.json()   # [ time, low, high, open, close, volume ] newest first
        if not candles or len(candles) < 48:
            return None

        closes = [c[4] for c in candles][::-1]   # oldest first
        spot = closes[-1]

        # Realized hourly vol from log returns (last 168h = 7 days)
        import math
        rets = []
        window = closes[-168:] if len(closes) >= 168 else closes
        for i in range(1, len(window)):
            if window[i-1] > 0:
                rets.append(math.log(window[i] / window[i-1]))
        if len(rets) < 24:
            return None
        mean = sum(rets) / len(rets)
        var  = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
        hourly_vol = math.sqrt(var)

        pct_24h = 0.0
        if len(closes) >= 25 and closes[-25] > 0:
            pct_24h = (closes[-1] / closes[-25] - 1) * 100

        return {
            "spot":        spot,
            "hourly_vol":  hourly_vol,
            "daily_vol":   hourly_vol * math.sqrt(24),
            "pct_24h":     round(pct_24h, 2),
        }
    except Exception as e:
        log.warning(f"Spot/vol fetch failed for {symbol}: {e}")
        return None


def prob_above(spot: float, threshold: float, hourly_vol: float, hours_left: float) -> float:
    """
    P(price > threshold at expiry) under lognormal drift-free assumption.
    Digital option pricing: N(d2) with mu=0.
    Returns probability as % (0-100).
    """
    import math
    if spot <= 0 or threshold <= 0 or hours_left <= 0 or hourly_vol <= 0:
        return 50.0
    sigma_t = hourly_vol * math.sqrt(hours_left)
    if sigma_t < 1e-9:
        return 100.0 if spot > threshold else 0.0
    d = (math.log(spot / threshold)) / sigma_t - sigma_t / 2

    # Standard normal CDF via erf
    p = 0.5 * (1 + math.erf(d / math.sqrt(2)))
    return round(p * 100, 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    q = " ".join(sys.argv[1:]) or "Will Bitcoin be above $66,000?"
    print(f"Searching: {q}")
    results = search_markets(q)
    for r in results:
        print(f"  [{r['match_score']}] {r['ticker']} — {r['title']} | implied {r['implied_prob']}% | {r['hours_left']}h left")
