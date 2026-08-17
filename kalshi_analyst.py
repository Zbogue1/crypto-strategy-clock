#!/usr/bin/env python3
"""
kalshi_analyst.py — Expert bet analysis framework for ANY Kalshi event market.

You ask a question in plain English ("Will Bitcoin be above $66,000 by Friday?"),
this module finds the matching Kalshi market and runs a superforecaster-grade
analysis stack, then returns a calibrated probability and a bet / no-bet verdict.

THE FRAMEWORK (applies to every category — crypto, politics, sports, culture, econ):

  LAYER 1 — MARKET PRICE (the prior)
      The Kalshi mid price IS a probability. Prediction markets are hard to beat.
      We start from the market's number and require real evidence to deviate.

  LAYER 2 — REFERENCE CLASS / BASE RATE (the outside view)
      Before looking at specifics: how often does this KIND of thing happen?
      Incumbents usually win. Favorites usually cover. Prices usually don't move 20% in a day.

  LAYER 3 — STATISTICAL MODEL (where the question is numeric)
      For "price above X by time T" bets we compute the real probability from
      realized volatility using lognormal digital-option math. This is an
      objective number the market can be measurably wrong about.

  LAYER 4 — TIME STRUCTURE
      Hours remaining vs distance to target. A 5% move needs very different
      odds over 3 hours than over 3 weeks. Short clock = mean reversion to
      current state; long clock = uncertainty dominates.

  LAYER 5 — LIVE EVIDENCE (the inside view)
      Web search for news, polls, injuries, filings, announcements that the
      market may not have priced in yet.

  LAYER 6 — LIQUIDITY & MICROSTRUCTURE
      Wide spread or thin volume = the "implied probability" is noise, not signal.
      Low-liquidity markets get a confidence haircut.

  LAYER 7 — EDGE CALCULATION & CALIBRATION
      Final probability vs market price = edge. We only bet on meaningful edge,
      and we size by edge, not by conviction. Most questions should return PASS.

Usage:
    from kalshi_analyst import analyze_question
    result = analyze_question("Will Bitcoin be above $66,000 this week?")
    print(result["telegram"])
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

from kalshi_events import (
    search_markets, get_market,
    detect_crypto_symbol, extract_threshold,
    get_spot_and_vol, prob_above,
)
from kalshi_domains import build_domain_block

log = logging.getLogger(__name__)

AI_MODEL      = "claude-haiku-4-5-20251001"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Minimum edge (in probability points) before we call it a bet
MIN_EDGE_POINTS    = float(os.getenv("KALSHI_MIN_EDGE", "7"))
# Below this liquidity the implied price is treated as unreliable
THIN_VOLUME        = 500
# Max fraction of bankroll to suggest on any single event bet
MAX_STAKE_FRACTION = 0.05


# ─── LAYER 3: STATISTICAL MODEL ───────────────────────────────────────────────

def _statistical_layer(question: str, market: Optional[dict]) -> dict:
    """
    If the question is a numeric threshold bet on a crypto asset, compute the
    objective probability from realized volatility. Returns {} if N/A.
    """
    text = f"{question} {market.get('title','') if market else ''} {market.get('subtitle','') if market else ''}"
    symbol = detect_crypto_symbol(text)
    if not symbol:
        return {}

    threshold = extract_threshold(text)
    if not threshold:
        return {}

    sv = get_spot_and_vol(symbol)
    if not sv:
        return {}

    hours_left = (market or {}).get("hours_left")
    if hours_left is None or hours_left <= 0:
        hours_left = 24.0   # assume same-day if the market didn't tell us

    spot = sv["spot"]
    p_above = prob_above(spot, threshold, sv["hourly_vol"], hours_left)

    distance_pct = (threshold / spot - 1) * 100 if spot else 0.0
    # How many standard deviations away is the target over the remaining window?
    import math
    sigma_t = sv["hourly_vol"] * math.sqrt(max(hours_left, 0.01))
    sigmas  = (math.log(threshold / spot) / sigma_t) if (spot > 0 and sigma_t > 0) else 0.0

    return {
        "applies":        True,
        "symbol":         symbol,
        "spot":           round(spot, 4),
        "threshold":      threshold,
        "distance_pct":   round(distance_pct, 2),
        "sigmas_away":    round(sigmas, 2),
        "hourly_vol_pct": round(sv["hourly_vol"] * 100, 3),
        "daily_vol_pct":  round(sv["daily_vol"] * 100, 2),
        "pct_24h":        sv["pct_24h"],
        "hours_left":     round(hours_left, 1),
        "model_prob_yes": p_above,
    }


# ─── LAYER 6: LIQUIDITY QUALITY ───────────────────────────────────────────────

def _liquidity_layer(market: Optional[dict]) -> dict:
    if not market:
        return {"quality": "unknown", "spread": None, "note": "No market matched."}

    bid, ask = market.get("yes_bid") or 0, market.get("yes_ask") or 0
    spread = (ask - bid) if (bid and ask) else None
    vol    = market.get("volume", 0) or 0

    if spread is None:
        quality, note = "poor", "No two-sided quote — implied probability is unreliable."
    elif spread > 10:
        quality, note = "poor", f"Spread is {spread}c wide — price is noisy, not a clean probability."
    elif spread > 4 or vol < THIN_VOLUME:
        quality, note = "fair", f"Spread {spread}c, volume {vol:,} — moderate confidence in the price."
    else:
        quality, note = "good", f"Spread {spread}c, volume {vol:,} — price is a reliable probability."

    return {"quality": quality, "spread": spread, "volume": vol, "note": note}


# ─── SYSTEM PROMPT ────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are Golem, an elite superforecaster analyzing prediction market questions on Kalshi. You have the track record of a top-1% Good Judgment Project forecaster. You handle EVERY category: crypto, equities, macro/econ, politics, elections, sports, weather, culture, awards, company events.

YOUR METHOD — follow this order, always:

1. OUTSIDE VIEW FIRST. Before any specifics, state the reference class and its base rate. "How often does this class of event happen?" Incumbents usually win. Favorites usually win. Records usually stand. Prices usually stay near where they are. Status quo is the single most underrated prediction.

2. TREAT THE MARKET PRICE AS THE PRIOR. The Kalshi mid price is a real probability produced by people with money at risk. Your default answer is "the market is right." You may only deviate when you can name the specific thing you know that the price does not reflect. Vague disagreement is not evidence.

3. USE THE STATISTICAL MODEL WHEN PROVIDED. For numeric threshold questions you are given a volatility-based probability. That number is objective. If it disagrees with the market by a lot, that is your single strongest signal — but check whether the model's assumptions fit (drift-free lognormal, no scheduled catalyst, no gap risk).

4. RESPECT THE CLOCK. Distance-to-target divided by remaining time is the core of any threshold bet. With hours left, the current state overwhelmingly persists. With weeks left, uncertainty dominates and extremes become reachable.

5. WEIGH LIVE EVIDENCE LAST, NOT FIRST. News moves markets fast; assume anything widely reported is already in the price. You are looking for evidence that is fresh, specific, and not yet reflected in the quote.

6. DISCOUNT FOR LIQUIDITY. If the spread is wide or volume is thin, the "implied probability" is noise. Lower your confidence and demand a bigger edge.

7. CALIBRATE, DON'T CONVICT. Use the full 0-100 range honestly. If you think it's a coin flip, say 50. Avoid clustering at 60-70 to sound smart. Overconfidence is the #1 forecaster killer.

CRITICAL RULES:
- PASS is the correct answer most of the time. Real edges in liquid prediction markets are rare. Forcing a bet on a 2-point edge is how bankrolls die.
- Never let a compelling narrative override a base rate.
- Beware conjunctions: "X AND Y both happen" is always less likely than either alone.
- Long-shot bias: markets systematically overprice unlikely events (things priced 1-5c usually resolve NO). Favorite-longshot bias means cheap YES contracts are usually still too expensive.
- If the market barely matches the question the user asked, say so plainly and lower confidence.
- Fees matter: Kalshi charges on the trade, so a 3-point edge is roughly break-even.

Output MUST be valid JSON, no markdown fences, exactly this shape:
{
  "probability_yes": <integer 0-100 — YOUR calibrated probability the market resolves YES>,
  "market_implied": <integer 0-100 — the market's price you were given>,
  "edge_points": <integer — probability_yes minus market_implied, can be negative>,
  "verdict": "BET_YES" | "BET_NO" | "PASS",
  "confidence": <integer 0-100 — how much you trust your own number>,
  "reference_class": "<the outside view: what class of event is this and what's the base rate? 1-2 sentences.>",
  "reasoning": "<plain English, 3-5 sentences, like you're texting a smart friend. Walk through: base rate → what the market says → what the data/model says → what news changes → your call. No jargon.>",
  "key_evidence": ["<specific fact 1>", "<specific fact 2>", "<specific fact 3>"],
  "what_would_change_my_mind": "<the specific development that would flip this call>",
  "key_risk": "<the main way this call goes wrong, one sentence>",
  "suggested_stake_pct": <float 0-5 — percent of bankroll, 0 if PASS. Size by edge, not excitement.>
}"""


