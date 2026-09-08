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

# ─── OUTPUT ENCODING ──────────────────────────────────────────────────────────
# On Windows, a PIPE makes Python fall back to cp1252 for stdout, which cannot
# encode the bar-chart block, the arrows, or the >= sign this tool prints. Run
# it in a terminal and it works; run `... | tee sweep.txt` and it dies with
# UnicodeEncodeError — after replaying 400 setups and collecting 147 trades,
# before printing a single result. The whole run is lost to a formatting glyph.
#
# This codebase has been bitten by the identical bug before (U+2212 in the
# daily-loss halt message), so fix the CLASS: force UTF-8 and never raise on an
# unencodable character. A mangled symbol is a cosmetic problem; a crash after
# several minutes of API calls is not.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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

CACHE_PATH = os.path.join(ROOT, "watch-out", "sweep_trades.json")


def _cache_key(days: int, max_setups: int, universe_limit: int) -> str:
    return f"{days}d-{max_setups}s-{universe_limit}u"


def load_cached_trades(days: int, max_setups: int, universe_limit: int):
    """Reuse a previous collection so repeated analysis sees IDENTICAL data."""
    try:
        import json
        with open(CACHE_PATH, encoding="utf-8") as f:
            blob = json.load(f)
    except Exception:
        return None
    if blob.get("key") != _cache_key(days, max_setups, universe_limit):
        return None
    return blob.get("trades") or None


def save_cached_trades(trades: list, days: int, max_setups: int,
                       universe_limit: int):
    try:
        import json
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"key": _cache_key(days, max_setups, universe_limit),
                       "collected_at": __import__("datetime").datetime.now()
                                       .isoformat(timespec="seconds"),
                       "trades": trades}, f)
    except Exception as e:
        print(f"  (could not cache trades: {e})")


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


# ─── MONTE CARLO ──────────────────────────────────────────────────────────────

def bootstrap(trades: list, n_runs: int = 2000, seed: int = 0) -> Optional[dict]:
    """
    Resample the trade list WITH replacement, n_runs times, and return the
    distribution of mean R.

    WHY. Every number above is a point estimate with no error bars, and we
    already watched one move a long way: MIN_RVOL=5.0 read +0.385R, then
    +0.800R, then +0.473R across three runs of the same strategy. Without a
    spread there is no way to tell a real difference between two cells from
    noise — which is why the plateau reading had to be hedged in prose.

    Bootstrapping asks: given these exact trades, how much would the mean move
    if luck had dealt them in a different order and mix? If the 5th-95th
    percentile straddles zero, the cell is not evidence of anything.

    Returns percentiles plus P(profitable) — the share of resamples with a
    positive mean.
    """
    if not trades:
        return None
    import random
    rng = random.Random(seed)          # seeded: same data -> same verdict
    rs = [t["r_multiple"] for t in trades]
    n = len(rs)
    means = []
    for _ in range(n_runs):
        means.append(sum(rng.choice(rs) for _ in range(n)) / n)
    means.sort()

    def pct(p):
        return means[min(n_runs - 1, max(0, int(p / 100 * n_runs)))]

    return {
        "n":        n,
        "mean":     sum(rs) / n,
        "p05":      pct(5),
        "p50":      pct(50),
        "p95":      pct(95),
        "p_profit": sum(1 for m in means if m > 0) / n_runs * 100,
    }


