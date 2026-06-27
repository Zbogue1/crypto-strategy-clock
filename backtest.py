#!/usr/bin/env python3
"""
backtest.py — Historical strategy backtester for Crypto Strategy Clock.

Fetches up to 365 days of daily OHLC from CoinGecko (free, no key),
replays the chart analysis engine across rolling windows, simulates
entries/exits using the same rules as the live agent, and reports
performance vs a BTC buy-and-hold benchmark.

Usage:
    python backtest.py                        # BTC, 365 days
    python backtest.py --coin ethereum        # ETH, 365 days
    python backtest.py --coin solana --days 180
    python backtest.py --top5                 # quick run on BTC/ETH/SOL/BNB/XRP
    python backtest.py --coin bitcoin --verbose

No Claude API calls — signals are generated purely from the chart engine
(RSI, MACD, candlestick patterns, chart patterns, MA alignment, volume).
This lets you run thousands of candles in seconds and see if the mechanical
strategy has edge before risking real money.
"""

import argparse
import json
import math
import time
import requests
import sys
from typing import List, Optional
from datetime import datetime, timezone

# ─── IMPORT CHART ENGINE ──────────────────────────────────────────────────────
try:
    from chart_analysis import analyze_chart
except ImportError:
    print("ERROR: chart_analysis.py not found. Run from the project directory.")
    sys.exit(1)

HEADERS = {"User-Agent": "CryptoOracle/3.0 (backtesting; non-commercial)"}
TAKER_FEE   = 0.001   # 0.1% per side (matches paper_portfolio.py)
STARTING_CASH = 1_000.0
MAX_POSITION_PCT = 0.40   # max 40% of cash per trade (high-confidence sizing)


# ─── DATA FETCHER ─────────────────────────────────────────────────────────────

