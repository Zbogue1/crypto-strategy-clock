#!/usr/bin/env python3
"""
kalshi_research.py — Golem AI analysis agent for Kalshi perp markets.

For each viable candidate (from kalshi_signals), this module:
  1. Assembles a rich context payload (price action, trend, funding, OI, postmortem history)
  2. Sends it to Claude Sonnet 5 for 5-layer analysis
  3. Gets back a structured verdict: UP / DOWN / FLAT with confidence + plain-English reasoning
  4. Returns an alert-ready dict for kalshi_telegram to format

The research agent is intentionally verbose in its context — we want the AI to
have everything it needs to make a genuinely informed call, not just pattern-match.

5 analysis layers:
  1. Technical regime (ADX, BBW, trend structure from 1H candles)
  2. Funding sentiment (is the crowd crowded one way? → contrarian signal)
  3. Open interest (rising OI = healthy trend; falling OI = exhaustion)
  4. Momentum (24H price action, recent acceleration or deceleration)
  5. Calibration history (how has Golem done on this asset / similar setups?)
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

AI_MODEL   = "claude-haiku-4-5-20251001"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Minimum confidence to generate an alert (below this → skip)
MIN_ALERT_CONFIDENCE = 55


# ─── CONTEXT BUILDER ──────────────────────────────────────────────────────────

def _build_context(scored: dict, snapshot: dict, postmortem_summary: str = "") -> str:
    """
    Assemble the full context string passed to Claude.
    """
    ticker = scored["ticker"]
    title  = scored.get("title", ticker)
    price  = scored["price"]
    signal = scored["signal"]
    score  = scored["composite_score"]

    candles = snapshot.get("candles", [])
    recent  = candles[-12:] if len(candles) >= 12 else candles
    funding = snapshot.get("funding") or {}
    hist_f  = snapshot.get("hist_funding") or []

    price_summary = ""
    if recent:
        prices = [c["close"] for c in recent]
        price_summary = (
            f"Recent 12H closes: {[round(p, 4) for p in prices]}\n"
            f"High={max(prices):.4f}  Low={min(prices):.4f}  "
            f"Now={prices[-1]:.4f}"
        )

    funding_history = ""
    if hist_f:
        rates = [f"{r['rate']*100:+.4f}%" for r in hist_f[:6]]
        funding_history = f"Last 6 funding payments: {', '.join(rates)}"

    oi_now  = snapshot.get("open_interest", 0)
    oi_usd  = snapshot.get("open_interest_usd", 0)
    vol_usd = snapshot.get("volume_24h_usd", 0)

    context = f"""
=== KALSHI PERP MARKET ANALYSIS REQUEST ===

Market: {title} ({ticker})
Current Price: ${price:.4f}
24H Change: {scored['momentum_24h_pct']:+.2f}%
Leverage available: {scored.get('leverage_estimate') or 'N/A'}x

--- TECHNICAL SIGNAL ENGINE OUTPUT ---
Composite Score: {score:+d} / 100  (threshold: ±45 = actionable)
Preliminary Signal: {signal}
Trend Label: {scored['trend_label']}
ADX(14): {scored.get('adx') or 'N/A'}  (≥25 = confirmed trend, <20 = ranging)
Candles analyzed: {scored['candle_count']} × 1H

{price_summary}

--- FUNDING RATE (SENTIMENT INDICATOR) ---
Current Rate: {scored['funding_rate_8h_pct']:+.4f}% per 8H
Daily cost equivalent: {scored['funding_daily_pct']:+.4f}% per day
Sentiment reading: {scored['funding_sentiment']}
  (crowded_longs = longs paying shorts → bearish lean)
  (crowded_shorts = shorts paying longs → bullish lean)
  (balanced = neutral)
{funding_history}

--- OPEN INTEREST ---
Current OI: {oi_now:.2f} contracts (${oi_usd:,.0f} notional)
24H Volume: ${vol_usd:,.0f}
OI Trend: {scored['oi_trend']}
  (rising OI + price up = healthy bull; rising OI + price down = healthy bear)
  (falling OI = exhaustion / position unwinding)

