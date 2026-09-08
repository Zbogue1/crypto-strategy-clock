#!/usr/bin/env python3
"""
tools/param_sweep.py — Is a threshold on a plateau, or on a knife edge?

WHY THIS EXISTS
Every screening number in Stock Golem was transcribed from a Ross Cameron video:
MIN_RVOL 5.0, PRICE_MIN 2.0, MIN_PCT_CHANGE 10.0, RVOL lookback 50 days. None of
them was ever tested against our own data.

That is not classic curve-fitting — we did not fit them to a backtest — but it
carries its own risk. Ross trades a different account size, a different broker
and a PAID consolidated feed. We are on Alpaca's free IEX feed, which sees a few
percent of real volume. His 5x is not necessarily our 5x.

A robust threshold sits on a BROAD PLATEAU: nearby settings also work. A fragile
one sits on a spike, and a spike means the result came from luck in the specific
sample, not from an effect. This sweeps each threshold and prints the shape so
you can see which one you have.

HOW IT AVOIDS BEING EXPENSIVE
Screening thresholds decide WHICH setups qualify. They do not change what the
price did afterwards. So:

    1. Find setups once, with the LOOSEST thresholds (a superset).
    2. Replay each one once to get its real outcome.
    3. Evaluate every threshold combination against those cached outcomes.

One expensive pass, then arbitrarily many evaluations.

WHAT IT CANNOT TELL YOU
Sample size. With a handful of setups per month, win rate is dominated by luck —
good luck inflates a result and it regresses next period. Any cell below
MIN_SAMPLE is printed as "n/a" rather than a number, because a 100% win rate on
3 trades is not a finding. Read the SHAPE across cells, never one cell.

USAGE
    python3 tools/param_sweep.py --days 60
    python3 tools/param_sweep.py --days 90 --max-setups 200
    python3 tools/param_sweep.py --self-test      # no network, checks the maths
"""

import argparse
import os
import sys
from typing import Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# Below this many trades a cell is noise, not a measurement.
MIN_SAMPLE = int(os.getenv("SWEEP_MIN_SAMPLE", "12"))


# ─── PURE EVALUATION (no network — this is the part under test) ───────────────

def evaluate(trades: list, min_rvol: float = 0.0, price_min: float = 0.0,
             price_max: float = 1e9, min_pct: float = 0.0) -> dict:
    """
    Aggregate cached trade outcomes under one set of screening thresholds.

    Each trade needs: rvol, price, pct, r_multiple (profit in units of risk).
    R-multiples rather than dollars, so results don't move when position sizing
    changes.
    """
    sel = [t for t in trades
           if t.get("rvol", 0) >= min_rvol
           and price_min <= t.get("price", 0) <= price_max
           and t.get("pct", 0) >= min_pct]

    n = len(sel)
    if n == 0:
        return {"n": 0, "win_rate": None, "expectancy": None,
                "profit_factor": None, "reliable": False}

    wins   = [t["r_multiple"] for t in sel if t["r_multiple"] > 0]
    losses = [t["r_multiple"] for t in sel if t["r_multiple"] <= 0]
    gross_w = sum(wins)
    gross_l = abs(sum(losses))

    return {
        "n":          n,
        "win_rate":   len(wins) / n * 100,
        "expectancy": sum(t["r_multiple"] for t in sel) / n,
        # No losses at all is not an infinite profit factor, it's a tiny sample.
        "profit_factor": (gross_w / gross_l) if gross_l > 0 else None,
        "reliable":   n >= MIN_SAMPLE,
    }


def sweep_1d(trades: list, field: str, values: list, **fixed) -> list:
    """Evaluate one threshold across a range, holding the others fixed."""
    out = []
    for v in values:
        kwargs = dict(fixed)
        kwargs[field] = v
        r = evaluate(trades, **kwargs)
        r["value"] = v
        out.append(r)
    return out


