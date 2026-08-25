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
of the gap is the size of the blind spot.

check_self() runs inside every tracker's main loop (default every 6h) against
the ONE bot that service has credentials for, and stays silent unless the books
disagree — so the next instance of this bug announces itself in hours rather
than being discovered by accident weeks later. /reconcile runs it on demand.

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


def _has_visible_data(state: dict, summary_positions: int = 0) -> bool:
    """
    Can this process actually SEE the bot's state?

    Each bot stores to its own backend, and each Railway service only has the
    credentials it was given. When FOMO's service runs a Kalshi reconciliation
    without the Upstash variables, kalshi_portfolio._load() falls back to a
    blank default — zero trades, zero movement — and a naive check happily
    reports "balanced", because 0 == 0.

    That's worse than no answer: it's a green tick over data we never loaded.
    A bot with no trades, no positions and no deposits is almost certainly
    invisible rather than genuinely empty.
    """
    return bool(
        state.get("trade_history")
        or state.get("holdings")
        or state.get("positions")
        or state.get("tranche_sales")
        or state.get("deposits")
        or summary_positions
    )


# ─── KALSHI ───────────────────────────────────────────────────────────────────

def reconcile_kalshi() -> dict:
    try:
        from kalshi_portfolio import _load, get_portfolio_summary
    except Exception as e:
        return {"bot": "Kalshi", "ok": None, "error": f"import failed: {e}"}

    try:
        state = _load()
        s     = get_portfolio_summary()
        if not _has_visible_data(state, len(s.get('positions', []))):
            return {"bot": "Kalshi", "ok": None,
                    "error": "no data visible from this service "
                             "(missing UPSTASH_REDIS_* credentials?)"}

        basis   = float(state.get("starting_cash", 0) or 0)
        actual  = float(s.get("total_value", 0) or 0) - basis

        # Every recorded event.
        #
        # Funding is deliberately NOT a separate term. It is already inside
        # both of the numbers below:
        #   close_position:  net_pnl       = realized_pnl - funding_paid
        #   apply_funding:   unrealized_pnl = calc_pnl(...) - funding_paid
        # Adding total_funding_paid on top subtracted it a second time and
        # produced a phantom gap exactly equal to the funding total — which is
        # what "Kalshi — $6.52 UNACCOUNTED" was, with funding at $6.52. The
        # books were correct; this formula was not.
        trades  = sum(float(t.get("net_pnl", 0) or 0)
                      for t in state.get("trade_history", []))
        unreal  = sum(float(p.get("unrealized_pnl", 0) or 0)
                      for p in s.get("positions", []))
        recorded = trades + unreal

        gap = actual - recorded
        return {
            "bot": "Kalshi", "ok": abs(gap) <= TOLERANCE,
            "basis": basis, "actual": round(actual, 2),
            "recorded": round(recorded, 2), "gap": round(gap, 2),
            "components": {
                "closed trades": round(trades, 2),
                "unrealized":    round(unreal, 2),
                # Shown for context only — already inside the two above.
                "(funding, already included)":
                    round(-float(state.get("total_funding_paid", 0) or 0), 2),
            },
            "n_trades": len(state.get("trade_history", [])),
        }
    except Exception as e:
        return {"bot": "Kalshi", "ok": None, "error": str(e)}


# ─── KALSHI EVENT BOOK ────────────────────────────────────────────────────────

def reconcile_kalshi_events() -> dict:
    """
    The event book keeps its own cash, so it needs its own invariant.

    Simpler than the others: binary contracts are carried at cost until they
    settle, so movement is exactly realized P&L. Any gap means cash changed
    without a settlement record — the same bug class as FOMO's tranche
    harvests, which credited cash and wrote nothing down.
    """
    try:
        from kalshi_event_portfolio import _load, get_summary
    except Exception as e:
        return {"bot": "Kalshi-Events", "ok": None, "error": f"import failed: {e}"}

    try:
        state = _load()
        s     = get_summary()
        if not _has_visible_data(state, len(s.get("positions", []))):
            return {"bot": "Kalshi-Events", "ok": None,
                    "error": "no data visible from this service "
                             "(missing UPSTASH_REDIS_* credentials?)"}

        basis  = float(state.get("starting_cash", 0) or 0)
        actual = float(s.get("total_value", 0) or 0) - basis

        settled = sum(float(t.get("net_pnl", 0) or 0)
                      for t in state.get("trade_history", []))

        gap = actual - settled
        return {
            "bot": "Kalshi-Events", "ok": abs(gap) <= TOLERANCE,
            "basis": basis, "actual": round(actual, 2),
            "recorded": round(settled, 2), "gap": round(gap, 2),
            "components": {
                "settled bets": round(settled, 2),
                "open at cost": round(float(s.get("at_risk", 0) or 0), 2),
            },
            "n_trades": len(state.get("trade_history", [])),
        }
    except Exception as e:
        return {"bot": "Kalshi-Events", "ok": None, "error": str(e)}


# ─── FOMO ─────────────────────────────────────────────────────────────────────

