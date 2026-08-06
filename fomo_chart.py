#!/usr/bin/env python3
"""
fomo_chart.py -- FOMO Golem Chart Analysis Engine.

Independent chart reading module. Uses DexScreener OHLCV candle data to identify
memecoin-specific price patterns and generate entry/exit signals:

  - Wave 2 setup detection (consolidation after Wave 1 pump)
  - Consolidation Coil breakout
  - Liquidity Grab reversals
  - Vertical Wall (EUPHORIA) exit signals
  - Volume collapse warnings

All analysis is tuned for memecoin price dynamics (hours to days, not weeks/months).
Standard TA (Fibonacci, RSI divergence on its own) is NOT relied upon -- narrative
and volume context are primary.

Usage:
    from fomo_chart import analyze_chart, ChartSignal
    signal = analyze_chart(contract, chain="solana")
    if signal.pattern in ("WAVE_2_BREAKOUT", "CONSOLIDATION_COIL"):
        # Strong setup confirmed by chart
"""

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "CryptoOracle/3.0 (fomo-chart)"}

# DexScreener candle resolutions
CANDLE_1M  = "1"
CANDLE_5M  = "5"
CANDLE_15M = "15"
CANDLE_1H  = "60"
CANDLE_4H  = "240"


# ─── DATA STRUCTURES ──────────────────────────────────────────────────────────

@dataclass
class Candle:
    timestamp:  int    # epoch seconds
    open:       float
    high:       float
    low:        float
    close:      float
    volume:     float

    @property
    def is_green(self) -> bool:
        return self.close > self.open

    @property
    def body_pct(self) -> float:
        """Body size as percentage of open price."""
        if self.open == 0:
            return 0
        return abs(self.close - self.open) / self.open * 100

    @property
    def upper_wick_pct(self) -> float:
        if self.open == 0:
            return 0
        return (self.high - max(self.open, self.close)) / self.open * 100

    @property
    def lower_wick_pct(self) -> float:
        if self.open == 0:
            return 0
        return (min(self.open, self.close) - self.low) / self.open * 100


@dataclass
class ChartSignal:
    """Output from analyze_chart()."""
    contract:        str
    chain:           str
    symbol:          str        = ""
    pattern:         str        = "NEUTRAL"   # see PATTERN_* constants below
    direction:       str        = "NONE"      # BUY | SELL | HOLD | NONE
    confidence:      str        = "low"       # low | medium | high
    score:           int        = 0           # 0-10
    summary:         str        = ""
    details:         list       = field(default_factory=list)
    warnings:        list       = field(default_factory=list)

    # Key metrics
    current_price:   float      = 0.0
    market_cap:      float      = 0.0
    liquidity_usd:   float      = 0.0
    volume_5m:       float      = 0.0
    volume_1h:       float      = 0.0
    volume_24h:      float      = 0.0
    price_change_5m: float      = 0.0
    price_change_1h: float      = 0.0
    price_change_24h: float     = 0.0

    # Wave detection
    wave1_high:      Optional[float] = None
    wave1_low:       Optional[float] = None
    consolidation_range_pct: Optional[float] = None
    retracement_pct: Optional[float] = None   # % retraced from wave 1 high

    # Suggested position management
    suggested_entry_pct:  float = 15.0   # % of FOMO cash to allocate
    stop_loss_pct:        float = -15.0  # hard stop at -15%
    tp1_pct:              float = 50.0   # take 25% profits
    tp2_pct:              float = 100.0  # take another 25%
    tp3_pct:              float = 200.0  # take another 25%

    def to_telegram_summary(self) -> str:
        direction_icon = {"BUY": "🟢", "SELL": "🔴", "HOLD": "🟡", "NONE": "⚪"}.get(self.direction, "⚪")
        conf_stars = {"high": "★★★", "medium": "★★☆", "low": "★☆☆"}.get(self.confidence, "★☆☆")
        lines = [
            f"{direction_icon} <b>Chart: ${self.symbol}</b>  [{self.pattern}]  {conf_stars}",
            f"📊 MCap: ${self.market_cap:,.0f}  |  💧 Liq: ${self.liquidity_usd:,.0f}",
            f"📈 5m: {self.price_change_5m:+.1f}%  1h: {self.price_change_1h:+.1f}%  24h: {self.price_change_24h:+.1f}%",
        ]
        if self.retracement_pct is not None:
            lines.append(f"🌊 Retraced {self.retracement_pct:.0f}% from Wave 1 high")
        if self.consolidation_range_pct is not None:
            lines.append(f"📦 Consolidation range: {self.consolidation_range_pct:.1f}%")
        if self.details:
            lines.append("✔ " + " | ".join(self.details[:3]))
        if self.warnings:
            lines.append("⚠️ " + " | ".join(self.warnings[:2]))
        lines.append(f"\n{self.summary}")
        return "\n".join(lines)


