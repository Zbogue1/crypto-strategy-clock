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