def reconcile_fomo(prices: dict = None) -> dict:
    try:
        from fomo_portfolio import load_fomo_portfolio
    except Exception as e:
        return {"bot": "FOMO", "ok": None, "error": f"import failed: {e}"}

    try:
        state    = load_fomo_portfolio()
        if not _has_visible_data(state):
            return {"bot": "FOMO", "ok": None,
                    "error": "no data visible from this service"}
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
        if not _has_visible_data(state, len(s.get('positions', []))):
            return {"bot": "Stock", "ok": None,
                    "error": "no data visible from this service "
                             "(missing UPSTASH_REDIS_* credentials?)"}

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
    fns = {"kalshi": reconcile_kalshi, "events": reconcile_kalshi_events,
           "fomo": reconcile_fomo, "stock": reconcile_stock}
    if which and which.lower() in fns:
        return [fns[which.lower()]()]
    return [f() for f in fns.values()]


def strip_html(text: str) -> str:
    """
    Plain-text version of a report.

    Kalshi and Stock both send with parse_mode=None, and their senders only
    strip Markdown markers (* and `) — an HTML tag sails straight through and
    the user sees a literal "<b>Kalshi</b>". Callers that aren't sending HTML
    must strip it rather than hope the transport does.
    """
    import re as _re
    text = _re.sub(r"</?(b|i|u|s|code|pre)>", "", text)
    return (text.replace("&lt;", "<").replace("&gt;", ">")
                .replace("&amp;", "&"))


def format_report(results: list, html: bool = True) -> str:
    lines = ["🧾 <b>RECONCILIATION</b>",
             "<i>Does account movement match the recorded events?</i>\n"]
    any_gap = False
    any_blind = False

    for r in results:
        bot = r["bot"]
        err = r.get("error") or ""
        if "no data visible" in err:
            # NOT the same as "balanced". This service can't read that bot's
            # store, so there is nothing to check. Saying "✅ balanced" here
            # would be a green tick over data we never loaded.
            any_blind = True
            lines.append(
                f"🚫 <b>{bot}</b> — <b>not visible from this service</b>\n"
                f"   {err}\n"
                f"   <i>Run /reconcile from {bot}'s own bot for a real answer.</i>\n"
            )
            continue
        if err:
            lines.append(f"⚠️ <b>{bot}</b> — check failed: {err[:90]}\n")
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
    elif any_blind:
        n_ok = sum(1 for r in results if r.get("ok") is True)
        lines.append(
            (f"<i>{n_ok} bot(s) balance. " if n_ok else "<i>")
            + "The ones marked not visible were not checked at all — this "
              "service has no credentials for their store, so a blank read "
              "is indistinguishable from an empty one.</i>"
        )
    else:
        lines.append("<i>All books balance. Every dollar of movement is "
                     "explained by a recorded event.</i>")

    out = "\n".join(lines)
    return out if html else strip_html(out)


# ─── SCHEDULED SELF-CHECK ─────────────────────────────────────────────────────
#
# The whole point of this module is that the NEXT accounting bug announces
# itself instead of waiting to be noticed. A /reconcile command the user has to
# remember to type does not do that — it's the same "someone spots a wrong
# number" workflow, just with better output.
#
# So each tracker calls check_self() on every loop pass. It rate-limits itself,
# only checks the ONE bot that service can actually see, and stays silent unless
# the books disagree. Silence is the normal state; a message means real news.

CHECK_INTERVAL_H = float(os.getenv("RECONCILE_INTERVAL_H", "6"))

_last_check: dict = {}
_last_alert: dict = {}


def check_self(bot: str, send_fn, html: bool = True) -> Optional[dict]:
    """
    Run this service's own reconciliation on a timer; alert only on a gap.

    `bot` is one of kalshi/fomo/stock — the one THIS service has credentials
    for. Checking the other two from here is what produced the false "balanced"
    report in the first place.

    Returns the result dict when a check ran, else None.
    """
    key = bot.lower()
    now = datetime.now(timezone.utc)

    last = _last_check.get(key)
    if last and (now - last).total_seconds() < CHECK_INTERVAL_H * 3600:
        return None
    _last_check[key] = now

    try:
        result = reconcile_all(key)[0]
    except Exception as e:
        log.error(f"reconcile: self-check for {bot} crashed: {e}")
        return None

    if result.get("ok") is not False:
        # Balanced, or not visible. Neither is worth a notification.
        log.info(f"reconcile: {bot} self-check — ok={result.get('ok')} "
                 f"{result.get('error', '')}")
        return result

    # Don't re-send the same unchanged gap every interval — one alert, then
    # quiet until the number actually moves.
    gap = round(float(result.get("gap", 0) or 0), 2)
    if _last_alert.get(key) == gap:
        log.info(f"reconcile: {bot} gap unchanged at ${gap:+.2f}, not re-alerting")
        return result
    _last_alert[key] = gap

    try:
        send_fn(format_report([result], html=html))
    except Exception as e:
        log.error(f"reconcile: could not send {bot} gap alert: {e}")
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import re
    print(format_report(reconcile_all(), html=False))
