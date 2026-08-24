#!/usr/bin/env python3
"""
reconcile.py — Catch silent money movement across all three bots.

THE PATTERN THIS EXISTS TO KILL:
Every accounting bug found on 2026-08-24 was the same shape — money moved and
nothing recorded it:

  · FOMO tranche harvests credited cash but wrote no trade record, so the log
    read "7 closed, all losses" while a winner was being banked.
  · FOMO showed $7.8M of phantom cash from a bad DexScreener price.
  · Kalshi's total_pnl counter never saw funding payments and drifted.
  · FOMO's "Total P&L" measured open positions only, hiding every closed loss.

None surfaced on their own. Each was found by noticing a number looked wrong.
That is not a monitoring strategy.

THE INVARIANT:
    account_value − starting_basis  ==  sum(every recorded event)

If the two disagree, something moved money without leaving a record. The size
of the gap is the size of the blind spot. This runs on a schedule and alerts
when it breaks — so the next instance of this bug announces itself in hours
rather than being discovered by accident weeks later.

Deliberately generic: it does not care WHAT the untracked movement was, only
that the books don't balance. That's what makes it catch bugs we haven't
imagined yet.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# Gaps below this are float noise, not missing records.
TOLERANCE = float(os.getenv("RECONCILE_TOLERANCE", "0.05"))


# ─── KALSHI ───────────────────────────────────────────────────────────────────

def reconcile_kalshi() -> dict:
    try:
        from kalshi_portfolio import _load, get_portfolio_summary
    except Exception as e:
        return {"bot": "Kalshi", "ok": None, "error": f"import failed: {e}"}

    try:
        state = _load()
        s     = get_portfolio_summary()

        basis   = float(state.get("starting_cash", 0) or 0)
        actual  = float(s.get("total_value", 0) or 0) - basis

        # Every recorded event
        trades  = sum(float(t.get("net_pnl", 0) or 0)
                      for t in state.get("trade_history", []))
        funding = -float(state.get("total_funding_paid", 0) or 0)
        unreal  = sum(float(p.get("unrealized_pnl", 0) or 0)
                      for p in s.get("positions", []))
        recorded = trades + funding + unreal

        gap = actual - recorded
        return {
            "bot": "Kalshi", "ok": abs(gap) <= TOLERANCE,
            "basis": basis, "actual": round(actual, 2),
            "recorded": round(recorded, 2), "gap": round(gap, 2),
            "components": {
                "closed trades": round(trades, 2),
                "funding paid":  round(funding, 2),
                "unrealized":    round(unreal, 2),
            },
            "n_trades": len(state.get("trade_history", [])),
        }
    except Exception as e:
        return {"bot": "Kalshi", "ok": None, "error": str(e)}


# ─── FOMO ─────────────────────────────────────────────────────────────────────

def reconcile_fomo(prices: dict = None) -> dict:
    try:
        from fomo_portfolio import load_fomo_portfolio
    except Exception as e:
        return {"bot": "FOMO", "ok": None, "error": f"import failed: {e}"}

    try:
        state    = load_fomo_portfolio()
        basis    = float(state.get("starting_cash", 0) or 0)
        cash     = float(state.get("cash", 0) or 0)
        holdings = state.get("holdings", [])

        # Value open positions at cost when no live price is supplied — using
        # cost keeps the check about RECORDS rather than price movement.
        prices = prices or {}
        pos_val  = sum(h.get("units", 0) *
                       (prices.get(h.get("contract_address"), h.get("entry_price", 0)))
                       for h in holdings)
        pos_cost = sum(float(h.get("spent", 0) or 0) for h in holdings)

        actual = (cash + pos_val) - basis

        full     = sum(float(t.get("profit", t.get("pnl_usd", 0)) or 0)
                       for t in state.get("trade_history", []))
        tranches = sum(float(t.get("profit", 0) or 0)
                       for t in state.get("tranche_sales", []))
        unreal   = pos_val - pos_cost
        recorded = full + tranches + unreal

        gap = actual - recorded
        return {
            "bot": "FOMO", "ok": abs(gap) <= TOLERANCE,
            "basis": basis, "actual": round(actual, 2),
            "recorded": round(recorded, 2), "gap": round(gap, 2),
            "components": {
                "full exits":       round(full, 2),
                "tranche harvests": round(tranches, 2),
                "unrealized":       round(unreal, 2),
            },
            "n_trades": len(state.get("trade_history", [])),
            "n_tranches": len(state.get("tranche_sales", [])),
        }
    except Exception as e:
        return {"bot": "FOMO", "ok": None, "error": str(e)}


# ─── STOCK ────────────────────────────────────────────────────────────────────

def reconcile_stock() -> dict:
    try:
        from stock_portfolio import _load, get_summary
    except Exception as e:
        return {"bot": "Stock", "ok": None, "error": f"import failed: {e}"}

    try:
        state = _load()
        s     = get_summary()

        basis  = float(state.get("starting_cash", 0) or 0)
        actual = float(s.get("total_value", 0) or 0) - basis

        trades = sum(float(t.get("pnl", 0) or 0)
                     for t in state.get("trade_history", []))
        unreal = sum(float(p.get("unrealized", 0) or 0)
                     for p in s.get("positions", []))
        recorded = trades + unreal

        gap = actual - recorded
        return {
            "bot": "Stock", "ok": abs(gap) <= TOLERANCE,
            "basis": basis, "actual": round(actual, 2),
            "recorded": round(recorded, 2), "gap": round(gap, 2),
            "components": {
                "closed trades": round(trades, 2),
                "unrealized":    round(unreal, 2),
            },
            "n_trades": len(state.get("trade_history", [])),
        }
    except Exception as e:
        return {"bot": "Stock", "ok": None, "error": str(e)}


# ─── REPORT ───────────────────────────────────────────────────────────────────

def reconcile_all(which: str = None) -> list:
    fns = {"kalshi": reconcile_kalshi, "fomo": reconcile_fomo,
           "stock": reconcile_stock}
    if which and which.lower() in fns:
        return [fns[which.lower()]()]
    return [f() for f in fns.values()]


def format_report(results: list) -> str:
    lines = ["🧾 <b>RECONCILIATION</b>",
             "<i>Does account movement match the recorded events?</i>\n"]
    any_gap = False

    for r in results:
        bot = r["bot"]
        if r.get("error"):
            lines.append(f"⚠️ <b>{bot}</b> — check failed: {r['error'][:90]}\n")
            continue
        if r["ok"] is None:
            lines.append(f"⚠️ <b>{bot}</b> — inconclusive\n")
            continue

        if r["ok"]:
            lines.append(
                f"✅ <b>{bot}</b> — balanced (${r['actual']:+.2f})\n"
                f"   {r.get('n_trades',0)} trade(s) recorded"
            )
        else:
            any_gap = True
            lines.append(
                f"🔴 <b>{bot}</b> — <b>${abs(r['gap']):.2f} UNACCOUNTED</b>\n"
                f"   account moved ${r['actual']:+.2f}\n"
                f"   records explain ${r['recorded']:+.2f}"
            )
            for k, v in r["components"].items():
                lines.append(f"     · {k}: ${v:+.2f}")
            lines.append(
                "   <i>Money moved without leaving a record — something is "
                "changing cash outside the logged paths.</i>"
            )
        lines.append("")

    if any_gap:
        lines.append("<b>A gap means a reporting blind spot, not necessarily "
                     "lost money.</b> The total is still correct; what's wrong "
                     "is that some category isn't being written down.")
    else:
        lines.append("<i>All books balance. Every dollar of movement is "
                     "explained by a recorded event.</i>")
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import re
    print(re.sub(r"<[^>]+>", "", format_report(reconcile_all())))
