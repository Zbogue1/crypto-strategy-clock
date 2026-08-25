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

PAGE_LIMIT        = 1000   # Kalshi max per page
MAX_SEARCH_PAGES  = 60     # up to ~60k markets — Kalshi has tens of thousands open
SEARCH_TIME_BUDGET = 45    # seconds; stop paging past this even if more remain
MIN_MATCH_SCORE   = 0.12   # F1 threshold — below this the market isn't the one asked about

# Full open-market list is cached so repeated /ask calls don't re-scan Kalshi
_ALL_CACHE: dict = {"data": None, "fetched_at": 0.0}
ALL_MARKETS_TTL = 600      # 10 minutes


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
    "will", "the", "a", "an", "be", "is", "are", "was", "were", "at", "on",
    "in", "by", "of", "to", "for", "than", "above", "below", "over", "under",
    "before", "after", "or", "and", "do", "does", "did", "what", "who", "whos",
    "when", "how", "much", "going", "gonna", "get", "got", "this", "that",
    "it", "its", "there", "their", "have", "has", "had", "can", "could",
    "would", "should", "win", "wins", "beat", "beats", "game", "match",
    "vs", "versus", "against", "next", "yes", "no", "s", "t", "m", "re",
}


def _tokenize(text: str) -> set:
    # Strip apostrophes first so "who's" → "whos" instead of {"who","s"}
    cleaned = re.sub(r"['’]", "", text.lower())
    words = re.findall(r"[a-z0-9$%.,]+", cleaned)
    toks = set()
    for w in words:
        w = w.strip(".,")
        if not w or w in _STOPWORDS:
            continue
        if len(w) < 2:          # single letters are noise
            continue
        toks.add(w)
        # numeric variants: $66,000 → 66000, 66k
        digits = re.sub(r"[^0-9]", "", w)
        if digits:
            toks.add(digits)
            if len(digits) > 3 and digits.endswith("000"):
                toks.add(digits[:-3] + "k")
    return toks


def _distinctive(tokens: set) -> set:
    """Tokens long enough to be meaningful identifiers (names, tickers, numbers)."""
    return {t for t in tokens if len(t) >= 4}


# Multi-leg / parlay / combo markets bundle dozens of unrelated events into one
# ticker. They match almost any question by brute force and are never what the
# user meant. Reject them outright.
_PARLAY_TICKER_PATTERNS = (
    "CROSSCATEGORY", "MVE", "SHARD", "PARLAY", "COMBO", "MULTI",
)
_MAX_TITLE_TOKENS = 40      # real market titles are short; parlays are enormous


def _is_parlay(market: dict) -> bool:
    ticker = (market.get("ticker", "") + market.get("event_ticker", "")).upper()
    if any(p in ticker for p in _PARLAY_TICKER_PATTERNS):
        return True
    title = market.get("title", "") or ""
    # A title listing many comma-separated legs is a parlay
    if title.count(",") >= 6:
        return True
    if len(_tokenize(title)) > _MAX_TITLE_TOKENS:
        return True
    return False


def _is_tradeable(market: dict) -> bool:
    """Reject dead markets — no quote and no activity means nothing to bet on."""
    bid = market.get("yes_bid") or 0
    ask = market.get("yes_ask") or 0
    vol = market.get("volume") or 0
    oi  = market.get("open_interest") or 0
    if bid == 0 and ask == 0 and vol == 0 and oi == 0:
        return False
    return True


def _score_match(question_tokens: set, market: dict) -> float:
    """
    F1 overlap between question and market text.

    Recall alone (overlap / question_tokens) lets a giant parlay market that
    contains every word in the language score 1.0. Precision punishes that:
    a market with 400 tokens that matches 3 of them scores near zero.
    """
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
    if not overlap:
        return 0.0

    recall    = len(overlap) / len(question_tokens)
    precision = len(overlap) / len(market_tokens)
    if recall + precision == 0:
        return 0.0
    f1 = 2 * recall * precision / (recall + precision)

    # Small bonus for markets with real activity — a live market matching the
    # same words is more likely the one the user is looking at.
    vol = market.get("volume") or 0
    if vol > 1000:
        f1 *= 1.15
    elif vol > 100:
        f1 *= 1.05

    return f1