--- LAYER-BY-LAYER BREAKDOWN ---
{json.dumps(scored.get('details', {}), indent=2)}

--- CALIBRATION HISTORY ---
{postmortem_summary if postmortem_summary else "No prior calls on this asset yet — first analysis."}

=== END CONTEXT ===
"""
    return context.strip()


SYSTEM_PROMPT = """You are Golem, an expert perpetual futures analyst embedded in a CFTC-regulated prediction market trading system (Kalshi perps). You analyze crypto perp markets and decide whether to bet UP, DOWN, or stay FLAT.

Your job is to synthesize 5 layers of evidence:
1. Trend structure (ADX + Bollinger Band Width — is there confirmed directional momentum?)
2. Funding rate sentiment (are longs or shorts overcrowded? → contrarian signal)
3. Open interest dynamics (rising OI confirms trend; falling OI = exhaustion)
4. 24H momentum (recent price action, acceleration/deceleration)
5. Calibration history (how has Golem done on similar setups?)

CRITICAL RULES:
- FLAT is a valid and often correct call. Do not force UP/DOWN.
- Funding rate is a contrarian indicator: high positive rate means longs are crowded → lean short (DOWN). High negative means shorts are crowded → lean long (UP).
- Rising OI with a directional move = healthy trend to ride. Falling OI = caution.
- ADX < 20 = ranging market. In ranging markets, prefer FLAT unless funding is extreme.
- Your confidence reflects how many layers align, not just the composite score.

