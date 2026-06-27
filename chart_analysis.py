#!/usr/bin/env python3
"""
chart_analysis.py — Technical chart pattern analysis for Crypto Strategy Clock.

Reads CoinGecko OHLC data (90 daily candles) and produces a structured analysis
covering candlestick patterns, chart patterns, support/resistance levels, trend,
moving average signals, and volume behavior.

The output dict is injected directly into Claude's context so it can reason about
the chart like an experienced trader would looking at a price graph.
"""

from typing import List, Dict, Tuple, Optional


# ─── MASTER FUNCTION ─────────────────────────────────────────────────────────

def analyze_chart(ohlc: list, ticker: str = "") -> dict:
    """
    Run full chart analysis on CoinGecko OHLC data.

    ohlc: list of [timestamp, open, high, low, close]  (or [..., volume])
    Returns a dict with all patterns, levels, and a human-readable summary.
    """
    if not ohlc or len(ohlc) < 10:
        return {"ticker": ticker, "error": "Insufficient OHLC data", "summary": "", "signal_bias": 0}

    opens  = [float(row[1]) for row in ohlc]
    highs  = [float(row[2]) for row in ohlc]
    lows   = [float(row[3]) for row in ohlc]
    closes = [float(row[4]) for row in ohlc]
    vols   = [float(row[5]) if len(row) > 5 else 0.0 for row in ohlc]

    candle_patterns = detect_candlestick_patterns(opens, highs, lows, closes)
    chart_patterns  = detect_chart_patterns(highs, lows, closes)
    sr_levels       = find_support_resistance(highs, lows, closes)
    trend_info      = analyze_trend(highs, lows, closes)
    ma_info         = analyze_moving_averages(closes)
    vol_info        = analyze_volume(closes, vols) if any(vols) else {}

    result = {
        "ticker":               ticker,
        "candles_analyzed":     len(ohlc),
        "candlestick_patterns": candle_patterns,
        "chart_patterns":       chart_patterns,
        "support_resistance":   sr_levels,
        "trend":                trend_info,
        "moving_averages":      ma_info,
        "volume_analysis":      vol_info,
    }

    result["signal_bias"] = _compute_signal_bias(result)
    result["summary"]     = _build_summary(result, ticker, closes[-1])
    return result


# ─── CANDLESTICK PATTERNS ─────────────────────────────────────────────────────