# Pattern name constants
PATTERN_WAVE2_BREAKOUT       = "WAVE_2_BREAKOUT"
PATTERN_CONSOLIDATION_COIL   = "CONSOLIDATION_COIL"
PATTERN_LIQUIDITY_GRAB       = "LIQUIDITY_GRAB"
PATTERN_VERTICAL_WALL_EXIT   = "VERTICAL_WALL_EXIT"
PATTERN_VOLUME_COLLAPSE_WARN = "VOLUME_COLLAPSE_WARN"
PATTERN_EARLY_DISCOVERY      = "EARLY_DISCOVERY"
PATTERN_DEAD_CAT_BOUNCE      = "DEAD_CAT_BOUNCE"
PATTERN_DISTRIBUTION         = "DISTRIBUTION"
PATTERN_NEUTRAL              = "NEUTRAL"


# ─── DEX DATA FETCHERS ────────────────────────────────────────────────────────

def _fetch_pair_info(contract: str) -> Optional[dict]:
    """Fetch the best (deepest-liquidity) pair from DexScreener."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            timeout=10,
            headers=HEADERS,
        )
        if r.status_code != 200:
            return None
        pairs = r.json().get("pairs", [])
        if not pairs:
            return None
        pairs.sort(
            key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0),
            reverse=True,
        )
        return pairs[0]
    except Exception as e:
        log.debug(f"DexScreener pair fetch error {contract[:8]}: {e}")
        return None


def _fetch_candles(pair_address: str, chain_id: str, resolution: str, limit: int = 100) -> list[Candle]:
    """
    Fetch OHLCV candles from DexScreener candle endpoint.
    chain_id examples: "solana", "base", "ethereum"
    resolution: "1", "5", "15", "60", "240" (minutes)
    """
    try:
        # DexScreener candle endpoint (v3 beta)
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/candles/{chain_id}/{pair_address}",
            params={"resolution": resolution, "countback": limit},
            timeout=10,
            headers=HEADERS,
        )
        if r.status_code != 200:
            log.debug(f"Candles {r.status_code} for {pair_address[:8]}")
            return []
        data = r.json().get("data", [])
        candles = []
        for c in data:
            try:
                candles.append(Candle(
                    timestamp=int(c.get("t", 0)),
                    open=float(c.get("o", 0)),
                    high=float(c.get("h", 0)),
                    low=float(c.get("l", 0)),
                    close=float(c.get("c", 0)),
                    volume=float(c.get("v", 0)),
                ))
            except Exception:
                continue
        # Sort ascending by time
        candles.sort(key=lambda x: x.timestamp)
        return candles
    except Exception as e:
        log.debug(f"Candle fetch error {pair_address[:8]}: {e}")
        return []


# ─── ANALYSIS HELPERS ─────────────────────────────────────────────────────────

def _avg_volume(candles: list[Candle], last_n: int = 12) -> float:
    """Average volume of the last N candles (excluding the most recent)."""
    if len(candles) < 2:
        return 0
    sample = candles[-(last_n + 1):-1]
    if not sample:
        return 0
    total = sum(c.volume for c in sample)
    return total / len(sample)


def _detect_wave1_high(candles: list[Candle], lookback: int = 48) -> Optional[tuple[float, float]]:
    """
    Find the most prominent peak+trough in the lookback window.
    Returns (wave1_high, wave1_low) or None if no clear pump detected.
    A valid Wave 1 pump is: price increased >100% from local low to local high
    within 24 hours.
    """
    if len(candles) < lookback:
        return None

    window = candles[-lookback:]

    # Find the highest high in the window
    peak_idx  = max(range(len(window)), key=lambda i: window[i].high)
    peak_high = window[peak_idx].high

    # Find the lowest low BEFORE the peak
    if peak_idx == 0:
        return None
    pre_peak = window[:peak_idx]
    trough_low = min(c.low for c in pre_peak)

    if trough_low <= 0:
        return None

    pump_pct = (peak_high - trough_low) / trough_low * 100

    # Require at least a 100% pump to call it a Wave 1
    if pump_pct >= 100:
        return (peak_high, trough_low)

    return None


def _current_retracement(candles: list[Candle], wave1_high: float, wave1_low: float) -> Optional[float]:
    """
    Returns what % of the Wave 1 move has been retraced.
    0% = still at the high, 100% = back to the low.
    50% retracement is the key level for Wave 2 setups.
    """
    if not candles or wave1_high <= wave1_low:
        return None
    current_price = candles[-1].close
    wave1_range   = wave1_high - wave1_low
    retrace       = (wave1_high - current_price) / wave1_range * 100
    return max(0, min(100, retrace))


def _consolidation_range(candles: list[Candle], last_n: int = 8) -> Optional[float]:
    """
    Returns the price range (high-low / low) as a % for the last N candles.
    A tight range (<20%) = consolidation. Very tight (<10%) = coil forming.
    """
    if len(candles) < last_n:
        return None
    window = candles[-last_n:]
    highs = [c.high for c in window]
    lows  = [c.low  for c in window]
    range_low = min(lows)
    range_high = max(highs)
    if range_low <= 0:
        return None
    return (range_high - range_low) / range_low * 100


def _volume_trend(candles: list[Candle]) -> str:
    """
    Compare last 3 candles avg volume vs prior 6 candles avg volume.
    Returns: "EXPANDING", "DECLINING", "FLAT"
    """
    if len(candles) < 9:
        return "FLAT"
    recent = sum(c.volume for c in candles[-3:]) / 3
    prior  = sum(c.volume for c in candles[-9:-3]) / 6
    if prior <= 0:
        return "FLAT"
    ratio = recent / prior
    if ratio > 1.5:
        return "EXPANDING"
    if ratio < 0.6:
        return "DECLINING"
    return "FLAT"


def _is_vertical_wall(candles: list[Candle], last_n: int = 4) -> bool:
    """
    Detects the Vertical Wall / Euphoria phase:
    - Last N candles are all green (or mostly)
    - Each candle body is large (>3% each)
    - Volume accelerating
    """
    if len(candles) < last_n:
        return False
    window = candles[-last_n:]
    green_count = sum(1 for c in window if c.is_green)
    large_body  = sum(1 for c in window if c.body_pct > 3)
    if green_count >= last_n - 1 and large_body >= last_n - 1:
        vt = _volume_trend(candles)
        return vt == "EXPANDING"
    return False


def _detect_liquidity_grab(candles: list[Candle]) -> bool:
    """
    Detects a Liquidity Grab reversal in the last 3 candles:
    - A candle with a very long lower wick (>5%) that closes near the top of its range
    - Followed by a strong green candle on expanding volume
    """
    if len(candles) < 3:
        return False
    grab_candle = candles[-2]
    follow      = candles[-1]

    wick_long   = grab_candle.lower_wick_pct > 5
    closed_high = grab_candle.close > (grab_candle.low + (grab_candle.high - grab_candle.low) * 0.6)
    strong_follow = follow.is_green and follow.body_pct > 2
    avg_vol = _avg_volume(candles, last_n=8)
    vol_spike = follow.volume > avg_vol * 1.5 if avg_vol > 0 else False

    return wick_long and closed_high and strong_follow and vol_spike


def _mcap_bucket(mcap: float) -> str:
    """Classify market cap for context."""
    if mcap < 100_000:
        return "micro (<$100K)"
    if mcap < 500_000:
        return "very low ($100K-500K)"
    if mcap < 1_000_000:
        return "low ($500K-1M)"
    if mcap < 3_000_000:
        return "sweet spot ($1M-3M)"
    if mcap < 10_000_000:
        return "mid ($3M-10M)"
    return "high (>$10M)"


# ─── MAIN ANALYSIS ENGINE ─────────────────────────────────────────────────────

def analyze_chart(contract: str, chain: str = "solana") -> ChartSignal:
    """
    Master entry point. Analyzes DexScreener data for a token and returns a
    ChartSignal with pattern classification, direction, and position guidance.

    chain: "solana", "base", "ethereum", etc.
    """
    sig = ChartSignal(contract=contract, chain=chain.lower())

    # ── 1. Fetch pair info ────────────────────────────────────────────────────
    pair = _fetch_pair_info(contract)
    if not pair:
        sig.pattern = PATTERN_NEUTRAL
        sig.summary = "No DEX pair found — cannot analyze chart"
        sig.warnings.append("Token not listed on DexScreener")
        return sig

    base = pair.get("baseToken", {})
    sig.symbol          = base.get("symbol", "?")
    sig.current_price   = float(pair.get("priceUsd") or 0)
    sig.market_cap      = float(pair.get("fdv") or 0)
    sig.liquidity_usd   = float((pair.get("liquidity") or {}).get("usd") or 0)
    sig.volume_5m       = float((pair.get("volume") or {}).get("m5") or 0)
    sig.volume_1h       = float((pair.get("volume") or {}).get("h1") or 0)
    sig.volume_24h      = float((pair.get("volume") or {}).get("h24") or 0)
    sig.price_change_5m = float((pair.get("priceChange") or {}).get("m5") or 0)
    sig.price_change_1h = float((pair.get("priceChange") or {}).get("h1") or 0)
    sig.price_change_24h = float((pair.get("priceChange") or {}).get("h24") or 0)

    pair_addr  = pair.get("pairAddress", "")
    chain_id   = pair.get("chainId", chain.lower())

    # ── 2. Fetch candles ──────────────────────────────────────────────────────
    candles_5m = _fetch_candles(pair_addr, chain_id, CANDLE_5M, limit=72)   # ~6 hours
    candles_1h = _fetch_candles(pair_addr, chain_id, CANDLE_1H, limit=48)   # 2 days

    # Fallback: if no candles (e.g. new token), use price change data only
    if not candles_5m and not candles_1h:
        sig.warnings.append("No OHLCV data available -- price-change analysis only")
        return _price_change_fallback(sig)

    # ── 3. Instant safety checks ──────────────────────────────────────────────
    if sig.liquidity_usd < 30_000:
        sig.pattern  = PATTERN_NEUTRAL
        sig.direction = "NONE"
        sig.confidence = "high"
        sig.warnings.append(f"Liquidity too low (${sig.liquidity_usd:,.0f}) -- skip")
        sig.summary  = "SKIP: Insufficient liquidity for safe entry/exit"
        return sig

    # ── 4. Vertical Wall detection (EXIT signal) ──────────────────────────────
    if candles_5m and _is_vertical_wall(candles_5m, last_n=4):
        sig.pattern    = PATTERN_VERTICAL_WALL_EXIT
        sig.direction  = "SELL"
        sig.confidence = "high"
        sig.score      = 9
        sig.details.append("4 consecutive large green candles + expanding volume")
        sig.details.append(f"1h: {sig.price_change_1h:+.1f}% -- Euphoria Zone")
        sig.summary    = (
            "VERTICAL WALL detected -- EUPHORIA ZONE. "
            "Take 50%+ profits NOW. Do NOT initiate new positions."
        )
        sig.suggested_entry_pct = 0   # No new entries in this pattern
        return sig

    # ── 5. Volume collapse warning ─────────────────────────────────────────────
    if candles_5m:
        avg_vol_5m = _avg_volume(candles_5m, last_n=12)
        vol_trend  = _volume_trend(candles_5m)
        recent_vol = candles_5m[-1].volume if candles_5m else 0

        if avg_vol_5m > 0 and recent_vol < avg_vol_5m * 0.3 and vol_trend == "DECLINING":
            sig.pattern    = PATTERN_VOLUME_COLLAPSE_WARN
            sig.direction  = "HOLD"  # Hold, don't add -- reduce if we have a position
            sig.confidence = "medium"
            sig.score      = 3
            sig.warnings.append(f"Volume collapsed to {(recent_vol/avg_vol_5m*100):.0f}% of recent avg")
            sig.summary    = (
                "VOLUME COLLAPSE: Buyers disappearing. Reduce position if held. "
                "Do NOT enter new position until volume recovers."
            )
            return sig

    # ── 6. Wave 1 / Wave 2 detection ──────────────────────────────────────────
    wave1 = _detect_wave1_high(candles_1h or candles_5m, lookback=48)

    if wave1:
        w1_high, w1_low = wave1
        sig.wave1_high  = w1_high
        sig.wave1_low   = w1_low

        retrace = _current_retracement(candles_5m or candles_1h, w1_high, w1_low)
        sig.retracement_pct = retrace

        pump_pct = (w1_high - w1_low) / w1_low * 100 if w1_low > 0 else 0
        sig.details.append(f"Wave 1 pump: {pump_pct:.0f}% detected")

        if retrace is not None:
            if 40 <= retrace <= 70:
                # Classic Wave 2 setup zone (40-70% retracement)
                consol_range = _consolidation_range(candles_5m or [], last_n=8)
                sig.consolidation_range_pct = consol_range

                vol_trend = _volume_trend(candles_5m or [])

                if consol_range is not None and consol_range < 20 and vol_trend != "DECLINING":
                    # Tight consolidation + volume not dying = ideal Wave 2 coil
                    sig.pattern    = PATTERN_WAVE2_BREAKOUT
                    sig.direction  = "BUY"
                    sig.confidence = "high"
                    sig.score      = 8
                    sig.details.append(f"Retraced {retrace:.0f}% from Wave 1 high (ideal zone: 40-70%)")
                    sig.details.append(f"Consolidation range: {consol_range:.1f}% (tight)")
                    sig.details.append(f"Volume trend: {vol_trend}")
                    sig.summary    = (
                        f"WAVE 2 SETUP: {retrace:.0f}% retracement with tight consolidation. "
                        f"HIGH CONVICTION entry zone. Wait for volume expansion or tracked wallet entry to confirm."
                    )
                    sig.suggested_entry_pct = 20.0  # Up to 20% position for Wave 2
                else:
                    sig.pattern    = PATTERN_CONSOLIDATION_COIL
                    sig.direction  = "BUY"
                    sig.confidence = "medium"
                    sig.score      = 6
                    sig.details.append(f"Retraced {retrace:.0f}% from Wave 1 -- consolidating")
                    if consol_range:
                        sig.details.append(f"Range: {consol_range:.1f}% (waiting for tighter coil)")
                    sig.summary    = (
                        f"CONSOLIDATION at {retrace:.0f}% retracement from Wave 1. "
                        f"Potential Wave 2 forming. Watch for volume return + holder growth."
                    )
                    sig.suggested_entry_pct = 10.0

            elif retrace < 20:
                # Near Wave 1 high -- could be trying to push higher or topping
                if sig.price_change_1h > 50:
                    sig.pattern    = PATTERN_VERTICAL_WALL_EXIT
                    sig.direction  = "SELL"
                    sig.confidence = "medium"
                    sig.score      = 7
                    sig.summary    = "Near Wave 1 high + rapid 1h move. Watch for topping. Consider profit-taking."
                    sig.suggested_entry_pct = 0
                else:
                    sig.pattern    = PATTERN_NEUTRAL
                    sig.direction  = "HOLD"
                    sig.confidence = "low"
                    sig.score      = 5
                    sig.summary    = "Near Wave 1 high. Momentum ambiguous. Hold existing; no new entry."
                    sig.suggested_entry_pct = 0

            elif retrace > 70:
                # Deep retracement -- Wave 1 may be completely dead, or rare deep-value buy
                if retrace > 90:
                    sig.pattern    = PATTERN_DISTRIBUTION
                    sig.direction  = "NONE"
                    sig.confidence = "medium"
                    sig.score      = 2
                    sig.warnings.append(f"Deep {retrace:.0f}% retracement -- narrative likely dead")
                    sig.summary    = "DEEP RETRACEMENT: Wave 1 gains almost fully reversed. Skip unless exceptional narrative catalyst."
                    sig.suggested_entry_pct = 0
                else:
                    # 70-90% range -- possible dead cat or very early recovery
                    sig.pattern    = PATTERN_DEAD_CAT_BOUNCE
                    sig.direction  = "NONE"
                    sig.confidence = "low"
                    sig.score      = 3
                    sig.warnings.append(f"{retrace:.0f}% retracement -- deep correction, recovery unconfirmed")
                    sig.summary    = "DEEP CORRECTION: Possible dead cat bounce territory. Need volume + narrative confirmation before re-entry."
                    sig.suggested_entry_pct = 0

    # ── 7. Liquidity Grab detection (fallback if no Wave pattern found) ────────
    if sig.pattern == PATTERN_NEUTRAL and candles_5m and _detect_liquidity_grab(candles_5m):
        sig.pattern    = PATTERN_LIQUIDITY_GRAB
        sig.direction  = "BUY"
        sig.confidence = "medium"
        sig.score      = 6
        sig.details.append("Long lower wick reversal + strong follow-through green candle")
        sig.details.append("Volume spike confirms whale accumulation at lows")
        sig.summary    = (
            "LIQUIDITY GRAB detected: Wick below support + reversal + volume spike. "
            "Whale(s) swept stop losses and bought. Potential entry after confirmation."
        )
        sig.suggested_entry_pct = 10.0

    # ── 8. Early discovery (token in sweet spot, rising, no Wave pattern yet) ──
    if sig.pattern == PATTERN_NEUTRAL:
        mcap = sig.market_cap
        if 200_000 <= mcap <= 3_000_000 and sig.price_change_1h > 10:
            # Rising from a low mcap = early discovery phase
            vol_trend = _volume_trend(candles_5m or [])
            if vol_trend == "EXPANDING":
                sig.pattern    = PATTERN_EARLY_DISCOVERY
                sig.direction  = "BUY"
                sig.confidence = "medium"
                sig.score      = 6
                sig.details.append(f"MCap in sweet spot: ${mcap:,.0f} ({_mcap_bucket(mcap)})")
                sig.details.append(f"+{sig.price_change_1h:.0f}% in 1h with expanding volume")
                sig.summary    = (
                    f"EARLY DISCOVERY: Rising from ${mcap:,.0f} mcap with expanding volume. "
                    f"Enter small (10% FOMO cash). Watch holder count growth."
                )
                sig.suggested_entry_pct = 10.0

    # ── 9. Default neutral ─────────────────────────────────────────────────────
    if sig.pattern == PATTERN_NEUTRAL and not sig.summary:
        vol_trend = _volume_trend(candles_5m or [])
        mcap_label = _mcap_bucket(sig.market_cap)
        sig.direction  = "NONE"
        sig.confidence = "low"
        sig.score      = 4
        sig.summary    = (
            f"No clear pattern. MCap {mcap_label}. Volume: {vol_trend}. "
            f"1h: {sig.price_change_1h:+.1f}%. Wait for clearer setup or wallet trigger."
        )

    # ── 10. Market cap guardrails ──────────────────────────────────────────────
    if sig.market_cap > 10_000_000 and sig.direction == "BUY":
        sig.warnings.append(f"MCap ${sig.market_cap:,.0f} may be too high for 2-5x move")
        sig.suggested_entry_pct = min(sig.suggested_entry_pct, 10.0)
        sig.confidence = "low" if sig.confidence == "high" else sig.confidence

    # ── 11. Log ───────────────────────────────────────────────────────────────
    log.info(
        "Chart %s $%s | %s | %s | score=%d | 1h=%+.1f%% | mcap=$%s",
        contract[:8], sig.symbol, sig.pattern, sig.direction, sig.score,
        sig.price_change_1h, f"{sig.market_cap:,.0f}",
    )
    return sig


def _price_change_fallback(sig: ChartSignal) -> ChartSignal:
    """
    When no OHLCV candles are available, make a basic signal from price change data alone.
    Used for very new tokens or unsupported pairs.
    """
    p1h  = sig.price_change_1h
    p24h = sig.price_change_24h

    if p1h > 100:
        sig.pattern    = PATTERN_VERTICAL_WALL_EXIT
        sig.direction  = "SELL"
        sig.confidence = "medium"
        sig.score      = 7
        sig.summary    = f"Price up {p1h:+.0f}% in 1h (no candle data). Possible Wave 1 peak -- caution."
    elif p24h > 200 and -30 < p1h < 20:
        sig.pattern    = PATTERN_CONSOLIDATION_COIL
        sig.direction  = "BUY"
        sig.confidence = "low"
        sig.score      = 5
        sig.summary    = f"Up {p24h:.0f}% in 24h, cooling in 1h ({p1h:+.0f}%). Possible consolidation -- needs candle confirmation."
    elif p1h > 20:
        sig.pattern    = PATTERN_EARLY_DISCOVERY
        sig.direction  = "BUY"
        sig.confidence = "low"
        sig.score      = 4
        sig.summary    = f"Rising {p1h:+.0f}% in 1h. Possibly early. No candle data -- reduce size."
    else:
        sig.pattern    = PATTERN_NEUTRAL
        sig.direction  = "NONE"
        sig.confidence = "low"
        sig.score      = 3
        sig.summary    = "No candle data + no clear price momentum. Skip until more data available."

    return sig


# ─── CONVENIENCE FUNCTION ─────────────────────────────────────────────────────

def chart_should_enter(signal: ChartSignal) -> bool:
    """Quick helper: does the chart signal support a new BUY entry?"""
    return (
        signal.direction == "BUY"
        and signal.score >= 5
        and signal.pattern not in (PATTERN_VERTICAL_WALL_EXIT, PATTERN_DISTRIBUTION,
                                    PATTERN_DEAD_CAT_BOUNCE, PATTERN_VOLUME_COLLAPSE_WARN)
        and signal.suggested_entry_pct > 0
    )


def chart_should_exit(signal: ChartSignal) -> bool:
    """Quick helper: does the chart signal recommend reducing or exiting a position?"""
    return (
        signal.direction in ("SELL",)
        or signal.pattern in (PATTERN_VERTICAL_WALL_EXIT, PATTERN_DISTRIBUTION,
                               PATTERN_VOLUME_COLLAPSE_WARN)
    )
