#!/usr/bin/env python3
"""
kalshi_event_portfolio.py — Paper book for Kalshi binary event contracts.

WHY THIS IS A SEPARATE STORE FROM kalshi_portfolio.py
Perps and binary contracts are different instruments, and the existing monitor
assumes perps everywhere:

  · _run_monitor_cycle() prices every open position via
    get_full_market_snapshot(), which hits the PERPS margin API. An event
    ticker returns nothing there, and the code does `if snap:` — so an event
    position would be silently skipped forever: never priced, never closed,
    never surfaced. Exactly the failure shape we spent this session killing.
  · apply_funding() charges an 8-hourly funding rate. Binary contracts have no
    funding. Every cycle would quietly bleed the book.
  · _should_force_close() applies same-day perp day-trade rules to something
    that settles on its own schedule.

Mixing them would also contaminate the 11-trade perp ledger the calibration
record is built on. Separate key, separate cash, separate history.

POSITION MATHS
A YES contract bought at 55c costs $0.55 and settles at $1.00 or $0.00.
There is no mark-to-market exit in this book: we hold to resolution, which is
the entire reason event markets were chosen over perps. A position closes when
Kalshi settles the market, and the P&L is arithmetic rather than a guess.

  cost_basis = contracts x cost_per
  win        = contracts x (1 - cost_per)
  loss       = -cost_basis
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests as _requests

log = logging.getLogger(__name__)

# Reuse the perp module's Redis plumbing rather than duplicating the env-var
# name matrix — a divergence there is how persistence silently breaks.
from kalshi_portfolio import _redis_get, _redis_set, _first_env  # noqa: F401

_DATA_DIR   = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__))
STATE_FILE  = os.getenv(
    "KALSHI_EVENT_PORTFOLIO_FILE",
    os.path.join(_DATA_DIR, "kalshi_event_portfolio.json"),
)
STATE_KEY   = "kalshi_event_portfolio"

STARTING_CASH = float(os.getenv("KALSHI_EVENT_STARTING_CASH", "1000.0"))
# Cash is the real constraint, not a position count. At $100/bet a $1,000 book
# supports ten concurrent bets; this cap only stops a runaway loop.
MAX_POSITIONS = int(os.getenv("KALSHI_EVENT_MAX_POSITIONS", "10"))


def _default_state() -> dict:
    return {
        "version":        "kalshi-event-v1",
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "cash":           STARTING_CASH,
        "starting_cash":  STARTING_CASH,
        "total_trades":   0,
        "winning_trades": 0,
        "losing_trades":  0,
        "total_pnl":      0.0,
        "holdings":       [],
        "trade_history":  [],
        "deposits":       [],
    }


def _load() -> dict:
    data = _redis_get(STATE_KEY)
    if data:
        return data
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Kalshi events: file load error: {e}")

    # First ever load — persist the opening state immediately.
    #
    # Without this the book has no stored size: every read rebuilds it from
    # STARTING_CASH, so the bank silently changes whenever that env var or its
    # default changes. Raising the default from 500 to 1000 "moved" $500 into
    # the book with no deposit and no record of it — the exact class of
    # untracked movement the reconciliation check exists to catch, except here
    # it would balance, because the basis moved too.
    #
    # Writing it once anchors the book to a real number that only deposit()
    # can change.
    state = _default_state()
    log.warning(f"Kalshi events: no stored book — creating one at "
                f"${STARTING_CASH:,.2f} and persisting it")
    _save(state)
    return state


def _save(state: dict) -> bool:
    """
    Persist to Redis AND to disk.

    Returns whether Redis accepted the write. Callers should log a failure
    rather than assume success — a save that silently didn't happen is how a
    week of trade history disappeared on a redeploy once already.
    """
    ok = _redis_set(STATE_KEY, state)
    if not ok:
        log.error("Kalshi events: Redis SET failed — state is local-only and "
                  "will be LOST on the next redeploy")
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Kalshi events: file save error: {e}")
    return ok


# ─── OPEN ─────────────────────────────────────────────────────────────────────

def open_bet(ticker: str, title: str, side: str, price_cents: float,
             contracts: int, cost_per: float, domain: str = "",
             close_time: str = "", confidence: int = 0, edge: float = 0.0,
             our_prob: float = 0.0, reasoning: str = "",
             group_id: str = "", group_size: int = 1) -> Optional[dict]:
    """Paper-buy a binary contract. Returns the position, or None if refused."""
    state = _load()

    if len(state["holdings"]) >= MAX_POSITIONS:
        log.warning(f"Kalshi events: at max {MAX_POSITIONS} positions — skipping {ticker}")
        return None

    for h in state["holdings"]:
        if h["ticker"] == ticker:
            log.info(f"Kalshi events: already holding {ticker} — skipping duplicate")
            return None

    cost = round(contracts * cost_per, 2)
    if cost > state["cash"]:
        log.warning(f"Kalshi events: insufficient cash ${state['cash']:.2f} "
                    f"for ${cost:.2f} on {ticker}")
        return None

    pos = {
        "ticker":      ticker,
        "title":       title,
        "side":        side,                 # YES or NO
        "entry_cents": round(price_cents, 1),
        "contracts":   contracts,
        "cost_per":    round(cost_per, 4),
        "cost_basis":  cost,
        "max_gain":    round(contracts * (1 - cost_per), 2),
        "domain":      domain,
        "close_time":  close_time,
        "confidence":  confidence,
        "edge":        edge,
        "our_prob":    our_prob,
        "reasoning":   reasoning[:600],
        "opened_at":   datetime.now(timezone.utc).isoformat(),
        "group_id":    group_id,
        "group_size":  group_size,
    }

    state["cash"] = round(state["cash"] - cost, 2)
    state["holdings"].append(pos)
    _save(state)

    log.info(f"Kalshi events: OPENED {side} {ticker} — {contracts} @ "
             f"{price_cents:.0f}c, risk ${cost:.2f} to win ${pos['max_gain']:.2f}")
    return pos


# ─── SETTLE ───────────────────────────────────────────────────────────────────

def settle_bet(ticker: str, result: str, reason: str = "settled") -> Optional[dict]:
    """
    Resolve a position against the market's actual outcome.

    `result` is "yes" or "no" as reported by Kalshi. A YES position wins when
    result == "yes"; a NO position wins when result == "no". Payout is $1.00
    per winning contract and $0.00 per losing one — no mark-to-market, no
    estimation.
    """
    state = _load()
    pos = next((h for h in state["holdings"] if h["ticker"] == ticker), None)
    if not pos:
        log.warning(f"Kalshi events: settle called for {ticker} but not held")
        return None

    result = (result or "").strip().lower()
    if result not in ("yes", "no"):
        log.warning(f"Kalshi events: {ticker} settled with unusable result "
                    f"{result!r} — leaving position open rather than guessing")
        return None

    won      = (pos["side"] == "YES" and result == "yes") or \
               (pos["side"] == "NO"  and result == "no")
    payout   = round(pos["contracts"] * 1.0, 2) if won else 0.0
    pnl      = round(payout - pos["cost_basis"], 2)

    trade = {
        **pos,
        "closed_at":   datetime.now(timezone.utc).isoformat(),
        "result":      result,
        "won":         won,
        "payout":      payout,
        "net_pnl":     pnl,
        "reason":      reason,
        "return_pct":  round(pnl / pos["cost_basis"] * 100, 1) if pos["cost_basis"] else 0.0,
    }

    state["cash"] = round(float(state.get("cash", 0) or 0) + payout, 2)
    state["holdings"] = [h for h in state.get("holdings", []) if h["ticker"] != ticker]
    state.setdefault("trade_history", []).append(trade)
    state["total_trades"]   = int(state.get("total_trades", 0) or 0) + 1
    state["winning_trades"] = int(state.get("winning_trades", 0) or 0) + (1 if won else 0)
    state["losing_trades"]  = int(state.get("losing_trades", 0) or 0) + (0 if won else 1)
    state["total_pnl"] = round(state.get("total_pnl", 0.0) + pnl, 2)
    _save(state)

    log.info(f"Kalshi events: SETTLED {ticker} {result.upper()} — "
             f"{'WON' if won else 'LOST'} ${pnl:+.2f}")
    return trade


def get_summary() -> dict:
    """
    Book state.

    total_value is cash plus positions AT COST, not at a guessed market price.
    These are held to resolution, so cost is the honest carrying value; marking
    them to a thin mid-quote would invent P&L that hasn't happened.
    """
    state = _load()
    at_cost = sum(float(h.get("cost_basis", 0) or 0) for h in state["holdings"])
    wins    = state.get("winning_trades", 0)
    settled = state.get("total_trades", 0)

    return {
        "cash":          round(state["cash"], 2),
        "positions":     state["holdings"],
        "n_positions":   len(state["holdings"]),
        "at_risk":       round(at_cost, 2),
        "total_value":   round(state["cash"] + at_cost, 2),
        "starting_cash": state.get("starting_cash", STARTING_CASH),
        "realized_pnl":  round(state.get("total_pnl", 0.0), 2),
        "total_trades":  settled,
        "wins":          wins,
        "losses":        state.get("losing_trades", 0),
        "win_rate":      round(wins / settled * 100, 1) if settled else 0.0,
    }


def deposit(target_bank: float) -> dict:
    """
    Top the book up to `target_bank` without touching trade history.

    NOT a reset. Changing KALSHI_EVENT_STARTING_CASH only affects a book that
    doesn't exist yet — an existing book keeps its old figure forever, which is
    how Stock Golem sat at $2,000 after the variable was raised to $10,000.

    Both cash and starting_cash rise by the same amount, so the reconciliation
    invariant (account movement == recorded events) still holds: adding money
    is not profit, and must not read as profit.
    """
    state = _load()
    current_basis = float(state.get("starting_cash", STARTING_CASH))
    cash          = float(state.get("cash", 0))
    at_cost       = sum(float(h.get("cost_basis", 0) or 0)
                        for h in state.get("holdings", []))
    current_value = cash + at_cost

    delta = round(float(target_bank) - current_value, 2)
    if delta <= 0:
        return {"ok": False,
                "reason": f"book is already ${current_value:,.2f} — "
                          f"nothing to add to reach ${target_bank:,.2f}"}

    state["cash"]          = round(cash + delta, 2)
    state["starting_cash"] = round(current_basis + delta, 2)
    state.setdefault("deposits", []).append({
        "at":     datetime.now(timezone.utc).isoformat(),
        "amount": delta,
        "from_value": current_value,
        "to_value":   round(target_bank, 2),
    })
    saved = _save(state)

    log.warning(f"Kalshi events: DEPOSIT ${delta:,.2f} — book now "
                f"${target_bank:,.2f}, basis ${state['starting_cash']:,.2f}")
    return {"ok": True, "added": delta,
            "cash": state["cash"], "basis": state["starting_cash"],
            "value": round(target_bank, 2), "persisted": saved,
            "trades_preserved": len(state.get("trade_history", []))}


def stale_positions(grace_hours: float = None) -> list:
    """
    Bets whose market should have resolved by now but hasn't reported one.

    settle_bet() is the ONLY exit in this book — deliberately, since resolving
    on an answer instead of a timer is the point. But that means a market that
    never reports a result holds capital forever, silently. A delisted ticker,
    a renamed market, a changed API path, or a Kalshi settlement delay would
    all look identical to "not resolved yet".

    This does not auto-close anything: we don't know the outcome, and guessing
    would invent P&L. It surfaces the position so a human can look.
    """
    if grace_hours is None:
        grace_hours = float(os.getenv("KALSHI_EVENT_STALE_HOURS", "24"))

    now  = datetime.now(timezone.utc)
    out  = []
    for h in _load()["holdings"]:
        ct = h.get("close_time") or ""
        if not ct:
            continue
        try:
            closes = datetime.fromisoformat(ct.replace("Z", "+00:00"))
        except Exception:
            continue
        overdue = (now - closes).total_seconds() / 3600
        if overdue >= grace_hours:
            out.append({**h, "overdue_hours": round(overdue, 1)})
    return out


def open_domains() -> list:
    """Domains we currently hold, for the correlation cap."""
    return [h.get("domain", "") for h in _load()["holdings"]]


def bets_opened_today() -> int:
    """Event bets opened so far in the current UTC day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    state = _load()
    n = sum(1 for h in state["holdings"]
            if str(h.get("opened_at", ""))[:10] == today)
    n += sum(1 for t in state["trade_history"]
             if str(t.get("opened_at", ""))[:10] == today)
    return n


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = get_summary()
    print(f"cash ${s['cash']:.2f} | at risk ${s['at_risk']:.2f} | "
          f"value ${s['total_value']:.2f}")
    print(f"{s['total_trades']} settled, {s['wins']}W/{s['losses']}L "
          f"({s['win_rate']:.0f}%), realized ${s['realized_pnl']:+.2f}")
    for p in s["positions"]:
        print(f"  {p['side']:3s} {p['ticker']:28s} {p['contracts']:>4} @ "
              f"{p['entry_cents']:.0f}c  risk ${p['cost_basis']:.2f}")