def detect_candlestick_patterns(opens: List[float], highs: List[float],
                                 lows: List[float], closes: List[float]) -> list:
    """
    Detect named candlestick patterns in the most recent candles.
    Returns a list of dicts: {pattern, bias, strength, description}
    """
    patterns = []
    n = len(closes)
    if n < 3:
        return patterns

    def body(i):    return abs(closes[i] - opens[i])
    def candle_range(i): return highs[i] - lows[i]
    def upper_wick(i): return highs[i] - max(opens[i], closes[i])
    def lower_wick(i): return min(opens[i], closes[i]) - lows[i]
    def is_bull(i): return closes[i] > opens[i]
    def is_bear(i): return closes[i] < opens[i]

    # ── Single-candle patterns (last 3 candles) ──────────────────────────────
    for i in range(max(0, n-3), n):
        b  = body(i)
        cr = candle_range(i)
        if cr == 0:
            continue
        uw = upper_wick(i)
        lw = lower_wick(i)

        # Doji: body tiny relative to range
        if b <= 0.05 * cr:
            patterns.append({
                "pattern":     "Doji",
                "bias":        "neutral",
                "strength":    "moderate",
                "candle_idx":  i,
                "description": f"Indecision candle — open and close nearly identical. Market is undecided."
            })

        # Hammer / Inverted Hammer (at candle i, need prior context)
        elif lw >= 2.0 * b and uw <= 0.3 * b and is_bull(i):
            # Check for recent downtrend
            prior_closes = closes[max(0, i-5):i]
            if prior_closes and prior_closes[-1] > closes[i]:
                patterns.append({
                    "pattern":     "Hammer",
                    "bias":        "bullish",
                    "strength":    "strong",
                    "candle_idx":  i,
                    "description": "Long lower wick after a decline — buyers rejected the lows. Classic reversal signal."
                })

        # Shooting Star (bearish)
        elif uw >= 2.0 * b and lw <= 0.3 * b and is_bear(i):
            prior_closes = closes[max(0, i-5):i]
            if prior_closes and prior_closes[-1] < closes[i - 1]:
                patterns.append({
                    "pattern":     "Shooting Star",
                    "bias":        "bearish",
                    "strength":    "strong",
                    "candle_idx":  i,
                    "description": "Long upper wick after a rally — sellers rejected the highs. Reversal warning."
                })

        # Marubozu (full conviction body, tiny wicks)
        elif b >= 0.85 * cr:
            bias = "bullish" if is_bull(i) else "bearish"
            patterns.append({
                "pattern":     f"{'Bullish' if is_bull(i) else 'Bearish'} Marubozu",
                "bias":        bias,
                "strength":    "strong",
                "candle_idx":  i,
                "description": f"Near-zero wicks — pure {'buying' if is_bull(i) else 'selling'} conviction with no pushback from the other side."
            })

    # ── Two-candle patterns (last candle vs previous) ────────────────────────
    if n >= 2:
        i = n - 1
        # Bullish Engulfing
        if (is_bear(i-1) and is_bull(i) and
                opens[i] <= closes[i-1] and closes[i] >= opens[i-1] and
                body(i) > body(i-1)):
            patterns.append({
                "pattern":     "Bullish Engulfing",
                "bias":        "bullish",
                "strength":    "strong",
                "candle_idx":  i,
                "description": "Today's bullish candle completely swallows yesterday's bearish one. Strong buying pressure taking over."
            })

        # Bearish Engulfing
        elif (is_bull(i-1) and is_bear(i) and
                opens[i] >= closes[i-1] and closes[i] <= opens[i-1] and
                body(i) > body(i-1)):
            patterns.append({
                "pattern":     "Bearish Engulfing",
                "bias":        "bearish",
                "strength":    "strong",
                "candle_idx":  i,
                "description": "Today's bearish candle completely swallows yesterday's bullish one. Sellers took firm control."
            })

        # Piercing Line (bullish)
        elif (is_bear(i-1) and is_bull(i) and
                opens[i] < lows[i-1] and closes[i] > (opens[i-1] + closes[i-1]) / 2):
            patterns.append({
                "pattern":     "Piercing Line",
                "bias":        "bullish",
                "strength":    "moderate",
                "candle_idx":  i,
                "description": "Price gapped down then rallied to pierce above the previous candle's midpoint. Buyers regaining ground."
            })

        # Dark Cloud Cover (bearish)
        elif (is_bull(i-1) and is_bear(i) and
                opens[i] > highs[i-1] and closes[i] < (opens[i-1] + closes[i-1]) / 2):
            patterns.append({
                "pattern":     "Dark Cloud Cover",
                "bias":        "bearish",
                "strength":    "moderate",
                "candle_idx":  i,
                "description": "Price gapped up then reversed to close below the previous midpoint. Bears reclaiming the rally."
            })

    # ── Three-candle patterns ────────────────────────────────────────────────
    if n >= 3:
        i = n - 1
        b1, b2, b3 = body(i-2), body(i-1), body(i)

        # Morning Star (bullish reversal)
        if (is_bear(i-2) and b1 > 0.5 * candle_range(i-2) and
                b2 < 0.3 * b1 and                     # small middle body
                is_bull(i) and                         # third is bullish
                closes[i] > (opens[i-2] + closes[i-2]) / 2):
            patterns.append({
                "pattern":     "Morning Star",
                "bias":        "bullish",
                "strength":    "very strong",
                "candle_idx":  i,
                "description": "Three-candle bullish reversal: big down, tiny indecision, big up reclaiming the loss. Classic bottom signal."
            })

        # Evening Star (bearish reversal)
        elif (is_bull(i-2) and b1 > 0.5 * candle_range(i-2) and
                b2 < 0.3 * b1 and
                is_bear(i) and
                closes[i] < (opens[i-2] + closes[i-2]) / 2):
            patterns.append({
                "pattern":     "Evening Star",
                "bias":        "bearish",
                "strength":    "very strong",
                "candle_idx":  i,
                "description": "Three-candle top reversal: big up, tiny pause, big down erasing the gains. Classic peak signal."
            })

        # Three White Soldiers (strong bullish momentum)
        elif (is_bull(i-2) and is_bull(i-1) and is_bull(i) and
                closes[i] > closes[i-1] > closes[i-2] and
                opens[i-1] > opens[i-2] and opens[i] > opens[i-1] and
                b3 > 0.6 * candle_range(i)):
            patterns.append({
                "pattern":     "Three White Soldiers",
                "bias":        "bullish",
                "strength":    "strong",
                "candle_idx":  i,
                "description": "Three consecutive strong bullish candles closing near their highs. Sustained buying pressure — trend likely accelerating up."
            })

        # Three Black Crows (strong bearish momentum)
        elif (is_bear(i-2) and is_bear(i-1) and is_bear(i) and
                closes[i] < closes[i-1] < closes[i-2] and
                opens[i-1] < opens[i-2] and opens[i] < opens[i-1] and
                b3 > 0.6 * candle_range(i)):
            patterns.append({
                "pattern":     "Three Black Crows",
                "bias":        "bearish",
                "strength":    "strong",
                "candle_idx":  i,
                "description": "Three consecutive strong bearish candles closing near their lows. Heavy selling pressure — trend likely accelerating down."
            })

    return patterns