# ─── CONTEXT BUILDER ──────────────────────────────────────────────────────────

def _build_context(question: str,
                   market: Optional[dict],
                   stat: dict,
                   liq: dict,
                   alternatives: list[dict],
                   domain: dict) -> str:

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    if market:
        market_block = f"""MATCHED KALSHI MARKET
  Ticker:        {market['ticker']}
  Title:         {market['title']}
  Subtitle:      {market['subtitle']}
  Category:      {market['category'] or 'n/a'}
  Match quality: {market['match_score']} (1.0 = exact wording overlap)

  YES bid / ask:      {market['yes_bid']}c / {market['yes_ask']}c
  MARKET-IMPLIED P(YES): {market['implied_prob']}%
  Last trade:         {market['last_price']}c
  Volume (total/24h): {market['volume']:,} / {market['volume_24h']:,}
  Open interest:      {market['open_interest']:,}
  Closes:             {market['close_time']}  ({market['hours_left']} hours left)

  Resolution rules: {market['rules'][:400] or 'not provided'}"""
    else:
        market_block = ("NO KALSHI MARKET MATCHED this question. Analyze the question on its "
                        "merits and state clearly that no tradeable market was found.")

    if stat.get("applies"):
        stat_block = f"""STATISTICAL MODEL (objective, volatility-based)
  Asset:              {stat['symbol']}
  Spot price now:     ${stat['spot']:,}
  Target threshold:   ${stat['threshold']:,}
  Distance to target: {stat['distance_pct']:+.2f}%
  Standard deviations away: {stat['sigmas_away']:+.2f}σ over the remaining window
  Realized hourly vol: {stat['hourly_vol_pct']}%   (daily: {stat['daily_vol_pct']}%)
  24H move so far:    {stat['pct_24h']:+.2f}%
  Time remaining:     {stat['hours_left']} hours

  >>> MODEL P(above threshold at expiry) = {stat['model_prob_yes']}%
  (lognormal, zero-drift digital option. Assumes no scheduled catalyst and no gap risk.)"""
    else:
        stat_block = ("STATISTICAL MODEL: not applicable — this is not a numeric price-threshold "
                      "question, so there is no closed-form probability. Rely on base rates and evidence.")

    alt_block = ""
    if alternatives:
        lines = [f"  - {a['ticker']}: {a['title']} | implied {a['implied_prob']}% | match {a['match_score']}"
                 for a in alternatives[:4]]
        alt_block = "OTHER MARKETS THAT ALSO MATCHED (check we picked the right one):\n" + "\n".join(lines)

    search_list = "\n".join(f'  {i+1}. "{s}"' for i, s in enumerate(domain["searches"]))

    return f"""=== KALSHI BET ANALYSIS REQUEST ===
Current time: {now}

USER'S QUESTION: "{question}"

DETECTED DOMAIN: {domain['label']}

{'=' * 70}
{domain['checklist']}
{'=' * 70}

{market_block}

{stat_block}

LIQUIDITY / PRICE QUALITY
  Rating: {liq['quality']}
  {liq['note']}

{alt_block}

=== END CONTEXT ===

MANDATORY RESEARCH STEP — run these searches before you answer:
{search_list}

Then work through the domain checklist above IN ORDER. Your "key_evidence" array
must contain the concrete findings for the top checklist factors — actual injury
statuses, actual records, actual poll numbers, actual consensus estimates. If you
searched and could not find a top-priority factor, say so explicitly in your
reasoning and lower your confidence accordingly. Do not produce generic evidence
like "the team has been playing well" — cite the specific number or status.

Then produce your JSON verdict."""


