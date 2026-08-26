#!/usr/bin/env python3
"""
stock_signals.py — Ross Cameron's methodology as deterministic rules.

Two independent layers:

  LAYER 1 — THE 5 PILLARS (stock selection)
      Which stocks are worth watching at all. Needs ≥4 of 5 to qualify as
      "A quality". Source: Stock Selection Guide + Small Account Strategy PDFs.

  LAYER 2 — THE FIRST PULLBACK PATTERN (entry timing)
      When to actually buy one of those stocks. Source: the class transcript,
      where the entry mechanics are spelled out but the PDFs stop short.

Deliberately rule-based, not AI. An LLM prompted to "trade like Ross" drifts;
a function that returns False when the stock retraced 62% does not. The AI
layer sits on top of this and can only veto, never override.

See STOCK_GOLEM_STRATEGY.md for the full extracted methodology and sourcing.
"""

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# ─── PILLAR THRESHOLDS ────────────────────────────────────────────────────────
MIN_RVOL          = float(os.getenv("STOCK_MIN_RVOL", "5.0"))
MIN_PCT_CHANGE    = float(os.getenv("STOCK_MIN_PCT", "10.0"))
PRICE_MIN         = float(os.getenv("STOCK_PRICE_MIN", "1.0"))
PRICE_MAX         = float(os.getenv("STOCK_PRICE_MAX", "20.0"))
# Small-account variant narrows to $5-10 (no leverage under $5)
SMALL_ACCT_MIN    = float(os.getenv("STOCK_SMALL_PRICE_MIN", "5.0"))
SMALL_ACCT_MAX    = float(os.getenv("STOCK_SMALL_PRICE_MAX", "10.0"))
# Ross's $5-10 band is a SMALL-ACCOUNT constraint: on $2,000 you need a price
# low enough to buy a meaningful share count and high enough to have real
# spreads. Stock Golem now runs $10,000, so that band just discards most of the
# day's movers — it was the second-biggest rejector (15 of 30) with names like
# AIXI +186% and BTA +154% screened out on price alone.
#
# Set STOCK_SMALL_ACCOUNT=true to restore the tighter band.
USE_SMALL_ACCT    = os.getenv("STOCK_SMALL_ACCOUNT", "false").lower() == "true"
FLOAT_MAX_HOT_M   = float(os.getenv("STOCK_FLOAT_MAX_HOT", "20.0"))
FLOAT_MAX_COLD_M  = float(os.getenv("STOCK_FLOAT_MAX_COLD", "10.0"))
MIN_PILLARS       = int(os.getenv("STOCK_MIN_PILLARS", "4"))

# ─── PULLBACK PARAMETERS ──────────────────────────────────────────────────────
MAX_RETRACE_PCT   = float(os.getenv("STOCK_MAX_RETRACE", "50.0"))
EMA_PERIOD        = int(os.getenv("STOCK_EMA_PERIOD", "9"))
MIN_SURGE_CANDLES = int(os.getenv("STOCK_MIN_SURGE", "2"))


# ─── INDICATORS (pure python, no numpy) ───────────────────────────────────────

def ema(values: list, period: int) -> list:
    """Exponential moving average, same length as input (None until seeded)."""
    if not values or len(values) < period:
        return [None] * len(values)
    out = [None] * (period - 1)
    seed = sum(values[:period]) / period
    out.append(seed)
    k = 2 / (period + 1)
    prev = seed
    for v in values[period:]:
        prev = v * k + prev * (1 - k)
        out.append(prev)
    return out


def session_vwap(bars: list) -> list:
    """
    Running VWAP from session open. Ross uses VWAP as a hard floor — a pullback
    that breaks it is invalid regardless of how good it otherwise looks.
    """
    out, cum_pv, cum_v = [], 0.0, 0
    for b in bars:
        typical = (b["h"] + b["l"] + b["c"]) / 3
        cum_pv += typical * b["v"]
        cum_v  += b["v"]
        out.append(cum_pv / cum_v if cum_v else b["c"])
    return out