# ─── CHART PATTERNS ───────────────────────────────────────────────────────────

def detect_chart_patterns(highs: List[float], lows: List[float],
                           closes: List[float]) -> list:
    """
    Detect multi-candle chart patterns: double top/bottom, head & shoulders,
    flags, triangles. Uses the full 90-candle history.
    """
    patterns = []
    n = len(closes)
    if n < 20:
        return patterns

    # ── Find significant peaks and troughs ───────────────────────────────────
    peaks   = _find_peaks(highs,  window=5)
    troughs = _find_peaks([-l for l in lows], window=5)  # invert for troughs

    # ── Double Top ────────────────────────────────────────────────────────────
    if len(peaks) >= 2:
        p1_i, p1_v = peaks[-2]
        p2_i, p2_v = peaks[-1]
        # Two peaks within 3% of each other, at least 10 candles apart
        if (abs(p1_v - p2_v) / max(p1_v, p2_v) < 0.03 and
                p2_i - p1_i >= 10):
            # Find trough between them
            between_lows = lows[p1_i:p2_i]
            neckline = min(between_lows) if between_lows else None
            if neckline and closes[-1] < p1_v * 0.97:
                patterns.append({
                    "pattern":    "Double Top",
                    "bias":       "bearish",
                    "strength":   "strong",
                    "price_level": round(p1_v, 4),
                    "neckline":   round(neckline, 4),
                    "description": (
                        f"Price hit resistance near ${p1_v:,.4f} twice and failed to break higher. "
                        f"Classic reversal — neckline support at ${neckline:,.4f}. "
                        f"A break below neckline would confirm bearish target."
                    )
                })

    # ── Double Bottom ─────────────────────────────────────────────────────────
    if len(troughs) >= 2:
        t1_i, t1_v = troughs[-2]
        t2_i, t2_v = troughs[-1]
        t1_low = -t1_v
        t2_low = -t2_v
        if (abs(t1_low - t2_low) / max(t1_low, t2_low) < 0.03 and
                t2_i - t1_i >= 10):
            between_highs = highs[t1_i:t2_i]
            neckline = max(between_highs) if between_highs else None
            if neckline and closes[-1] > t1_low * 1.03:
                patterns.append({
                    "pattern":    "Double Bottom",
                    "bias":       "bullish",
                    "strength":   "strong",
                    "price_level": round(t1_low, 4),
                    "neckline":   round(neckline, 4),
                    "description": (
                        f"Price found support near ${t1_low:,.4f} twice without breaking lower. "
                        f"Classic reversal — neckline resistance at ${neckline:,.4f}. "
                        f"A break above neckline confirms bullish target."
                    )
                })

    # ── Head and Shoulders (bearish) ─────────────────────────────────────────
    if len(peaks) >= 3:
        ls_i, ls_v = peaks[-3]  # left shoulder
        hd_i, hd_v = peaks[-2]  # head
        rs_i, rs_v = peaks[-1]  # right shoulder
        if (hd_v > ls_v and hd_v > rs_v and
                abs(ls_v - rs_v) / max(ls_v, rs_v) < 0.05):
            neckline = min(lows[ls_i:rs_i]) if ls_i < rs_i else None
            if neckline and closes[-1] < hd_v * 0.95:
                patterns.append({
                    "pattern":    "Head and Shoulders",
                    "bias":       "bearish",
                    "strength":   "very strong",
                    "price_level": round(hd_v, 4),
                    "neckline":   round(neckline, 4) if neckline else None,
                    "description": (
                        f"Classic H&S top: left shoulder at ${ls_v:,.4f}, "
                        f"head at ${hd_v:,.4f}, right shoulder at ${rs_v:,.4f}. "
                        f"Neckline at ${neckline:,.4f}. Very high probability reversal if neckline breaks."
                    )
                })

    # ── Inverse Head and Shoulders (bullish) ─────────────────────────────────
    if len(troughs) >= 3:
        ls_i, ls_v = troughs[-3]
        hd_i, hd_v = troughs[-2]
        rs_i, rs_v = troughs[-1]
        ls_low = -ls_v; hd_low = -hd_v; rs_low = -rs_v
        if (hd_low < ls_low and hd_low < rs_low and
                abs(ls_low - rs_low) / max(ls_low, rs_low) < 0.05):
            neckline = max(highs[ls_i:rs_i]) if ls_i < rs_i else None
            if neckline and closes[-1] > hd_low * 1.05:
                patterns.append({
                    "pattern":    "Inverse Head and Shoulders",
                    "bias":       "bullish",
                    "strength":   "very strong",
                    "price_level": round(hd_low, 4),
                    "neckline":   round(neckline, 4) if neckline else None,
                    "description": (
                        f"Inverse H&S bottom: head at ${hd_low:,.4f}, shoulders at "
                        f"${ls_low:,.4f} / ${rs_low:,.4f}. Neckline at ${neckline:,.4f}. "
                        f"Strong reversal signal — a close above neckline confirms the pattern."
                    )
                })

    # ── Bull Flag ─────────────────────────────────────────────────────────────
    if n >= 20:
        # Look for sharp pole (>10% rise in 5-10 candles) then tight consolidation
        pole_end = n - 8
        if pole_end > 5:
            pole_rise = (closes[pole_end] - closes[pole_end - 7]) / closes[pole_end - 7] * 100
            if pole_rise > 10:
                flag_closes = closes[pole_end:]
                flag_range  = (max(flag_closes) - min(flag_closes)) / closes[pole_end] * 100
                flag_drift  = (flag_closes[-1] - flag_closes[0]) / closes[pole_end] * 100
                if flag_range < 8 and -8 < flag_drift < 2:
                    patterns.append({
                        "pattern":    "Bull Flag",
                        "bias":       "bullish",
                        "strength":   "moderate",
                        "description": (
                            f"Sharp {pole_rise:.0f}% rally (flagpole) followed by tight "
                            f"{flag_range:.1f}% consolidation. Classic continuation — "
                            f"breakout would typically target a move equal to the flagpole."
                        )
                    })

    # ── Bear Flag ─────────────────────────────────────────────────────────────
    if n >= 20:
        pole_end = n - 8
        if pole_end > 5:
            pole_drop = (closes[pole_end - 7] - closes[pole_end]) / closes[pole_end - 7] * 100
            if pole_drop > 10:
                flag_closes = closes[pole_end:]
                flag_range  = (max(flag_closes) - min(flag_closes)) / closes[pole_end] * 100
                flag_drift  = (flag_closes[-1] - flag_closes[0]) / closes[pole_end] * 100
                if flag_range < 8 and -2 < flag_drift < 8:
                    patterns.append({
                        "pattern":    "Bear Flag",
                        "bias":       "bearish",
                        "strength":   "moderate",
                        "description": (
                            f"Sharp {pole_drop:.0f}% drop (flagpole) followed by tight "
                            f"{flag_range:.1f}% bounce. Likely continuation lower — "
                            f"breakdown targets a move equal to the original drop."
                        )
                    })

    # ── Ascending Triangle ────────────────────────────────────────────────────
    if n >= 20:
        recent_highs = highs[-20:]
        recent_lows  = lows[-20:]
        # Flat resistance: recent highs cluster within 2%
        high_max = max(recent_highs)
        high_min = min(recent_highs)
        high_spread = (high_max - high_min) / high_max
        # Rising lows: lows are trending up
        low_start = recent_lows[0]
        low_end   = recent_lows[-1]
        rising_lows = low_end > low_start * 1.03
        if high_spread < 0.03 and rising_lows:
            patterns.append({
                "pattern":    "Ascending Triangle",
                "bias":       "bullish",
                "strength":   "moderate",
                "price_level": round(high_max, 4),
                "description": (
                    f"Flat resistance at ~${high_max:,.4f} with rising support. "
                    f"Buyers pushing harder each dip. Bullish breakout likely on volume."
                )
            })

    # ── Descending Triangle ───────────────────────────────────────────────────
    if n >= 20:
        recent_highs = highs[-20:]
        recent_lows  = lows[-20:]
        low_max  = max(recent_lows)
        low_min  = min(recent_lows)
        low_spread = (low_max - low_min) / low_max
        high_start = recent_highs[0]
        high_end   = recent_highs[-1]
        falling_highs = high_end < high_start * 0.97
        if low_spread < 0.03 and falling_highs:
            patterns.append({
                "pattern":    "Descending Triangle",
                "bias":       "bearish",
                "strength":   "moderate",
                "price_level": round(low_min, 4),
                "description": (
                    f"Flat support at ~${low_min:,.4f} with falling resistance. "
                    f"Sellers pressing down each rally. Bearish breakdown likely."
                )
            })

    return patterns