def fetch_all_open_markets(use_cache: bool = True) -> list[dict]:
    """
    Page through every open Kalshi market. Kalshi has tens of thousands, so this
    is slow on a cold call (~20-40s) and cached for ALL_MARKETS_TTL afterwards.
    """
    now = time.time()
    if use_cache and _ALL_CACHE["data"] and (now - _ALL_CACHE["fetched_at"]) < ALL_MARKETS_TTL:
        return _ALL_CACHE["data"]

    started = time.time()
    markets: list[dict] = []
    cursor = None
    pages = 0

    for _ in range(MAX_SEARCH_PAGES):
        params = {"status": "open", "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor
        data = _get("/markets", params=params)
        if not data:
            break

        batch = data.get("markets", [])
        markets.extend(batch)
        pages += 1

        cursor = data.get("cursor")
        if not cursor or not batch:
            break
        if (time.time() - started) > SEARCH_TIME_BUDGET:
            log.warning(f"Kalshi: hit {SEARCH_TIME_BUDGET}s search budget after {len(markets)} markets")
            break

    if markets:
        _ALL_CACHE["data"] = markets
        _ALL_CACHE["fetched_at"] = now
        log.info(f"Kalshi: cached {len(markets)} open markets from {pages} pages "
                 f"in {time.time()-started:.1f}s")
    return markets or (_ALL_CACHE["data"] or [])


def search_markets(question: str, max_results: int = 5) -> list[dict]:
    """
    Fuzzy-search open Kalshi event markets matching a free-text question.
    Returns up to max_results parsed market dicts, best match first.
    """
    q_tokens = _tokenize(question)
    if not q_tokens:
        return []
    q_distinct = _distinctive(q_tokens)

    all_markets = fetch_all_open_markets()
    if not all_markets:
        log.warning("Kalshi search: could not fetch any open markets")
        return []

    scored: list[tuple] = []
    skipped_parlay = skipped_dead = 0

    for m in all_markets:
        if _is_parlay(m):
            skipped_parlay += 1
            continue
        if not _is_tradeable(m):
            skipped_dead += 1
            continue

        s = _score_match(q_tokens, m)
        if s < MIN_MATCH_SCORE:
            continue

        # Require at least one distinctive token (a name/number) to overlap,
        # so we don't match on generic filler words alone.
        if q_distinct:
            m_tokens = _tokenize(" ".join([
                m.get("title", ""), m.get("subtitle", ""),
                m.get("yes_sub_title", ""), m.get("ticker", ""),
            ]))
            if not (q_distinct & _distinctive(m_tokens)):
                continue

        scored.append((s, m))

    scored.sort(key=lambda x: -x[0])
    log.info(
        f"Kalshi search '{question[:40]}': {len(scored)} matches from "
        f"{len(all_markets)} open markets (skipped {skipped_parlay} parlay, {skipped_dead} dead)"
    )
    return [_parse_market(m, score) for score, m in scored[:max_results]]


def get_market(ticker: str) -> Optional[dict]:
    data = _get(f"/markets/{ticker}")
    if not data:
        return None
    m = data.get("market") or data
    return _parse_market(m)


def get_market_settlement(ticker: str) -> Optional[dict]:
    """
    Raw settlement state for one market.

    _parse_market() deliberately normalizes into what the ANALYST needs and
    drops `status` and `result` — but those two fields are the only way to know
    a binary position actually resolved and which way. Reading them through the
    parsed view would return None forever and the position would never close.

    Returns {"status", "result", "settled"} or None if the fetch failed.
    Distinguishing "not settled yet" from "we couldn't ask" matters: the caller
    must not treat a network error as an unresolved market indefinitely.
    """
    data = _get(f"/markets/{ticker}")
    if not data:
        return None
    m = data.get("market") or data
    status = (m.get("status") or "").strip().lower()
    result = (m.get("result") or "").strip().lower()
    return {
        "status":  status,
        "result":  result,
        # Kalshi reports "settled" (and historically "finalized"). Require a
        # real yes/no result too — a settled market with a blank result is
        # something we should not guess at.
        "settled": status in ("settled", "finalized") and result in ("yes", "no"),
    }


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
