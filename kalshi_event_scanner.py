#!/usr/bin/env python3
"""
kalshi_event_scanner.py — Autonomous hunter for fast-resolving event markets.

WHY THIS EXISTS:
Kalshi Golem was only trading crypto perps. Those are leveraged directional
bets on assets that all move together, with no natural resolution — so trades
ended when a timer expired rather than when the thesis played out. Eleven
trades produced 7 clock-exits, 1 target hit, and an average win smaller than
the average loss.

Event markets are structurally different and structurally better here:

  RESOLUTION IS BINARY AND REAL. Buy YES at 40¢, it settles at $1.00 or $0.00.
  No ambiguity, no trailing stop, no "time ran out while slightly green."

  RETURNS ARE LARGE BY CONSTRUCTION. A 40¢ contract that resolves YES returns
  150%. The perps were producing 1-2% clock-exits.

  THEY ARE GENUINELY UNCORRELATED. A Yankees game, a CPI print and a hurricane
  landfall share no common factor. Six crypto longs were one bet wearing six
  tickers.

  RESOLUTION IS FAST — if you select for it. Same-day sports settle in hours.
  Daily weather settles overnight. That's the filter this module applies:
  an election 14 months out ties up capital for a year, so it's excluded.

WHAT IT DOES NOT DO:
Pick markets on price alone. Every candidate goes through kalshi_analyst's
full framework — base rates, domain checklists, live research, liquidity
quality, edge calculation. A market is only tradeable if our estimate differs
materially from the market's, which is rare and should be.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from kalshi_events import (
    fetch_all_open_markets, _parse_market, _is_parlay, _is_tradeable,
    parlay_reason,
)
from kalshi_domains import detect_domain, DOMAIN_LABELS

log = logging.getLogger(__name__)

# ─── RESOLUTION WINDOW ────────────────────────────────────────────────────────
# The whole point: capital must recycle. An election a year out is a fine bet
# and a terrible use of a small bankroll.
MIN_HOURS_LEFT = float(os.getenv("KALSHI_EVENT_MIN_HOURS", "2"))
MAX_HOURS_LEFT = float(os.getenv("KALSHI_EVENT_MAX_HOURS", "72"))

# ─── LIQUIDITY ────────────────────────────────────────────────────────────────
# A market with no two-sided quote has no real price, so "edge" against it is
# meaningless. These floors are deliberately low — Kalshi event markets are
# thinner than equities — but non-zero.
MIN_VOLUME       = int(os.getenv("KALSHI_EVENT_MIN_VOLUME", "500"))
MIN_OPEN_INTEREST = int(os.getenv("KALSHI_EVENT_MIN_OI", "200"))
MAX_SPREAD_CENTS = int(os.getenv("KALSHI_EVENT_MAX_SPREAD", "6"))

# ─── PRICE BAND ───────────────────────────────────────────────────────────────
# Avoid both extremes. Below ~10¢ you're buying lottery tickets into the
# favourite-longshot bias (cheap contracts are systematically overpriced).
# Above ~90¢ you risk 90 to make 10 — one loss erases nine wins.
MIN_PRICE_CENTS = int(os.getenv("KALSHI_EVENT_MIN_PRICE", "12"))
MAX_PRICE_CENTS = int(os.getenv("KALSHI_EVENT_MAX_PRICE", "88"))

# Domains worth trading autonomously. Sports and weather resolve fast and have
# genuine reference classes. Elections are excluded not because they're
# unpredictable but because they're slow.
TRADEABLE_DOMAINS = set(
    (os.getenv("KALSHI_EVENT_DOMAINS") or
     "sports_team,sports_player,weather_climate,macro_econ,"
     "company_event,awards_culture,equity,crypto").split(",")
)

MAX_CANDIDATES = int(os.getenv("KALSHI_EVENT_MAX_CANDIDATES", "12"))


def _hours_left(m: dict) -> Optional[float]:
    return m.get("hours_left")


def _spread(m: dict) -> Optional[int]:
    bid, ask = m.get("yes_bid") or 0, m.get("yes_ask") or 0
    if not bid or not ask:
        return None
    return ask - bid


def screen_markets(limit: int = MAX_CANDIDATES) -> dict:
    """
    Find fast-resolving, liquid, analyzable event markets.

    Returns {"candidates": [...], "stats": {...}} where stats explains what was
    filtered and why — a scan that returns nothing should say which constraint
    bit, not just report zero.
    """
    raw = fetch_all_open_markets()
    from kalshi_events import LAST_FETCH
    if not raw:
        return {"candidates": [], "stats": {"error": "no markets returned"}}

    stats = {
        "total": len(raw), "parlay": 0, "dead": 0, "wrong_window": 0,
        "illiquid": 0, "price_band": 0, "domain": 0, "passed": 0,
    }
    out = []

    for m in raw:
        pr = parlay_reason(m)
        if pr:
            stats["parlay"] += 1
            # Sample WHY, so a filter that eats 98% of the universe can be
            # audited instead of trusted.
            stats.setdefault("parlay_reasons", {})
            stats["parlay_reasons"][pr] = stats["parlay_reasons"].get(pr, 0) + 1
            if len(stats.setdefault("parlay_samples", [])) < 5:
                stats["parlay_samples"].append(
                    f"{m.get('ticker','?')} :: {(m.get('title') or '')[:60]}")
            continue
        if not _is_tradeable(m):
            stats["dead"] += 1
            # Sample the RAW fields. All 81 surviving markets were rejected as
            # dead, which is implausible for a live exchange — far more likely
            # the list endpoint omits quote fields that only appear on the
            # single-market endpoint. Show what actually came back rather than
            # inferring it.
            if len(stats.setdefault("dead_samples", [])) < 4:
                stats["dead_samples"].append(
                    f"{m.get('ticker','?')} bid={m.get('yes_bid')} "
                    f"ask={m.get('yes_ask')} last={m.get('last_price')} "
                    f"vol={m.get('volume')} oi={m.get('open_interest')} "
                    f"status={m.get('status')} keys={len(m)}")
            continue

        p = _parse_market(m)

        hrs = _hours_left(p)
        if hrs is None or not (MIN_HOURS_LEFT <= hrs <= MAX_HOURS_LEFT):
            stats["wrong_window"] += 1
            continue

        vol = p.get("volume") or 0
        oi  = p.get("open_interest") or 0
        sp  = _spread(p)
        if vol < MIN_VOLUME or oi < MIN_OPEN_INTEREST or sp is None or sp > MAX_SPREAD_CENTS:
            stats["illiquid"] += 1
            # 683 of the 704 markets that survived the time window died here.
            # Three different thresholds can cause that and they need different
            # fixes, so count them separately instead of tuning blind.
            if vol < MIN_VOLUME:
                stats["liq_volume"] = stats.get("liq_volume", 0) + 1
            if oi < MIN_OPEN_INTEREST:
                stats["liq_oi"] = stats.get("liq_oi", 0) + 1
            if sp is None:
                stats["liq_no_quote"] = stats.get("liq_no_quote", 0) + 1
            elif sp > MAX_SPREAD_CENTS:
                stats["liq_spread"] = stats.get("liq_spread", 0) + 1
            # Track the best near-misses so the thresholds can be set from
            # the actual distribution rather than from a guess.
            if vol >= MIN_VOLUME * 0.5 and len(stats.setdefault("liq_near", [])) < 4:
                stats["liq_near"].append(
                    f"{p.get('ticker','?')[:34]} vol={vol} oi={oi} spread={sp}")
            continue

        price = p.get("implied_prob") or 0
        if not (MIN_PRICE_CENTS <= price <= MAX_PRICE_CENTS):
            stats["price_band"] += 1
            continue

        domain = detect_domain(f"{p.get('title','')} {p.get('subtitle','')}")
        if domain not in TRADEABLE_DOMAINS:
            stats["domain"] += 1
            continue

        p["domain"]       = domain
        p["domain_label"] = DOMAIN_LABELS.get(domain, domain)
        p["spread"]       = sp
        p["hours_left"]   = round(hrs, 1)
        out.append(p)
        stats["passed"] += 1

    # Prefer sooner resolution, then tighter spread, then more volume —
    # capital recycling is the point, and tight spreads mean the quoted
    # probability is real.
    out.sort(key=lambda x: (x["hours_left"], x["spread"], -(x.get("volume") or 0)))
    return {"candidates": out[:limit], "stats": stats,
            "fetch": dict(LAST_FETCH)}


def format_scan_summary(res: dict) -> str:
    s = res.get("stats", {})
    c = res.get("candidates", [])

    if s.get("error"):
        return f"⚠️ Event scan failed: {s['error']}"

    fetch = res.get("fetch") or {}
    lines = [
        f"🔍 *KALSHI EVENT SCAN*\n",
        f"Screened {s.get('total',0):,} open markets → *{len(c)} candidate(s)*",
    ]
    if fetch:
        lines.append(
            f"_fetch: {fetch.get('pages',0)} pages, "
            f"{fetch.get('parlays_skipped',0):,} parlays skipped, "
            f"{fetch.get('usable',0):,} usable in {fetch.get('elapsed',0):.0f}s_")
        if fetch.get("hit_budget"):
            lines.append("_⚠️ hit the time budget — raise KALSHI_SEARCH_BUDGET_")
        if fetch.get("hit_page_cap"):
            lines.append("_⚠️ hit the page cap — raise KALSHI_MAX_PAGES_")
    lines += [
        "",
        "*Filtered out:*",
        f"  {s.get('parlay',0):,} multi-leg parlays",
    ] + ([f"     -> {k}: {v:,}" for k, v in
          sorted((s.get("parlay_reasons") or {}).items(), key=lambda x: -x[1])[:4]]
         ) + ([f"     eg {x}" for x in (s.get("parlay_samples") or [])[:3]]) + [
        f"  {s.get('dead',0):,} no quote / no activity",
    ] + ([f"     {x}" for x in (s.get("dead_samples") or [])[:4]]) + [
        f"  {s.get('wrong_window',0):,} outside {MIN_HOURS_LEFT:.0f}-{MAX_HOURS_LEFT:.0f}h window",
        f"  {s.get('illiquid',0):,} too illiquid or wide spread",
    ] + ([f"     -> volume < {MIN_VOLUME}: {s.get('liq_volume',0):,}",
          f"     -> open interest < {MIN_OPEN_INTEREST}: {s.get('liq_oi',0):,}",
          f"     -> spread > {MAX_SPREAD_CENTS}c: {s.get('liq_spread',0):,}",
          f"     -> no two-sided quote: {s.get('liq_no_quote',0):,}"]
         if s.get("illiquid") else []
        ) + ([f"     near miss {x}" for x in (s.get("liq_near") or [])[:3]]) + [
        f"  {s.get('price_band',0):,} outside {MIN_PRICE_CENTS}-{MAX_PRICE_CENTS}c band",
        f"  {s.get('domain',0):,} slow/untradeable category",
        "",
    ]

    if not c:
        lines.append("_Nothing qualified. That's a normal outcome._")
        return "\n".join(lines)

    lines.append("*Candidates (soonest first):*")
    for m in c:
        lines.append(
            f"\n`{m['ticker']}`\n"
            f"  {m['title'][:70]}\n"
            f"  {m['domain_label']} · {m['implied_prob']:.0f}c · "
            f"{m['hours_left']:.0f}h left · spread {m['spread']}c · "
            f"vol {m.get('volume',0):,}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    res = screen_markets()
    print(re.sub(r"[*`]", "", format_scan_summary(res)))