Output MUST be valid JSON in exactly this format (no markdown, no commentary outside JSON):
{
  "verdict": "UP" | "DOWN" | "FLAT",
  "confidence": <integer 0-100>,
  "reasoning": "<plain English, 2-4 sentences explaining your call like you're texting a friend who bets on crypto. Use 'bet UP' / 'bet DOWN'. No jargon. Mention the key signals that drove the call.>",
  "key_risk": "<one sentence on the main thing that could make this call wrong>",
  "funding_cost_note": "<plain English on what the funding rate means in dollar terms for a $500 position, e.g. 'costs about $X/day to hold long'>",
  "suggested_leverage": <float — conservative leverage given current volatility, max 4.0>,
  "stop_pct": <float — suggested stop loss % from entry, e.g. 3.5>,
  "take_profit_pct": <float — suggested take profit % from entry, e.g. 7.0>
}"""


# ─── CLAUDE CALL ──────────────────────────────────────────────────────────────

def analyze_market(scored: dict, snapshot: dict, postmortem_summary: str = "") -> Optional[dict]:
    """
    Run Claude Sonnet 5 analysis on a single pre-scored market.

    Returns structured verdict dict or None on error.
    """
    if not ANTHROPIC_KEY:
        log.error("Kalshi research: no ANTHROPIC_API_KEY — cannot run analysis")
        return None

    context = _build_context(scored, snapshot, postmortem_summary)

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        response = client.messages.create(
            model=AI_MODEL,
            max_tokens=800,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": context}],
        )
        raw = response.content[0].text.strip()
        log.debug(f"Kalshi research raw response for {scored['ticker']}: {raw[:300]}")
    except Exception as e:
        log.error(f"Kalshi research: Claude API error for {scored['ticker']}: {e}")
        return None

    # Parse JSON — strip markdown fences if present
    try:
        json_str = raw
        if "```" in raw:
            m = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
            if m:
                json_str = m.group(1).strip()
        verdict = json.loads(json_str)
    except json.JSONDecodeError as e:
        log.error(f"Kalshi research: JSON parse error for {scored['ticker']}: {e}\nRaw: {raw}")
        return None

    # Validate required fields
    required = {"verdict", "confidence", "reasoning", "key_risk",
                "funding_cost_note", "suggested_leverage", "stop_pct", "take_profit_pct"}
    if not required.issubset(verdict.keys()):
        log.error(f"Kalshi research: missing fields in verdict for {scored['ticker']}: {verdict.keys()}")
        return None

    # Enforce confidence threshold
    confidence = int(verdict.get("confidence", 0))
    if confidence < MIN_ALERT_CONFIDENCE and verdict["verdict"] != "FLAT":
        log.info(f"Kalshi research: {scored['ticker']} confidence {confidence} < {MIN_ALERT_CONFIDENCE} — suppressing alert")
        return None

    return {
        "ticker":            scored["ticker"],
        "title":             scored.get("title", scored["ticker"]),
        "price":             scored["price"],
        "verdict":           verdict["verdict"],
        "confidence":        confidence,
        "reasoning":         verdict["reasoning"],
        "key_risk":          verdict["key_risk"],
        "funding_cost_note": verdict["funding_cost_note"],
        "suggested_leverage": float(verdict.get("suggested_leverage", 2.0)),
        "stop_pct":          float(verdict.get("stop_pct", 5.0)),
        "take_profit_pct":   float(verdict.get("take_profit_pct", 10.0)),
        # Signal layer data (for postmortem later)
        "composite_score":   scored["composite_score"],
        "trend_label":       scored["trend_label"],
        "adx":               scored.get("adx"),
        "funding_rate_8h":   scored["funding_rate_8h_pct"],
        "funding_sentiment": scored["funding_sentiment"],
        "oi_trend":          scored["oi_trend"],
        "momentum_24h":      scored["momentum_24h_pct"],
        "analyzed_at":       datetime.now(timezone.utc).isoformat(),
    }


# ─── BATCH SCAN ───────────────────────────────────────────────────────────────

def scan_all_viable(viable_scored: list[dict],
                    snapshots_by_ticker: dict,
                    postmortems_by_ticker: dict = None) -> list[dict]:
    """
    Run the research agent on every viable market candidate.

    viable_scored:          output of kalshi_signals.get_viable_signals()
    snapshots_by_ticker:    {ticker: snapshot_dict} from kalshi_data
    postmortems_by_ticker:  {ticker: summary_str} from kalshi_postmortem (optional)

    Returns list of verdict dicts for markets that pass the confidence threshold.
    Only UP and DOWN verdicts are returned (FLAT suppressed — nothing to trade).
    """
    if postmortems_by_ticker is None:
        postmortems_by_ticker = {}

    results = []
    for scored in viable_scored:
        ticker   = scored["ticker"]
        snapshot = snapshots_by_ticker.get(ticker)
        if not snapshot:
            log.warning(f"Kalshi research: no snapshot for {ticker} — skipping")
            continue

        pm_summary = postmortems_by_ticker.get(ticker, "")
        log.info(f"Kalshi research: analyzing {ticker} (score={scored['composite_score']:+d}) ...")
        verdict = analyze_market(scored, snapshot, pm_summary)

        if verdict and verdict["verdict"] in ("UP", "DOWN"):
            results.append(verdict)
            log.info(
                f"Kalshi research: {ticker} → {verdict['verdict']} "
                f"{verdict['confidence']}/100"
            )

    log.info(f"Kalshi research: scan complete — {len(results)} actionable signals found")
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json as _json
    from kalshi_data    import get_all_markets, get_full_market_snapshot
    from kalshi_signals import get_viable_signals

    markets   = get_all_markets()
    print(f"Loaded {len(markets)} markets. Fetching snapshots...")

    snapshots_by_ticker = {}
    snapshots = []
    for m in markets:
        snap = get_full_market_snapshot(m["ticker"])
        if snap:
            snapshots.append(snap)
            snapshots_by_ticker[m["ticker"]] = snap

    viable = get_viable_signals(snapshots)
    print(f"Viable signals: {[v['ticker'] for v in viable]}")

    if not viable:
        print("No viable signals to analyze.")
    else:
        verdicts = scan_all_viable(viable, snapshots_by_ticker)
        print(f"\n=== Research Verdicts ({len(verdicts)}) ===")
        for v in verdicts:
            print(f"\n{v['ticker']} — {v['verdict']} {v['confidence']}/100")
            print(f"  {v['reasoning']}")
            print(f"  Risk: {v['key_risk']}")
