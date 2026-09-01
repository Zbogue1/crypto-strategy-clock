#!/usr/bin/env python3
"""
tools/simulate.py — Fire a real action now instead of waiting for a live signal.

THE PROBLEM THIS SOLVES
The tranche bug ($MADE flagged as harvested while holding 100% of its units)
existed for days because nothing exercised a tranche until a token actually
doubled. The stop-loss had the same bug and would have waited for a real 35%
drawdown to reveal it. Waiting for the market to test your code is the slowest
and most expensive feedback loop available.

So: after changing anything that closes, opens, scores or settles a position,
fire that exact action synthetically and look at what really happens.

WHAT IS REAL AND WHAT IS FAKE
Only the NETWORK is mocked — Alpaca, Kalshi, DexScreener, Telegram, Anthropic.
Everything else runs for real: the actual portfolio modules, the actual state
transitions, the actual arithmetic. A test that mocks the portfolio is testing
the mock.

    real:  position maths, flags, records, cash, invariants, exit logic
    fake:  HTTP responses, Telegram sends, LLM calls

That boundary means this catches logic bugs and cannot catch API schema
changes — Kalshi renaming `yes_bid` to `yes_bid_dollars` would sail through.
Recorded-response replay would cover that; it isn't built.

SAFETY
PAPER_TEST_MODE=1 is forced before any import. Portfolio writes are redirected
to a temp dir and Redis is disabled. A simulation must never be able to touch
the live book — that already happened once with test trades reaching production
state.

USAGE
    python3 tools/simulate.py --list
    python3 tools/simulate.py tranche_fires
    python3 tools/simulate.py --all
"""

import argparse
import json
import os
import sys
import tempfile
import types
from datetime import datetime, timezone, timedelta