def fetch_ohlc(coin_id: str, days: int = 365) -> list:
    """
    Fetch daily OHLC from CoinGecko.
    Returns list of [timestamp, open, high, low, close] candles.
    Retries 3x on 429.
    """
    print(f"Fetching {days} days of OHLC for {coin_id}...", end=" ", flush=True)
    for attempt in range(3):
        try:
            r = requests.get(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}/ohlc",
                params={"vs_currency": "usd", "days": days},
                timeout=15,
                headers=HEADERS,
            )
            if r.status_code == 429:
                wait = 20 * (attempt + 1)
                print(f"rate-limited, waiting {wait}s...", end=" ", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            data = r.json()
            print(f"{len(data)} candles fetched.")
            return data
        except Exception as e:
            if attempt < 2:
                time.sleep(10)
                continue
            print(f"FAILED: {e}")
            return []
    return []


# ─── RULES-BASED SIGNAL GENERATOR ────────────────────────────────────────────

def generate_signal(chart: dict, rsi: float = None) -> dict:
    """
    Translate chart analysis output into a BUY / HOLD / SELL decision.
    Uses the same logic principles Claude uses, but mechanically.

    Returns: {"action": "BUY"|"HOLD"|"SELL", "confidence": 0-100, "reason": str}
    """
    bias    = chart.get("signal_bias", 0)
    trend   = chart.get("trend", {})
    ma      = chart.get("moving_averages", {})
    vol     = chart.get("volume_analysis", {})
    candles = chart.get("candlestick_patterns", [])
    charts  = chart.get("chart_patterns", [])
    sr      = chart.get("support_resistance", {})

    score   = 0.0
    reasons = []

    # ── Chart bias (aggregate) ────────────────────────────────────────────────
    score += bias * 20   # -40 to +40 points
    if abs(bias) > 0.5:
        reasons.append(f"chart bias {bias:+.2f}")

    # ── MA structure ──────────────────────────────────────────────────────────
    cross = ma.get("cross_signal")
    ma_align = ma.get("ma_alignment", 1)
    if cross == "golden_cross":
        score += 30; reasons.append("GOLDEN CROSS")
    elif cross == "death_cross":
        score -= 30; reasons.append("DEATH CROSS")
    elif cross == "above_200":
        score += 10 * (ma_align - 1); reasons.append(f"above {ma_align} MAs")
    elif cross == "below_200":
        score -= 10 * (3 - ma_align)

    # ── RSI ───────────────────────────────────────────────────────────────────
    if rsi is not None:
        if rsi < 30:
            score += 25; reasons.append(f"RSI oversold ({rsi:.0f})")
        elif rsi < 40:
            score += 12; reasons.append(f"RSI low ({rsi:.0f})")
        elif rsi > 70:
            score -= 20; reasons.append(f"RSI overbought ({rsi:.0f})")
        elif rsi > 60:
            score -= 8

    # ── Trend ─────────────────────────────────────────────────────────────────
    direction = trend.get("direction", "")
    strength  = trend.get("strength", "")
    if direction == "uptrend" and strength == "strong":
        score += 15
    elif direction == "uptrend":
        score += 7
    elif direction == "downtrend" and strength == "strong":
        score -= 15
    elif direction == "downtrend":
        score -= 7

    # ── Volume ────────────────────────────────────────────────────────────────
    if vol.get("breakout_confirmed"):
        mom = trend.get("momentum_5d_pct", 0) or 0
        score += 15 if mom > 0 else -15
        reasons.append("volume-confirmed breakout")
    if vol.get("obv_divergence") == "bullish":
        score += 12; reasons.append("bullish OBV divergence")
    elif vol.get("obv_divergence") == "bearish":
        score -= 12

    # ── Risk/Reward at current price ──────────────────────────────────────────
    rr = sr.get("risk_reward")
    if rr and rr > 2:
        score += 8; reasons.append(f"R/R {rr:.1f}x")
    elif rr and rr < 0.8:
        score -= 8

    # ── Strong candlestick reversal patterns ──────────────────────────────────
    for p in candles[-2:]:
        if p["bias"] == "bullish" and p["strength"] in ("strong", "very strong"):
            score += 15; reasons.append(p["pattern"])
        elif p["bias"] == "bearish" and p["strength"] in ("strong", "very strong"):
            score -= 15; reasons.append(p["pattern"])

    # ── Strong chart patterns ─────────────────────────────────────────────────
    for p in charts:
        if p["bias"] == "bullish" and p["strength"] in ("strong", "very strong"):
            score += 20; reasons.append(p["pattern"])
        elif p["bias"] == "bearish" and p["strength"] in ("strong", "very strong"):
            score -= 20; reasons.append(p["pattern"])

    # ── Decision thresholds ───────────────────────────────────────────────────
    score = max(-100, min(100, score))
    confidence = int(abs(score))

    if score >= 35:
        return {"action": "BUY",  "confidence": confidence, "reason": " | ".join(reasons)}
    elif score <= -35:
        return {"action": "SELL", "confidence": confidence, "reason": " | ".join(reasons)}
    else:
        return {"action": "HOLD", "confidence": confidence, "reason": "no clear edge"}


# ─── SIMPLE RSI FROM CLOSES ───────────────────────────────────────────────────

def compute_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
    gains  = [max(d, 0) for d in deltas]
    losses = [max(-d, 0) for d in deltas]
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
    return round(100 - 100 / (1 + ag / al), 2) if al else 100.0


# ─── PORTFOLIO SIMULATOR ──────────────────────────────────────────────────────

class BacktestPortfolio:
    def __init__(self, starting_cash: float = STARTING_CASH):
        self.cash         = starting_cash
        self.position     = None   # {units, entry_price, entry_candle, stop_loss, target, cost}
        self.trades       = []
        self.peak_value   = starting_cash
        self.drawdown_halt = False

    def total_value(self, current_price: float) -> float:
        pos_val = self.position["units"] * current_price if self.position else 0
        return self.cash + pos_val

    def check_drawdown(self, current_price: float):
        total = self.total_value(current_price)
        dd = (self.peak_value - total) / self.peak_value * 100 if self.peak_value > 0 else 0
        if dd >= 15 and not self.drawdown_halt:
            self.drawdown_halt = True
        elif dd < 10 and self.drawdown_halt:
            self.drawdown_halt = False

    def buy(self, price: float, confidence: int, candle_idx: int,
            stop_loss: float = None, target: float = None):
        if self.position:
            return  # already holding
        if self.drawdown_halt:
            return  # circuit breaker active

        # Confidence-based sizing (matches paper_portfolio.py)
        if confidence >= 80:
            frac = 0.40
        elif confidence >= 60:
            frac = 0.25
        elif confidence >= 50:
            frac = 0.15
        else:
            return  # too low confidence

        alloc = round(self.cash * frac, 2)
        if alloc < 10:
            return

        fee   = round(alloc * TAKER_FEE, 4)
        units = (alloc - fee) / price
        self.cash -= alloc

        self.position = {
            "units":        units,
            "entry_price":  price,
            "entry_candle": candle_idx,
            "cost":         alloc,
            "stop_loss":    stop_loss or price * 0.92,    # default 8% stop
            "target":       target    or price * 1.15,    # default 15% target
            "partial_taken": False,
        }

    def sell(self, price: float, candle_idx: int, reason: str):
        if not self.position:
            return
        p      = self.position
        fee    = round(p["units"] * price * TAKER_FEE, 4)
        gross  = round(p["units"] * price, 4)
        net    = round(gross - fee, 4)
        pnl    = round(net - p["cost"], 4)
        pnl_pct= round(pnl / p["cost"] * 100, 2)

        self.cash += net
        self.trades.append({
            "entry_candle": p["entry_candle"],
            "exit_candle":  candle_idx,
            "entry_price":  p["entry_price"],
            "exit_price":   price,
            "pnl_pct":      pnl_pct,
            "pnl_usd":      pnl,
            "reason":       reason,
            "won":          pnl_pct > 0,
        })
        total = self.total_value(price)
        if total > self.peak_value:
            self.peak_value = total
        self.position = None

    def partial_sell(self, price: float, candle_idx: int):
        """Sell 50% at halfway to target, move stop to breakeven."""
        if not self.position or self.position.get("partial_taken"):
            return
        p = self.position
        half_target = p["entry_price"] + (p["target"] - p["entry_price"]) * 0.5
        if price < half_target:
            return

        half_units = p["units"] / 2
        fee   = round(half_units * price * TAKER_FEE, 4)
        gross = round(half_units * price, 4)
        net   = round(gross - fee, 4)
        half_cost = p["cost"] / 2
        pnl   = round(net - half_cost, 4)
        pnl_pct = round(pnl / half_cost * 100, 2)

        self.cash += net
        self.trades.append({
            "entry_candle": p["entry_candle"],
            "exit_candle":  candle_idx,
            "entry_price":  p["entry_price"],
            "exit_price":   price,
            "pnl_pct":      pnl_pct,
            "pnl_usd":      pnl,
            "reason":       "partial_50pct",
            "won":          pnl_pct > 0,
        })
        # Update remaining half: halve units/cost, move stop to breakeven
        p["units"]         = half_units
        p["cost"]          = half_cost
        p["stop_loss"]     = p["entry_price"]   # no-lose position
        p["partial_taken"] = True


# ─── MAIN BACKTEST ────────────────────────────────────────────────────────────

def run_backtest(coin_id: str, days: int = 365, verbose: bool = False) -> dict:
    ohlc = fetch_ohlc(coin_id, days)
    if len(ohlc) < 30:
        return {"error": f"Insufficient data for {coin_id}"}

    portfolio = BacktestPortfolio()
    window = 30   # minimum candles needed for meaningful analysis

    closes  = [row[4] for row in ohlc]
    start_price = closes[window]
    end_price   = closes[-1]
    btc_hold_return = (end_price - start_price) / start_price * 100

    if verbose:
        print(f"\nBacktesting {coin_id.upper()} | {len(ohlc)} candles | "
              f"${start_price:,.2f} → ${end_price:,.2f} "
              f"({btc_hold_return:+.1f}% buy-and-hold)\n")
        print(f"{'Candle':<8} {'Price':>10} {'Action':<6} {'Reason':<40} {'Portfolio':>10}")
        print("-" * 80)

    # Slide a window of at least 30 candles across the data
    for i in range(window, len(ohlc)):
        window_ohlc = ohlc[max(0, i - 89):i + 1]   # up to 90-candle window
        current_price = float(ohlc[i][4])

        chart  = analyze_chart(window_ohlc)
        rsi    = compute_rsi([row[4] for row in window_ohlc])
        signal = generate_signal(chart, rsi)

        portfolio.check_drawdown(current_price)

        # ── Exit checks ───────────────────────────────────────────────────────
        if portfolio.position:
            p = portfolio.position
            # Partial take at 50% of target
            portfolio.partial_sell(current_price, i)
            # Stop-loss
            if current_price <= p["stop_loss"]:
                portfolio.sell(current_price, i, "stop_loss")
                if verbose:
                    val = portfolio.total_value(current_price)
                    t = portfolio.trades[-1]
                    stop_str = f"stop @ ${p['stop_loss']:,.2f}"
                    print(f"{i:<8} ${current_price:>9,.2f} {'STOP':<6} {stop_str:<40} ${val:>9,.2f}  {t['pnl_pct']:+.1f}%")
            # Take profit
            elif current_price >= p["target"]:
                portfolio.sell(current_price, i, "take_profit")
                if verbose:
                    val = portfolio.total_value(current_price)
                    t = portfolio.trades[-1]
                    tgt_str = f"target hit @ ${p['target']:,.2f}"
                    print(f"{i:<8} ${current_price:>9,.2f} {'SELL':<6} {tgt_str:<40} ${val:>9,.2f}  {t['pnl_pct']:+.1f}%")
            # Signal-driven exit
            elif signal["action"] == "SELL":
                portfolio.sell(current_price, i, "signal")
                if verbose:
                    val = portfolio.total_value(current_price)
                    t = portfolio.trades[-1]
                    print(f"{i:<8} ${current_price:>9,.2f} {'SELL':<6} {signal['reason'][:40]:<40} ${val:>9,.2f}  {t['pnl_pct']:+.1f}%")
            # Time exit: if held > 20 candles and signal is weak
            elif i - p["entry_candle"] > 20 and signal["action"] == "HOLD" and chart.get("signal_bias", 0) < 0:
                portfolio.sell(current_price, i, "time_exit")
                if verbose:
                    val = portfolio.total_value(current_price)
                    t = portfolio.trades[-1]
                    print(f"{i:<8} ${current_price:>9,.2f} {'EXIT':<6} {'time exit - stalled':<40} ${val:>9,.2f}  {t['pnl_pct']:+.1f}%")

        # ── Entry check ───────────────────────────────────────────────────────
        if not portfolio.position and signal["action"] == "BUY" and not portfolio.drawdown_halt:
            sr     = chart.get("support_resistance", {})
            target = sr.get("nearest_resistance") or current_price * 1.15
            stop   = sr.get("nearest_support")    or current_price * 0.92
            # Sanity: stop must be below entry, target above
            if stop >= current_price: stop = current_price * 0.92
            if target <= current_price: target = current_price * 1.15
            portfolio.buy(current_price, signal["confidence"], i, stop_loss=stop, target=target)
            if verbose and portfolio.position:
                val = portfolio.total_value(current_price)
                print(f"{i:<8} ${current_price:>9,.2f} {'BUY':<6} {signal['reason'][:40]:<40} ${val:>9,.2f}  "
                      f"stop=${stop:,.2f} tgt=${target:,.2f}")

    # Force-close any open position at end of backtest
    if portfolio.position:
        portfolio.sell(closes[-1], len(ohlc) - 1, "end_of_backtest")

    # ── Compute metrics ───────────────────────────────────────────────────────
    trades    = portfolio.trades
    n_trades  = len(trades)
    wins      = [t for t in trades if t["won"]]
    losses    = [t for t in trades if not t["won"]]
    win_rate  = round(len(wins) / n_trades * 100, 1) if n_trades else 0
    avg_win   = round(sum(t["pnl_pct"] for t in wins)   / len(wins),   2) if wins   else 0
    avg_loss  = round(sum(t["pnl_pct"] for t in losses) / len(losses), 2) if losses else 0
    avg_trade = round(sum(t["pnl_pct"] for t in trades) / n_trades,    2) if trades else 0
    best_trade= round(max((t["pnl_pct"] for t in trades), default=0),  2)
    worst_trade=round(min((t["pnl_pct"] for t in trades), default=0),  2)

    final_val   = portfolio.cash
    total_return= round((final_val - STARTING_CASH) / STARTING_CASH * 100, 2)
    vs_hold     = round(total_return - btc_hold_return, 2)

    # Max drawdown
    max_dd = 0.0
    peak   = STARTING_CASH
    running_val = STARTING_CASH
    for t in trades:
        running_val += t["pnl_usd"]
        if running_val > peak:
            peak = running_val
        dd = (peak - running_val) / peak * 100
        if dd > max_dd:
            max_dd = dd

    return {
        "coin_id":         coin_id,
        "days_tested":     days,
        "candles":         len(ohlc),
        "start_price":     round(start_price, 4),
        "end_price":       round(end_price, 4),
        "buy_hold_return": round(btc_hold_return, 2),
        "strategy_return": total_return,
        "vs_buy_hold":     vs_hold,
        "final_value":     round(final_val, 2),
        "total_trades":    n_trades,
        "win_rate":        win_rate,
        "avg_trade":       avg_trade,
        "avg_win":         avg_win,
        "avg_loss":        avg_loss,
        "best_trade":      best_trade,
        "worst_trade":     worst_trade,
        "max_drawdown":    round(max_dd, 2),
        "trades":          trades,
    }


def print_results(r: dict):
    """Pretty-print backtest results."""
    if "error" in r:
        print(f"ERROR: {r['error']}")
        return

    win_col   = "\033[92m" if r["win_rate"]        >= 50 else "\033[91m"
    ret_col   = "\033[92m" if r["strategy_return"] >= 0  else "\033[91m"
    edge_col  = "\033[92m" if r["vs_buy_hold"]     >= 0  else "\033[91m"
    RESET = "\033[0m"

    print(f"\n{'━'*56}")
    print(f"  BACKTEST: {r['coin_id'].upper()}  |  {r['days_tested']} days  |  {r['candles']} candles")
    print(f"{'━'*56}")
    print(f"  Start price:    ${r['start_price']:>12,.4f}")
    print(f"  End price:      ${r['end_price']:>12,.4f}")
    print(f"  Buy & hold:     {r['buy_hold_return']:>+12.2f}%")
    print(f"  Strategy return:{ret_col}{r['strategy_return']:>+12.2f}%{RESET}")
    print(f"  vs Buy & Hold:  {edge_col}{r['vs_buy_hold']:>+12.2f}%{RESET}  ← key metric")
    print(f"  Final value:    ${r['final_value']:>12,.2f}  (started $1,000)")
    print(f"{'─'*56}")
    print(f"  Trades:         {r['total_trades']:>12}")
    print(f"  Win rate:       {win_col}{r['win_rate']:>11.1f}%{RESET}")
    print(f"  Avg trade:      {r['avg_trade']:>+12.2f}%")
    print(f"  Avg win:        {r['avg_win']:>+12.2f}%")
    print(f"  Avg loss:       {r['avg_loss']:>+12.2f}%")
    print(f"  Best trade:     {r['best_trade']:>+12.2f}%")
    print(f"  Worst trade:    {r['worst_trade']:>+12.2f}%")
    print(f"  Max drawdown:   {r['max_drawdown']:>11.2f}%")
    print(f"{'━'*56}\n")

    # Verdict
    if r["vs_buy_hold"] >= 10:
        verdict = "✅ STRONG EDGE over buy-and-hold"
    elif r["vs_buy_hold"] >= 0:
        verdict = "✅ Slight edge over buy-and-hold"
    elif r["vs_buy_hold"] >= -10:
        verdict = "⚠️  Underperforming buy-and-hold slightly"
    else:
        verdict = "❌ Significantly underperforming buy-and-hold"
    print(f"  Verdict: {verdict}\n")


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backtest the Crypto Strategy Clock chart analysis engine"
    )
    parser.add_argument("--coin",    default="bitcoin",
                        help="CoinGecko coin ID (e.g. bitcoin, ethereum, solana)")
    parser.add_argument("--days",    type=int, default=365,
                        help="Days of history to test (max 365 on free tier)")
    parser.add_argument("--top5",    action="store_true",
                        help="Run on top 5 coins: BTC, ETH, SOL, BNB, XRP")
    parser.add_argument("--verbose", action="store_true",
                        help="Print every trade as it happens")
    args = parser.parse_args()

    coins = ["bitcoin", "ethereum", "solana", "binancecoin", "ripple"] \
            if args.top5 else [args.coin]

    all_results = []
    for coin in coins:
        if len(coins) > 1:
            time.sleep(2)  # rate limit between coins
        result = run_backtest(coin, days=args.days, verbose=args.verbose)
        print_results(result)
        all_results.append(result)

    # Summary table for multi-coin run
    if len(all_results) > 1:
        print(f"\n{'━'*70}")
        print(f"  {'COIN':<14} {'B&H':>8} {'Strategy':>10} {'vs B&H':>8} {'Trades':>7} {'WR%':>6} {'MaxDD':>7}")
        print(f"{'─'*70}")
        for r in all_results:
            if "error" not in r:
                edge_col = "\033[92m" if r["vs_buy_hold"] >= 0 else "\033[91m"
                RESET = "\033[0m"
                print(f"  {r['coin_id'].upper():<14} {r['buy_hold_return']:>+7.1f}% "
                      f"{r['strategy_return']:>+9.1f}% "
                      f"{edge_col}{r['vs_buy_hold']:>+7.1f}%{RESET} "
                      f"{r['total_trades']:>7} {r['win_rate']:>5.0f}% {r['max_drawdown']:>6.1f}%")
        print(f"{'━'*70}\n")

    # Save results to JSON
    output_file = "backtest_results.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    print(f"  Results saved to {output_file}")


if __name__ == "__main__":
    main()