# ─── SUPPORT AND RESISTANCE ───────────────────────────────────────────────────

def find_support_resistance(highs: List[float], lows: List[float],
                             closes: List[float]) -> dict:
    """
    Find key price levels where the market has repeatedly reacted.
    Returns nearest support below current price and resistance above it.
    """
    current = closes[-1]
    all_levels = []

    # Collect all local highs and lows as candidate levels
    peaks   = _find_peaks(highs,  window=4)
    troughs = _find_peaks([-l for l in lows], window=4)

    for _, v in peaks:
        all_levels.append(v)
    for _, v in troughs:
        all_levels.append(-v)

    # Also add round numbers ±5% of current price
    magnitude = 10 ** (len(str(int(current))) - 2)
    for mult in range(-5, 6):
        level = round(current / magnitude) * magnitude + mult * magnitude
        if level > 0:
            all_levels.append(float(level))

    # Cluster close levels (within 1%)
    levels = _cluster_levels(all_levels, tolerance=0.015)

    # Separate into support (below) and resistance (above)
    supports    = sorted([l for l in levels if l < current * 0.995], reverse=True)
    resistances = sorted([l for l in levels if l > current * 1.005])

    # Distance as % from current price
    def pct_away(level):
        return round((level - current) / current * 100, 2)

    result = {
        "current_price": round(current, 4),
        "key_supports": [
            {"price": round(s, 4), "pct_away": pct_away(s)}
            for s in supports[:3]
        ],
        "key_resistances": [
            {"price": round(r, 4), "pct_away": pct_away(r)}
            for r in resistances[:3]
        ],
    }

    # Tag nearest levels with risk/reward context
    if result["key_supports"] and result["key_resistances"]:
        nearest_sup = result["key_supports"][0]["price"]
        nearest_res = result["key_resistances"][0]["price"]
        risk  = abs(pct_away(nearest_sup))
        rwd   = abs(pct_away(nearest_res))
        result["risk_reward"] = round(rwd / risk, 2) if risk > 0 else None
        result["nearest_support"]    = round(nearest_sup, 4)
        result["nearest_resistance"] = round(nearest_res, 4)

    return result


