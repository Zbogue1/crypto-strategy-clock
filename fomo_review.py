#!/usr/bin/env python3
"""
fomo_review.py — Re-decide a stale position instead of timing it out.

THE PROBLEM THIS REPLACES
FOMO had no time-based exit running at all, so a token drifting at -20% never
hit the -35% stop, never hit the 2x tranche, and held capital indefinitely.
The obvious fix is a timer: sell anything older than N days. That fix is worse
than the disease. Memecoins routinely go nowhere for a week and then move —
a hard 24h timer would have closed GTA6 before it hit 2x and again before 3x.

A timer sells on the calendar. What should drive the decision is whether the
reason for buying is still true.

WHAT A REVIEW ACTUALLY LOOKS AT
  1. THE ORIGINAL THESIS. The catalyst was recorded at entry. Does it still
     hold, or did we buy a narrative that has since died?
  2. WHAT THE MARKET DID. Liquidity, volume and holder trend since entry.
     Falling liquidity on a flat price is a slow rug in progress.
  3. THE TRADER WE COPIED. If this was a copy trade, what have they said
     since? "Still holding" and "I'm out" are opposite information and neither
     is a trade signal, so the existing social parser discards both.
  4. THEIR TRACK RECORD. Does this wallet's history show positions that
     recover from drawdown, or ones that keep bleeding?

WHAT IT DOES NOT DO
Sell anything. Every review ends in an alert with a button. The system has
never auto-sold and this does not change that.

MISSING DATA IS NOT BEARISH DATA
If the Twitter lookup fails, that is reported as unavailable, never as
"the trader is quiet, so cut it". A missing integration must not manufacture
a reason to sell.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

try:
    import anthropic
except Exception:                                    # pragma: no cover
    anthropic = None

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL      = os.getenv("FOMO_REVIEW_MODEL", "claude-sonnet-4-5")

# A position becomes reviewable when it's BOTH old enough that the thesis has
# had time to play out AND underwater enough that it isn't quietly working.
REVIEW_AFTER_DAYS   = float(os.getenv("FOMO_REVIEW_DAYS", "3"))
REVIEW_BELOW_PCT    = float(os.getenv("FOMO_REVIEW_PCT", "-15"))
# Don't re-review the same position daily — it becomes noise and gets ignored.
REVIEW_COOLDOWN_H   = float(os.getenv("FOMO_REVIEW_COOLDOWN_H", "48"))


def _age_days(holding: dict) -> float:
    ts = holding.get("entered_at") or ""
    if not ts:
        return 0.0
    try:
        entered = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return 0.0
    return (datetime.now(timezone.utc) - entered).total_seconds() / 86400


def needs_review(holding: dict, current_price: float) -> tuple:
    """
    Is this position stale enough to re-decide? Returns (bool, why).

    Both conditions must hold. An old position that's up 40% is working; a
    day-old position that's down 20% is normal volatility. It's the
    combination — time has passed AND it hasn't worked — that says the thesis
    deserves re-examination.
    """
    entry = float(holding.get("entry_price") or 0)
    if not entry or not current_price:
        return False, "no price"

    age = _age_days(holding)
    if age < REVIEW_AFTER_DAYS:
        return False, f"only {age:.1f}d old"

    pct = (current_price - entry) / entry * 100
    if pct > REVIEW_BELOW_PCT:
        return False, f"{pct:+.0f}% — not underwater enough"

    last = holding.get("last_reviewed_at") or ""
    if last:
        try:
            prev = datetime.fromisoformat(last.replace("Z", "+00:00"))
            hrs  = (datetime.now(timezone.utc) - prev).total_seconds() / 3600
            if hrs < REVIEW_COOLDOWN_H:
                return False, f"reviewed {hrs:.0f}h ago"
        except Exception:
            pass

    return True, f"{age:.1f}d old at {pct:+.0f}%"


def gather_context(holding: dict, current_price: float,
                   current_liq: float = 0.0) -> dict:
    """Assemble everything the decision should be based on."""
    entry = float(holding.get("entry_price") or 0)
    pct   = ((current_price - entry) / entry * 100) if entry else 0.0

    ctx = {
        "ticker":        holding.get("token_ticker", "?"),
        "contract":      holding.get("contract_address", ""),
        "age_days":      round(_age_days(holding), 1),
        "entry_price":   entry,
        "current_price": current_price,
        "pct":           round(pct, 1),
        "spent":         float(holding.get("spent") or 0),
        "value_now":     round(float(holding.get("units") or 0) * current_price, 2),
        # The thesis, as recorded at the moment we bought
        "entry_catalyst":       holding.get("catalyst", ""),
        "entry_catalyst_score": holding.get("catalyst_score"),
        "entry_liquidity":      float(holding.get("liquidity_usd") or 0),
        "entry_market_cap":     float(holding.get("market_cap") or 0),
        "current_liquidity":    current_liq,
        "wallet_alias":         holding.get("wallet_alias", ""),
        "source":               holding.get("source", ""),
        "tranche_1_sold":       bool(holding.get("tranche_1_sold")),
    }

    # Liquidity trend — the single most reliable tell. Price can hold up while
    # the exit door is being quietly bricked over.
    if ctx["entry_liquidity"] and current_liq:
        ctx["liq_change_pct"] = round(
            (current_liq - ctx["entry_liquidity"]) / ctx["entry_liquidity"] * 100, 1)
    else:
        ctx["liq_change_pct"] = None

    # What has the trader we copied said since?
    ctx["social"] = {"available": False, "reason": "not attempted"}
    alias = ctx["wallet_alias"]
    if alias:
        try:
            from fomo_social import (fetch_trader_context,
                                     summarize_trader_sentiment,
                                     get_trader_handle)
            handle = get_trader_handle(alias)
            found  = fetch_trader_context(handle, ctx["ticker"])
            ctx["social"] = found
            if found.get("available") and found.get("posts"):
                ctx["social"]["sentiment"] = summarize_trader_sentiment(
                    found["posts"], ctx["ticker"])
        except Exception as e:
            ctx["social"] = {"available": False, "reason": f"lookup failed: {e}"}

    # Does this wallet's history show recoveries, or continued bleeding?
    try:
        from fomo_portfolio import get_wallet_lessons
        ctx["wallet_lessons"] = get_wallet_lessons(alias) if alias else {}
    except Exception as e:
        ctx["wallet_lessons"] = {"error": str(e)}

    return ctx


_SYSTEM = (
    "You are reviewing an open, underwater memecoin position to decide whether "
    "the original reason for buying still holds.\n\n"
    "Judge the THESIS, not the price. A position being down is why you are "
    "looking; it is not by itself a reason to sell. Equally, refusing to sell "
    "a dead token because it 'might come back' is how small losses become "
    "total ones.\n\n"
    "Weigh most heavily:\n"
    "- Liquidity trend. Falling liquidity while price holds is a slow rug. "
    "This outranks everything else.\n"
    "- Whether the entry catalyst has played out, died, or is still pending.\n"
    "- What the copied trader has actually said, if anything.\n\n"
    "If social data was unavailable, treat it as UNKNOWN. Do not infer "
    "bearishness from an absent integration.\n\n"
    "Respond with JSON only."
)


def review_position(holding: dict, current_price: float,
                    current_liq: float = 0.0) -> dict:
    """
    Re-decide a stale position. Returns a verdict dict; never sells.

    verdict: HOLD | TRIM | EXIT | UNCLEAR
    """
    ctx = gather_context(holding, current_price, current_liq)

    if not ANTHROPIC_KEY or anthropic is None:
        return {**ctx, "verdict": "UNCLEAR", "confidence": 0,
                "reasoning": "No ANTHROPIC_API_KEY — cannot run the review.",
                "thesis_status": "unknown"}

    social = ctx.get("social", {})
    if social.get("available") and social.get("sentiment"):
        s = social["sentiment"]
        social_line = (f"Trader @{ctx['wallet_alias']} stance: {s.get('stance')} "
                       f"({s.get('confidence')} confidence) — {s.get('summary')}"
                       + (f" Quote: \"{s['key_quote']}\"" if s.get("key_quote") else ""))
    else:
        social_line = (f"UNAVAILABLE — {social.get('reason', 'unknown')}. "
                       f"Treat as unknown, not as bearish.")

    liq_line = (f"{ctx['liq_change_pct']:+.0f}% since entry"
                if ctx["liq_change_pct"] is not None else "unknown")

    prompt = (
        f"POSITION: {ctx['ticker']}\n"
        f"Held {ctx['age_days']} days, currently {ctx['pct']:+.1f}%\n"
        f"Put in ${ctx['spent']:.2f}, now worth ${ctx['value_now']:.2f}\n"
        f"Took first tranche already: {ctx['tranche_1_sold']}\n\n"
        f"WHY WE BOUGHT (recorded at entry):\n"
        f"{ctx['entry_catalyst'] or '(none recorded)'}\n"
        f"Catalyst score at entry: {ctx['entry_catalyst_score']}\n\n"
        f"MARKET SINCE:\n"
        f"Liquidity: ${ctx['entry_liquidity']:,.0f} -> ${ctx['current_liquidity']:,.0f} "
        f"({liq_line})\n"
        f"Market cap at entry: ${ctx['entry_market_cap']:,.0f}\n\n"
        f"COPIED TRADER:\n{social_line}\n\n"
        f"THAT TRADER'S RECORD:\n{json.dumps(ctx.get('wallet_lessons', {}))[:600]}\n\n"
        "Respond JSON only:\n"
        '{"verdict": "HOLD" or "TRIM" or "EXIT" or "UNCLEAR", '
        '"confidence": 0-100, '
        '"thesis_status": "intact" or "played_out" or "dead" or "pending", '
        '"reasoning": "2-3 sentences on why", '
        '"key_factor": "the single thing driving this call", '
        '"what_would_change_it": "what you would need to see to flip this"}'
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=AI_MODEL, max_tokens=600, system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        verdict = json.loads(raw.strip())
    except Exception as e:
        log.error(f"FOMO review: AI call failed for {ctx['ticker']}: {e}")
        return {**ctx, "verdict": "UNCLEAR", "confidence": 0,
                "reasoning": f"Review failed: {e}", "thesis_status": "unknown"}

    if verdict.get("verdict") not in ("HOLD", "TRIM", "EXIT", "UNCLEAR"):
        verdict["verdict"] = "UNCLEAR"
    return {**ctx, **verdict}


def format_review(r: dict) -> str:
    """Telegram-ready HTML summary of a review."""
    icon = {"HOLD": "🟢", "TRIM": "🟡", "EXIT": "🔴"}.get(r.get("verdict"), "⚪")
    liq = (f"{r['liq_change_pct']:+.0f}%" if r.get("liq_change_pct") is not None
           else "unknown")

    social = r.get("social", {})
    if social.get("sentiment"):
        s = social["sentiment"]
        social_txt = f"{s.get('stance')} — {s.get('summary','')[:120]}"
    else:
        social_txt = f"<i>unavailable: {social.get('reason','?')[:70]}</i>"

    return (
        f"{icon} <b>POSITION REVIEW: {r['ticker']}</b>\n\n"
        f"Held <b>{r['age_days']}d</b> at <b>{r['pct']:+.0f}%</b>  ·  "
        f"${r['spent']:.0f} in, ${r['value_now']:.0f} now\n"
        f"Liquidity since entry: <b>{liq}</b>\n\n"
        f"<b>Verdict: {r.get('verdict')}</b> ({r.get('confidence',0)}/100)\n"
        f"Thesis: <b>{r.get('thesis_status','?')}</b>\n\n"
        f"💬 {r.get('reasoning','')[:400]}\n\n"
        f"🔑 {r.get('key_factor','')[:180]}\n"
        f"🔄 Would change if: {r.get('what_would_change_it','')[:180]}\n\n"
        f"👤 Trader ({r.get('wallet_alias','?')}): {social_txt}\n\n"
        f"<i>Nothing sold. This is a recommendation.</i>"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    demo = {"token_ticker": "TEST", "entry_price": 1.0, "units": 100,
            "spent": 100.0, "entered_at": "2026-08-15T00:00:00+00:00",
            "catalyst": "Listed on a major CEX, volume spiking",
            "catalyst_score": 8, "liquidity_usd": 200000,
            "market_cap": 5_000_000, "wallet_alias": "demo"}
    ok, why = needs_review(demo, 0.75)
    print(f"needs_review -> {ok} ({why})")
    print(json.dumps(gather_context(demo, 0.75, 90000), indent=2, default=str)[:900])