def monte_carlo_sweep(trades: list, field: str, values: list, label: str,
                      n_runs: int = 2000, **fixed) -> str:
    """
    Bootstrap EVERY cell, not one.

    The source is explicit about this: "If you do a Monte Carlo on just one
    parameter set you're doing it completely wrong." One cell tells you the
    noise inside that cell. Every cell tells you whether the DIFFERENCES between
    cells survive their own error bars — which is the actual question when
    picking a threshold.
    """
    L = [f"\n{label}", "-" * 72,
         f"  {'value':>7}  {'n':>4}  {'mean':>8}  {'5th':>8}  {'95th':>8}  "
         f"{'P(profit)':>9}  verdict"]

    any_cell = False
    for v in values:
        kwargs = dict(fixed); kwargs[field] = v
        sel = [t for t in trades
               if t.get("rvol", 0) >= kwargs.get("min_rvol", 0)
               and kwargs.get("price_min", 0) <= t.get("price", 0)
                   <= kwargs.get("price_max", 1e9)
               and t.get("pct", 0) >= kwargs.get("min_pct", 0)]
        if len(sel) < MIN_SAMPLE:
            L.append(f"  {v:>7}  {len(sel):>4}  — below {MIN_SAMPLE}, not shown")
            continue
        any_cell = True
        b = bootstrap(sel, n_runs=n_runs)
        # A cell whose 5th percentile is under zero cannot be distinguished
        # from a losing setting by this data, however good its mean looks.
        verdict = ("solid"  if b["p05"] > 0 else
                   "shaky"  if b["p_profit"] >= 90 else
                   "NOISE")
        L.append(f"  {v:>7}  {b['n']:>4}  {b['mean']:>+8.3f}  {b['p05']:>+8.3f}  "
                 f"{b['p95']:>+8.3f}  {b['p_profit']:>8.0f}%  {verdict}")

    if not any_cell:
        L.append(f"  no cell reached {MIN_SAMPLE} trades — widen --days")
    return "\n".join(L)


# ─── WALK-FORWARD ─────────────────────────────────────────────────────────────

def split_by_date(trades: list, frac: float = 0.5) -> tuple:
    """
    Chronological split. NOT random — a random split leaks the future into the
    training half, because trades from the same week share market conditions.
    """
    ordered = sorted(trades, key=lambda t: t.get("date", ""))
    cut = int(len(ordered) * frac)
    return ordered[:cut], ordered[cut:]


def best_setting(trades: list, field: str, values: list, **fixed):
    """Highest-expectancy value that still clears MIN_SAMPLE. None if none do."""
    rows = [r for r in sweep_1d(trades, field, values, **fixed) if r["reliable"]]
    if not rows:
        return None
    return max(rows, key=lambda r: r["expectancy"])