# ─── TREND ANALYSIS ───────────────────────────────────────────────────────────

def analyze_trend(highs: List[float], lows: List[float],
                  closes: List[float]) -> dict:
    """
    Determine trend direction, strength, and key characteristics.
    """
    n = len(closes)
    if n < 10:
        return {"direction": "unknown", "strength": "unknown"}

    # ── Short-term trend (last 10 candles) ───────────────────────────────────
    short_highs = highs[-10:]
    short_lows  = lows[-10:]
    short_hh = short_highs[-1] > max(short_highs[:-1])   # new high?
    short_hl = short_lows[-1]  > min(short_lows[:-1])    # higher low?
    short_lh = short_highs[-1] < max(short_highs[:-1])   # lower high?
    short_ll = short_lows[-1]  < min(short_lows[:-1])    # new low?

    # ── Medium-term trend (last 30 candles) ──────────────────────────────────
    mid_closes = closes[-30:] if n >= 30 else closes
    mid_trend  = "up" if mid_closes[-1] > mid_closes[0] * 1.02 else \
                 "down" if mid_closes[-1] < mid_closes[0] * 0.98 else "sideways"

    # ── Long-term trend (full 90 candles) ────────────────────────────────────
    long_trend = "up" if closes[-1] > closes[0] * 1.05 else \
                 "down" if closes[-1] < closes[0] * 0.95 else "sideways"

    # ── Higher highs / higher lows analysis ──────────────────────────────────
    peaks   = _find_peaks(highs, window=5)
    troughs = _find_peaks([-l for l in lows], window=5)

    hh_hl = False  # uptrend structure
    lh_ll = False  # downtrend structure

    if len(peaks) >= 2:
        hh_hl = peaks[-1][1] > peaks[-2][1]  # higher highs
        lh_ll = peaks[-1][1] < peaks[-2][1]  # lower highs
    if len(troughs) >= 2:
        hl = -troughs[-1][1] > -troughs[-2][1]  # higher lows
        ll = -troughs[-1][1] < -troughs[-2][1]  # lower lows
        hh_hl = hh_hl and hl
        lh_ll = lh_ll and ll

    # ── Consolidation detection ───────────────────────────────────────────────
    recent_closes = closes[-15:] if n >= 15 else closes
    price_range = (max(recent_closes) - min(recent_closes)) / min(recent_closes) * 100
    is_consolidating = price_range < 8

    # ── Direction verdict ─────────────────────────────────────────────────────
    if hh_hl:
        direction = "uptrend"
        strength  = "strong"
    elif lh_ll:
        direction = "downtrend"
        strength  = "strong"
    elif mid_trend == "up":
        direction = "uptrend"
        strength  = "moderate"
    elif mid_trend == "down":
        direction = "downtrend"
        strength  = "moderate"
    elif is_consolidating:
        direction = "consolidating"
        strength  = "neutral"
    else:
        direction = "mixed"
        strength  = "weak"

    # ── Momentum: recent close % change ──────────────────────────────────────
    mom_5d  = round((closes[-1] - closes[-6]) / closes[-6] * 100, 2) if n >= 6 else None
    mom_20d = round((closes[-1] - closes[-21]) / closes[-21] * 100, 2) if n >= 21 else None

    return {
        "direction":         direction,
        "strength":          strength,
        "short_term":        mid_trend,
        "long_term":         long_trend,
        "higher_highs_lows": hh_hl,
        "lower_highs_lows":  lh_ll,
        "consolidating":     is_consolidating,
        "price_range_15d_pct": round(price_range, 1),
        "momentum_5d_pct":   mom_5d,
        "momentum_20d_pct":  mom_20d,
        "description": (
            f"{'Strong uptrend with higher highs and higher lows' if hh_hl else 'Strong downtrend with lower highs and lower lows' if lh_ll else 'Price is consolidating' if is_consolidating else f'{direction.capitalize()} trend'}. "
            f"5-day momentum: {mom_5d:+.1f}%" if mom_5d else ""
        )
    }


