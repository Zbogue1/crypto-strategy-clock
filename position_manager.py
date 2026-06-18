#!/usr/bin/env python3
"""
position_manager.py — Investment lifecycle state machine.

Tracks which phase your Bitcoin investment is in:

  HUNTING  — No position held. Watching for the right time to enter.
  HOLDING  — You own Bitcoin. Watching for the right time to sell.
  WAITING  — You sold. Watching for the right time to buy back in.

Transitions are automatic, triggered by Claude's signals:
  HUNTING  → HOLDING   when BUY or STRONG_BUY fires
  HOLDING  → WAITING   when SELL or STRONG_SELL fires
  WAITING  → HOLDING   when RE_ENTER fires

Manual overrides available via command-line flags:
  python btc_oracle_v2.py --set-position HOLDING --price 95000
  python btc_oracle_v2.py --reset-position
"""

import os
import json
import logging
from datetime import datetime

log = logging.getLogger("crystal_ball")

POSITION_FILE = "position_state.json"

# Which Claude signals trigger mode transitions
ENTRY_SIGNALS   = {"BUY", "STRONG_BUY"}
EXIT_SIGNALS    = {"SELL", "STRONG_SELL"}
REENTRY_SIGNALS = {"RE_ENTER"}


# ─── STATE I/O ────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "mode":             "HUNTING",   # HUNTING | HOLDING | WAITING
        "coin_ticker":      None,        # which coin is currently held or was last sold
        "entry_price":      None,
        "entry_date":       None,
        "entry_signal":     None,
        "entry_confidence": None,
        "sold_price":       None,
        "sold_date":        None,
        "last_profit_pct":  None,
        "total_profit_pct": 0.0,
        "trade_count":      0,
        "trade_history":    [],
    }


