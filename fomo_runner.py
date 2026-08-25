#!/usr/bin/env python3
"""
fomo_runner.py — Let the final tranche run when the evidence says run.

THE PROBLEM
CATE hit 2x, then 3x, and both tranches fired correctly. The final third —
the piece whose entire job is to catch a move like that — exited on a 30%
drop from peak. For a memecoin mid-run, 30% is ordinary noise, not a trend
break. The tranches worked; the runner got shaken out.

WHY THE FINAL TRANCHE IS DIFFERENT
By the time it's the only piece left, 66% has already been sold at 2x and 3x.
The original stake is back with profit on top. What's at risk is winnings, not
capital. That asymmetry is real and it justifies a looser stop HERE and
nowhere else — this logic must never touch a position that hasn't already
banked its tranches.

TWO INPUTS
  MANUAL   — screenshots you forward. If the trader you copied says they're
             still holding, that's information the chart doesn't have yet.
  GATHERED — on-chain momentum. Buy/sell transaction ratio, volume
             acceleration, liquidity being added rather than pulled. Hype has
             a measurable signature before it has a price.

THE GUARDRAILS, AND WHY EACH EXISTS
A system that can talk itself out of a stop-loss is how a winning trade
becomes a losing one. So:

  · Only the final tranche. Requires tranche_2_sold.
  · Widen, never remove. Hard ceiling at MAX_TRAILING.
  · Extensions expire and must be re-earned on fresh evidence.
  · A liquidity collapse is NEVER overridden. Rug detection outranks
    everything here — if the exit door is closing, conviction is irrelevant.
  · Never let a 3x winner round-trip to break-even. If price falls to the
    profit floor, exit regardless of how bullish anything looks.

That last rule is the one that matters most. Every argument for holding
sounds good right up until it doesn't.
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

# Base trailing stop is 30% (fomo_exit.TRAILING_STOP_PCT). Strong evidence can
# widen it to this, and no further.
MAX_TRAILING       = float(os.getenv("FOMO_RUNNER_MAX_TRAILING", "0.50"))
# An extension is worth this long before the evidence must be re-established.
EXTENSION_HOURS    = float(os.getenv("FOMO_RUNNER_EXTENSION_H", "6"))
# Hype score (0-100) needed before the stop widens at all.
MIN_HYPE_SCORE     = float(os.getenv("FOMO_RUNNER_MIN_HYPE", "60"))
# Never let the runner give back more than this share of its gain over entry.
PROFIT_FLOOR_MULT  = float(os.getenv("FOMO_RUNNER_PROFIT_FLOOR", "1.5"))
# A forwarded screenshot counts as fresh evidence for this long.
NOTE_FRESH_HOURS   = float(os.getenv("FOMO_RUNNER_NOTE_HOURS", "12"))


# ─── GATHERED: on-chain momentum ──────────────────────────────────────────────

def gather_momentum(contract: str) -> dict:
    """
    Measure hype from on-chain activity.

    Returns {"score": 0-100, "signals": [...], "available": bool}.

    Hype leaves a signature before it shows in price: buyers outnumbering
    sellers, volume accelerating against its own recent average, and liquidity
    being ADDED rather than withdrawn. The last one is the most honest — it
    costs money to fake.
    """
    out = {"score": 0.0, "signals": [], "available": False}
    if not contract:
        return out

    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            timeout=15)
        if r.status_code != 200:
            out["signals"].append(f"DexScreener HTTP {r.status_code}")
            return out
        pairs = (r.json() or {}).get("pairs") or []
    except Exception as e:
        out["signals"].append(f"fetch failed: {e}")
        return out

    if not pairs:
        out["signals"].append("no pairs returned")
        return out

    p = max(pairs, key=lambda x: float((x.get("liquidity") or {}).get("usd", 0) or 0))
    out["available"] = True
    score = 0.0

    # 1. Buy/sell transaction ratio over the last hour. More buyers than
    #    sellers is the cleanest live read on demand.
    txns = (p.get("txns") or {}).get("h1") or {}
    buys, sells = int(txns.get("buys", 0) or 0), int(txns.get("sells", 0) or 0)
    if buys + sells >= 20:
        ratio = buys / max(sells, 1)
        if ratio >= 2.0:
            score += 30; out["signals"].append(f"buyers {ratio:.1f}x sellers (1h)")
        elif ratio >= 1.3:
            score += 15; out["signals"].append(f"buyers leading {ratio:.1f}x (1h)")
        elif ratio <= 0.7:
            score -= 20; out["signals"].append(f"sellers leading ({ratio:.1f}x) (1h)")
    else:
        out["signals"].append("too few transactions to read demand")

    # 2. Volume acceleration — last hour against the 24h hourly average.
    vol = p.get("volume") or {}
    v1, v24 = float(vol.get("h1", 0) or 0), float(vol.get("h24", 0) or 0)
    if v24 > 0:
        accel = v1 / (v24 / 24)
        if accel >= 3.0:
            score += 30; out["signals"].append(f"volume {accel:.1f}x its daily average")
        elif accel >= 1.5:
            score += 15; out["signals"].append(f"volume {accel:.1f}x average")
        elif accel < 0.4:
            score -= 15; out["signals"].append(f"volume fading ({accel:.1f}x)")

    # 3. Price structure. Rising over 6h with a positive last hour is a trend;
    #    a spike that's already reversing is not.
    ch = p.get("priceChange") or {}
    c1, c6 = float(ch.get("h1", 0) or 0), float(ch.get("h6", 0) or 0)
    if c6 > 20 and c1 > 0:
        score += 25; out["signals"].append(f"up {c6:.0f}% over 6h, still rising")
    elif c6 > 0 and c1 > 5:
        score += 15; out["signals"].append(f"up {c1:.0f}% this hour")
    elif c1 < -10:
        score -= 25; out["signals"].append(f"down {c1:.0f}% this hour")

    # 4. Liquidity direction. Adding LP costs real money, so it's the hardest
    #    signal to fake — and its opposite is the first sign of a rug.
    liq = float((p.get("liquidity") or {}).get("usd", 0) or 0)
    out["liquidity"] = liq
    if liq >= 100_000:
        score += 15; out["signals"].append(f"deep liquidity ${liq:,.0f}")
    elif liq < 20_000:
        score -= 30; out["signals"].append(f"thin liquidity ${liq:,.0f}")

    out["score"] = max(0.0, min(100.0, score))
    return out


# ─── MANUAL: your forwarded screenshots ───────────────────────────────────────

def read_manual_context(holding: dict) -> dict:
    """
    Recent screenshot intel attached to this position.

    _handle_screenshot writes social_notes onto the holding. Only recent notes
    count — a trader's conviction from four days ago says nothing about now.
    """
    out = {"stance": None, "conviction": None, "quote": "", "fresh": False}
    notes = holding.get("social_notes") or []
    if not notes:
        return out

    cutoff = datetime.now(timezone.utc) - timedelta(hours=NOTE_FRESH_HOURS)
    recent = []
    for n in notes:
        try:
            if datetime.fromisoformat(str(n.get("at", "")).replace("Z", "+00:00")) >= cutoff:
                recent.append(n)
        except Exception:
            pass
    if not recent:
        return out

    latest = recent[-1]
    return {"stance": latest.get("stance"), "conviction": latest.get("conviction"),
            "quote": (latest.get("quote") or "")[:200], "fresh": True,
            "poster": latest.get("poster"), "at": latest.get("at")}


# ─── THE DECISION ─────────────────────────────────────────────────────────────

def evaluate_runner(holding: dict, current_price: float, peak: float,
                    base_trailing: float, current_liq: float = 0.0) -> dict:
    """
    Should the final tranche get a wider leash?

    Returns {"trailing_pct", "extended", "reason", "hype", "manual", "hard_exit"}.
    `hard_exit` True means exit NOW regardless of anything else.
    """
    entry = float(holding.get("entry_price") or 0)
    result = {"trailing_pct": base_trailing, "extended": False,
              "reason": "", "hype": None, "manual": None, "hard_exit": False}

    # GUARD 1: final tranche only. Anything still holding 66% has its capital
    # at risk and gets no leniency.
    if not holding.get("tranche_2_sold"):
        result["reason"] = "not the final tranche — standard stop applies"
        return result

    # GUARD 2: never round-trip a winner. If price has fallen to the profit
    # floor, exit regardless of how good the story is. This check comes before
    # any evidence is even gathered, so nothing can argue past it.
    if entry and current_price <= entry * PROFIT_FLOOR_MULT:
        result["hard_exit"] = True
        result["reason"] = (f"price fell to {current_price/entry:.1f}x entry — "
                            f"profit floor {PROFIT_FLOOR_MULT:.1f}x breached, "
                            f"exiting regardless of momentum")
        return result

    # GUARD 3: liquidity collapse always wins. A closing exit door makes
    # conviction irrelevant.
    if current_liq and current_liq < 20_000:
        result["hard_exit"] = True
        result["reason"] = f"liquidity down to ${current_liq:,.0f} — exit door closing"
        return result

    # An extension already granted and still valid
    ext_until = holding.get("runner_extension_until")
    if ext_until:
        try:
            if datetime.fromisoformat(ext_until.replace("Z", "+00:00")) > \
               datetime.now(timezone.utc):
                result["trailing_pct"] = float(
                    holding.get("runner_trailing_pct", base_trailing))
                result["extended"] = True
                result["reason"] = f"extension active until {ext_until[:16]}"
                return result
        except Exception:
            pass

    # Gather fresh evidence
    hype   = gather_momentum(holding.get("contract_address", ""))
    manual = read_manual_context(holding)
    result["hype"], result["manual"] = hype, manual

    score = hype.get("score", 0.0)

    # Your screenshot is evidence the chart doesn't have. It can add, but it
    # cannot carry the decision alone — a trader saying "hold" while volume
    # dies and liquidity drains is exactly when copying them costs money.
    if manual["fresh"]:
        if manual["stance"] in ("HOLDING", "BULLISH"):
            bump = 25 if manual["conviction"] == "high" else 15
            score += bump
            hype.setdefault("signals", []).append(
                f"you forwarded: trader {manual['stance']} "
                f"({manual['conviction']} conviction)")
        elif manual["stance"] in ("EXITING", "BEARISH"):
            # Bearish manual context TIGHTENS rather than merely failing to
            # loosen. If the person we copy is getting out, so should we.
            result["trailing_pct"] = base_trailing * 0.6
            result["reason"] = (f"you forwarded: trader is {manual['stance']} — "
                                f"tightening stop to "
                                f"{result['trailing_pct']*100:.0f}%")
            return result

    score = max(0.0, min(100.0, score))
    hype["score"] = score

    if score < MIN_HYPE_SCORE:
        result["reason"] = (f"hype {score:.0f}/100 below {MIN_HYPE_SCORE:.0f} — "
                            f"standard {base_trailing*100:.0f}% stop")
        return result

    # Scale the widening with the strength of the evidence, capped hard.
    span   = MAX_TRAILING - base_trailing
    frac   = (score - MIN_HYPE_SCORE) / (100.0 - MIN_HYPE_SCORE)
    widened = min(MAX_TRAILING, base_trailing + span * frac)

    result.update({
        "trailing_pct": widened, "extended": True,
        "reason": (f"hype {score:.0f}/100 — stop widened "
                   f"{base_trailing*100:.0f}% -> {widened*100:.0f}% "
                   f"for {EXTENSION_HOURS:.0f}h"),
    })
    return result


def apply_extension(holding: dict, decision: dict):
    """Record the extension on the holding so it survives the next cycle."""
    if not decision.get("extended"):
        return
    holding["runner_trailing_pct"]   = decision["trailing_pct"]
    holding["runner_extension_until"] = (
        datetime.now(timezone.utc) + timedelta(hours=EXTENSION_HOURS)).isoformat()
    holding.setdefault("runner_extensions", []).append({
        "at":      datetime.now(timezone.utc).isoformat(),
        "trailing": decision["trailing_pct"],
        "hype":    (decision.get("hype") or {}).get("score"),
        "reason":  decision["reason"][:200],
    })


def format_extension(ticker: str, decision: dict, gain_x: float) -> str:
    hype   = decision.get("hype") or {}
    manual = decision.get("manual") or {}
    L = [f"🏃 <b>{ticker} — runner given more room</b>", "",
         f"Currently <b>{gain_x:.1f}x</b> entry. Final third only — "
         f"2x and 3x already banked.",
         f"Trailing stop widened to <b>{decision['trailing_pct']*100:.0f}%</b> "
         f"for {EXTENSION_HOURS:.0f}h.", ""]
    if hype.get("signals"):
        L.append(f"<b>Hype {hype.get('score',0):.0f}/100</b>")
        for s in hype["signals"][:5]:
            L.append(f"  · {s}")
        L.append("")
    if manual.get("fresh"):
        L += [f"📷 Your screenshot: {manual.get('stance')} "
              f"({manual.get('conviction')})", f"  \"{manual.get('quote','')[:120]}\"", ""]
    L.append(f"<i>Only winnings are at risk here. Hard floor still stands: "
             f"if it falls to {PROFIT_FLOOR_MULT:.1f}x entry it exits "
             f"regardless.</i>")
    return "\n".join(L)