# ─── MOVING AVERAGE ANALYSIS ─────────────────────────────────────────────────

def analyze_moving_averages(closes: List[float]) -> dict:
    """
    Compute SMA 20/50/200, detect golden/death cross, slope, and structure.
    """
    n = len(closes)

    def sma(period):
        if n < period:
            return None
        return sum(closes[-period:]) / period

    def sma_slope(period, lookback=5):
        """Slope of SMA over last N candles. Positive = rising."""
        if n < period + lookback:
            return None
        s1 = sum(closes[-(period + lookback):-lookback]) / period
        s2 = sum(closes[-period:]) / period
        return round((s2 - s1) / s1 * 100, 2)

    sma20  = sma(20)
    sma50  = sma(50)
    sma200 = sma(200) if n >= 200 else sma(min(90, n))
    current = closes[-1]

    result = {
        "sma_20":  round(sma20,  4) if sma20  else None,
        "sma_50":  round(sma50,  4) if sma50  else None,
        "sma_200": round(sma200, 4) if sma200 else None,
        "price_vs_sma20":  round((current - sma20)  / sma20  * 100, 2) if sma20  else None,
        "price_vs_sma50":  round((current - sma50)  / sma50  * 100, 2) if sma50  else None,
        "price_vs_sma200": round((current - sma200) / sma200 * 100, 2) if sma200 else None,
        "sma20_slope":  sma_slope(20),
        "sma50_slope":  sma_slope(50),
    }

    # Golden / Death Cross detection
    if sma50 and sma200:
        if n >= 51:
            prev_sma50  = sum(closes[-51:-1]) / 50
            prev_sma200 = sum(closes[-min(201, n):-1]) / min(200, n - 1) if n > 1 else sma200
            if prev_sma50 < prev_sma200 and sma50 > sma200:
                result["cross_signal"] = "golden_cross"
                result["cross_description"] = "GOLDEN CROSS just formed — 50 MA crossed above 200 MA. Historically the strongest long-term buy signal."
            elif prev_sma50 > prev_sma200 and sma50 < sma200:
                result["cross_signal"] = "death_cross"
                result["cross_description"] = "DEATH CROSS just formed — 50 MA crossed below 200 MA. Major long-term sell/avoid signal."
            elif sma50 > sma200:
                result["cross_signal"] = "above_200"
                result["cross_description"] = f"50 MA is above 200 MA — price is in a bull structure ({round((sma50 - sma200) / sma200 * 100, 1)}% above)."
            else:
                result["cross_signal"] = "below_200"
                result["cross_description"] = f"50 MA is below 200 MA — price is in a bear structure ({round((sma200 - sma50) / sma200 * 100, 1)}% below)."

    # Alignment score: how many MAs is price above?
    above_count = sum([
        1 if sma20  and current > sma20  else 0,
        1 if sma50  and current > sma50  else 0,
        1 if sma200 and current > sma200 else 0,
    ])
    result["ma_alignment"] = above_count
    result["ma_alignment_description"] = {
        0: "Price is below all major moving averages — bearish structure.",
        1: "Price is above one MA — weak / mixed structure.",
        2: "Price is above two MAs — moderately bullish structure.",
        3: "Price is above all three MAs — strongly bullish structure.",
    }.get(above_count, "")

    return result