def load_position() -> dict:
    if os.path.exists(POSITION_FILE):
        try:
            with open(POSITION_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load position state: {e}")
    return _default_state()


def save_position(state: dict):
    with open(POSITION_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def get_mode() -> str:
    return load_position().get("mode", "HUNTING")


# ─── TRANSITIONS ──────────────────────────────────────────────────────────────

def process_signal(signal: str, current_price: float, confidence: int, coin_ticker: str = None) -> str:
    """
    Called after each Claude analysis cycle. Automatically transitions
    investment mode based on the signal received.
    Returns the current mode after processing.
    """
    state = load_position()
    mode  = state["mode"]

    if mode == "HUNTING" and signal in ENTRY_SIGNALS:
        _enter_position(state, current_price, signal, confidence, coin_ticker)
        log.info(f"⚡ MODE CHANGE: HUNTING → HOLDING  ({coin_ticker or '?'} entry at ${current_price:,.2f})")

    elif mode == "HOLDING" and signal in EXIT_SIGNALS:
        _exit_position(state, current_price, signal)
        log.info(f"⚡ MODE CHANGE: HOLDING → WAITING  (sold {state.get('coin_ticker','?')} at ${current_price:,.2f})")

    elif mode == "WAITING" and signal in REENTRY_SIGNALS:
        _enter_position(state, current_price, signal, confidence, coin_ticker)
        log.info(f"⚡ MODE CHANGE: WAITING → HOLDING  ({coin_ticker or '?'} re-entered at ${current_price:,.2f})")

    return state["mode"]


def _enter_position(state: dict, price: float, signal: str, confidence: int, coin_ticker: str = None):
    state["mode"]             = "HOLDING"
    state["entry_price"]      = price
    state["entry_date"]       = datetime.now().isoformat()
    state["entry_signal"]     = signal
    state["entry_confidence"] = confidence
    if coin_ticker:
        state["coin_ticker"]  = coin_ticker
    save_position(state)


def _exit_position(state: dict, price: float, signal: str):
    entry      = state.get("entry_price") or price
    profit_pct = round((price - entry) / entry * 100, 2)

    # Archive this trade
    state["trade_history"].append({
        "trade_num":    (state.get("trade_count") or 0) + 1,
        "entry_price":  entry,
        "entry_date":   state.get("entry_date"),
        "entry_signal": state.get("entry_signal"),
        "sold_price":   price,
        "sold_date":    datetime.now().isoformat(),
        "exit_signal":  signal,
        "profit_pct":   profit_pct,
        "days_held":    _days_between(state.get("entry_date"), datetime.now().isoformat()),
    })

    state["last_profit_pct"]  = profit_pct
    state["total_profit_pct"] = round((state.get("total_profit_pct") or 0) + profit_pct, 2)
    state["trade_count"]      = (state.get("trade_count") or 0) + 1
    state["mode"]             = "WAITING"
    state["sold_price"]       = price
    state["sold_date"]        = datetime.now().isoformat()
    # Clear current position slot
    state["entry_price"]      = None
    state["entry_date"]       = None
    state["entry_signal"]     = None
    state["entry_confidence"] = None
    save_position(state)


def _days_between(date_str1, date_str2) -> int:
    try:
        d1 = datetime.fromisoformat(date_str1)
        d2 = datetime.fromisoformat(date_str2)
        return abs((d2 - d1).days)
    except Exception:
        return 0


# ─── CONTEXT FOR CLAUDE ───────────────────────────────────────────────────────

def get_position_context(current_price: float) -> dict:
    """
    Returns a dict that gets injected into Claude's analysis payload each cycle.
    Claude uses this to understand whether it's looking for an entry, managing
    an open position, or looking for a re-entry after a sale.
    """
    state = load_position()
    mode  = state["mode"]
    ctx   = {"investment_mode": mode}

    if mode == "HOLDING" and state.get("entry_price"):
        entry          = state["entry_price"]
        unrealized_pct = round((current_price - entry) / entry * 100, 2)
        days_held      = _days_between(state.get("entry_date", ""), datetime.now().isoformat())
        ctx.update({
            "coin_ticker":          state.get("coin_ticker"),
            "entry_price":          entry,
            "entry_date":           state.get("entry_date", ""),
            "entry_signal":         state.get("entry_signal", ""),
            "entry_confidence":     state.get("entry_confidence"),
            "unrealized_pct":       unrealized_pct,
            "days_held":            days_held,
            "note": (f"Investor is holding {state.get('coin_ticker','crypto')} entered at "
                     f"${entry:,.2f}, now {unrealized_pct:+.1f}% after {days_held} days. "
                     f"Evaluate whether to HOLD or SELL."),
        })

    elif mode == "WAITING" and state.get("sold_price"):
        sold            = state["sold_price"]
        chg_since_sale  = round((current_price - sold) / sold * 100, 2)
        days_since_sale = _days_between(state.get("sold_date", ""), datetime.now().isoformat())
        ctx.update({
            "sold_price":            sold,
            "sold_date":             state.get("sold_date", ""),
            "last_profit_pct":       state.get("last_profit_pct"),
            "price_change_since_sale": chg_since_sale,
            "days_since_sale":       days_since_sale,
            "total_profit_pct":      state.get("total_profit_pct"),
            "trade_count":           state.get("trade_count"),
            "note": (f"Investor sold at ${sold:,.0f} ({days_since_sale} days ago). "
                     f"Bitcoin is now {chg_since_sale:+.1f}% {'lower' if chg_since_sale < 0 else 'higher'} "
                     f"since that sale. Evaluate whether to RE_ENTER or WAIT_LONGER."),
        })

    else:
        ctx["note"] = "No position held. Looking for optimal entry point."

    ctx["completed_trades"] = len(state.get("trade_history", []))
    return ctx


def build_mode_prompt(mode: str, pos_ctx: dict) -> str:
    """
    Returns a mode-specific block appended to Claude's system prompt.
    This tells Claude exactly what role it's playing this cycle.
    """
    if mode == "HOLDING":
        entry         = pos_ctx.get("entry_price", 0)
        unrealized    = pos_ctx.get("unrealized_pct", 0)
        days_held     = pos_ctx.get("days_held", 0)
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INVESTMENT MODE: HOLDING — Investor currently owns Bitcoin
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Entry price: ${entry:,.0f}  |  Held: {days_held} days  |  Unrealized: {unrealized:+.1f}%

YOUR JOB THIS CYCLE: Should the investor HOLD or SELL?
You are now a position manager, not an entry-finder.

HOLD when:
- Pi Cycle gap is >10% (no top signal imminent)
- Rainbow chart is not yet in bubble/sell territory
- MVRV equivalent is below euphoria levels (< ~2.5)
- No distribution signals (exchange inflows flat, funding normal)
- Macro conditions still support further upside
- The current unrealized gain is modest relative to cycle potential

SELL when (dynamic — weigh all signals):
- Pi Cycle Top approaching (<10% gap) — historically calls tops within 3 days
- Fear & Greed >85 AND RSI >75 simultaneously (crowd euphoria)
- Funding rates >0.08% (dangerous over-leveraging)
- Exchange reserves sharply rising (major sell pressure building)
- Rainbow chart entering bubble zone
- MVRV equivalent in historical top range

STRONG_SELL (urgent) when:
- Multiple top indicators firing simultaneously
- Pi Cycle actually triggering
- Extreme signals across 3+ categories all bearish

Output signal must be one of: HOLD | SELL | STRONG_SELL
Write layman text for someone who HOLDS and must decide: keep holding or cash out?
Replace all "should I buy" language with "should I keep holding or take profits" language.
"""

    elif mode == "WAITING":
        sold     = pos_ctx.get("sold_price", 0)
        chg      = pos_ctx.get("price_change_since_sale", 0)
        profit   = pos_ctx.get("last_profit_pct", 0)
        days_ago = pos_ctx.get("days_since_sale", 0)
        return f"""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INVESTMENT MODE: WAITING — Investor sold, hunting for re-entry
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Last sold: ${sold:,.0f}  ({days_ago} days ago)
Bitcoin is now {chg:+.1f}% {'lower' if chg < 0 else 'higher'} than the sale price.
Last trade profit: {profit:+.1f}%

YOUR JOB THIS CYCLE: Is it time to buy back in, or wait for a better entry?
You are now a re-entry scout, not a first-time-buyer analyst.

RE_ENTER when:
- Price has pulled back meaningfully from the sold price (ideally 15-30%+ drawdown)
- Accumulation signals re-emerging: Hash Ribbon recovery, low MVRV, exchange outflows
- Fear & Greed resetting toward Fear zone (below 45)
- Macro supports the next leg up (M2 expansion, DXY weakening, rate cuts)
- Risk/reward is clearly favorable — downside is limited, upside is significant

WAIT_LONGER when:
- Price is near or above the sold level (discount not sufficient)
- Market may still be distributing or correcting further
- A clearly better entry price is likely coming
- No accumulation signals yet

Output signal must be one of: RE_ENTER | WAIT_LONGER
Write layman text for someone who SOLD and is waiting to buy back.
All language should be "should I buy back in now?" framing.
"""

    else:  # HUNTING
        return """
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INVESTMENT MODE: HUNTING — No position. Looking for optimal entry.
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Output signal: STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL
"""


# ─── DISPLAY HELPERS ──────────────────────────────────────────────────────────

def get_display_state(current_price: float) -> dict:
    """Returns the full position state plus computed display fields."""
    state = load_position()
    ctx   = get_position_context(current_price)
    ctx["raw_state"] = state
    return ctx


# ─── MANUAL OVERRIDES ─────────────────────────────────────────────────────────

def reset_to_hunting():
    """Reset to HUNTING mode (e.g. if you sold manually outside the agent)."""
    state = load_position()
    state["mode"]             = "HUNTING"
    state["entry_price"]      = None
    state["entry_date"]       = None
    state["entry_signal"]     = None
    state["entry_confidence"] = None
    save_position(state)
    log.info("Position manually reset to HUNTING mode.")


def force_set_position(mode: str, price: float = None, confidence: int = 80):
    """
    Manually set position state, e.g.:
      force_set_position("HOLDING", price=95000)  # I bought at $95k
      force_set_position("WAITING", price=126000) # I sold at $126k
      force_set_position("HUNTING")               # reset
    """
    state = load_position()
    if mode == "HOLDING" and price:
        _enter_position(state, price, "MANUAL_OVERRIDE", confidence)
        log.info(f"Position manually set to HOLDING — entry ${price:,.0f}")
    elif mode == "WAITING" and price:
        # Simulate a sale at this price
        if state.get("entry_price"):
            _exit_position(state, price, "MANUAL_OVERRIDE")
        else:
            state["mode"]           = "WAITING"
            state["sold_price"]     = price
            state["sold_date"]      = datetime.now().isoformat()
            state["last_profit_pct"] = 0
            save_position(state)
        log.info(f"Position manually set to WAITING — sold ${price:,.0f}")
    elif mode == "HUNTING":
        reset_to_hunting()
    else:
        log.warning(f"force_set_position: unknown mode '{mode}'")


def get_trade_summary() -> dict:
    """Returns a summary of all completed trades."""
    state = load_position()
    trades = state.get("trade_history", [])
    if not trades:
        return {"trade_count": 0, "total_profit_pct": 0}
    winners = [t for t in trades if t["profit_pct"] > 0]
    return {
        "trade_count":      len(trades),
        "winners":          len(winners),
        "losers":           len(trades) - len(winners),
        "win_rate":         round(len(winners) / len(trades) * 100, 1),
        "total_profit_pct": state.get("total_profit_pct", 0),
        "best_trade":       max(trades, key=lambda t: t["profit_pct"])["profit_pct"],
        "worst_trade":      min(trades, key=lambda t: t["profit_pct"])["profit_pct"],
        "avg_profit":       round(sum(t["profit_pct"] for t in trades) / len(trades), 1),
        "trades":           trades,
    }