def walk_forward(trades: list, field: str, values: list, label: str,
                 **fixed) -> str:
    """
    Choose a threshold on the FIRST half, then measure it on the second half
    without re-tuning.

    This is the only test here that can distinguish a real effect from a shape
    fitted to one particular six months. Everything above describes the sample;
    this asks whether the sample generalises.

    A setting that wins in-sample and collapses out-of-sample was curve-fit —
    even though we never consciously fitted it, because CHOOSING the best cell
    from a sweep is fitting, whatever the number's origin.
    """
    train, test = split_by_date(trades)
    L = [f"\n{label}", "-" * 64,
         f"  train: {len(train)} trades ({train[0]['date']} → {train[-1]['date']})"
         if train else "  train: empty",
         f"  test:  {len(test)} trades ({test[0]['date']} → {test[-1]['date']})"
         if test else "  test:  empty"]

    if not train or not test:
        L.append("  not enough history to split — widen --days")
        return "\n".join(L)

    pick = best_setting(train, field, values, **fixed)
    if not pick:
        L.append(f"  no cell on the training half reached {MIN_SAMPLE} trades — "
                 f"cannot choose a setting to test")
        return "\n".join(L)

    kwargs = dict(fixed); kwargs[field] = pick["value"]
    out = evaluate(test, **kwargs)

    L.append(f"\n  chosen on train: {field}={pick['value']} "
             f"(exp {pick['expectancy']:+.3f}R, n={pick['n']})")
    if not out["reliable"]:
        L.append(f"  out-of-sample:   n={out['n']} — below {MIN_SAMPLE}, "
                 f"NO VERDICT POSSIBLE")
        return "\n".join(L)

    L.append(f"  out-of-sample:   exp {out['expectancy']:+.3f}R, "
             f"win {out['win_rate']:.0f}%, n={out['n']}")

    drop = pick["expectancy"] - out["expectancy"]
    if out["expectancy"] <= 0:
        L.append("  VERDICT: FAILED — profitable in training, unprofitable out "
                 "of sample. This setting was fitted to the first half.")
    elif drop > pick["expectancy"] * 0.5:
        L.append(f"  VERDICT: WEAK — held its sign but lost {drop:+.3f}R "
                 f"({drop / pick['expectancy'] * 100:.0f}%). Expect the live "
                 f"result nearer the out-of-sample number.")
    else:
        L.append("  VERDICT: HELD — similar performance on data it was not "
                 "chosen from. This is the only encouraging outcome available.")
    return "\n".join(L)


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

    # ── monte carlo ─────────────────────────────────────────────────────────
    # A strong, consistent edge: 200 trades, every one +1R.
    strong = [{"rvol": 9, "price": 5, "pct": 20, "r_multiple": 1.0}] * 200
    b = bootstrap(strong, n_runs=500)
    check("a certain winner has a 5th percentile above zero", b["p05"] > 0,
          f"p05={b['p05']:+.3f}")
    check("and 100% of resamples profitable", b["p_profit"] == 100.0)

    # A coin flip: +1R and -1R in equal measure. Mean ~0, must NOT read solid.
    coin = ([{"rvol": 9, "price": 5, "pct": 20, "r_multiple": 1.0}] * 100
            + [{"rvol": 9, "price": 5, "pct": 20, "r_multiple": -1.0}] * 100)
    bc = bootstrap(coin, n_runs=500)
    check("a coin flip straddles zero", bc["p05"] < 0 < bc["p95"],
          f"[{bc['p05']:+.3f}, {bc['p95']:+.3f}]")
    check("coin flip is ~50% profitable", 30 < bc["p_profit"] < 70,
          f"{bc['p_profit']:.0f}%")

    # THE case that matters: a positive MEAN that is not distinguishable from
    # noise. Three big winners carrying twenty losers — exactly the shape a
    # small sample produces, and exactly what a point estimate hides.
    lucky = ([{"rvol": 9, "price": 5, "pct": 20, "r_multiple": 9.0}] * 3
             + [{"rvol": 9, "price": 5, "pct": 20, "r_multiple": -1.0}] * 20)
    bl = bootstrap(lucky, n_runs=800)
    check("a positive mean carried by outliers is flagged, not celebrated",
          bl["mean"] > 0 and bl["p05"] < 0,
          f"mean={bl['mean']:+.3f} but p05={bl['p05']:+.3f}")

    # Every rendered string must survive a Windows cp1252 pipe. The real run
    # died on U+2265 after replaying 400 setups — the results were computed and
    # then thrown away by a print. Render each block and encode it the way a
    # piped Windows stdout would.
    rendered = "\n".join([
        format_sweep(sweep_1d(trades, "min_rvol", [2, 5]), "enc"),
        monte_carlo_sweep(trades, "min_rvol", [2], "enc", n_runs=50),
        walk_forward([{**t, "date": f"2026-0{1 + i % 2}-01"}
                      for i, t in enumerate(trades)],
                     "min_rvol", [2], "enc"),
    ])
    try:
        rendered.encode("cp1252")
        cp1252_safe = True
    except UnicodeEncodeError:
        cp1252_safe = False
    check("output survives a Windows cp1252 pipe", cp1252_safe or
          sys.stdout.encoding.lower().startswith("utf"),
          "stdout=" + str(sys.stdout.encoding))
    check("stdout was reconfigured to UTF-8",
          str(sys.stdout.encoding).lower().replace("-", "").startswith("utf8"),
          str(sys.stdout.encoding))

    # ── reproducibility ─────────────────────────────────────────────────────
    # The universe used to be "first N in whatever order Alpaca returned", so
    # two runs measured different symbols and the difference looked like a
    # result. Same input must give the same universe.
    import stock_backtest as _bt

    class _FakeResp:
        status_code = 200
        def __init__(self, order): self._order = order
        def json(self):
            return [{"symbol": s, "tradable": True, "status": "active"}
                    for s in self._order]

    # 3-letter tickers ending in X: under the 5-char limit and not matching the
    # warrant/unit suffix regex, so they survive is_tradeable_instrument.
    # (First attempt used SYM000 — six characters — and the filter dropped every
    # one, producing an empty universe that looked like a code failure.)
    _A = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
    syms = [f"{a}{b}X" for a in _A for b in _A][:300]
    import random as _r
    shuffled_a = syms[:];  _r.Random(1).shuffle(shuffled_a)
    shuffled_b = syms[:];  _r.Random(2).shuffle(shuffled_b)

    _orig_get = _bt.requests.get
    try:
        _bt.requests.get = lambda *a, **k: _FakeResp(shuffled_a)
        u1 = _bt.get_universe(limit=50)
        _bt.requests.get = lambda *a, **k: _FakeResp(shuffled_b)
        u2 = _bt.get_universe(limit=50)
    finally:
        _bt.requests.get = _orig_get

    check("universe is identical regardless of API response order", u1 == u2,
          f"{len(u1)} symbols, first={u1[0] if u1 else None}")
    check("universe spans the alphabet, not just the front",
          bool(u1) and u1[-1] > u1[len(u1) // 2] > u1[0],
          f"{u1[0]}..{u1[-1]}" if u1 else "empty")

    # Cache must round-trip exactly, and must NOT serve a different shape.
    import tempfile
    _orig_cache = globals()["CACHE_PATH"]
    globals()["CACHE_PATH"] = os.path.join(tempfile.mkdtemp(), "c.json")
    try:
        save_cached_trades(trades, 180, 400, 600)
        same = load_cached_trades(180, 400, 600)
        other = load_cached_trades(90, 400, 600)
        check("cache round-trips the exact trade set", same == trades,
              f"{len(same or [])} vs {len(trades)}")
        check("cache refuses a different parameter set", other is None)
    finally:
        globals()["CACHE_PATH"] = _orig_cache

    check("bootstrap is deterministic for a given seed",
          bootstrap(coin, n_runs=200)["p50"] == bootstrap(coin, n_runs=200)["p50"])
    check("empty input returns None, not a fake distribution",
          bootstrap([]) is None)

    # Assert the VERDICT is right, not merely that a verdict appeared.
    # The first version of this check only counted verdict words, so a mutation
    # hardcoding "solid" for every cell sailed through — a test that cannot
    # distinguish a correct answer from a constant is not a test.
    mc_strong = monte_carlo_sweep(strong, "min_rvol", [2], "demo", n_runs=300)
    check("a certain winner is called solid",
          "solid" in mc_strong and "NOISE" not in mc_strong)

    mc_coin = monte_carlo_sweep(coin, "min_rvol", [2], "demo", n_runs=300)
    check("a coin flip is called NOISE, never solid",
          "NOISE" in mc_coin and "solid" not in mc_coin)

    mc_lucky = monte_carlo_sweep(lucky, "min_rvol", [2], "demo", n_runs=300)
    check("an outlier-carried mean is not called solid",
          "solid" not in mc_lucky)

    mc_both = monte_carlo_sweep(strong + coin, "min_rvol", [2, 99],
                                "demo", n_runs=200)
    check("monte carlo evaluates every cell, not one",
          mc_both.count("\n  ") >= 2)

    # ── walk-forward ────────────────────────────────────────────────────────
    early = [{"date": f"2026-01-{d:02d}", "rvol": 9, "price": 5, "pct": 20,
              "r_multiple": 1.0} for d in range(1, 21)]
    late  = [{"date": f"2026-06-{d:02d}", "rvol": 9, "price": 5, "pct": 20,
              "r_multiple": -1.0} for d in range(1, 21)]
    tr, te = split_by_date(early + late)
    check("split is chronological, not random",
          all(t["date"].startswith("2026-01") for t in tr)
          and all(t["date"].startswith("2026-06") for t in te),
          f"train {tr[0]['date']}..{tr[-1]['date']}")

    # Profitable in training, a disaster after — must be reported as FAILED.
    wf = walk_forward(early + late, "min_rvol", [2, 9], "demo")
    check("a setting that dies out of sample is called FAILED",
          "FAILED" in wf)

    # Consistent throughout — must be reported as HELD.
    steady = [{"date": f"2026-0{m}-{d:02d}", "rvol": 9, "price": 5, "pct": 20,
               "r_multiple": 1.0 if d % 3 else -1.0}
              for m in (1, 6) for d in range(1, 21)]
    check("a setting that survives out of sample is called HELD",
          "HELD" in walk_forward(steady, "min_rvol", [2, 9], "demo"))

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
    ap.add_argument("--fresh", action="store_true",
                    help="ignore the cached trade set and re-collect")
    a = ap.parse_args()

    if a.self_test:
        sys.exit(self_test())

    print("PARAMETER STABILITY SWEEP")
    print(f"  lookback {a.days} days · min sample {MIN_SAMPLE} trades/cell\n")

    # Reuse the previous collection unless asked not to. Two analyses of the
    # same cached trades are comparable; two fresh collections are not
    # necessarily, because the market data underneath keeps moving. Comparing
    # runs was how a +0.473R result became -0.182R overnight and looked like a
    # finding rather than a different sample.
    trades = None if a.fresh else load_cached_trades(a.days, a.max_setups,
                                                     a.universe_limit)
    if trades:
        print(f"  using {len(trades)} CACHED trade(s) — "
              f"run with --fresh to re-collect\n")
    else:
        trades = collect_trades(a.days, a.max_setups, a.universe_limit)
        if trades:
            save_cached_trades(trades, a.days, a.max_setups, a.universe_limit)
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

    # ── Monte Carlo on EVERY cell ─────────────────────────────────────────────
    print("\n" + "=" * 72)
    print(" MONTE CARLO — error bars on every cell, not just the best one")
    print("=" * 72)

    print(monte_carlo_sweep(trades, "min_rvol", [2, 3, 4, 5, 6, 7, 8, 10],
                            "MIN_RVOL", price_min=sig.PRICE_MIN,
                            price_max=sig.PRICE_MAX, min_pct=sig.MIN_PCT_CHANGE))

    print(monte_carlo_sweep(trades, "price_min", [0.5, 1, 2, 3, 4, 5],
                            "PRICE_MIN", min_rvol=sig.MIN_RVOL,
                            price_max=sig.PRICE_MAX, min_pct=sig.MIN_PCT_CHANGE))

    print(monte_carlo_sweep(trades, "min_pct", [5, 10, 15, 20, 30, 50],
                            "MIN_PCT_CHANGE", min_rvol=sig.MIN_RVOL,
                            price_min=sig.PRICE_MIN, price_max=sig.PRICE_MAX))

    print("\n  solid = 5th percentile above zero — the edge survives bad luck")
    print("  shaky = mostly profitable but the 5th percentile dips below zero")
    print("  NOISE = cannot be told apart from a losing setting by this data")

    # ── Walk-forward: the only test that asks whether any of this generalises ──
    print("\n" + "=" * 64)
    print(" WALK-FORWARD — chosen on the first half, measured on the second")
    print("=" * 64)

    print(walk_forward(trades, "min_rvol", [2, 3, 4, 5, 6, 7, 8, 10],
                       "MIN_RVOL", price_min=sig.PRICE_MIN,
                       price_max=sig.PRICE_MAX, min_pct=sig.MIN_PCT_CHANGE))

    print(walk_forward(trades, "price_min", [0.5, 1, 2, 3, 4, 5],
                       "PRICE_MIN", min_rvol=sig.MIN_RVOL,
                       price_max=sig.PRICE_MAX, min_pct=sig.MIN_PCT_CHANGE))

    print(walk_forward(trades, "min_pct", [5, 10, 15, 20, 30, 50],
                       "MIN_PCT_CHANGE", min_rvol=sig.MIN_RVOL,
                       price_min=sig.PRICE_MIN, price_max=sig.PRICE_MAX))

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