# ─── LAYER 1: THE 5 PILLARS ───────────────────────────────────────────────────

def score_pillars(snap: dict, market_hot: bool = True) -> dict:
    """
    Evaluate a snapshot against the 5 Pillars.

    Returns pillar-by-pillar detail plus a pass/fail. Unknown data (typically
    float) scores as UNKNOWN, not FAIL — a missing lookup shouldn't silently
    reject a good setup, but it also shouldn't count toward the ≥4 threshold.
    """
    pillars = {}

    # 1 — Relative volume ≥5x
    rvol = snap.get("rvol")
    if rvol is None:
        pillars["rvol"] = {"pass": None, "value": None,
                           "note": "RVOL unavailable"}
    else:
        pillars["rvol"] = {
            "pass":  rvol >= MIN_RVOL,
            "value": rvol,
            "note":  f"{rvol:.1f}x vs {MIN_RVOL:.0f}x required",
        }

    # 2 — Already up ≥10%
    pct = snap.get("pct_change")
    pillars["momentum"] = {
        "pass":  (pct is not None and pct >= MIN_PCT_CHANGE),
        "value": pct,
        "note":  f"{pct:+.1f}% today vs +{MIN_PCT_CHANGE:.0f}% required"
                 if pct is not None else "no change data",
    }

    # 3 — News catalyst.
    # Not merely "is there news" — a dilutive offering IS news and moves the
    # stock, but it's the reason to FADE the move, not join it. If catalyst
    # analysis ran, use its verdict; otherwise fall back to headline presence.
    cat = snap.get("catalyst")
    if cat:
        harmful = cat.get("quality") == "harmful"
        pillars["catalyst"] = {
            "pass":  bool(cat.get("passes")) and not harmful,
            "value": cat.get("score"),
            "note":  f"{cat.get('catalyst_type','?')} — {cat.get('reasoning','')[:90]}",
            "harmful": harmful,
        }
    else:
        n_news = snap.get("news_count", 0) or 0
        pillars["catalyst"] = {
            "pass":  n_news > 0,
            "value": n_news,
            "note":  f"{n_news} headline(s) in 48h (unanalyzed)" if n_news
                     else "no catalyst found — higher risk of sudden drop",
            "harmful": False,
        }

    # 4 — Price range
    price = snap.get("price") or 0
    lo, hi = (SMALL_ACCT_MIN, SMALL_ACCT_MAX) if USE_SMALL_ACCT else (PRICE_MIN, PRICE_MAX)
    pillars["price"] = {
        "pass":  lo <= price <= hi,
        "value": price,
        "note":  f"${price:.2f} vs ${lo:.0f}-${hi:.0f} range",
    }

    # 5 — Float
    float_m = snap.get("float_m")
    cap = FLOAT_MAX_HOT_M if market_hot else FLOAT_MAX_COLD_M
    if float_m is None:
        pillars["float"] = {"pass": None, "value": None,
                            "note": "float unavailable — treat as unknown"}
    else:
        pillars["float"] = {
            "pass":  float_m <= cap,
            "value": float_m,
            "note":  f"{float_m:.1f}M vs <{cap:.0f}M ({'hot' if market_hot else 'cold'} market)",
        }

    passed  = sum(1 for p in pillars.values() if p["pass"] is True)
    failed  = sum(1 for p in pillars.values() if p["pass"] is False)
    unknown = sum(1 for p in pillars.values() if p["pass"] is None)

    # A dilution or distress catalyst is DISQUALIFYING, not merely a failed
    # pillar. A stock can pass 4 of 5 while the reason it moved is an offering
    # that's about to be sold into — that's a fade setup wearing a breakout's
    # clothes. Hard veto regardless of how strong the other pillars look.
    harmful_catalyst = bool(pillars.get("catalyst", {}).get("harmful"))

    return {
        "symbol":     snap.get("symbol", "?"),
        "pillars":    pillars,
        "passed":     passed,
        "failed":     failed,
        "unknown":    unknown,
        "qualifies":  passed >= MIN_PILLARS and not harmful_catalyst,
        "disqualified_by": "harmful catalyst (dilution/distress)" if harmful_catalyst else "",
        "grade":      "F" if harmful_catalyst else
                      ("A" if passed == 5 else ("B" if passed == 4 else "C")),
    }


