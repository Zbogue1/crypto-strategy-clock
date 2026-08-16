#!/usr/bin/env python3
"""
kalshi_signals.py — Per-asset trend scoring for Kalshi perp markets.

Reuses the ADX+BBW engine from fomo_regime.py, adapted for Kalshi's
hourly candlestick data instead of Kraken 4H candles.

Signal layers:
  1. ADX(14) + Bollinger Band Width(20) on 1H candles → trend structure
  2. Funding rate sentiment → contrarian lean when crowded
  3. Open interest direction → trend confirmation or exhaustion
  4. 24H momentum → short-term price action

Output per asset:
  {
    ticker, trend_vote (+1/0/-1), trend_label, adx, direction_score,
    funding_sentiment, oi_trend, momentum_24h,
    composite_score (-100 to +100),
    signal ("UP" | "DOWN" | "FLAT"),
    confidence_contribution (0-40 points — fed into research agent)
  }
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ─── ADX / BBW PARAMETERS ─────────────────────────────────────────────────────
# Same values as fomo_regime.py v2

ADX_PERIOD        = 14
ADX_TREND_MIN     = 25.0
ADX_GRAY_MIN      = 20.0
BBW_PERIOD        = 20
BBW_MEDIAN_WINDOW = 50
MIN_CANDLES       = 80


# ─── PURE-PYTHON INDICATORS (mirrored from fomo_regime.py) ───────────────────

def _adx_di(candles_hlc: list, period: int = ADX_PERIOD) -> Optional[tuple]:
    """
    Wilder-smoothed ADX with +DI / -DI.
    candles_hlc: list of (high, low, close), oldest first.
    Returns (adx, plus_di, minus_di) or None.
    """
    n = len(candles_hlc)
    if n < period * 2 + 1:
        return None

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, n):
        hi, lo, _    = candles_hlc[i]
        phi, plo, pc = candles_hlc[i - 1]
        tr       = max(hi - lo, abs(hi - pc), abs(lo - pc))
        up_move  = hi - phi
        dn_move  = plo - lo
        plus_dm  = up_move  if (up_move  > dn_move  and up_move  > 0) else 0.0
        minus_dm = dn_move  if (dn_move  > up_move  and dn_move  > 0) else 0.0
        trs.append(tr)
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)

    atr    = sum(trs[:period])
    plus_s = sum(plus_dms[:period])
    minus_s= sum(minus_dms[:period])

    dxs = []
    plus_di = minus_di = 0.0
    for i in range(period, len(trs)):
        atr     = atr     - atr     / period + trs[i]
        plus_s  = plus_s  - plus_s  / period + plus_dms[i]
        minus_s = minus_s - minus_s / period + minus_dms[i]
        if atr <= 0:
            continue
        plus_di  = 100.0 * plus_s  / atr
        minus_di = 100.0 * minus_s / atr
        di_sum   = plus_di + minus_di
        if di_sum > 0:
            dxs.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if len(dxs) < period:
        return None

    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period

    return adx, plus_di, minus_di


def _bbw_series(closes: list, period: int = BBW_PERIOD) -> list:
    out = []
    for i in range(period - 1, len(closes)):
        window = closes[i - period + 1: i + 1]
        mean   = sum(window) / period
        var    = sum((c - mean) ** 2 for c in window) / period
        sd     = var ** 0.5
        out.append((4.0 * sd / mean) if mean > 0 else 0.0)
    return out


def _median(vals: list) -> float:
    s = sorted(vals)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


# ─── TREND VOTE ───────────────────────────────────────────────────────────────

def _trend_vote(candles_hlc: list) -> dict:
    """
    Same logic as fomo_regime._asset_vote(), adapted for any candle interval.
    Returns {vote, adx, plus_di, minus_di, bbw_expanding, label}.
    """
    closes = [c[2] for c in candles_hlc]

    result = {
        "vote": 0, "adx": None, "plus_di": None, "minus_di": None,
        "bbw_expanding": None, "label": "RANGING",
    }

    adx_out = _adx_di(candles_hlc)
    if adx_out is None:
        return result

    adx, plus_di, minus_di = adx_out
    result.update({
        "adx":      round(adx, 1),
        "plus_di":  round(plus_di, 1),
        "minus_di": round(minus_di, 1),
    })

    bbw = _bbw_series(closes)
    bbw_expanding = None
    if len(bbw) >= BBW_MEDIAN_WINDOW:
        bbw_expanding = bbw[-1] > _median(bbw[-BBW_MEDIAN_WINDOW:])
        result["bbw_expanding"] = bbw_expanding

    direction = 1 if plus_di > minus_di else -1

    if adx >= ADX_TREND_MIN:
        result["vote"]  = direction
        result["label"] = "UPTREND" if direction > 0 else "DOWNTREND"
    elif adx >= ADX_GRAY_MIN and bbw_expanding:
        result["vote"]  = direction
        result["label"] = "EARLY UPTREND" if direction > 0 else "EARLY DOWNTREND"
    else:
        result["label"] = "RANGING"

    return result


# ─── COMPOSITE SCORE ──────────────────────────────────────────────────────────

def score_asset(snapshot: dict) -> dict:
    """
    Score a single Kalshi perp market across 4 signal layers.

    snapshot: output of kalshi_data.get_full_market_snapshot()

    Scoring bands (composite -100 to +100, UP threshold ≥ 45, DOWN ≤ -45):

      Layer 1 — Trend structure (ADX+BBW):     ±40 pts
        UPTREND / DOWNTREND (ADX≥25):          ±40
        EARLY UP / EARLY DOWN (gray zone):     ±20
        RANGING:                                 0

      Layer 2 — Funding sentiment (contrarian):  ±25 pts
        crowded_longs  (rate > +0.1%/8H):       -25  (lean SHORT)
        crowded_longs  (rate > +0.05%/8H):      -12
        crowded_shorts (rate < -0.1%/8H):       +25  (lean LONG)
        crowded_shorts (rate < -0.05%/8H):      +12
        balanced:                                  0

      Layer 3 — Open interest direction:         ±20 pts
        rising OI + uptrend direction:          +20  (healthy trend)
        rising OI + downtrend direction:        -20
        falling OI (exhaustion regardless):      ±5 in counter direction
        flat:                                     0

      Layer 4 — 24H momentum:                   ±15 pts
        |change| > 5%:                           ±15
        |change| > 2%:                           ±8
        |change| > 0.5%:                         ±3
        flat:                                     0
    """
    ticker   = snapshot.get("ticker", "?")
    candles  = snapshot.get("candles", [])
    funding  = snapshot.get("funding") or {}
    oi_trend = snapshot.get("oi_trend", "unknown")
    mom_24h  = snapshot.get("price_24h_pct", 0.0)

    score    = 0
    details  = {}

    # ── Layer 1: Trend (ADX+BBW) ─────────────────────────────────────────────
    trend = {"vote": 0, "label": "NO_DATA"}
    if len(candles) >= MIN_CANDLES:
        hlc   = [(c["high"], c["low"], c["close"]) for c in candles]
        trend = _trend_vote(hlc)

    label = trend["label"]
    if label in ("UPTREND",):
        trend_pts = 40
    elif label in ("EARLY UPTREND",):
        trend_pts = 20
    elif label in ("DOWNTREND",):
        trend_pts = -40
    elif label in ("EARLY DOWNTREND",):
        trend_pts = -20
    else:
        trend_pts = 0

    score += trend_pts
    details["trend"] = {"label": label, "adx": trend.get("adx"), "pts": trend_pts}

    # ── Layer 2: Funding sentiment (contrarian) ───────────────────────────────
    fund_rate  = funding.get("funding_rate", 0.0) or 0.0
    fund_daily = funding.get("daily_pct", 0.0)    or 0.0
    sentiment  = funding.get("sentiment", "balanced")

    if fund_rate > 0.001:      # > 0.1%/8H — very crowded longs → short lean
        fund_pts = -25
    elif fund_rate > 0.0005:   # > 0.05%/8H
        fund_pts = -12
    elif fund_rate < -0.001:   # < -0.1%/8H — very crowded shorts → long lean
        fund_pts = +25
    elif fund_rate < -0.0005:
        fund_pts = +12
    else:
        fund_pts = 0

    score += fund_pts
    details["funding"] = {
        "rate_8h_pct": round(fund_rate * 100, 4),
        "daily_pct":   round(fund_daily, 4),
        "sentiment":   sentiment,
        "pts":         fund_pts,
    }

    # ── Layer 3: Open interest direction ─────────────────────────────────────
    trend_direction = trend.get("vote", 0)  # +1 = up, -1 = down, 0 = ranging

    if oi_trend == "rising":
        oi_pts = 20 * trend_direction if trend_direction != 0 else 5
    elif oi_trend == "falling":
        # Falling OI = exhaustion → slight counter-signal
        oi_pts = -5 * (trend_direction or 1)
    else:
        oi_pts = 0

    score += oi_pts
    details["oi"] = {"trend": oi_trend, "pts": oi_pts}

    # ── Layer 4: 24H momentum ─────────────────────────────────────────────────
    if abs(mom_24h) > 5.0:
        mom_pts = int(15 * (1 if mom_24h > 0 else -1))
    elif abs(mom_24h) > 2.0:
        mom_pts = int(8  * (1 if mom_24h > 0 else -1))
    elif abs(mom_24h) > 0.5:
        mom_pts = int(3  * (1 if mom_24h > 0 else -1))
    else:
        mom_pts = 0

    score += mom_pts
    details["momentum"] = {"pct_24h": round(mom_24h, 2), "pts": mom_pts}

    # ── Final signal ──────────────────────────────────────────────────────────
    score = max(-100, min(100, score))

    if score >= 45:
        signal = "UP"
    elif score <= -45:
        signal = "DOWN"
    else:
        signal = "FLAT"

    # Confidence contribution (0-40) — how strongly the technicals point one way
    confidence_contribution = min(40, int(abs(score) * 0.4))

    return {
        "ticker":                  ticker,
        "title":                   snapshot.get("title", ""),
        "price":                   snapshot.get("price", 0.0),
        "composite_score":         score,
        "signal":                  signal,
        "confidence_contribution": confidence_contribution,
        "trend_label":             label,
        "adx":                     trend.get("adx"),
        "funding_rate_8h_pct":     round(fund_rate * 100, 4),
        "funding_daily_pct":       round(fund_daily, 4),
        "funding_sentiment":       sentiment,
        "oi_trend":                oi_trend,
        "momentum_24h_pct":        round(mom_24h, 2),
        "details":                 details,
        "candle_count":            len(candles),
    }


def score_all_markets(snapshots: list[dict]) -> list[dict]:
    """Score a list of market snapshots. Returns scored list sorted by |score| desc."""
    scored = []
    for snap in snapshots:
        try:
            scored.append(score_asset(snap))
        except Exception as e:
            log.warning(f"Kalshi signals: error scoring {snap.get('ticker','?')}: {e}")
    scored.sort(key=lambda x: abs(x["composite_score"]), reverse=True)
    return scored


def get_viable_signals(snapshots: list[dict], min_score: int = 45) -> list[dict]:
    """
    Return only markets with |composite_score| >= min_score.
    These are the candidates sent to the research agent for deep analysis.
    """
    scored = score_all_markets(snapshots)
    return [s for s in scored if abs(s["composite_score"]) >= min_score]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json as _json
    from kalshi_data import get_all_markets, get_full_market_snapshot

    markets   = get_all_markets()
    print(f"Fetching snapshots for {len(markets)} markets...")
    snapshots = []
    for m in markets:
        snap = get_full_market_snapshot(m["ticker"])
        if snap:
            snapshots.append(snap)

    viable = get_viable_signals(snapshots)
    print(f"\n=== Viable signals ({len(viable)}) ===")
    for v in viable:
        print(f"  {v['ticker']:30s}  {v['signal']:4s}  score={v['composite_score']:+4d}  "
              f"trend={v['trend_label']}  funding={v['funding_rate_8h_pct']:+.3f}%/8H")