def format_sweep(rows: list, label: str, unit: str = "") -> str:
    """
    Render a sweep as a bar chart of expectancy.

    Reading it: a PLATEAU (several adjacent cells with similar, positive
    expectancy) means the threshold is robust. A SPIKE — one good cell
    surrounded by bad ones — means that number was luck.
    """
    L = [f"\n{label}", "-" * 64]
    usable = [r for r in rows if r["reliable"] and r["expectancy"] is not None]
    if not usable:
        L.append(f"  every cell below {MIN_SAMPLE} trades — no reading possible")
        L.append("  widen --days, or loosen the fixed thresholds")
        return "\n".join(L)

    hi = max(abs(r["expectancy"]) for r in usable) or 1.0
    for r in rows:
        v = f"{r['value']}{unit}"
        if not r["reliable"]:
            L.append(f"  {v:>8}  n={r['n']:<4} — below {MIN_SAMPLE}, not shown")
            continue
        e = r["expectancy"]
        bar = "█" * max(1, int(abs(e) / hi * 28))
        sign = "+" if e >= 0 else "-"
        L.append(f"  {v:>8}  n={r['n']:<4} exp={e:+.3f}R  win={r['win_rate']:.0f}%  "
                 f"{sign}{bar}")
    return "\n".join(L)


# ─── DATA COLLECTION (network — runs on a machine with Alpaca keys) ──────────

def collect_trades(days: int, max_setups: int, universe_limit: int) -> list:
    """
    One expensive pass: find setups with permissive thresholds, replay each.

    Deliberately loose here — the sweep tightens afterwards. Screening out a
    setup at this stage removes it from EVERY cell in the sweep and quietly
    truncates the surface we are trying to see.
    """
    import stock_backtest as bt
    import stock_data as sd

    if not sd.is_configured():
        raise SystemExit("ALPACA_API_KEY / ALPACA_SECRET_KEY not set — "
                         "this needs live historical data.")

    # Permissive superset.
    bt.BT_MIN_PCT   = 5.0
    bt.BT_MIN_RVOL  = 2.0
    bt.BT_PRICE_MIN = 0.5
    bt.BT_PRICE_MAX = 50.0

    universe = bt.get_universe(limit=universe_limit) \
        if hasattr(bt, "get_universe") else []
    setups = bt.find_setup_days(universe, days=days)[:max_setups]
    print(f"  {len(setups)} candidate setup day(s) to replay")

    trades = []
    for k, s in enumerate(setups, 1):
        bars = bt.get_session_bars(s["symbol"], s["date"])
        if not bars:
            continue
        for t in bt.replay_session(s["symbol"], s["date"], bars):
            # replay_session already returns r_multiple (pnl_ps / risk_ps), and
            # its exit key is "exit", not "exit_price". Recomputing it here from
            # a key that doesn't exist would have produced silent zeros — every
            # cell reading exactly 0.000R, which looks like a real (terrible)
            # result rather than a bug.
            if t.get("risk_ps", 0) <= 0:
                continue
            trades.append({
                "symbol": s["symbol"], "date": s["date"],
                "rvol": s["rvol"], "price": s["close"], "pct": s["pct"],
                "r_multiple": t["r_multiple"],   # sizing-independent
                "outcome": t.get("outcome"),
            })
        if k % 10 == 0:
            print(f"    replayed {k}/{len(setups)} …")

    print(f"  {len(trades)} simulated trade(s) collected")
    return trades


# ─── SELF TEST ────────────────────────────────────────────────────────────────