# ─── LAYER 2: THE FIRST PULLBACK PATTERN ──────────────────────────────────────

def detect_pullback(bars: list) -> Optional[dict]:
    """
    Find a valid first-pullback setup on the most recent bars.

    Ross's four validity conditions, all of which must hold:
      1. Retraces ≤50% of the prior surge
      2. Volume higher on the surge (green) than the pullback (red)
      3. Does NOT break below VWAP
      4. Does NOT break below the 9 EMA

    Entry trigger is the CROSSING CANDLE — the first candle to make a new high
    above the previous candle's high. We report readiness rather than firing,
    because the crossing candle may not have formed yet.

    Stop = the low of the pullback.
    """
    if len(bars) < EMA_PERIOD + 6:
        return None

    closes = [b["c"] for b in bars]
    emas   = ema(closes, EMA_PERIOD)
    vwaps  = session_vwap(bars)

    cur      = bars[-1]
    cur_ema  = emas[-1]
    cur_vwap = vwaps[-1]
    if cur_ema is None:
        return None

    # Walk back to find the surge → pullback structure.
    #
    # The final candle may already be turning up — that's the potential CROSSING
    # candle and must not be counted as part of the pullback, otherwise the
    # structure never resolves and detection silently returns nothing.
    prev = bars[-2]
    last_is_crossing = cur["h"] > prev["h"] and cur["c"] > cur["o"]
    scan_from = len(bars) - 2 if last_is_crossing else len(bars) - 1

    # Pullback = most recent run of non-advancing candles before that point
    i = scan_from
    pullback = []
    while i > 0 and bars[i]["c"] <= bars[i - 1]["c"]:
        pullback.append(bars[i])
        i -= 1
    if not pullback:
        return None          # no pullback structure — nothing to trade
    pullback.reverse()

    # Surge = the advancing run that preceded the pullback
    surge = []
    j = i
    while j > 0 and bars[j]["c"] >= bars[j - 1]["c"]:
        surge.append(bars[j])
        j -= 1
    surge.reverse()

    if len(surge) < MIN_SURGE_CANDLES:
        return None

    surge_low  = min(b["l"] for b in surge)
    surge_high = max(b["h"] for b in surge)
    move       = surge_high - surge_low
    if move <= 0:
        return None

    pb_low  = min(b["l"] for b in pullback)
    retrace = (surge_high - pb_low) / move * 100

    surge_vol = sum(b["v"] for b in surge) / max(len(surge), 1)
    pb_vol    = sum(b["v"] for b in pullback) / max(len(pullback), 1)

    checks = {
        "retrace_ok": {
            "pass": retrace <= MAX_RETRACE_PCT,
            "note": f"retraced {retrace:.0f}% of the move (max {MAX_RETRACE_PCT:.0f}%)",
        },
        "volume_ok": {
            "pass": surge_vol > pb_vol,
            "note": f"surge avg {surge_vol:,.0f} vs pullback avg {pb_vol:,.0f}/bar",
        },
        "above_vwap": {
            "pass": pb_low >= cur_vwap * 0.999,
            "note": f"pullback low ${pb_low:.2f} vs VWAP ${cur_vwap:.2f}",
        },
        "above_ema": {
            "pass": pb_low >= cur_ema * 0.999,
            "note": f"pullback low ${pb_low:.2f} vs {EMA_PERIOD}EMA ${cur_ema:.2f}",
        },
    }

    valid = all(c["pass"] for c in checks.values())

    # Entry trigger — the crossing candle, determined above
    crossing = last_is_crossing
    # Buy the break of the last pullback candle's high
    entry = cur["c"] if crossing else pullback[-1]["h"]
    stop  = pb_low
    risk  = entry - stop
    target = entry + (risk * 2)                   # 2:1 minimum

    return {
        "valid":          valid,
        "checks":         checks,
        "crossing_candle": crossing,
        "ready":          valid and crossing,
        "surge_candles":  len(surge),
        "pullback_candles": len(pullback),
        "surge_high":     round(surge_high, 4),
        "pullback_low":   round(pb_low, 4),
        "retrace_pct":    round(retrace, 1),
        "entry":          round(entry, 4),
        "stop":           round(stop, 4),
        "target":         round(target, 4),
        "risk_per_share": round(risk, 4),
        "vwap":           round(cur_vwap, 4),
        "ema":            round(cur_ema, 4),
        "price":          round(cur["c"], 4),
    }