# ─── VOLUME ANALYSIS ─────────────────────────────────────────────────────────

def analyze_volume(closes: List[float], volumes: List[float]) -> dict:
    """
    Analyze volume patterns: OBV trend, volume on up vs down days,
    breakout volume detection.
    """
    if not volumes or not any(volumes):
        return {}

    n = len(closes)

    # ── On Balance Volume ────────────────────────────────────────────────────
    obv = [0.0]
    for i in range(1, n):
        if closes[i] > closes[i-1]:
            obv.append(obv[-1] + volumes[i])
        elif closes[i] < closes[i-1]:
            obv.append(obv[-1] - volumes[i])
        else:
            obv.append(obv[-1])

    obv_trend = "rising" if obv[-1] > obv[-min(20, n)] else "falling"
    obv_divergence = None
    if n >= 20:
        price_up  = closes[-1] > closes[-20]
        obv_up    = obv[-1]  > obv[-20]
        if price_up and not obv_up:
            obv_divergence = "bearish"   # price rising but OBV falling = weak move
        elif not price_up and obv_up:
            obv_divergence = "bullish"   # price falling but OBV rising = hidden buying

    # ── Volume averages ───────────────────────────────────────────────────────
    avg_vol_20 = sum(volumes[-20:]) / 20 if n >= 20 else sum(volumes) / n
    recent_vol = volumes[-1]
    vol_ratio  = round(recent_vol / avg_vol_20, 2) if avg_vol_20 > 0 else None

    # ── Volume on up days vs down days (last 20) ──────────────────────────────
    up_vol   = sum(volumes[i] for i in range(max(0, n-20), n) if closes[i] >= closes[i-1])
    down_vol = sum(volumes[i] for i in range(max(0, n-20), n) if closes[i] <  closes[i-1])
    vol_sentiment = "buying" if up_vol > down_vol * 1.2 else \
                    "selling" if down_vol > up_vol * 1.2 else "neutral"

    # ── Breakout volume detection ─────────────────────────────────────────────
    price_breakout = abs(closes[-1] - closes[-2]) / closes[-2] > 0.03  # >3% move
    volume_spike   = vol_ratio is not None and vol_ratio > 2.0
    breakout_confirmed = price_breakout and volume_spike

    result = {
        "obv_trend":         obv_trend,
        "obv_divergence":    obv_divergence,
        "vol_ratio_vs_avg":  vol_ratio,
        "vol_sentiment":     vol_sentiment,
        "breakout_confirmed": breakout_confirmed,
        "description": ""
    }

    parts = []
    if obv_trend == "rising":
        parts.append("OBV is rising — smart money accumulating on dips")
    else:
        parts.append("OBV is falling — distribution in progress")
    if obv_divergence == "bullish":
        parts.append("BULLISH OBV DIVERGENCE: price falling but volume says buyers absorbing supply")
    elif obv_divergence == "bearish":
        parts.append("BEARISH OBV DIVERGENCE: price rising but volume is weak — rally may fade")
    if breakout_confirmed:
        parts.append(f"VOLUME-CONFIRMED BREAKOUT: {vol_ratio:.1f}x average volume on a {abs(closes[-1]-closes[-2])/closes[-2]*100:.1f}% move")
    result["description"] = ". ".join(parts) + "."
    return result


# ─── SIGNAL BIAS ─────────────────────────────────────────────────────────────

def _compute_signal_bias(analysis: dict) -> float:
    """
    Aggregate all patterns into a single bias score: -2.0 (very bearish) to +2.0 (very bullish).
    This is a numeric summary for quick comparison — Claude still makes the final call.
    """
    score = 0.0
    weights = {"very strong": 1.0, "strong": 0.7, "moderate": 0.4, "weak": 0.2}

    # Candlestick patterns
    for p in analysis.get("candlestick_patterns", []):
        w = weights.get(p.get("strength", "moderate"), 0.4)
        if p["bias"] == "bullish":   score += w
        elif p["bias"] == "bearish": score -= w

    # Chart patterns
    for p in analysis.get("chart_patterns", []):
        w = weights.get(p.get("strength", "moderate"), 0.4)
        if p["bias"] == "bullish":   score += w
        elif p["bias"] == "bearish": score -= w

    # Trend
    trend = analysis.get("trend", {})
    if trend.get("direction") == "uptrend":
        score += 0.5 if trend.get("strength") == "strong" else 0.25
    elif trend.get("direction") == "downtrend":
        score -= 0.5 if trend.get("strength") == "strong" else 0.25

    # MA alignment (0-3)
    ma = analysis.get("moving_averages", {})
    ma_align = ma.get("ma_alignment", 1)
    score += (ma_align - 1.5) * 0.3   # +0.45 if all above, -0.45 if all below

    cross = ma.get("cross_signal")
    if cross == "golden_cross":   score += 1.0
    elif cross == "death_cross":  score -= 1.0

    # Volume
    vol = analysis.get("volume_analysis", {})
    if vol.get("obv_divergence") == "bullish":   score += 0.4
    elif vol.get("obv_divergence") == "bearish":  score -= 0.4
    if vol.get("breakout_confirmed"):
        mom = trend.get("momentum_5d_pct", 0) or 0
        score += 0.3 if mom > 0 else -0.3

    return round(max(-2.0, min(2.0, score)), 2)