def self_test() -> int:
    """Check the aggregation maths without touching the network."""
    fails = []

    def check(name, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f" — {detail}" if detail else ""))
        if not ok:
            fails.append(name)

    trades = ([{"rvol": 8, "price": 5, "pct": 20, "r_multiple": 2.0}] * 10 +
              [{"rvol": 8, "price": 5, "pct": 20, "r_multiple": -1.0}] * 10 +
              [{"rvol": 3, "price": 5, "pct": 20, "r_multiple": -1.0}] * 10)

    all_r = evaluate(trades)
    check("counts every trade with no thresholds", all_r["n"] == 30)

    hi = evaluate(trades, min_rvol=5.0)
    check("rvol filter excludes the low-rvol block", hi["n"] == 20)
    check("win rate correct", abs(hi["win_rate"] - 50.0) < 0.01,
          f"{hi['win_rate']:.1f}%")
    check("expectancy correct", abs(hi["expectancy"] - 0.5) < 0.001,
          f"{hi['expectancy']:+.3f}R")
    check("profit factor correct", abs(hi["profit_factor"] - 2.0) < 0.001,
          f"{hi['profit_factor']:.2f}")

    # An all-winners sample must NOT report an infinite profit factor.
    only_wins = evaluate([{"rvol": 9, "price": 5, "pct": 20, "r_multiple": 1.0}] * 3)
    check("no losses -> profit factor is None, not infinity",
          only_wins["profit_factor"] is None)
    check("3 trades flagged unreliable", only_wins["reliable"] is False)

    empty = evaluate(trades, min_rvol=99)
    check("empty selection is safe", empty["n"] == 0 and empty["win_rate"] is None)

    rows = sweep_1d(trades, "min_rvol", [2, 5, 9])
    check("sweep returns one row per value", len(rows) == 3)
    check("sweep is monotone in selectivity",
          rows[0]["n"] >= rows[1]["n"] >= rows[2]["n"],
          f"{[r['n'] for r in rows]}")

    txt = format_sweep(rows, "demo")
    check("unreliable cells are not printed as numbers",
          "not shown" in txt or all(r["reliable"] for r in rows))

    print()
    if fails:
        print(f"SELF-TEST FAILED: {', '.join(fails)}")
        return len(fails)
    print("SELF-TEST CLEAN — aggregation maths verified.")
    return 0


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Parameter stability sweep")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--max-setups", type=int, default=120)
    ap.add_argument("--universe-limit", type=int, default=600)
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(self_test())

    print("PARAMETER STABILITY SWEEP")
    print(f"  lookback {a.days} days · min sample {MIN_SAMPLE} trades/cell\n")

    trades = collect_trades(a.days, a.max_setups, a.universe_limit)
    if not trades:
        print("\nNo trades collected — nothing to sweep. Widen --days.")
        sys.exit(1)

    import stock_signals as sig
    print(f"\nCurrent live settings: RVOL≥{sig.MIN_RVOL} · "
          f"${sig.PRICE_MIN:.0f}-${sig.PRICE_MAX:.0f} · ≥{sig.MIN_PCT_CHANGE}%")

    print(format_sweep(
        sweep_1d(trades, "min_rvol", [2, 3, 4, 5, 6, 7, 8, 10],
                 price_min=sig.PRICE_MIN, price_max=sig.PRICE_MAX,
                 min_pct=sig.MIN_PCT_CHANGE),
        "MIN_RVOL sweep (live setting: %.1f)" % sig.MIN_RVOL, "x"))

    print(format_sweep(
        sweep_1d(trades, "price_min", [0.5, 1, 2, 3, 4, 5],
                 min_rvol=sig.MIN_RVOL, price_max=sig.PRICE_MAX,
                 min_pct=sig.MIN_PCT_CHANGE),
        "PRICE_MIN sweep (live setting: $%.0f)" % sig.PRICE_MIN, ""))

    print(format_sweep(
        sweep_1d(trades, "min_pct", [5, 10, 15, 20, 30, 50],
                 min_rvol=sig.MIN_RVOL, price_min=sig.PRICE_MIN,
                 price_max=sig.PRICE_MAX),
        "MIN_PCT_CHANGE sweep (live setting: %.0f%%)" % sig.MIN_PCT_CHANGE, "%"))

    print("\n" + "=" * 64)
    print(" HOW TO READ THIS")
    print(" A PLATEAU — several adjacent cells with similar positive")
    print(" expectancy — means the threshold is robust and the exact number")
    print(" doesn't much matter.")
    print("")
    print(" A SPIKE — one good cell surrounded by bad ones — means that")
    print(" number worked by luck in this sample. Do not trust it.")
    print("")
    print(f" Cells under {MIN_SAMPLE} trades are not shown at all. A 100% win")
    print(" rate on 3 trades is not a finding.")
    print("=" * 64)


if __name__ == "__main__":
    main()
