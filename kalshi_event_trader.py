#!/usr/bin/env python3
"""
kalshi_event_trader.py — Autonomous trading of Kalshi event markets.

Binary contracts behave nothing like the perps, and the position maths has to
reflect that:

  A YES contract bought at 55c costs $0.55 and settles at $1.00 or $0.00.
  Max loss  = 55c per contract (the entire stake)
  Max gain  = 45c per contract
  Break-even win rate = 55%

That last line is the whole game. Buying at 55c requires being right more than
55% of the time just to break even. So the only justification for a position is
a genuine, quantified disagreement with the market price — never "this looks
likely." A 70%-likely event priced at 70c is a coin flip after fees.

Contrast with the perps, where a trade could end merely because a timer expired
while it happened to be green. Here the market resolves and you are simply
right or wrong.

EDGE IS THE ONLY REASON TO TRADE:
  edge = our_probability − market_implied_probability
Positions require edge ≥ MIN_EDGE_POINTS after accounting for the spread,
because crossing a 4c spread eats 4 points of a supposed 8-point edge.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Minimum edge in probability points AFTER spread cost. Kalshi charges on the
# trade, so a nominal 5-point edge is roughly break-even.
MIN_EDGE_POINTS   = float(os.getenv("KALSHI_EVENT_MIN_EDGE", "8"))
MIN_CONFIDENCE    = int(os.getenv("KALSHI_EVENT_MIN_CONF", "60"))
# Stake per event bet, in dollars.
STAKE_PER_BET     = float(os.getenv("KALSHI_EVENT_STAKE", "50"))
MAX_EVENT_POSITIONS = int(os.getenv("KALSHI_EVENT_MAX_POSITIONS", "8"))
# Cap exposure to any single domain — six baseball games in one evening is one
# bet on "favourites hold up tonight", not six independent positions.
MAX_PER_DOMAIN    = int(os.getenv("KALSHI_EVENT_MAX_PER_DOMAIN", "3"))


def calc_position(price_cents: float, side: str,
                  stake: float = STAKE_PER_BET) -> dict:
    """
    Contract count and payoff for a binary bet.

    side "YES" buys at the ask; "NO" is equivalent to buying the complement at
    (100 − price).
    """
    cost_per = price_cents / 100.0 if side == "YES" else (100 - price_cents) / 100.0
    if cost_per <= 0 or cost_per >= 1:
        return {"contracts": 0, "reason": f"unusable price {price_cents}c"}

    contracts = int(stake / cost_per)
    if contracts < 1:
        return {"contracts": 0, "reason": f"stake ${stake:.0f} buys <1 contract"}

    total_cost = contracts * cost_per
    max_gain   = contracts * (1 - cost_per)

    return {
        "contracts":   contracts,
        "cost_per":    round(cost_per, 4),
        "total_cost":  round(total_cost, 2),
        "max_gain":    round(max_gain, 2),
        "max_loss":    round(total_cost, 2),
        "breakeven_pct": round(cost_per * 100, 1),
        "payout_ratio":  round(max_gain / total_cost, 2) if total_cost else 0,
    }


def evaluate(market: dict, analysis: dict) -> dict:
    """
    Decide whether a market is worth betting, given our probability estimate.

    Returns {"trade": bool, "side": "YES"/"NO", "edge": float, "reason": str, ...}
    """
    implied = float(market.get("implied_prob") or 0)
    ours    = float(analysis.get("probability_yes") or 0)
    conf    = int(analysis.get("confidence") or 0)
    spread  = float(market.get("spread") or 0)

    raw_edge = ours - implied

    # Crossing the spread costs roughly half of it on entry. An 8-point edge
    # against a 6c spread is really a 5-point edge.
    net_edge = abs(raw_edge) - (spread / 2.0)
    side     = "YES" if raw_edge > 0 else "NO"

    if conf < MIN_CONFIDENCE:
        return {"trade": False, "side": side, "edge": round(net_edge, 1),
                "reason": f"confidence {conf} below {MIN_CONFIDENCE}"}

    if analysis.get("verdict") == "PASS":
        return {"trade": False, "side": side, "edge": round(net_edge, 1),
                "reason": "analyst says PASS"}

    if net_edge < MIN_EDGE_POINTS:
        return {"trade": False, "side": side, "edge": round(net_edge, 1),
                "reason": (f"edge {net_edge:.1f}pts after {spread:.0f}c spread "
                           f"< {MIN_EDGE_POINTS:.0f} required")}

    sizing = calc_position(implied, side)
    if sizing["contracts"] < 1:
        return {"trade": False, "side": side, "edge": round(net_edge, 1),
                "reason": sizing.get("reason", "sizing failed")}

    return {
        "trade":     True,
        "side":      side,
        "edge":      round(net_edge, 1),
        "raw_edge":  round(raw_edge, 1),
        "our_prob":  ours,
        "implied":   implied,
        "confidence": conf,
        "sizing":    sizing,
        "reason":    (f"{side} at {implied:.0f}c, we estimate {ours:.0f}% — "
                      f"{net_edge:.1f}pt edge after spread"),
    }


def check_domain_limit(domain: str, open_positions: list) -> tuple:
    """
    Cap same-domain exposure.

    Six same-evening baseball games are not six independent bets — they share
    weather, umpiring, league-wide scoring conditions, and above all the
    question of whether favourites hold up tonight. Correlated positions
    inflate apparent diversification exactly like the six crypto longs did.
    """
    n = sum(1 for p in open_positions if p.get("domain") == domain)
    if n >= MAX_PER_DOMAIN:
        return False, (f"already hold {n} {domain} position(s), "
                       f"max {MAX_PER_DOMAIN}")
    return True, ""


def format_bet_alert(market: dict, decision: dict, analysis: dict) -> str:
    s = decision["sizing"]
    side = decision["side"]
    icon = "🟢" if side == "YES" else "🔴"

    hrs = market.get("hours_left") or 0
    when = f"{hrs:.0f}h" if hrs < 48 else f"{hrs/24:.1f}d"

    return (
        f"🎯 *KALSHI EVENT BET*\n\n"
        f"*{market['title'][:90]}*\n"
        f"`{market['ticker']}`\n"
        f"{market.get('domain_label','?')} · resolves in {when}\n\n"
        f"{icon} *Betting {side}* — {s['contracts']} contracts @ "
        f"{decision['implied']:.0f}c\n"
        f"Stake: *${s['total_cost']:.2f}*  ·  Win: *+${s['max_gain']:.2f}*  ·  "
        f"Lose: *-${s['max_loss']:.2f}*\n"
        f"Payout {s['payout_ratio']:.2f}:1 · break-even {s['breakeven_pct']:.0f}%\n\n"
        f"*Our estimate: {decision['our_prob']:.0f}%*  vs market "
        f"{decision['implied']:.0f}%\n"
        f"Edge: *{decision['edge']:.1f} points* after spread\n\n"
        f"💬 {analysis.get('reasoning','')[:320]}\n\n"
        f"📚 {analysis.get('reference_class','')[:180]}\n"
        f"⚠️ {analysis.get('key_risk','')[:180]}\n\n"
        f"_Paper bet. Resolves {market.get('close_time','')[:16].replace('T',' ')} UTC._"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("BINARY POSITION MATHS")
    print("-" * 62)
    for price in (20, 35, 55, 70, 85):
        p = calc_position(price, "YES", 50)
        print(f"  buy YES @ {price:2d}c: {p['contracts']:>3} contracts, "
              f"risk ${p['max_loss']:>5.2f} to win ${p['max_gain']:>5.2f} "
              f"({p['payout_ratio']:.2f}:1, need {p['breakeven_pct']:.0f}% to break even)")

    print("\nEDGE GATE")
    print("-" * 62)
    cases = [
        ("clear edge, tight spread",   {"implied_prob": 55, "spread": 2},
                                       {"probability_yes": 70, "confidence": 75, "verdict": "BET_YES"}),
        ("edge eaten by wide spread",  {"implied_prob": 55, "spread": 10},
                                       {"probability_yes": 65, "confidence": 75, "verdict": "BET_YES"}),
        ("agrees with market",         {"implied_prob": 60, "spread": 2},
                                       {"probability_yes": 61, "confidence": 80, "verdict": "PASS"}),
        ("big edge, low confidence",   {"implied_prob": 40, "spread": 2},
                                       {"probability_yes": 65, "confidence": 45, "verdict": "BET_YES"}),
        ("betting NO",                 {"implied_prob": 70, "spread": 2},
                                       {"probability_yes": 45, "confidence": 72, "verdict": "BET_NO"}),
    ]
    for name, mkt, an in cases:
        d = evaluate(mkt, an)
        verdict = f"BET {d['side']}" if d["trade"] else "pass"
        print(f"  {name:28s} -> {verdict:8s} ({d['reason'][:52]})")
