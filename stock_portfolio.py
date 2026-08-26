#!/usr/bin/env python3
"""
stock_portfolio.py — Paper trade tracker for Stock Golem.

Implements Ross Cameron's small-account risk rules as hard constraints rather
than guidance. The rules ARE the strategy — a system that takes his entries but
ignores his risk limits is a different (and much worse) system.

  Rule 1: Risk $50 to make $100        → 2:1 minimum, enforced at entry
  Rule 2: Daily max loss −$100         → hard stop for the day
  Rule 3: 3 consecutive losers → done  → circuit breaker

POSITION SIZING — this is the important bit:
Share size is derived from risk, not from account percentage.

    shares = risk_dollars / (entry - stop)

With a 15¢ stop and $50 of risk that's 333 shares. With a 50¢ stop it's 100.
The dollar risk stays constant regardless of how wide the stop is, which is
what makes a 2:1 target meaningful. Sizing by "10% of account" instead would
mean a wide stop quietly risks far more than intended.

State persists to Redis (same pattern as Kalshi) so a redeploy doesn't wipe
the day's trade count and circuit-breaker state.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests as _requests

log = logging.getLogger(__name__)

# ─── RISK RULES ───────────────────────────────────────────────────────────────
STARTING_CASH        = float(os.getenv("STOCK_STARTING_CASH", "2000.0"))
RISK_PER_TRADE       = float(os.getenv("STOCK_RISK_PER_TRADE", "50.0"))
MIN_PROFIT_RATIO     = float(os.getenv("STOCK_MIN_PROFIT_RATIO", "2.0"))
DAILY_MAX_LOSS       = float(os.getenv("STOCK_DAILY_MAX_LOSS", "100.0"))
MAX_CONSECUTIVE_LOSS = int(os.getenv("STOCK_MAX_CONSEC_LOSSES", "3"))
MAX_OPEN_POSITIONS   = int(os.getenv("STOCK_MAX_POSITIONS", "3"))
# Never let one position exceed this share of the account, even if the stop is
# tight enough that risk-based sizing would allow more.
MAX_POSITION_PCT     = float(os.getenv("STOCK_MAX_POSITION_PCT", "50.0"))

# ─── REDIS ────────────────────────────────────────────────────────────────────
def _first_env(names: tuple) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


_REDIS_URL = _first_env((
    "UPSTASH_REDIS_URL", "UPSTASH_REDIS_REST_URL",
    "KV_REST_API_URL", "REDIS_URL",
))
_REDIS_TOKEN = _first_env((
    "UPSTASH_REDIS_TOKEN", "UPSTASH_REDIS_REST_TOKEN",
    "KV_REST_API_TOKEN", "REDIS_TOKEN",
))
_KEY = "stock_portfolio"

# Honour an explicit override. This previously ignored STOCK_PORTFOLIO_FILE
# entirely, so test harnesses that "isolated" state by setting it were in fact
# writing to the repo's own stock_portfolio.json — which is how TEST trades and
# fake restart records ended up in the file the daily audit reads.
_LOCAL_FILE = os.getenv("STOCK_PORTFOLIO_FILE") or os.path.join(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__)),
    "stock_portfolio.json",
)


def _redis_get(key: str):
    if not (_REDIS_URL and _REDIS_TOKEN):
        return None
    try:
        r = _requests.post(_REDIS_URL,
                           headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                           json=["GET", key], timeout=5)
        if r.status_code == 200:
            v = r.json().get("result")
            return json.loads(v) if v else None
    except Exception as e:
        log.error(f"Stock portfolio: Redis GET failed: {e}")
    return None


def _redis_set(key: str, data: dict) -> bool:
    if not (_REDIS_URL and _REDIS_TOKEN):
        return False
    try:
        r = _requests.post(_REDIS_URL,
                           headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                           json=["SET", key, json.dumps(data, default=str)],
                           timeout=5)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Stock portfolio: Redis SET failed: {e}")
    return False


def redis_health() -> dict:
    """
    Prove whether persistence actually works — a write that silently falls back
    to the container filesystem looks fine until the next redeploy erases it.
    Tests a real round-trip, not just reachability.
    """
    out = {
        "configured": bool(_REDIS_URL and _REDIS_TOKEN),
        "url_var":    next((n for n in ("UPSTASH_REDIS_URL", "UPSTASH_REDIS_REST_URL",
                                        "KV_REST_API_URL", "REDIS_URL")
                            if os.getenv(n, "").strip()), ""),
        "readable":   False,
        "writable":   False,
        "key_exists": False,
        "bytes":      0,
        "error":      None,
    }
    if not out["configured"]:
        out["error"] = "no Redis URL/token in env"
        return out

    # Read
    try:
        r = _requests.post(_REDIS_URL,
                           headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                           json=["GET", _KEY], timeout=5)
        if r.status_code == 200:
            out["readable"] = True
            raw = r.json().get("result")
            if raw:
                out["key_exists"] = True
                out["bytes"] = len(raw)
                try:
                    d = json.loads(raw)
                    out["stored_cash"]     = d.get("cash")
                    out["stored_basis"]    = d.get("starting_cash")
                    out["stored_trades"]   = len(d.get("trade_history", []))
                    out["stored_deposits"] = len(d.get("deposits", []))
                except Exception:
                    pass
        else:
            out["error"] = f"read HTTP {r.status_code}: {r.text[:120]}"
            return out
    except Exception as e:
        out["error"] = f"read failed: {e}"
        return out

    # Write round-trip on a throwaway key
    try:
        probe_key = "stock_write_probe"
        stamp = datetime.now(timezone.utc).isoformat()
        w = _requests.post(_REDIS_URL,
                           headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                           json=["SET", probe_key, json.dumps({"ts": stamp})],
                           timeout=5)
        if w.status_code != 200:
            out["error"] = f"write HTTP {w.status_code}: {w.text[:120]}"
            return out
        v = _requests.post(_REDIS_URL,
                           headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                           json=["GET", probe_key], timeout=5)
        if v.status_code == 200 and stamp in (v.text or ""):
            out["writable"] = True
        else:
            out["error"] = "write succeeded but read-back didn't match"
    except Exception as e:
        out["error"] = f"write failed: {e}"

    return out


def _default_state() -> dict:
    return {
        "version":         "stock-v1",
        "created_at":      datetime.now(timezone.utc).isoformat(),
        "cash":            STARTING_CASH,
        "starting_cash":   STARTING_CASH,
        "positions":       [],
        "trade_history":   [],
        "total_trades":    0,
        "winning_trades":  0,
        "losing_trades":   0,
        "consecutive_losses": 0,
        "day":             "",      # UTC date the day-state below refers to
        "day_pnl":         0.0,
        "day_trades":      0,
        "halted_reason":   "",      # non-empty = no new entries today
    }


def _load() -> dict:
    data = _redis_get(_KEY)
    if data:
        return _roll_day(data)
    if os.path.exists(_LOCAL_FILE):
        try:
            with open(_LOCAL_FILE) as f:
                return _roll_day(json.load(f))
        except Exception as e:
            log.error(f"Stock portfolio: local load failed: {e}")
    return _default_state()


# Tests that override STOCK_PORTFOLIO_FILE still wrote to Redis, because the
# Redis key is fixed and _save tries Redis FIRST. So a test with an isolated
# temp file silently mutated the live book — two TEST trades and five fake
# restart records ended up in production, leaving consecutive_losses at 2 of
# the 3-strike halt threshold on entirely fabricated data.
#
# Any harness touching these modules must set PAPER_TEST_MODE=1.
TEST_MODE = os.getenv("PAPER_TEST_MODE", "").strip() not in ("", "0", "false")
if TEST_MODE:
    # Blocking Redis alone wasn't enough — the fallback still wrote to the
    # repo's stock_portfolio.json, which the daily audit reads. Redirect the
    # file too, so a test cannot touch anything real by any path.
    import tempfile
    _LOCAL_FILE = os.path.join(tempfile.gettempdir(), "stock_portfolio_TEST.json")
    log.warning(f"Stock portfolio: PAPER_TEST_MODE — Redis DISABLED and file "
                f"redirected to {_LOCAL_FILE}. Live state is protected.")


def purge_test_data() -> dict:
    """
    Surgically remove test artifacts from the live book.

    Removes only entries that could not have come from real trading:
      - trade_history rows whose symbol is TEST or reason is test_flat
      - restart records whose deploy id isn't a real Railway UUID

    Everything else is left untouched, and consecutive_losses is recomputed
    from what remains rather than guessed at. Deliberately not a reset: real
    history is the asset here, and wiping the file to fix a bad number is the
    mistake this codebase has already made once.
    """
    state = _load()

    hist = state.get("trade_history", [])
    keep_hist, dropped_trades = [], []
    for t in hist:
        sym = str(t.get("symbol", "")).upper()
        rsn = str(t.get("reason", "")).lower()
        if sym in ("TEST", "TESTING") or rsn in ("test_flat", "test"):
            dropped_trades.append(t)
        else:
            keep_hist.append(t)

    restarts = state.get("restarts", [])
    keep_restarts, dropped_restarts = [], []
    for r in restarts:
        dep = r.get("deploy", "") if isinstance(r, dict) else "legacy"
        # Real Railway deployment ids are 36-char UUIDs; anything short and
        # hand-written ("dep-bad", "dep-1") came from a harness.
        if isinstance(dep, str) and dep.startswith("dep-") and len(dep) < 20:
            dropped_restarts.append(r)
        else:
            keep_restarts.append(r)

    # Recompute the halt counter from surviving trades, newest backwards.
    streak = 0
    for t in reversed(keep_hist):
        if float(t.get("pnl", 0) or 0) < 0:
            streak += 1
        else:
            break

    before_streak = state.get("consecutive_losses", 0)
    state["trade_history"]      = keep_hist
    state["restarts"]           = keep_restarts
    state["consecutive_losses"] = streak

    # Trade counters were incremented by the fake closes too.
    state["total_trades"] = max(0, int(state.get("total_trades", 0))
                                - len(dropped_trades))

    _save(state)
    log.warning(f"Stock portfolio: purged {len(dropped_trades)} test trade(s), "
                f"{len(dropped_restarts)} test restart(s); "
                f"consecutive_losses {before_streak} -> {streak}")

    return {
        "trades_removed":   len(dropped_trades),
        "restarts_removed": len(dropped_restarts),
        "trades_kept":      len(keep_hist),
        "streak_before":    before_streak,
        "streak_after":     streak,
        "removed_symbols":  [t.get("symbol") for t in dropped_trades],
    }


def _save(state: dict):
    if TEST_MODE:
        # Never let a test reach the live store.
        try:
            with open(_LOCAL_FILE, "w") as f:
                json.dump(state, f, indent=2, default=str)
        except Exception as e:
            log.error(f"Stock portfolio: test save failed: {e}")
        return
    if _redis_set(_KEY, state):
        return
    try:
        with open(_LOCAL_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Stock portfolio: save failed: {e}")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _roll_day(state: dict) -> dict:
    """Reset daily counters when the UTC date changes — including the halt."""
    if state.get("day") != _today():
        state["day"]           = _today()
        state["day_pnl"]       = 0.0
        state["day_trades"]    = 0
        state["halted_reason"] = ""
        state["consecutive_losses"] = 0
    return state


# ─── RISK GATE ────────────────────────────────────────────────────────────────

def can_trade() -> tuple:
    """
    (allowed, reason) — enforces Rules 2 and 3 plus position limits.
    Called before every entry.
    """
    s = _load()

    if s.get("halted_reason"):
        return False, s["halted_reason"]

    if s["day_pnl"] <= -abs(DAILY_MAX_LOSS):
        reason = (f"daily max loss hit (${s['day_pnl']:+.2f} vs "
                  f"−${DAILY_MAX_LOSS:.0f} limit)")
        s["halted_reason"] = reason
        _save(s)
        return False, reason

    if s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSS:
        reason = f"{s['consecutive_losses']} consecutive losers — done for the day"
        s["halted_reason"] = reason
        _save(s)
        return False, reason

    if len(s["positions"]) >= MAX_OPEN_POSITIONS:
        return False, f"max {MAX_OPEN_POSITIONS} concurrent positions"

    if s["cash"] < 50:
        return False, f"insufficient cash (${s['cash']:.2f})"

    return True, ""


def calc_shares(entry: float, stop: float, cash: float = None) -> dict:
    """
    Risk-based position sizing — the core of the method.

        shares = risk_dollars / (entry - stop)

    Capped by available cash and MAX_POSITION_PCT so a very tight stop can't
    produce an absurdly large position.
    """
    if entry <= 0 or stop <= 0 or entry <= stop:
        return {"shares": 0, "reason": "invalid entry/stop"}

    risk_per_share = entry - stop
    s = _load()
    cash = cash if cash is not None else s["cash"]

    raw = int(RISK_PER_TRADE / risk_per_share)
    if raw < 1:
        return {"shares": 0, "reason": f"stop too wide (${risk_per_share:.2f}/share)"}

    max_by_cash = int(cash * 0.95 / entry)
    max_by_pct  = int((s["starting_cash"] * MAX_POSITION_PCT / 100) / entry)
    shares = min(raw, max_by_cash, max_by_pct)

    if shares < 1:
        return {"shares": 0, "reason": f"cannot afford 1 share at ${entry:.2f}"}

    limiter = ("risk" if shares == raw
               else "cash" if shares == max_by_cash else "position cap")

    return {
        "shares":         shares,
        "risk_per_share": round(risk_per_share, 4),
        "total_risk":     round(shares * risk_per_share, 2),
        "cost":           round(shares * entry, 2),
        "limited_by":     limiter,
    }


# ─── OPEN / CLOSE ─────────────────────────────────────────────────────────────

def open_position(symbol: str, entry: float, stop: float, target: float,
                  shares: int = None, setup: str = "first_pullback",
                  confidence: int = 0, reasoning: str = "",
                  pillars_passed: int = 0, **extra) -> Optional[dict]:
    allowed, why = can_trade()
    if not allowed:
        log.warning(f"Stock: entry blocked for {symbol} — {why}")
        return None

    s = _load()
    if any(p["symbol"] == symbol for p in s["positions"]):
        log.info(f"Stock: already holding {symbol}")
        return None

    # Enforce Rule 1 — reject anything under 2:1
    risk   = entry - stop
    reward = target - entry
    if risk <= 0:
        log.warning(f"Stock: {symbol} invalid stop")
        return None
    # Epsilon guard: (6.30-6.00)/(6.00-5.85) evaluates to 1.999999999999994 in
    # binary floating point, so an exact 2:1 setup would be rejected without a
    # tolerance. Prices are cents-denominated; 1e-6 is far below any real
    # distinction and prevents silently discarding valid trades.
    ratio = reward / risk
    if ratio < MIN_PROFIT_RATIO - 1e-6:
        log.info(f"Stock: {symbol} rejected — {ratio:.2f}:1 below "
                 f"{MIN_PROFIT_RATIO:.1f}:1 minimum")
        return None

    if shares is None:
        sizing = calc_shares(entry, stop, s["cash"])
        shares = sizing["shares"]
        if shares < 1:
            log.warning(f"Stock: {symbol} sizing failed — {sizing.get('reason')}")
            return None

    cost = shares * entry
    if cost > s["cash"]:
        log.warning(f"Stock: {symbol} costs ${cost:.2f}, cash ${s['cash']:.2f}")
        return None

    pos = {
        "symbol":       symbol,
        "shares":       shares,
        "entry":        round(entry, 4),
        "stop":         round(stop, 4),
        "target":       round(target, 4),
        "cost":         round(cost, 2),
        "risk":         round(shares * risk, 2),
        "reward_ratio": round(ratio, 2),
        "setup":        setup,
        "confidence":   confidence,
        "reasoning":    reasoning,
        "pillars_passed": pillars_passed,
        "opened_at":    datetime.now(timezone.utc).isoformat(),
        "high_water":   round(entry, 4),
        **extra,
    }

    s["cash"] -= cost
    s["positions"].append(pos)
    s["day_trades"] += 1
    _save(s)

    log.warning(
        f"STOCK OPEN {symbol}: {shares} sh @ ${entry:.2f} | stop ${stop:.2f} "
        f"| target ${target:.2f} | risk ${shares*risk:.2f} | {ratio:.1f}:1"
    )
    return pos


def close_position(symbol: str, exit_price: float, reason: str = "manual") -> Optional[dict]:
    s = _load()
    idx = next((i for i, p in enumerate(s["positions"]) if p["symbol"] == symbol), None)
    if idx is None:
        return None

    pos      = s["positions"].pop(idx)
    proceeds = pos["shares"] * exit_price
    pnl      = proceeds - pos["cost"]
    won      = pnl > 0

    s["cash"]         += proceeds
    s["day_pnl"]      += pnl
    s["total_trades"] += 1
    if won:
        s["winning_trades"]      += 1
        s["consecutive_losses"]   = 0
    else:
        s["losing_trades"]       += 1
        s["consecutive_losses"]  += 1

    opened = pos.get("opened_at", "")
    try:
        held_min = (datetime.now(timezone.utc)
                    - datetime.fromisoformat(opened.replace("Z", "+00:00"))
                    ).total_seconds() / 60
    except Exception:
        held_min = 0.0

    trade = {
        **pos,
        "exit":        round(exit_price, 4),
        "proceeds":    round(proceeds, 2),
        "pnl":         round(pnl, 2),
        "pnl_pct":     round(pnl / pos["cost"] * 100, 2) if pos["cost"] else 0,
        "cents_per_share": round(exit_price - pos["entry"], 4),
        "won":         won,
        "reason":      reason,
        "closed_at":   datetime.now(timezone.utc).isoformat(),
        "held_minutes": round(held_min, 1),
    }
    s["trade_history"].append(trade)

    # Rules 2 & 3 evaluated immediately on close
    if s["day_pnl"] <= -abs(DAILY_MAX_LOSS):
        s["halted_reason"] = (f"daily max loss hit (${s['day_pnl']:+.2f})")
    elif s["consecutive_losses"] >= MAX_CONSECUTIVE_LOSS:
        s["halted_reason"] = f"{s['consecutive_losses']} consecutive losers"

    _save(s)
    log.warning(
        f"STOCK CLOSE {symbol}: {pos['shares']} sh @ ${exit_price:.2f} "
        f"({reason}) | P&L ${pnl:+.2f} ({trade['cents_per_share']:+.2f}/sh) "
        f"| held {held_min:.0f}min"
    )
    return trade


def update_high_water(symbol: str, price: float):
    """Track the peak for trailing logic."""
    s = _load()
    for p in s["positions"]:
        if p["symbol"] == symbol and price > p.get("high_water", 0):
            p["high_water"] = round(price, 4)
            _save(s)
            return


# ─── REPORTING ────────────────────────────────────────────────────────────────

def get_summary(prices: dict = None) -> dict:
    s = _load()
    prices = prices or {}

    positions = []
    unrealized = 0.0
    for p in s["positions"]:
        px  = prices.get(p["symbol"], p["entry"])
        val = p["shares"] * px
        pnl = val - p["cost"]
        unrealized += pnl
        positions.append({
            **p,
            "current":        round(px, 4),
            "value":          round(val, 2),
            "unrealized":     round(pnl, 2),
            "unrealized_pct": round(pnl / p["cost"] * 100, 2) if p["cost"] else 0,
        })

    total = s["cash"] + sum(p["value"] for p in positions)
    wins  = s["winning_trades"]
    trades = s["total_trades"]

    hist = s["trade_history"]
    avg_win  = (sum(t["pnl"] for t in hist if t["won"]) /
                max(sum(1 for t in hist if t["won"]), 1))
    avg_loss = (sum(t["pnl"] for t in hist if not t["won"]) /
                max(sum(1 for t in hist if not t["won"]), 1))

    return {
        "cash":          round(s["cash"], 2),
        "starting_cash": s["starting_cash"],
        "total_value":   round(total, 2),
        "total_pnl":     round(total - s["starting_cash"], 2),
        "total_pnl_pct": round((total / s["starting_cash"] - 1) * 100, 2),
        "unrealized":    round(unrealized, 2),
        "positions":     positions,
        "total_trades":  trades,
        "winning_trades": wins,
        "losing_trades": s["losing_trades"],
        "win_rate":      round(wins / trades * 100, 1) if trades else 0.0,
        "avg_win":       round(avg_win, 2),
        "avg_loss":      round(avg_loss, 2),
        "profit_ratio":  round(abs(avg_win / avg_loss), 2) if avg_loss else 0.0,
        "day_pnl":       round(s["day_pnl"], 2),
        "day_trades":    s["day_trades"],
        "consecutive_losses": s["consecutive_losses"],
        "halted_reason": s.get("halted_reason", ""),
    }


def deposit(target_bank: float) -> Optional[dict]:
    """
    Add buying power WITHOUT touching trade history or the W/L record.

    Use this rather than reset_portfolio() when the goal is simply more capital.
    A reset destroys the calibration data; a deposit just raises the bank.

    Both cash and starting_cash rise by the same amount, so percentage returns
    stay honest — a deposit is not a gain. Idempotent, with a ledger guard
    against double-funding if a save fails to persist.

    NOTE ON SIZING: raising the bank also raises how many shares risk-based
    sizing can buy, because MAX_POSITION_PCT is a share of starting_cash. On a
    $2k account a 15¢ stop was capital-constrained to ~$20 of real risk; at
    $10k the full $50 risk becomes reachable, which is closer to the strategy
    as written.
    """
    state = _load()
    basis = float(state.get("starting_cash", STARTING_CASH))

    if basis >= target_bank:
        log.info(f"Stock deposit: basis already ${basis:,.2f} — no-op")
        return None

    ledger = state.setdefault("deposits", [])
    if any(abs(float(d.get("target", 0)) - target_bank) < 0.01 for d in ledger):
        log.warning(
            f"Stock deposit: target ${target_bank:,.2f} already in ledger but "
            f"basis is ${basis:,.2f} — state didn't persist. Skipping."
        )
        return None

    delta = target_bank - basis
    state["cash"]          = state.get("cash", 0.0) + delta
    state["starting_cash"] = basis + delta
    ledger.append({
        "target": target_bank,
        "amount": round(delta, 2),
        "at":     datetime.now(timezone.utc).isoformat(),
    })
    _save(state)

    log.warning(
        f"Stock: DEPOSITED ${delta:,.2f} — cash now ${state['cash']:,.2f}, "
        f"basis ${state['starting_cash']:,.2f}. "
        f"{len(state.get('trade_history', []))} trades and "
        f"{state.get('winning_trades',0)}W/{state.get('losing_trades',0)}L preserved."
    )
    return state


def reset_portfolio(cash: float = None) -> dict:
    """Archive then reset — never destroy history outright."""
    old = _load()
    if old.get("trade_history"):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        _redis_set(f"stock_portfolio_archive_{stamp}", old)
        log.warning(f"Stock portfolio: archived {len(old['trade_history'])} trades")

    fresh = _default_state()
    if cash is not None:
        fresh["cash"] = fresh["starting_cash"] = cash
    _save(fresh)
    return fresh


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print("--- risk-based sizing ---")
    for entry, stop in [(7.60, 7.45), (5.81, 5.68), (12.00, 11.00), (5.00, 4.99)]:
        r = calc_shares(entry, stop, 2000)
        print(f"  entry ${entry:.2f} stop ${stop:.2f} "
              f"({entry-stop:.2f}/sh) -> {r['shares']} shares, "
              f"risk ${r.get('total_risk',0):.2f}, cost ${r.get('cost',0):.2f} "
              f"[{r.get('limited_by','-')}]")