# ─── HUMAN-READABLE SUMMARY ───────────────────────────────────────────────────

def _build_summary(analysis: dict, ticker: str, current_price: float) -> str:
    """Build a concise English summary for Claude's context."""
    lines = [f"CHART ANALYSIS — {ticker} (current: ${current_price:,.4f})"]

    # Trend
    trend = analysis.get("trend", {})
    if trend.get("description"):
        lines.append(f"Trend: {trend['description']}")

    # MA structure
    ma = analysis.get("moving_averages", {})
    if ma.get("cross_description"):
        lines.append(f"Moving Averages: {ma['cross_description']}")
    elif ma.get("ma_alignment_description"):
        lines.append(f"Moving Averages: {ma['ma_alignment_description']}")

    # Candlestick patterns
    candle_pats = analysis.get("candlestick_patterns", [])
    if candle_pats:
        for p in candle_pats[-2:]:  # last 2 patterns
            lines.append(f"Candle Pattern: [{p['bias'].upper()}] {p['pattern']} — {p['description']}")

    # Chart patterns
    chart_pats = analysis.get("chart_patterns", [])
    if chart_pats:
        for p in chart_pats[:2]:
            lines.append(f"Chart Pattern: [{p['bias'].upper()}] {p['pattern']} — {p['description']}")

    # Support/resistance
    sr = analysis.get("support_resistance", {})
    sups = sr.get("key_supports", [])
    ress = sr.get("key_resistances", [])
    if sups:
        lines.append(f"Key Support: ${sups[0]['price']:,.4f} ({sups[0]['pct_away']:+.1f}% away)")
    if ress:
        lines.append(f"Key Resistance: ${ress[0]['price']:,.4f} ({ress[0]['pct_away']:+.1f}% away)")
    if sr.get("risk_reward"):
        lines.append(f"Risk/Reward to nearest levels: {sr['risk_reward']:.2f}x")

    # Volume
    vol = analysis.get("volume_analysis", {})
    if vol.get("description"):
        lines.append(f"Volume: {vol['description']}")

    # Overall bias
    bias = analysis.get("signal_bias", 0)
    bias_label = (
        "STRONGLY BULLISH" if bias >= 1.5 else
        "BULLISH"          if bias >= 0.5 else
        "SLIGHTLY BULLISH" if bias >= 0.2 else
        "NEUTRAL"          if abs(bias) < 0.2 else
        "SLIGHTLY BEARISH" if bias >= -0.5 else
        "BEARISH"          if bias >= -1.5 else
        "STRONGLY BEARISH"
    )
    lines.append(f"Overall Chart Bias: {bias_label} (score: {bias:+.2f})")

    return "\n".join(lines)


# ─── INTERNAL HELPERS ─────────────────────────────────────────────────────────

def _find_peaks(data: List[float], window: int = 5) -> List[Tuple[int, float]]:
    """
    Find local maxima in data. Returns list of (index, value) pairs.
    A peak is a point higher than all points in [i-window, i+window].
    """
    peaks = []
    n = len(data)
    for i in range(window, n - window):
        window_data = data[max(0, i-window):i+window+1]
        if data[i] == max(window_data):
            peaks.append((i, data[i]))
    return peaks


def _cluster_levels(levels: List[float], tolerance: float = 0.015) -> List[float]:
    """
    Merge price levels that are within tolerance% of each other.
    Returns deduplicated list of key levels.
    """
    if not levels:
        return []
    sorted_levels = sorted(set(levels))
    clusters = [[sorted_levels[0]]]
    for level in sorted_levels[1:]:
        if abs(level - clusters[-1][-1]) / clusters[-1][-1] < tolerance:
            clusters[-1].append(level)
        else:
            clusters.append([level])
    return [sum(c) / len(c) for c in clusters]
