#!/usr/bin/env python3
"""
stock_research.py — AI judgment layer for Stock Golem.

IMPORTANT DESIGN CONSTRAINT: this layer can only VETO, never override.

The deterministic rules in stock_signals.py decide what qualifies. The AI's job
is to catch what rules can't see — a catalyst that's actually dilutive, a chart
that's extended after a multi-day run, a setup that's technically valid but
obviously the third pullback rather than the first. It cannot invent an entry,
loosen a threshold, or approve something the pillars rejected.

That asymmetry matters. An LLM told to "trade like Ross" will drift toward
plausible-sounding trades. An LLM allowed only to say "no" can improve
precision without degrading discipline.

Model is Haiku — the reasoning here is judgment on structured inputs, not
open-ended analysis, and it runs on every candidate during market hours.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

AI_MODEL      = "claude-haiku-4-5-20251001"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")
MIN_CONFIDENCE = int(os.getenv("STOCK_MIN_CONFIDENCE", "60"))


SYSTEM_PROMPT = """You are Golem, executing a specific momentum day-trading strategy on small-cap stocks. The strategy is not yours to modify — your role is quality control on setups that have already passed mechanical screening.

THE STRATEGY YOU ARE EXECUTING:

Stock selection — the 5 Pillars (a stock needs at least 4):
1. Relative volume ≥5x the 30-day average
2. Already up ≥10% on the day
3. A news catalyst that justifies the move
4. Price $5-$10 (small account; no leverage below $5)
5. Float under 20M shares in a hot market, under 10M in a cold one

Entry — the first pullback pattern:
A valid pullback retraces ≤50% of the prior surge, shows heavier volume on the
green candles than the red, holds above VWAP, and holds above the 9 EMA. Entry
is the crossing candle — the first candle to make a new high over the previous
candle's high. The stop is the low of the pullback.

Risk — non-negotiable:
Minimum 2:1 reward-to-risk. Risk ~$50 per trade. Daily max loss $100. Three
consecutive losers ends the day.

YOUR JOB — VETO ONLY:

You are given setups that already passed all of the above. Approve or reject.
You cannot adjust the entry, stop, or target. You cannot approve something that
failed screening. Say NO when any of these apply:

- THE CATALYST IS FAKE OR HARMFUL. Offerings, dilution, reverse splits, and
  going-concern warnings move a stock without supporting continuation. "Company
  announces $50M offering" is not a bullish catalyst even though price moved.
- THE MOVE IS ALREADY EXTENDED. If the stock is up 300% and this is the fourth
  pullback of the day, the easy move is gone. Ross trades the FRONT SIDE of
  momentum — the first or second pullback, not the fifth.
- THE SETUP IS NOT OBVIOUS. The edge comes from many traders seeing the same
  thing. A marginal pattern on a stock nobody is watching lacks the follow-through
  that makes the pattern work.
- HEAVY OVERHEAD SUPPLY. Price is pushing into an area where heavy selling
  previously occurred — a level it already failed at today, or a gap-fill zone.
- THE RISK IS ILLUSORY. A 2¢ stop on a stock moving 30¢ a minute will be taken
  out by noise, making the stated 2:1 meaningless.
- IT'S LATE. Momentum reliably cools after 10:30-11:00 AM ET. Late setups need
  to be materially better to be worth taking.

CALIBRATION:
Rejecting is cheap. Most setups should be rejected — Ross takes a handful of
trades from dozens of candidates. A day with no trades is a normal outcome and
is much better than a forced one. Do not manufacture reasons to approve.

Output MUST be valid JSON, no markdown fences, exactly this shape:
{
  "approve": true | false,
  "confidence": <integer 0-100>,
  "reasoning": "<2-3 plain sentences. Why this is or isn't worth taking, in the language of the strategy.>",
  "catalyst_quality": "strong" | "weak" | "harmful" | "none",
  "front_side": true | false,
  "key_risk": "<the single biggest way this trade goes wrong>",
  "veto_reason": "<if rejecting, which specific rule above. empty string if approving.>"
}"""


def _build_context(snap: dict, pillars: dict, pullback: dict,
                   sizing: dict, clock_note: str = "",
                   track_record: str = "") -> str:
    p = pillars["pillars"]

    def line(key, label):
        d = p.get(key, {})
        mark = "PASS" if d.get("pass") is True else ("FAIL" if d.get("pass") is False else "UNKNOWN")
        return f"  [{mark:7s}] {label}: {d.get('note','')}"

    heads = snap.get("headlines") or []
    head_block = "\n".join(f"    • {h}" for h in heads[:3]) or "    (none found)"

    checks = pullback["checks"]
    check_block = "\n".join(
        f"  [{'PASS' if c['pass'] else 'FAIL'}] {k}: {c['note']}"
        for k, c in checks.items()
    )

    return f"""=== SETUP REVIEW ===
Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
{clock_note}

SYMBOL: {snap['symbol']}  —  ${snap.get('price', 0):.2f}  ({snap.get('pct_change', 0):+.1f}% today)

--- 5 PILLARS ({pillars['passed']}/5 passed, grade {pillars['grade']}) ---
{line('rvol', 'Relative volume')}
{line('momentum', 'Already moving')}
{line('catalyst', 'News catalyst')}
{line('price', 'Price range')}
{line('float', 'Float')}

Headlines (48h):
{head_block}

