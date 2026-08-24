#!/usr/bin/env python3
"""
stock_backtest.py — Replay the strategy against real historical bars.

WHY THIS EXISTS:
Stock Golem's rules pass synthetic tests, which proves only that the code does
what I wrote it to do. It proves nothing about whether the pattern occurs in
real price action, how often, or whether those trades make money. This replays
actual 1-minute bars from past sessions and reports what would have happened.

NO LOOKAHEAD:
The single way backtests lie is by letting the detector see bars that hadn't
printed yet. Here, `detect_pullback` is fed `bars[:i]` and nothing more. The
exit simulation then walks forward from bar i using only high/low of each
subsequent bar. If a bar's low touches the stop AND its high touches the
target, we assume the STOP filled first — the pessimistic assumption, because
assuming otherwise is how backtests manufacture profits that never existed.

WHAT THIS CAN AND CANNOT TEST:
  Testable historically: RVOL, % change, price range, the full pullback
      pattern, entry trigger, stop/target outcomes.
  NOT testable: news catalyst (Pillar 3) and float (Pillar 5) — neither is
      available historically from Alpaca. Results therefore reflect a
      3-pillar screen, which is LOOSER than live. Expect live to trade less
      and, if the catalyst/float pillars carry signal, to perform better.

Usage:
    python stock_backtest.py                 # last 30 days, default universe
    python stock_backtest.py --days 60
    python stock_backtest.py --symbols HOWL,USDE,ABCD
"""

import argparse
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

import stock_data    as sd
import stock_signals as sig

log = logging.getLogger("stock_backtest")

DATA_URL = "https://data.alpaca.markets"

# Screen thresholds for finding historical setup days. Pillars 3 (news) and 5
# (float) are unavailable historically — see module docstring.
BT_MIN_PCT   = float(os.getenv("BT_MIN_PCT", "10.0"))
BT_MIN_RVOL  = float(os.getenv("BT_MIN_RVOL", "5.0"))
BT_PRICE_MIN = float(os.getenv("BT_PRICE_MIN", "1.0"))
BT_PRICE_MAX = float(os.getenv("BT_PRICE_MAX", "20.0"))


# ─── UNIVERSE ─────────────────────────────────────────────────────────────────

def get_universe(limit: int = 600) -> list:
    """
    Tradeable US common stock, filtered through the same instrument rules the
    live scanner uses so the backtest universe matches production.
    """
    try:
        r = requests.get(f"{sd.PAPER_URL}/v2/assets",
                         headers=sd._headers(),
                         params={"status": "active", "asset_class": "us_equity"},
                         timeout=30)
        if r.status_code != 200:
            log.error(f"Assets fetch failed: HTTP {r.status_code}")
            return []
        assets = r.json()
    except Exception as e:
        log.error(f"Assets fetch failed: {e}")
        return []

    out = []
    for a in assets:
        if not a.get("tradable") or a.get("status") != "active":
            continue
        sym = a.get("symbol", "")
        ok, _ = sd.is_tradeable_instrument(sym)
        if not ok:
            continue
        out.append(sym)
        if len(out) >= limit:
            break

    log.info(f"Universe: {len(out)} tradeable common-stock symbols")
    return out


# ─── FIND HISTORICAL SETUP DAYS ───────────────────────────────────────────────