# ─── SAFETY: force isolation BEFORE anything imports a portfolio module ───────
_TMP = tempfile.mkdtemp(prefix="sim_")
os.environ["PAPER_TEST_MODE"] = "1"
os.environ["STOCK_PORTFOLIO_FILE"] = os.path.join(_TMP, "stock.json")
os.environ["KALSHI_PORTFOLIO_FILE"] = os.path.join(_TMP, "kalshi.json")
os.environ["KALSHI_EVENT_PORTFOLIO_FILE"] = os.path.join(_TMP, "kevent.json")
os.environ["RAILWAY_VOLUME_MOUNT_PATH"] = _TMP
for _v in ("UPSTASH_REDIS_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL",
           "REDIS_URL", "UPSTASH_REDIS_TOKEN", "UPSTASH_REDIS_REST_TOKEN",
           "KV_REST_API_TOKEN", "REDIS_TOKEN"):
    os.environ.pop(_v, None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# ─── FAKE THE NETWORK, NOTHING ELSE ───────────────────────────────────────────

SENT: list = []          # every Telegram message the run would have sent


def _install_fakes():
    """Replace only the outside world."""
    fake_anthropic = types.ModuleType("anthropic")
    fake_anthropic.Anthropic = lambda **k: None
    fake_anthropic.APIStatusError = Exception
    fake_anthropic.APIError = Exception
    sys.modules.setdefault("anthropic", fake_anthropic)

    # fomo_tracker imports flask at module scope for its webhook server. The
    # HTTP server is outside-world infrastructure, so it gets faked like any
    # other network dependency — and stubbing it here keeps the harness working
    # on machines where flask isn't installed, rather than making a scenario's
    # result depend on the environment it runs in.
    if "flask" not in sys.modules:
        fake_flask = types.ModuleType("flask")

        class _App:
            def __init__(self, *a, **k): pass
            def route(self, *a, **k):
                return lambda fn: fn          # decorator passthrough
            def run(self, *a, **k): pass
            def add_url_rule(self, *a, **k): pass

        fake_flask.Flask   = _App
        fake_flask.request = types.SimpleNamespace(
            json=None, args={}, headers={}, get_json=lambda *a, **k: None)
        fake_flask.jsonify = lambda *a, **k: (a[0] if len(a) == 1 else dict(**k))
        fake_flask.Response = lambda *a, **k: None
        sys.modules["flask"] = fake_flask

    import requests

    class _Resp:
        status_code = 200
        text = "{}"
        content = b""

        def json(self):
            return {}

    def _blocked(*a, **k):
        raise AssertionError(
            "A simulation tried to make a real network call. Every external "
            "dependency must be faked by the scenario — an unmocked call means "
            "the test is not hermetic and could hit a live API."
        )

    requests.get = _blocked
    requests.post = _blocked
    requests.put = _blocked
    return requests


REQUESTS = _install_fakes()


def capture_telegram():
    """Point every bot's sender at SENT so alerts can be asserted on."""
    SENT.clear()
    try:
        import fomo_telegram
        fomo_telegram.send = lambda t, *a, **k: (SENT.append(t), True)[1]
    except Exception:
        pass
    try:
        import fomo_exit
        fomo_exit._send_telegram = lambda t: SENT.append(t)
        fomo_exit._send_telegram_button_local = lambda m, b, c: SENT.append(m)
    except Exception:
        pass
    try:
        import kalshi_telegram
        kalshi_telegram.send_telegram = lambda t, *a, **k: (SENT.append(t), True)[1]
    except Exception:
        pass
    try:
        import stock_telegram
        stock_telegram.send = lambda t, *a, **k: (SENT.append(t), True)[1]
    except Exception:
        pass


# ─── SCENARIOS ────────────────────────────────────────────────────────────────
# Each returns (ok: bool, lines: list[str]). They assert INVARIANTS, not exact
# numbers, so they stay valid as thresholds change.

SCENARIOS = {}


def scenario(name, desc):
    def deco(fn):
        SCENARIOS[name] = (desc, fn)
        return fn
    return deco


@scenario("tranche_fires", "FOMO tranche 1 at 2x — sells, flags and records together")
def sim_tranche_fires():
    import fomo_exit as FE, fomo_portfolio as FP
    capture_telegram()
    FP.save_fomo_portfolio = lambda s: None
    FP.sync_fomo_state_to_github = lambda: None

    h = {"token_ticker": "SIM", "units": 300_000, "spent": 100.0,
         "entry_price": 0.0006, "contract_address": "SIMC", "position_id": "p1"}
    st = {"cash": 0.0, "tranche_sales": []}

    net = FE._execute_partial_sell(h, 1/3, 0.0012, "tranche_1_2x", st,
                                   flags=["tranche_1_sold"])
    L = [f"sold ${net:.2f}", f"units {300_000} -> {h['units']:.0f}",
         f"flag={h.get('tranche_1_sold')}", f"records={len(st['tranche_sales'])}",
         f"cash +${st['cash']:.2f}"]
    ok = (net > 0 and h.get("tranche_1_sold") is True
          and len(st["tranche_sales"]) == 1
          and abs(h["units"] - 200_000) < 1
          and abs(st["cash"] - net) < 0.01)
    return ok, L


@scenario("tranche_aborts", "FOMO tranche on a bad price — nothing sold, nothing flagged")
def sim_tranche_aborts():
    import fomo_exit as FE, fomo_portfolio as FP
    capture_telegram()
    FP.save_fomo_portfolio = lambda s: None
    FP.sync_fomo_state_to_github = lambda: None

    h = {"token_ticker": "SIM", "units": 300_000, "spent": 100.0,
         "entry_price": 0.0006, "contract_address": "SIMC"}
    st = {"cash": 0.0, "tranche_sales": []}
    net = FE._execute_partial_sell(h, 1/3, 9_999.0, "tranche_1_2x", st,
                                   flags=["tranche_1_sold"])
    L = [f"returned ${net:.2f}", f"flag={h.get('tranche_1_sold')}",
         f"units unchanged={h['units'] == 300_000}",
         f"alerts={len(SENT)}"]
    ok = (net == 0.0 and not h.get("tranche_1_sold")
          and h["units"] == 300_000 and len(st["tranche_sales"]) == 0
          and len(SENT) > 0)
    return ok, L


@scenario("stop_loss_refused", "FOMO stop-loss on a bad price — position kept, alarm raised")
def sim_stop_refused():
    import fomo_exit as FE, fomo_portfolio as FP
    capture_telegram()
    FP.save_fomo_portfolio = lambda s: None
    FP.sync_fomo_state_to_github = lambda: None

    h = {"token_ticker": "SIM", "units": 300_000, "spent": 100.0,
         "entry_price": 0.0006, "contract_address": "SIMC"}
    st = {"cash": 0.0, "holdings": [h], "trade_history": []}
    net = FE._execute_full_sell(h, 9_999.0, "stop_loss", st)
    unprotected = any("unprotected" in s.lower() or "still open" in s.lower()
                      for s in SENT)
    L = [f"returned ${net:.2f}", f"still held={h in st['holdings']}",
         f"alarm mentions exposure={unprotected}"]
    return (net == 0.0 and h in st["holdings"] and unprotected), L


@scenario("perp_close", "Kalshi perp close — corrupt price refused, real price closes")
def sim_perp_close():
    import kalshi_portfolio as KP
    KP.open_position(ticker="SIMPERP", title="t", direction="UP",
                     entry_price=100.0, leverage=2.0, margin=50.0,
                     stop_pct=5.0, tp_pct=10.0, confidence=70)
    bad = KP.close_position("SIMPERP", 99_999.0, "stop_loss")
    still = len(KP._load()["holdings"]) == 1
    good = KP.close_position("SIMPERP", 95.0, "stop_loss")
    gone = len(KP._load()["holdings"]) == 0
    L = [f"corrupt refused={bad is None}", f"kept open={still}",
         f"real close={good is not None}", f"book empty={gone}"]
    return (bad is None and still and good is not None and gone), L


@scenario("event_settle", "Kalshi event bet settles — payout, record, cash")
def sim_event_settle():
    import kalshi_event_portfolio as KE
    KE.open_bet("SIMEV", "sim market", "YES", 40, 125, 0.40, domain="sports_team")
    before = KE.get_summary()["total_value"]
    tr = KE.settle_bet("SIMEV", "yes")
    after = KE.get_summary()
    L = [f"won={tr and tr['won']}", f"pnl=${tr['net_pnl']:+.2f}" if tr else "no trade",
         f"value ${before:.2f} -> ${after['total_value']:.2f}",
         f"recorded={after['total_trades']}"]
    ok = (tr is not None and tr["won"] and tr["net_pnl"] > 0
          and after["total_trades"] == 1
          and abs(after["total_value"] - (before + tr["net_pnl"])) < 0.01)
    return ok, L


@scenario("event_settle_blank", "Kalshi event with no result — stays open, no guess")
def sim_event_blank():
    import kalshi_event_portfolio as KE
    KE.open_bet("SIMEV2", "sim market", "YES", 40, 125, 0.40, domain="macro_econ")
    tr = KE.settle_bet("SIMEV2", "")
    held = any(h["ticker"] == "SIMEV2" for h in KE.get_summary()["positions"])
    L = [f"returned={tr}", f"still open={held}"]
    return (tr is None and held), L


@scenario("report_covers_both_books",
          "Weekly report footer — an event loss must move the headline total")
def sim_report_both_books():
    """
    The bug this pins: build_weekly_report printed get_portfolio_summary(),
    which is kalshi_portfolio — perps only. Two event bets settling to zero
    (-$199.85 on 2026-08-29) left "Account: $10,048.79 / All-time: +0.49%"
    untouched, because the event book is separate cash.

    Losing money must move the number the report leads with.
    """
    import kalshi_tracker as KT
    import kalshi_event_portfolio as KE

    before = KT._account_footer(10_000.0, 10_000.0)

    # A real losing event bet: buy 100 contracts at 40c, resolve against us.
    KE.open_bet("SIMRPT", "sim market", "YES", 40, 100, 0.40, domain="weather")
    tr = KE.settle_bet("SIMRPT", "no")

    after = KT._account_footer(10_000.0, 10_000.0)
    loss  = tr["net_pnl"] if tr else 0.0

    # Parse the combined figure back out of each footer.
    import re
    def _combined(s):
        m = re.search(r"Combined: \$([\d,.]+)", s)
        return float(m.group(1).replace(",", "")) if m else None

    c_before, c_after = _combined(before), _combined(after)
    moved = (c_before is not None and c_after is not None
             and abs((c_before + loss) - c_after) < 0.01)

    L = [f"event loss ${loss:+.2f}",
         f"combined ${c_before} -> ${c_after}",
         f"footer names both books={'Perps:' in after and 'Events:' in after}",
         f"headline moved by the loss={moved}"]
    ok = (tr is not None and not tr["won"] and loss < 0
          and "Perps:" in after and "Events:" in after
          and moved)
    return ok, L


@scenario("no_bet_price_display",
          "A NO bet must display what we PAID, not the YES quote")
def sim_no_bet_price():
    """
    Shipped bug: "Bet NO at 16c · resolved NO · +20%". A 16c contract paying
    $1.00 returns +525%; +20% means we actually paid ~83c. calc_position buys
    NO at (100 - price), open_bet stored the YES price, the display read it.
    """
    import kalshi_tracker as KT
    import kalshi_event_portfolio as KE
    from kalshi_event_trader import calc_position

    # Market quotes YES at 16c → a NO bet costs 84c.
    sizing = calc_position(16.0, "NO", stake=100.0)
    KE.open_bet("SIMNO", "sim", "NO", 16.0, sizing["contracts"],
                sizing["cost_per"], domain="weather")
    # By ticker, NOT positions[0] — under --all, earlier scenarios leave their
    # own bets in the book and [0] silently picked up a 40c YES position from
    # event_settle. A scenario that reads the wrong row tests nothing.
    pos = next(p for p in KE.get_summary()["positions"] if p["ticker"] == "SIMNO")

    shown = KT._our_price_cents(pos)
    # Round-trip: a winning NO at this price must return what the price implies.
    tr = KE.settle_bet("SIMNO", "no")
    implied_ret = (100.0 / shown - 1) * 100 if shown else 0.0

    L = [f"stored entry_cents={pos['entry_cents']:.0f}c (the YES quote)",
         f"displayed={shown:.0f}c", f"actual return={tr['return_pct']:+.0f}%",
         f"return implied by displayed price={implied_ret:+.0f}%"]
    # The displayed price must be the ~84c we paid, and must be consistent
    # with the realised return. 16c would imply +525% against an actual +19%.
    ok = (abs(shown - 84.0) < 0.5
          and abs(implied_ret - tr["return_pct"]) < 1.0)
    return ok, L


@scenario("underfunded_book_alarms",
          "Event book below one stake — alarms instead of going quiet")
def sim_underfunded():
    """
    open_bet refuses when cost > cash and only writes log.warning. The book hit
    $79.10 against a $100 stake and stopped betting with no notification at all.
    """
    import kalshi_tracker as KT
    import kalshi_event_portfolio as KE
    import kalshi_portfolio as KP
    capture_telegram()
    KT.send_telegram = lambda t, *a, **k: (SENT.append(t), True)[1]

    # Stand-in for Redis that SURVIVES a simulated restart, so the cooldown is
    # tested the way production actually behaves.
    store = {}
    KP._redis_set = lambda k, v: (store.__setitem__(k, v), True)[1]
    KP._redis_get = lambda k: store.get(k)
    store.pop(KT._UNDERFUNDED_KEY, None)

    st = KE._load()
    st["cash"] = 79.10                      # the real observed figure
    KE._save(st)

    # A standard bet must be refused...
    refused = KE.open_bet("SIMUF", "sim", "YES", 40, 250, 0.40) is None
    # ...and the scan must say so rather than silently screening.
    KT._run_event_scan_cycle(force=True)
    alarmed = any("underfunded" in s.lower() or "halted" in s.lower() for s in SENT)
    names_cash = any("79.10" in s for s in SENT)
    first_count = len(SENT)

    # RESTART. The cooldown was a module global and reset to 0.0 here, which is
    # why the real alarm fired 4x in one night (21:04, 02:25, 02:35, 02:39).
    # Reloading the module reproduces that exactly.
    import importlib
    importlib.reload(KT)
    KT.send_telegram = lambda t, *a, **k: (SENT.append(t), True)[1]
    KP._redis_set = lambda k, v: (store.__setitem__(k, v), True)[1]
    KP._redis_get = lambda k: store.get(k)

    KT._run_event_scan_cycle(force=True)
    quiet_after_restart = len(SENT) == first_count

    L = [f"bet refused={refused}", f"alarm raised={alarmed}",
         f"states the cash figure={names_cash}",
         f"alerts before restart={first_count}, after={len(SENT)}",
         f"silent across restart={quiet_after_restart}"]
    return (refused and alarmed and names_cash and quiet_after_restart), L


@scenario("funnel_accumulates",
          "Scan funnel sums across scans and rolls over on a new day")
def sim_funnel_accumulates():
    """
    One scan cannot tell a strict gate from an empty moment. detect_pullback is
    a point-in-time read, so "5 no valid pullback" in a single sample says
    nothing — the scan runs ~105 times a session and each overwrote the last.

    Three things must hold: counters add up, nested pillar tallies add up, and
    a new ET date starts from zero rather than summing into yesterday.
    """
    import stock_tracker as ST

    store = {}
    ST._redis_stub = store
    import stock_portfolio as SP
    SP._redis_set = lambda k, v: (store.__setitem__(k, v), True)[1]
    SP._redis_get = lambda k: store.get(k)

    day = "2026-08-31"
    ST._et_date = lambda: day

    scan_a = {"gainers": 20, "failed_pillars": 14, "no_pullback": 5,
              "candidates": 0, "catalyst_no_news": 10,
              "pillar_detail": {"catalyst": 10, "rvol": 8},
              "pillar_unknown": {"float": 3}}
    scan_b = {"gainers": 18, "failed_pillars": 11, "no_pullback": 4,
              "candidates": 1, "catalyst_no_news": 7,
              "pillar_detail": {"catalyst": 7, "price": 2},
              "pillar_unknown": {"float": 1}}

    ST._accumulate_funnel(scan_a)
    cum = ST._accumulate_funnel(scan_b)

    summed = (cum["scans"] == 2 and cum["gainers"] == 38
              and cum["no_pullback"] == 9 and cum["candidates"] == 1
              and cum["catalyst_no_news"] == 17)
    nested = (cum["pillar_detail"]["catalyst"] == 17          # 10 + 7
              and cum["pillar_detail"]["rvol"] == 8           # only scan A
              and cum["pillar_detail"]["price"] == 2          # only scan B
              and cum["pillar_unknown"]["float"] == 4)

    # New trading day must NOT inherit yesterday's totals.
    day = "2026-09-01"
    fresh = ST._accumulate_funnel(scan_a)
    rolled = (fresh["scans"] == 1 and fresh["gainers"] == 20
              and fresh["date"] == "2026-09-01")

    txt = ST.format_cumulative(cum)
    reports_rate = "pullback gate:" in txt

    L = [f"2 scans -> scans={cum['scans']}, gainers={cum['gainers']}, "
         f"no_pullback={cum['no_pullback']}",
         f"nested summed correctly={nested}",
         f"catalyst 10+7={cum['pillar_detail']['catalyst']}",
         f"new day resets={rolled} (scans={fresh['scans']})",
         f"reports pullback rejection rate={reports_rate}"]
    return (summed and nested and rolled and reports_rate), L


@scenario("forming_candle_no_exit",
          "A still-forming candle must not trigger an exit")
def sim_forming_candle():
    """
    The monitor polls every 20s but bars are 1 minute, so the newest bar is read
    ~3x before it closes. On a forming bar:

      topping_tail   body is near zero, so any upper wick clears `upper >= 2*body`
      false_breakout `h > prev_high and c < prev_high` is the normal state of a
                     candle mid-poke — the very crossing candle Ross enters on

    Both are severity "high", and run_monitor closes the position on any high
    signal. This scenario pins that a forming bar is ignored and the SAME bar,
    once closed, is judged normally.
    """
    import stock_signals as SG
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)

    def bar(mins_ago, o, h, l, c, v=10000):
        t = (now - timedelta(minutes=mins_ago)).replace(second=0, microsecond=0)
        return {"t": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "o": o, "h": h, "l": l, "c": c, "v": v, "n": 50, "vw": c}

    # Five closed bars advancing, then a 6th that is STILL FORMING and looks
    # like both a topping tail (tiny body, long upper wick) and a false
    # breakout (new high, price currently back under the prior high).
    history = [bar(6, 5.00, 5.10, 4.98, 5.08), bar(5, 5.08, 5.20, 5.06, 5.18),
               bar(4, 5.18, 5.30, 5.16, 5.28), bar(3, 5.28, 5.40, 5.26, 5.38),
               bar(2, 5.38, 5.50, 5.36, 5.48)]
    forming = bar(0, 5.48, 5.62, 5.47, 5.485)      # stamped this minute

    live = SG.check_exit_signals(history + [forming])
    fired_live = [s["indicator"] for s in live if s["severity"] == "high"]

    # Same candle, one minute later — now closed, and must be judged.
    closed = dict(forming)
    closed["t"] = (now - timedelta(minutes=1)).replace(
        second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    after = SG.check_exit_signals(history + [closed])
    fired_closed = [s["indicator"] for s in after if s["severity"] == "high"]

    # An unparseable timestamp must also be excluded, not trusted.
    junk = dict(forming); junk["t"] = "not-a-timestamp"
    junk_fired = [s["indicator"] for s in SG.check_exit_signals(history + [junk])
                  if s["severity"] == "high"]

    L = [f"forming bar high-severity signals={fired_live} (want none)",
         f"same bar once closed={fired_closed} (want at least one)",
         f"unparseable timestamp={junk_fired} (want none)"]
    ok = (not fired_live) and bool(fired_closed) and (not junk_fired)
    return ok, L


@scenario("rvol_lookback_50d",
          "Recent volume spikes must not suppress RVOL below the 5x floor")
def sim_rvol_lookback():
    """
    Ross: "five times higher volume than the 50-DAY average". We used 30.

    A short lookback lets a few recent pop-and-reject days dominate the average,
    which understates RVOL on exactly the stocks that keep spiking — the effect
    he names when STKH read 4.46 and he called it "lower than I'd prefer".

    Built from the same synthetic history both ways, so only the window differs.
    """
    import stock_data as SD

    # 50 quiet days at 100k, then 3 recent pop-and-reject days at 2M.
    #
    # These figures are chosen to straddle the 5x floor and demonstrate the
    # MECHANISM. They are not evidence that real gainers distribute this way —
    # that would need a measurement against live data, which this does not do.
    hist = [100_000] * 47 + [2_000_000] * 3
    today = 1_200_000
    #   30-day avg = (27*100k + 3*2M)/30 = 290k -> 4.14x  REJECTED
    #   50-day avg = (47*100k + 3*2M)/50 = 214k -> 5.61x  passes

    def rvol(window):
        sample = hist[-window:]
        return today / (sum(sample) / len(sample))

    r30, r50 = rvol(30), rvol(50)
    L = [f"30-day avg -> RVOL {r30:.2f}x  ({'PASS' if r30 >= 5 else 'REJECTED'} at 5x floor)",
         f"50-day avg -> RVOL {r50:.2f}x  ({'PASS' if r50 >= 5 else 'REJECTED'} at 5x floor)",
         f"code default now {SD.RVOL_LOOKBACK_DAYS} days"]
    # The point: same stock, same day — the shorter window rejects it and the
    # window Ross actually specifies does not.
    ok = (SD.RVOL_LOOKBACK_DAYS == 50 and r30 < 5.0 <= r50)
    return ok, L


@scenario("obvious_mover_exception",
          "No-news stock passes catalyst ONLY when extreme, never when feed is down")
def sim_obvious_mover():
    """
    Ross allows a no-news stock if it is "clearly moving really well and one of
    the most obvious stocks today". Catalyst was our top rejector at 10/14.

    Four cases matter, and the last two are the ones that protect us:
      extreme + no news        -> pass (the exception)
      ordinary + no news       -> fail (exception must not swallow the rule)
      extreme + FEED DOWN      -> fail (we never looked; can't rule out dilution)
      harmful news present     -> fail (bad news is not absent news)
    """
    import stock_signals as SG

    def snap(pct, rvol, news=0, feed_ok=True, catalyst=None):
        s = {"symbol": "SIM", "price": 5.0, "pct_change": pct, "rvol": rvol,
             "news_count": news, "news_feed_ok": feed_ok, "float_m": 8.0}
        if catalyst:
            s["catalyst"] = catalyst
        return s

    extreme  = SG.score_pillars(snap(120.0, 18.0), market_hot=True)
    ordinary = SG.score_pillars(snap(14.0, 6.0), market_hot=True)
    blind    = SG.score_pillars(snap(120.0, 18.0, feed_ok=False), market_hot=True)
    harmful  = SG.score_pillars(
        snap(120.0, 18.0, catalyst={"passes": True, "quality": "harmful",
                                    "score": 20, "catalyst_type": "offering",
                                    "reasoning": "dilutive offering"}),
        market_hot=True)

    c = lambda r: r["pillars"]["catalyst"]
    L = [f"extreme+no news  -> catalyst pass={c(extreme)['pass']} "
         f"(exception={c(extreme).get('exception', False)})",
         f"ordinary+no news -> catalyst pass={c(ordinary)['pass']}",
         f"extreme+feed down-> catalyst pass={c(blind)['pass']}",
         f"harmful news     -> qualifies={harmful['qualifies']} "
         f"({harmful['disqualified_by']})"]
    ok = (c(extreme)["pass"] is True and c(extreme).get("exception") is True
          and c(ordinary)["pass"] is False
          and c(blind)["pass"] is False
          and harmful["qualifies"] is False)
    return ok, L


@scenario("fresh_token_dropped_early",
          "A <1-day token is dropped BEFORE research, not after paying for it")
def sim_fresh_token_dropped():
    """
    The contradiction: fomo_tracker allowed Golem signals from 30 minutes old,
    fomo_research hard-vetoed anything under 1 day. Every new_launch signal ran
    full research + an LLM call and was then rejected for an age known upfront.
    Nine times in four days, APEC three times in 24 minutes, zero trades.

    With GOLEM_INDEPENDENT_TRADING off, the signal must die at validation —
    research must never be called at all.
    """
    import fomo_tracker as FT
    capture_telegram()

    assert FT.GOLEM_INDEPENDENT_TRADING is False, \
        "master switch should default OFF"

    called = {"research": 0, "consensus": 0}
    FT.research_token = lambda *a, **k: (
        called.__setitem__("research", called["research"] + 1),
        {"go": False, "skip_reason": "should never run"})[1]
    FT._check_launch_consensus = lambda *a, **k: called.__setitem__(
        "consensus", called["consensus"] + 1)

    # A token that fails ONLY on age — exactly the APEC shape.
    FT.validate_token = lambda ca: {
        "valid": False, "age_only_reject": True, "age_days": 0.04,   # ~1 hour
        "liquidity_usd": 72_353, "market_cap": 3_000_000,
        "symbol": "APECSIM", "name": "sim", "price": 0.001,
        "reject_reason": "Token < 1 day old",
    }
    FT._lookup_contract_by_symbol = lambda s: "SIMCA"

    FT.process_social_signal({
        "alias": "Golem", "action": "BUY", "token_symbol": "APECSIM",
        "contract_address": "SIMCA", "source": "new_launch", "tier": "B",
        "chain": "solana", "confidence": 70,
    })

    L = [f"research_token calls={called['research']} (want 0)",
         f"routed to consensus tracker={called['consensus'] == 1}",
         f"telegram alerts={len(SENT)} (want 0 — rejections are not news)"]
    ok = (called["research"] == 0 and called["consensus"] == 1 and not SENT)
    return ok, L


@scenario("stock_close", "Stock close on a bare state — no crash, cash credited")
def sim_stock_close():
    import stock_portfolio as SP
    SP._save({"positions": [{"symbol": "SIM", "entry": 10.0, "shares": 100,
                             "cost": 1000.0, "opened_at": "2026-08-27T12:00:00Z"}],
              "cash": 9000.0})
    bad = SP.close_position("SIM", 5000.0, "stop")
    kept = len(SP._load()["positions"]) == 1
    good = SP.close_position("SIM", 9.4, "stop")
    st = SP._load()
    L = [f"corrupt refused={bad is None}", f"kept={kept}",
         f"closed={good is not None}", f"cash=${st.get('cash',0):.2f}",
         f"recorded={len(st.get('trade_history',[]))}"]
    ok = (bad is None and kept and good is not None
          and abs(float(st.get("cash", 0)) - 9940.0) < 0.01
          and len(st.get("trade_history", [])) == 1)
    return ok, L


@scenario("pillars_score", "Stock 5 Pillars — unknown data scores unknown, not fail")
def sim_pillars():
    import stock_signals as sig
    down = sig.score_pillars({"symbol": "X", "price": 8.0, "rvol": 6.0,
                              "pct_change": 25.0, "float_m": None,
                              "news_count": 0, "news_feed_ok": False,
                              "news_feed_error": "HTTP 403"}, market_hot=True)
    ok_feed = sig.score_pillars({"symbol": "Y", "price": 8.0, "rvol": 6.0,
                                 "pct_change": 25.0, "float_m": 9.0,
                                 "news_count": 3, "news_feed_ok": True},
                                market_hot=True)
    L = [f"broken feed -> catalyst unknown={down['pillars']['catalyst'].get('unknown')}",
         f"missing float -> pass={down['pillars']['float']['pass']}",
         f"good setup qualifies={ok_feed['qualifies']}"]
    return (down["pillars"]["catalyst"].get("unknown") is True
            and down["pillars"]["float"]["pass"] is None
            and ok_feed["qualifies"] is True), L


@scenario("position_sizing", "Stock sizing — risk stays constant as the stop widens")
def sim_sizing():
    """
    calc_shares is Ross's core rule: shares = risk / (entry - stop). It decides
    how much money is exposed on every trade and had ZERO test coverage — a
    mutation sweep killed 0 of its mutants. A silent error here mis-sizes every
    position without changing anything visible.
    """
    import stock_portfolio as SP
    SP._save({**SP._load(), "cash": 10_000.0, "starting_cash": 10_000.0,
              "positions": []})
    tight = SP.calc_shares(10.00, 9.85)      # 15c stop
    wide  = SP.calc_shares(10.00, 9.50)      # 50c stop
    L = [f"15c stop -> {tight.get('shares')} sh, risk ${tight.get('total_risk',0):.2f}",
         f"50c stop -> {wide.get('shares')} sh, risk ${wide.get('total_risk',0):.2f}",
         f"limiter: {tight.get('limiter')} / {wide.get('limiter')}"]
    # The whole point of risk-based sizing: dollar risk is constant, share
    # count shrinks as the stop widens.
    ok = (tight.get("shares", 0) > wide.get("shares", 0)
          and abs(tight.get("total_risk", 0) - SP.RISK_PER_TRADE) < 1.0
          and abs(wide.get("total_risk", 0) - SP.RISK_PER_TRADE) < 1.0)
    bad = SP.calc_shares(10.0, 10.5)          # stop above entry
    L.append(f"stop above entry refused={bad.get('shares', 0) == 0}")
    return (ok and bad.get("shares", 0) == 0), L


@scenario("circuit_breakers", "Stock halts on daily loss and consecutive losers")
def sim_breakers():
    """
    The two hard limits that stop a bad day becoming a disaster. Both survived
    every mutation — nothing verified they fire.
    """
    import stock_portfolio as SP
    SP._save({**SP._load(), "cash": 10_000.0, "positions": [],
              "day_pnl": 0.0, "consecutive_losses": 0, "halted_reason": ""})
    clear_ok, _ = SP.can_trade()

    # Test EXACTLY at the threshold. Using -101 against a -100 limit passes
    # for both `<=` and `<`, so a mutation swapping them survives undetected —
    # the test looks thorough and proves nothing about the boundary. The whole
    # question is whether hitting the limit exactly stops trading.
    SP._save({**SP._load(), "day_pnl": -abs(SP.DAILY_MAX_LOSS)})
    loss_ok, loss_why = SP.can_trade()

    SP._save({**SP._load(), "day_pnl": -abs(SP.DAILY_MAX_LOSS) + 0.01,
              "halted_reason": ""})
    just_under_ok, _ = SP.can_trade()

    SP._save({**SP._load(), "day_pnl": 0.0, "halted_reason": "",
              "consecutive_losses": SP.MAX_CONSECUTIVE_LOSS})
    streak_ok, streak_why = SP.can_trade()

    # One below the limit must still trade — otherwise `>=` vs `>` is untested.
    SP._save({**SP._load(), "day_pnl": 0.0, "halted_reason": "",
              "consecutive_losses": SP.MAX_CONSECUTIVE_LOSS - 1})
    just_under_streak, _ = SP.can_trade()

    L = [f"clean state trades={clear_ok}",
         f"AT daily max loss blocked={not loss_ok} ({loss_why[:38]})",
         f"1c under the limit still trades={just_under_ok}",
         f"AT consecutive limit blocked={not streak_ok} ({streak_why[:38]})",
         f"one below the limit still trades={just_under_streak}"]
    return (clear_ok and not loss_ok and just_under_ok
            and not streak_ok and just_under_streak), L


@scenario("reconcile_invariant", "Books balance after a full open/close cycle")
def sim_reconcile():
    import kalshi_portfolio as KP, reconcile as R
    KP.open_position(ticker="SIMREC", title="t", direction="UP",
                     entry_price=100.0, leverage=2.0, margin=50.0,
                     stop_pct=5.0, tp_pct=10.0, confidence=70)
    KP.update_prices({"SIMREC": 100.0})
    KP.apply_funding({"SIMREC": 0.001})
    KP.close_position("SIMREC", 105.0, "take_profit")
    r = R.reconcile_kalshi()
    L = [f"ok={r.get('ok')}", f"actual=${r.get('actual',0):+.2f}",
         f"recorded=${r.get('recorded',0):+.2f}", f"gap=${r.get('gap',0):+.2f}"]
    return r.get("ok") is True, L


# ─── RUNNER ───────────────────────────────────────────────────────────────────

def run(names) -> int:
    failed = 0
    for name in names:
        desc, fn = SCENARIOS[name]
        try:
            ok, lines = fn()
        except Exception as e:
            ok, lines = False, [f"EXCEPTION {type(e).__name__}: {e}"]
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        print(f"        {desc}")
        for l in lines:
            print(f"          · {l}")
        if not ok:
            failed += 1
        print()
    return failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", nargs="*", help="scenario name(s)")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    if a.list:
        print("SCENARIOS\n")
        for n, (d, _) in sorted(SCENARIOS.items()):
            print(f"  {n:22s} {d}")
        return 0

    names = sorted(SCENARIOS) if (a.all or not a.scenario) else a.scenario
    unknown = [n for n in names if n not in SCENARIOS]
    if unknown:
        print(f"unknown scenario(s): {unknown}\nuse --list")
        return 1

    print(f"SIMULATION — {len(names)} action(s), real logic, faked network")
    print(f"state redirected to {_TMP}\n")
    failed = run(names)

    if failed:
        print(f"{failed} action(s) did NOT behave correctly.")
    else:
        print("Every simulated action behaved correctly.")
    return failed


if __name__ == "__main__":
    sys.exit(main())