# ─── EXIT INDICATORS (the automatable subset) ─────────────────────────────────

def check_exit_signals(bars: list) -> list:
    """
    Of Ross's six exit indicators, four need Level 2 / time & sales, which the
    free feed doesn't carry. These two are computable from OHLCV:

      #4 dramatic reversal → topping tail + false breakout
      #6 topping tail candle or red candle forming

    Documented as a known gap in BEFORE_REAL_MONEY.md — the missing four are
    the discretionary tape reading, which is plausibly where much of the real
    edge lives.
    """
    if len(bars) < 3:
        return []

    signals = []
    cur = bars[-1]

    body  = abs(cur["c"] - cur["o"])
    upper = cur["h"] - max(cur["c"], cur["o"])
    rng   = cur["h"] - cur["l"]

    # #6a — topping tail: upper wick ≥2x the body on a meaningful range
    if rng > 0 and body > 0 and upper >= body * 2:
        signals.append({
            "indicator": "topping_tail",
            "severity":  "high",
            "note":      f"upper wick {upper:.3f} vs body {body:.3f} — bearish rejection",
        })

    # #6b — red candle after an advance
    if cur["c"] < cur["o"] and bars[-2]["c"] > bars[-2]["o"]:
        signals.append({
            "indicator": "red_candle",
            "severity":  "medium",
            "note":      "first red candle after green — momentum stalling",
        })

    # #4 — false breakout: made a new high then closed back below it
    prev_high = max(b["h"] for b in bars[-6:-1])
    if cur["h"] > prev_high and cur["c"] < prev_high:
        signals.append({
            "indicator": "false_breakout",
            "severity":  "high",
            "note":      f"broke {prev_high:.3f} then closed back under — failed breakout",
        })

    # #5 proxy — volume decay across the last three bars
    if len(bars) >= 4:
        v1, v2, v3 = bars[-3]["v"], bars[-2]["v"], bars[-1]["v"]
        if v1 > v2 > v3 and v3 < v1 * 0.5:
            signals.append({
                "indicator": "volume_decay",
                "severity":  "medium",
                "note":      f"volume fading {v1:,}→{v3:,} — buying interest drying up",
            })

    return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Synthetic surge → shallow pullback → crossing candle
    bars = []
    price = 5.00
    for i in range(12):
        price += 0.02
        bars.append({"o": price-0.02, "h": price+0.01, "l": price-0.03, "c": price, "v": 40000})
    for i in range(6):   # surge
        price += 0.10
        bars.append({"o": price-0.10, "h": price+0.02, "l": price-0.11, "c": price, "v": 250000})
    for i in range(3):   # shallow pullback on light volume
        price -= 0.05
        bars.append({"o": price+0.05, "h": price+0.06, "l": price-0.01, "c": price, "v": 60000})
    price += 0.12        # crossing candle
    bars.append({"o": price-0.12, "h": price+0.03, "l": price-0.13, "c": price, "v": 180000})

    import json
    print(json.dumps(detect_pullback(bars), indent=2))