def _multi_daily_bars(symbols: list, days: int) -> dict:
    """Batch daily bars — Alpaca accepts comma-separated symbols."""
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=days + 45)
    out: dict = {}

    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        params = {
            "symbols":   ",".join(chunk),
            "timeframe": "1Day",
            "start":     start.strftime("%Y-%m-%d"),
            "end":       end.strftime("%Y-%m-%d"),
            "limit":     10000,
            "feed":      sd.FEED,
            "adjustment": "raw",
        }
        page_token = None
        while True:
            if page_token:
                params["page_token"] = page_token
            try:
                r = requests.get(f"{DATA_URL}/v2/stocks/bars",
                                 headers=sd._headers(), params=params, timeout=30)
                if r.status_code != 200:
                    log.warning(f"Daily bars HTTP {r.status_code}")
                    break
                d = r.json()
            except Exception as e:
                log.warning(f"Daily bars failed: {e}")
                break

            for sym, bars in (d.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            page_token = d.get("next_page_token")
            if not page_token:
                break
        time.sleep(0.25)

    return out


def find_setup_days(symbols: list, days: int = 30) -> list:
    """
    Historical days where a stock met the testable pillars:
    up ≥10%, RVOL ≥5x, price in range.
    """
    daily = _multi_daily_bars(symbols, days)
    log.info(f"Daily bars for {len(daily)} symbols")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    setups = []

    for sym, bars in daily.items():
        if len(bars) < 20:
            continue
        bars.sort(key=lambda b: b["t"])
        vols = [b.get("v", 0) for b in bars]

        for i in range(20, len(bars)):
            b    = bars[i]
            date = b["t"][:10]
            if date < cutoff:
                continue

            prev_close = bars[i - 1].get("c", 0)
            if not prev_close:
                continue
            pct = (b.get("c", 0) / prev_close - 1) * 100
            if pct < BT_MIN_PCT:
                continue

            price = b.get("c", 0)
            if not (BT_PRICE_MIN <= price <= BT_PRICE_MAX):
                continue

            avg_vol = sum(vols[max(0, i - 30):i]) / max(min(i, 30), 1)
            if avg_vol <= 0:
                continue
            rvol = b.get("v", 0) / avg_vol
            if rvol < BT_MIN_RVOL:
                continue

            setups.append({
                "symbol": sym, "date": date,
                "pct": round(pct, 1), "rvol": round(rvol, 1),
                "close": round(price, 2), "volume": b.get("v", 0),
            })

    setups.sort(key=lambda s: (s["date"], -s["rvol"]))
    log.info(f"Found {len(setups)} historical setup day(s)")
    return setups


# ─── INTRADAY REPLAY ──────────────────────────────────────────────────────────

def get_session_bars(symbol: str, date: str) -> list:
    """1-minute bars for one session."""
    d = sd._get(DATA_URL, f"/v2/stocks/{symbol}/bars", {
        "timeframe": "1Min",
        "start":     f"{date}T00:00:00Z",
        "end":       f"{date}T23:59:59Z",
        "limit":     10000,
        "feed":      sd.FEED,
        "adjustment": "raw",
    })
    if not d:
        return []
    return [{
        "t": b.get("t", ""), "o": float(b.get("o", 0) or 0),
        "h": float(b.get("h", 0) or 0), "l": float(b.get("l", 0) or 0),
        "c": float(b.get("c", 0) or 0), "v": int(b.get("v", 0) or 0),
    } for b in (d.get("bars") or [])]


def replay_session(symbol: str, date: str, bars: list,
                   max_trades: int = 3) -> list:
    """
    Walk the session bar by bar. At each step the detector sees only bars that
    had already printed. When a setup is ready, simulate the trade forward.
    """
    if len(bars) < 30:
        return []

    trades = []
    i = 25
    while i < len(bars) - 2 and len(trades) < max_trades:
        window = bars[:i]                    # ← no lookahead
        pb = sig.detect_pullback(window)

        if not pb or not pb["ready"]:
            i += 1
            continue

        entry  = pb["entry"]
        stop   = pb["stop"]
        target = pb["target"]
        if entry <= stop:
            i += 1
            continue

        outcome, exit_px, exit_i = "open", bars[-1]["c"], len(bars) - 1
        for j in range(i, len(bars)):
            b = bars[j]
            hit_stop   = b["l"] <= stop
            hit_target = b["h"] >= target
            if hit_stop and hit_target:
                # Pessimistic: assume the stop filled first.
                outcome, exit_px, exit_i = "stop", stop, j
                break
            if hit_stop:
                outcome, exit_px, exit_i = "stop", stop, j
                break
            if hit_target:
                outcome, exit_px, exit_i = "target", target, j
                break

        risk_ps = entry - stop
        pnl_ps  = exit_px - entry
        trades.append({
            "symbol":     symbol,
            "date":       date,
            "entry_time": bars[i]["t"][11:16],
            "exit_time":  bars[exit_i]["t"][11:16],
            "entry":      round(entry, 4),
            "stop":       round(stop, 4),
            "target":     round(target, 4),
            "exit":       round(exit_px, 4),
            "outcome":    outcome,
            "risk_ps":    round(risk_ps, 4),
            "pnl_ps":     round(pnl_ps, 4),
            "r_multiple": round(pnl_ps / risk_ps, 2) if risk_ps else 0,
            "bars_held":  exit_i - i,
            "retrace":    pb["retrace_pct"],
        })
        i = exit_i + 2        # don't immediately re-enter the same move

    return trades


# ─── RUNNER ───────────────────────────────────────────────────────────────────

def run_backtest(days: int = 30, universe_limit: int = 600,
                 max_setups: int = 60, symbols: list = None) -> dict:
    if not sd.is_configured():
        return {"error": "ALPACA_API_KEY / ALPACA_SECRET_KEY not set"}

    universe = symbols or get_universe(universe_limit)
    if not universe:
        return {"error": "no universe"}

    setups = find_setup_days(universe, days)
    if not setups:
        return {"error": "no historical setup days found",
                "universe": len(universe), "days": days}

    all_trades, sessions_tested = [], 0
    for s in setups[:max_setups]:
        bars = get_session_bars(s["symbol"], s["date"])
        if len(bars) < 30:
            continue
        sessions_tested += 1
        for t in replay_session(s["symbol"], s["date"], bars):
            t.update({"day_pct": s["pct"], "day_rvol": s["rvol"]})
            all_trades.append(t)
        time.sleep(0.2)

    return _summarize(all_trades, setups, sessions_tested, days, len(universe))


def _summarize(trades: list, setups: list, sessions: int,
               days: int, universe: int) -> dict:
    if not trades:
        return {
            "universe": universe, "days": days,
            "setup_days_found": len(setups), "sessions_tested": sessions,
            "trades": 0,
            "note": ("No pullback setups triggered. Either the pattern is rarer "
                     "than expected on this universe, or the detector is too strict."),
        }

    wins   = [t for t in trades if t["outcome"] == "target"]
    losses = [t for t in trades if t["outcome"] == "stop"]
    opens  = [t for t in trades if t["outcome"] == "open"]

    closed = wins + losses
    win_rate = len(wins) / len(closed) * 100 if closed else 0.0
    total_r  = sum(t["r_multiple"] for t in trades)
    avg_r    = total_r / len(trades)

    gross_w = sum(t["r_multiple"] for t in trades if t["r_multiple"] > 0)
    gross_l = abs(sum(t["r_multiple"] for t in trades if t["r_multiple"] < 0))
    pf = gross_w / gross_l if gross_l else float("inf")

    by_hour: dict = {}
    for t in trades:
        hr = t["entry_time"][:2]
        by_hour.setdefault(hr, []).append(t)

    return {
        "universe":         universe,
        "days":             days,
        "setup_days_found": len(setups),
        "sessions_tested":  sessions,
        "trades":           len(trades),
        "wins":             len(wins),
        "losses":           len(losses),
        "still_open":       len(opens),
        "win_rate":         round(win_rate, 1),
        "total_r":          round(total_r, 2),
        "avg_r":            round(avg_r, 3),
        "profit_factor":    round(pf, 2) if pf != float("inf") else None,
        "avg_bars_held":    round(sum(t["bars_held"] for t in trades) / len(trades), 1),
        "by_hour": {
            h: {
                "trades": len(ts),
                "win_rate": round(
                    sum(1 for t in ts if t["outcome"] == "target") /
                    max(sum(1 for t in ts if t["outcome"] in ("target", "stop")), 1) * 100, 1),
                "total_r": round(sum(t["r_multiple"] for t in ts), 2),
            } for h, ts in sorted(by_hour.items())
        },
        "sample_trades": trades[:15],
    }


def format_report(r: dict) -> str:
    if r.get("error"):
        return f"❌ Backtest failed: {r['error']}"

    if not r.get("trades"):
        return (
            "📊 *BACKTEST — no trades*\n\n"
            f"Universe: {r['universe']} symbols over {r['days']} days\n"
            f"Setup days found: {r['setup_days_found']}\n"
            f"Sessions replayed: {r['sessions_tested']}\n\n"
            f"_{r.get('note','')}_"
        )

    lines = [
        "📊 *STOCK GOLEM BACKTEST*\n",
        f"Universe {r['universe']} symbols · {r['days']} days",
        f"{r['setup_days_found']} setup days · {r['sessions_tested']} sessions replayed",
        "",
        f"*Trades: {r['trades']}*  ({r['wins']}W / {r['losses']}L"
        + (f" / {r['still_open']} unresolved)" if r["still_open"] else ")"),
        f"Win rate: *{r['win_rate']}%*",
        f"Total R: *{r['total_r']:+.1f}R*  ·  Avg {r['avg_r']:+.2f}R/trade",
        f"Profit factor: {r['profit_factor'] if r['profit_factor'] is not None else '∞'}",
        f"Avg hold: {r['avg_bars_held']:.0f} min",
        "",
        "*By hour (UTC):*",
    ]
    for h, d in r["by_hour"].items():
        lines.append(f"  {h}:00 — {d['trades']} trades, {d['win_rate']:.0f}% win, {d['total_r']:+.1f}R")

    lines += ["", "*Sample:*"]
    for t in r["sample_trades"][:6]:
        icon = "✅" if t["outcome"] == "target" else ("❌" if t["outcome"] == "stop" else "⏳")
        lines.append(
            f"{icon} {t['symbol']} {t['date']} {t['entry_time']} "
            f"${t['entry']:.2f}→${t['exit']:.2f} ({t['r_multiple']:+.1f}R)"
        )

    lines.append(
        "\n_R = multiples of risk. +1R means the trade made exactly what it "
        "risked. Screen excludes news and float (not available historically), "
        "so live will trade less than this._"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--universe", type=int, default=600)
    ap.add_argument("--max-setups", type=int, default=60)
    ap.add_argument("--symbols", type=str, default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    syms = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or None
    res = run_backtest(days=a.days, universe_limit=a.universe,
                       max_setups=a.max_setups, symbols=syms)

    if a.json:
        print(json.dumps(res, indent=2))
    else:
        import re
        print(re.sub(r"[*_`]", "", format_report(res)))
