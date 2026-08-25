#!/usr/bin/env python3
"""
fomo_aftermath.py — Watch what a token does AFTER we sell it.

THE BLIND SPOT THIS FILLS
Every exit currently scores as a win or a loss based on the price we got, and
then the token vanishes from the system. So "we sold CATE too early and it ran"
is a thing you can notice and feel, but not a thing the bot can learn from.
Selling at 3x scores identically whether the token then died or went to 20x.

That makes the tranche levels (2x / 3x / trail) unfalsifiable. There is no way
to tell whether they're too tight or exactly right, because the counterfactual
was never recorded. One vivid example argues for holding longer; the ten
tokens that went to zero right after we sold are forgotten.

WHAT THIS DOES
After a full exit, keep pricing the token for a couple of weeks and record the
peak it reached. Then the question "should we hold longer?" has an answer made
of numbers:

    "Across 14 exits, the median peak after selling was 1.1x. Three ran past
     2x. Nine fell below the exit price within a week."

versus

    "Across 14 exits, the median peak after selling was 3.4x. We are
     systematically selling into the first leg of a move."

The first says the tranches are right. The second says raise them. Without
this data both feel equally true depending on the last trade you remember.

WHAT IT DOES NOT DO
Trade. It only measures. Nothing here can buy a token back.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from statistics import median
from typing import Optional

log = logging.getLogger(__name__)

_DATA_DIR  = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__))
STATE_FILE = os.path.join(_DATA_DIR, "fomo_aftermath.json")
STATE_KEY  = "fomo_aftermath"

# How long to keep watching after we sell. Two weeks covers the usual memecoin
# arc without holding a watchlist forever.
WATCH_DAYS      = float(os.getenv("FOMO_AFTERMATH_DAYS", "14"))
CHECK_INTERVAL  = int(os.getenv("FOMO_AFTERMATH_INTERVAL_SEC", "1800"))
# Tell the user when something we sold runs away from us — that is the signal
# worth acting on, and it should not wait for someone to run a report.
ALERT_MULTIPLE  = float(os.getenv("FOMO_AFTERMATH_ALERT_X", "2.0"))


def _redis():
    try:
        from kalshi_portfolio import _redis_get, _redis_set
        return _redis_get, _redis_set
    except Exception:
        return None, None


def _load() -> dict:
    get, _ = _redis()
    if get:
        data = get(STATE_KEY)
        if data:
            return data
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Aftermath: load error: {e}")
    return {"watching": [], "completed": []}


def _save(state: dict):
    _, put = _redis()
    if put:
        put(STATE_KEY, state)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Aftermath: save error: {e}")


# ─── RECORD AN EXIT ───────────────────────────────────────────────────────────

def record_exit(holding: dict, exit_price: float, reason: str,
                entry_price: float = None):
    """
    Start watching a token we just sold out of completely.

    Called from _execute_full_sell. Partial tranche sells are NOT watched —
    we still hold those, so the position itself already tracks the upside.
    """
    contract = holding.get("contract_address")
    if not contract or not exit_price:
        return

    state = _load()
    if any(w["contract"] == contract for w in state["watching"]):
        return                       # already watching

    state["watching"].append({
        "contract":      contract,
        "ticker":        holding.get("token_ticker", "?"),
        "entry_price":   float(entry_price or holding.get("entry_price") or 0),
        "exit_price":    float(exit_price),
        "exit_reason":   reason,
        "exited_at":     datetime.now(timezone.utc).isoformat(),
        "peak_after":    float(exit_price),
        "peak_at":       None,
        "last_price":    float(exit_price),
        "checks":        0,
        "alerted":       False,
        # What we'd have made per dollar had we held the whole position
        "tranche_1_sold": bool(holding.get("tranche_1_sold")),
        "tranche_2_sold": bool(holding.get("tranche_2_sold")),
    })
    _save(state)
    log.info(f"Aftermath: now watching {holding.get('token_ticker')} "
             f"post-exit at ${exit_price:.8f} ({reason})")


# ─── THE WATCH ────────────────────────────────────────────────────────────────

def run_check(notify=None) -> dict:
    """
    Price everything on the watchlist once. Returns a small summary.

    `notify` is a callable taking a message string, so this module never needs
    to know how any particular bot talks to Telegram.
    """
    state = _load()
    if not state["watching"]:
        return {"watched": 0, "retired": 0, "alerts": 0}

    try:
        from fomo_exit import get_prices_batch
    except Exception as e:
        log.error(f"Aftermath: cannot import price fetcher: {e}")
        return {"watched": 0, "retired": 0, "alerts": 0, "error": str(e)}

    contracts = [w["contract"] for w in state["watching"]]
    prices    = get_prices_batch(contracts)

    now      = datetime.now(timezone.utc)
    still    = []
    retired  = 0
    alerts   = 0

    for w in state["watching"]:
        px, _liq = prices.get(w["contract"], (0, 0))
        w["checks"] += 1

        if px:
            w["last_price"] = px
            if px > w["peak_after"]:
                w["peak_after"] = px
                w["peak_at"]    = now.isoformat()

            mult = px / w["exit_price"] if w["exit_price"] else 0
            if mult >= ALERT_MULTIPLE and not w["alerted"] and notify:
                w["alerted"] = True
                alerts += 1
                notify(
                    f"📈 <b>{w['ticker']} ran after we sold</b>\n\n"
                    f"We exited at ${w['exit_price']:.8f} ({w['exit_reason']})\n"
                    f"It is now ${px:.8f} — <b>{mult:.1f}x our exit</b>\n\n"
                    f"<i>Recorded for tranche calibration. This is one data "
                    f"point, not a reason to change the rules — /aftermath "
                    f"shows whether it's a pattern.</i>"
                )

        # Retire once the watch window closes
        try:
            exited = datetime.fromisoformat(w["exited_at"].replace("Z", "+00:00"))
            if (now - exited).days >= WATCH_DAYS:
                w["final_multiple"] = round(
                    w["peak_after"] / w["exit_price"], 3) if w["exit_price"] else 0
                state["completed"].append(w)
                retired += 1
                continue
        except Exception:
            pass
        still.append(w)

    state["watching"] = still
    _save(state)
    return {"watched": len(contracts), "retired": retired, "alerts": alerts}


def start_watcher(notify=None) -> threading.Thread:
    def _loop():
        time.sleep(300)              # let startup settle
        while True:
            try:
                run_check(notify)
            except Exception as e:
                log.error(f"Aftermath check failed: {e}")
            time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-aftermath")
    t.start()
    log.info(f"Aftermath watcher started — tracking exits for {WATCH_DAYS:.0f} days")
    return t


# ─── THE ANSWER ───────────────────────────────────────────────────────────────

def build_report() -> str:
    """
    Did we sell too early? Plain text — ticker names break Markdown.
    """
    state = _load()
    watching  = state.get("watching", [])
    completed = state.get("completed", [])
    everything = watching + completed

    if not everything:
        return ("AFTERMATH — what happened after we sold\n\n"
                "No exits tracked yet. This starts recording from the next "
                "full exit.")

    mults = [ (w.get("final_multiple")
               or (w["peak_after"] / w["exit_price"] if w["exit_price"] else 0))
              for w in everything ]
    mults = [m for m in mults if m > 0]

    ran   = [m for m in mults if m >= 2.0]
    faded = [m for m in mults if m < 1.1]

    L = ["AFTERMATH — what happened after we sold", ""]
    L.append(f"Tracking {len(watching)} open, {len(completed)} completed")
    if mults:
        L += [
            f"Median peak after exit:  {median(mults):.2f}x",
            f"Best:                    {max(mults):.2f}x",
            f"Ran 2x+ after we sold:   {len(ran)}/{len(mults)}",
            f"Never recovered exit:    {len(faded)}/{len(mults)}",
            "",
        ]
        med = median(mults)
        if med >= 2.0:
            L += ["READ: we are systematically selling into the first leg.",
                  "      Raising the tranche multiples is supported by this data.", ""]
        elif med <= 1.2:
            L += ["READ: exits are landing near the top. Tranches look right;",
                  "      holding longer would have given most of it back.", ""]
        else:
            L += ["READ: mixed. Not enough separation to justify changing the",
                  "      tranche levels yet.", ""]

    L.append("BIGGEST RUNAWAYS")
    ranked = sorted(everything,
                    key=lambda w: -(w.get("final_multiple")
                                    or (w["peak_after"] / w["exit_price"]
                                        if w["exit_price"] else 0)))
    for w in ranked[:8]:
        m = (w.get("final_multiple")
             or (w["peak_after"] / w["exit_price"] if w["exit_price"] else 0))
        tag = "" if w in completed else "  (still watching)"
        L.append(f"  {w['ticker']:<12} {m:>6.2f}x after {w['exit_reason']}{tag}")

    L += ["", "Note: peak after exit is the BEST case — it assumes selling at "
          "the exact top, which nothing does. Treat these as a ceiling, not a "
          "forgone profit."]
    return "\n".join(L)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build_report())