# ─── MAIN ANALYSIS ────────────────────────────────────────────────────────────

def analyze_question(question: str, ticker: str = None) -> dict:
    """
    Full expert analysis of a Kalshi bet question.

    question: free text, e.g. "Will Bitcoin be above $66,000 on Friday?"
    ticker:   optional — skip search and analyze this exact market

    Returns dict with the verdict fields plus 'telegram' (formatted message)
    and 'error' if something went wrong.
    """
    if not ANTHROPIC_KEY:
        return {"error": "No ANTHROPIC_API_KEY configured."}

    # 1. Find the market
    alternatives: list[dict] = []
    if ticker:
        market = get_market(ticker)
    else:
        matches = search_markets(question, max_results=5)
        market  = matches[0] if matches else None
        alternatives = matches[1:] if len(matches) > 1 else []

    # 2. Run the deterministic layers
    stat   = _statistical_layer(question, market)
    liq    = _liquidity_layer(market)
    domain = build_domain_block(question, (market or {}).get("title", ""))
    log.info(f"Kalshi analyst: domain={domain['label']} for '{question[:50]}'")

    context = _build_context(question, market, stat, liq, alternatives, domain)

    # 3. Claude synthesis with live web search
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": 6,
            }],
            messages=[{"role": "user", "content": context}],
        )
    except Exception as e:
        log.error(f"Kalshi analyst: API error: {e}")
        # Retry once without web search in case the tool isn't enabled on the key
        try:
            client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
            response = client.messages.create(
                model=AI_MODEL,
                max_tokens=2000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": context}],
            )
        except Exception as e2:
            return {"error": f"Analysis failed: {e2}"}

    # 4. Extract the JSON from the final text block
    raw = ""
    for block in response.content:
        if getattr(block, "type", "") == "text":
            raw += block.text
    raw = raw.strip()

    verdict = _parse_verdict(raw)
    if not verdict:
        return {"error": "Could not parse analysis output.", "raw": raw[:500]}

    # 5. Enforce our own edge discipline on top of the model's call
    implied = market["implied_prob"] if market else None
    if implied is not None:
        edge = verdict["probability_yes"] - implied
        verdict["edge_points"]   = round(edge)
        verdict["market_implied"] = round(implied)
        if abs(edge) < MIN_EDGE_POINTS:
            verdict["verdict"] = "PASS"
            verdict["suggested_stake_pct"] = 0.0
        if liq["quality"] == "poor":
            verdict["confidence"] = min(verdict.get("confidence", 50), 45)

    verdict["suggested_stake_pct"] = min(
        float(verdict.get("suggested_stake_pct", 0) or 0),
        MAX_STAKE_FRACTION * 100,
    )

    verdict["question"]    = question
    verdict["market"]      = market
    verdict["statistical"] = stat
    verdict["liquidity"]   = liq
    verdict["domain"]      = domain["domain"]
    verdict["domain_label"] = domain["label"]
    verdict["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    verdict["telegram"]    = format_analysis_telegram(verdict)
    return verdict


def _parse_verdict(raw: str) -> Optional[dict]:
    txt = raw
    if "```" in txt:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", txt)
        if m:
            txt = m.group(1).strip()
    else:
        # Grab the outermost JSON object if there's prose around it
        start, end = txt.find("{"), txt.rfind("}")
        if start != -1 and end > start:
            txt = txt[start:end + 1]
    try:
        v = json.loads(txt)
    except json.JSONDecodeError:
        return None

    required = {"probability_yes", "verdict", "reasoning"}
    if not required.issubset(v.keys()):
        return None

    v["probability_yes"] = int(v.get("probability_yes", 50))
    v["confidence"]      = int(v.get("confidence", 50))
    return v


# ─── TELEGRAM FORMATTING ──────────────────────────────────────────────────────

def format_analysis_telegram(v: dict) -> str:
    market = v.get("market")
    stat   = v.get("statistical") or {}
    liq    = v.get("liquidity") or {}

    verdict = v.get("verdict", "PASS")
    emoji   = {"BET_YES": "🟢", "BET_NO": "🔴", "PASS": "⚪"}.get(verdict, "⚪")
    word    = {"BET_YES": "BET YES", "BET_NO": "BET NO", "PASS": "SIT THIS ONE OUT"}.get(verdict, verdict)

    p    = v["probability_yes"]
    bar  = "█" * int(p / 10) + "░" * (10 - int(p / 10))

    dom_label = v.get("domain_label", "")
    header = f"🎯 *KALSHI* — Bet Analysis"
    if dom_label and dom_label != "General":
        header += f"  ·  _{dom_label}_"
    lines = [header + "\n", f"_{v.get('question','')}_\n"]

    if market:
        lines.append(f"*{market['title']}*")
        if market.get("subtitle"):
            lines.append(f"{market['subtitle']}")
        lines.append(f"`{market['ticker']}`")
        if market.get("hours_left") is not None:
            hrs = market["hours_left"]
            when = f"{hrs:.0f} hours" if hrs < 48 else f"{hrs/24:.0f} days"
            lines.append(f"⏳ Closes in {when}")
        lines.append("")
    else:
        lines.append("⚠️ No matching Kalshi market found — analysis is on the question only.\n")

    lines.append(f"*Golem says: {p}% chance of YES*")
    lines.append(f"`{bar}`")

    if market:
        mi   = v.get("market_implied", market["implied_prob"])
        edge = v.get("edge_points", 0)
        lines.append(f"Market is pricing it at *{mi}%*")
        if edge > 0:
            lines.append(f"→ Golem thinks YES is *{abs(edge)} points underpriced*")
        elif edge < 0:
            lines.append(f"→ Golem thinks YES is *{abs(edge)} points overpriced*")
        else:
            lines.append("→ Golem agrees with the market")
    lines.append("")

    lines.append(f"{emoji} *{word}*")
    if verdict != "PASS" and v.get("suggested_stake_pct"):
        lines.append(f"Suggested size: {v['suggested_stake_pct']:.1f}% of bankroll")
    lines.append("")

    if v.get("reference_class"):
        lines.append(f"📚 *Base rate:* {v['reference_class']}\n")

    lines.append(f"💬 *Why:* {v.get('reasoning','')}\n")

    ev = v.get("key_evidence") or []
    if ev:
        label = {
            "sports_team":   "🏈 *Injuries, form & matchup:*",
            "sports_player": "🏈 *Health, usage & matchup:*",
            "politics_election": "🗳 *Polls & fundamentals:*",
            "macro_econ":    "🏦 *Market odds & data:*",
            "equity":        "📊 *Estimates & positioning:*",
            "weather_climate": "🌪 *Model guidance:*",
            "awards_culture": "🏆 *Precursors & campaign:*",
        }.get(v.get("domain", ""), "🔍 *What matters:*")
        lines.append(label)
        for e in ev[:5]:
            lines.append(f"• {e}")
        lines.append("")

    if stat.get("applies"):
        lines.append(
            f"📐 *The math:* {stat['symbol']} at ${stat['spot']:,} needs "
            f"{stat['distance_pct']:+.1f}% to reach ${stat['threshold']:,} "
            f"({abs(stat['sigmas_away']):.1f}σ). Volatility model says {stat['model_prob_yes']}%.\n"
        )

    if liq.get("quality") in ("poor", "fair"):
        lines.append(f"💧 *Liquidity:* {liq['note']}\n")

    if v.get("what_would_change_my_mind"):
        lines.append(f"🔄 *Would flip this call:* {v['what_would_change_my_mind']}\n")

    if v.get("key_risk"):
        lines.append(f"⚠️ *Main risk:* {v['key_risk']}\n")

    lines.append(f"_Confidence in this read: {v.get('confidence',50)}/100. Analysis only — not financial advice._")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import sys
    q = " ".join(sys.argv[1:]) or "Will Bitcoin be above $120,000 this week?"
    res = analyze_question(q)
    if res.get("error"):
        print("ERROR:", res["error"])
        print(res.get("raw", ""))
    else:
        print(res["telegram"])