--- INTRADAY CONTEXT ---
  Day open:   ${snap.get('day_open', 0):.2f}
  Day high:   ${snap.get('day_high', 0):.2f}
  Day low:    ${snap.get('day_low', 0):.2f}
  Current:    ${snap.get('price', 0):.2f}
  Day volume: {snap.get('day_volume', 0):,}
  Position in day range: {_range_pos(snap):.0f}% (100% = at highs)

--- PULLBACK PATTERN ---
{check_block}
  Surge candles: {pullback['surge_candles']} | Pullback candles: {pullback['pullback_candles']}
  Retrace: {pullback['retrace_pct']}% of the move
  VWAP ${pullback['vwap']:.2f} | 9EMA ${pullback['ema']:.2f}
  Crossing candle formed: {pullback['crossing_candle']}

--- PROPOSED TRADE ---
  Entry:  ${pullback['entry']:.2f}
  Stop:   ${pullback['stop']:.2f}  (risk ${pullback['risk_per_share']:.2f}/share)
  Target: ${pullback['target']:.2f}  (2:1)
  Shares: {sizing.get('shares', 0)} → total risk ${sizing.get('total_risk', 0):.2f}, cost ${sizing.get('cost', 0):.2f}
  Sizing limited by: {sizing.get('limited_by', '-')}

--- OUR OWN TRACK RECORD ---
{track_record or "No closed trades yet — no calibration data."}

=== END ===

Approve or reject. Remember: rejecting is cheap, forcing a trade is not."""


def _range_pos(snap: dict) -> float:
    hi, lo, px = snap.get("day_high", 0), snap.get("day_low", 0), snap.get("price", 0)
    if hi <= lo:
        return 0.0
    return (px - lo) / (hi - lo) * 100


def _parse(raw: str) -> Optional[dict]:
    txt = raw.strip()
    if "```" in txt:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", txt)
        if m:
            txt = m.group(1).strip()
    else:
        s, e = txt.find("{"), txt.rfind("}")
        if s != -1 and e > s:
            txt = txt[s:e + 1]
    try:
        v = json.loads(txt)
    except json.JSONDecodeError:
        return None
    if "approve" not in v or "reasoning" not in v:
        return None
    v["approve"]    = bool(v["approve"])
    v["confidence"] = int(v.get("confidence", 50))
    return v


def review_setup(snap: dict, pillars: dict, pullback: dict,
                 sizing: dict, clock_note: str = "",
                 track_record: str = "") -> dict:
    """
    Returns {approve, confidence, reasoning, ...}.

    Fails CLOSED: if the AI is unavailable or unparseable, the setup is
    rejected. A trading system that opens positions when its judgment layer is
    broken is worse than one that sits out.
    """
    if not ANTHROPIC_KEY:
        return {"approve": False, "confidence": 0,
                "reasoning": "No API key — cannot review, skipping trade.",
                "catalyst_quality": "none", "front_side": False,
                "key_risk": "", "veto_reason": "no_api_key"}

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=AI_MODEL,
            max_tokens=700,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user",
                       "content": _build_context(snap, pillars, pullback, sizing,
                                                 clock_note, track_record)}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        v = _parse(raw)
        if v:
            if v["approve"] and v["confidence"] < MIN_CONFIDENCE:
                v["approve"] = False
                v["veto_reason"] = f"confidence {v['confidence']} < {MIN_CONFIDENCE}"
            return v
        log.warning(f"Stock research: unparseable response for {snap.get('symbol')}")
    except Exception as e:
        log.error(f"Stock research: API error for {snap.get('symbol')}: {e}")

    return {"approve": False, "confidence": 0,
            "reasoning": "Review failed — skipping rather than trading blind.",
            "catalyst_quality": "none", "front_side": False,
            "key_risk": "", "veto_reason": "review_error"}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    snap = {"symbol": "HOWL", "price": 6.42, "pct_change": 103.0,
            "day_open": 3.20, "day_high": 6.55, "day_low": 3.10,
            "day_volume": 1958542, "headlines": ["Company announces FDA clearance"],
            "news_count": 1, "rvol": 18.4, "float_m": 8.2}
    pillars = {"symbol": "HOWL", "passed": 5, "grade": "A", "qualifies": True,
               "pillars": {
                   "rvol":     {"pass": True,  "note": "18.4x vs 5x required"},
                   "momentum": {"pass": True,  "note": "+103.0% today vs +10% required"},
                   "catalyst": {"pass": True,  "note": "1 headline(s) in 48h"},
                   "price":    {"pass": True,  "note": "$6.42 vs $5-$10 range"},
                   "float":    {"pass": True,  "note": "8.2M vs <20M (hot market)"}}}
    pullback = {"valid": True, "crossing_candle": True, "surge_candles": 5,
                "pullback_candles": 3, "retrace_pct": 32.0, "entry": 6.42,
                "stop": 6.28, "target": 6.70, "risk_per_share": 0.14,
                "vwap": 5.98, "ema": 6.31,
                "checks": {"retrace_ok": {"pass": True, "note": "retraced 32% (max 50%)"},
                           "volume_ok": {"pass": True, "note": "surge 240k vs pullback 70k/bar"},
                           "above_vwap": {"pass": True, "note": "low $6.28 vs VWAP $5.98"},
                           "above_ema": {"pass": True, "note": "low $6.28 vs 9EMA $6.31"}}}
    sizing = {"shares": 142, "total_risk": 19.88, "cost": 911.64, "limited_by": "position cap"}
    print(json.dumps(review_setup(snap, pillars, pullback, sizing), indent=2))
