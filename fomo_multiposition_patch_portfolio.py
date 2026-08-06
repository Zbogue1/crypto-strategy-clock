#!/usr/bin/env python3
"""
fomo_multiposition_patch_portfolio.py

Converts fomo_portfolio.py from a single active position (`state["holding"]`)
to multiple concurrent positions (`state["holdings"]`, capped at 5).

WHAT CHANGES:
  - state["holding"] (dict or None) -> state["holdings"] (list, cap 5)
  - Existing live position is migrated automatically on next load -- nothing
    is lost.
  - execute_fomo_buy(): now allows a new buy as long as (a) fewer than 5
    positions are open, and (b) you're not already holding that exact token.
    Each position gets a unique position_id.
  - execute_fomo_sell(): now takes contract_address as its first argument, to
    say WHICH position to close (previously there was only ever one).
  - check_fomo_auto_exits(): now checks stop/target/24h for EVERY open
    position independently and returns a LIST of exits (previously a single
    dict-or-None). This also fixes a latent bug -- crypto_oracle_v3.py's
    cron loop already does `for exit_rec in check_fomo_auto_exits(...)`,
    which expects a list; the old single-position version returning None (the
    common case, no exit) would have raised a TypeError there every cycle.
  - get_fomo_value()/get_fomo_stats(): now aggregate across all open
    positions. Both still include a "holding" key (first open position, or
    None) for backward compatibility with any other code reading it.

Run from the repo root:
    python fomo_multiposition_patch_portfolio.py

Safe to re-run -- each edit checks whether it's already applied first.
"""

import subprocess
import sys

TARGET = "fomo_portfolio.py"

EDITS = []

def add_edit(name, old, new):
    EDITS.append((name, old, new))


# ── 1. import uuid ────────────────────────────────────────────────────────────
add_edit(
    "import uuid",
    '''import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic
import requests''',
    '''import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic
import requests''',
)

# ── 2. max concurrent positions constant ──────────────────────────────────────
add_edit(
    "max positions constant",
    '''FOMO_PORTFOLIO_FILE  = "fomo_portfolio.json"
FOMO_LESSONS_FILE    = "fomo_lessons.json"
FOMO_STARTING_CASH   = 500.0
FOMO_MAX_POSITION_PCT = 0.30   # max 30% of FOMO cash per trade
FOMO_TAKER_FEE       = 0.001   # 0.1% per side
FOMO_AUTO_EXIT_HOURS = 24      # auto-exit if original trader hasn't sold
FOMO_HARD_STOP_PCT   = -0.15   # -15% hard stop''',
    '''FOMO_PORTFOLIO_FILE  = "fomo_portfolio.json"
FOMO_LESSONS_FILE    = "fomo_lessons.json"
FOMO_STARTING_CASH   = 500.0
FOMO_MAX_POSITION_PCT = 0.30   # max 30% of FOMO cash per trade
FOMO_TAKER_FEE       = 0.001   # 0.1% per side
FOMO_AUTO_EXIT_HOURS = 24      # auto-exit if original trader hasn't sold
FOMO_HARD_STOP_PCT   = -0.15   # -15% hard stop
FOMO_MAX_CONCURRENT_POSITIONS = 5   # cap on simultaneously open positions''',
)

# ── 3. default state: holding -> holdings ─────────────────────────────────────
add_edit(
    "default state",
    '''def _default_state() -> dict:
    return {
        "cash":           FOMO_STARTING_CASH,
        "starting_cash":  FOMO_STARTING_CASH,
        "holding":        None,
        "trade_history":  [],
        "total_trades":   0,
        "winning_trades": 0,
        "peak_value":     FOMO_STARTING_CASH,
        "max_drawdown":   0.0,
        "started_at":     datetime.now(timezone.utc).isoformat(),
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }''',
    '''def _default_state() -> dict:
    return {
        "cash":           FOMO_STARTING_CASH,
        "starting_cash":  FOMO_STARTING_CASH,
        "holdings":       [],
        "trade_history":  [],
        "total_trades":   0,
        "winning_trades": 0,
        "peak_value":     FOMO_STARTING_CASH,
        "max_drawdown":   0.0,
        "started_at":     datetime.now(timezone.utc).isoformat(),
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }''',
)

# ── 4. load_fomo_portfolio: migrate old single-holding schema ────────────────
add_edit(
    "migration on load",
    '''def load_fomo_portfolio() -> dict:
    if os.path.exists(FOMO_PORTFOLIO_FILE):
        try:
            with open(FOMO_PORTFOLIO_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return _default_state()''',
    '''def _migrate_to_multi_position(state: dict) -> dict:
    """One-time migration: the old schema had a single `holding` dict (or
    None). The new schema uses a `holdings` list so multiple positions can be
    open at once. Safe to call repeatedly -- a no-op once already migrated,
    so any existing live position is preserved rather than lost."""
    if "holdings" not in state:
        old = state.pop("holding", None)
        state["holdings"] = [old] if old else []
    return state


def load_fomo_portfolio() -> dict:
    if os.path.exists(FOMO_PORTFOLIO_FILE):
        try:
            with open(FOMO_PORTFOLIO_FILE) as f:
                return _migrate_to_multi_position(json.load(f))
        except Exception:
            pass
    return _default_state()''',
)

# ── 5. get_fomo_value: aggregate across holdings ──────────────────────────────
add_edit(
    "get_fomo_value multi-position",
    '''def get_fomo_value(current_price: float = None) -> dict:
    state   = load_fomo_portfolio()
    cash    = state["cash"]
    holding = state.get("holding")

    position_value = 0.0
    unrealized_pct = 0.0
    if holding:
        price          = current_price or holding["entry_price"]
        position_value = holding["units"] * price
        unrealized_pct = (price - holding["entry_price"]) / holding["entry_price"] * 100

    total            = cash + position_value
    total_return_pct = (total - FOMO_STARTING_CASH) / FOMO_STARTING_CASH * 100
    n                = state["total_trades"]
    wins             = state["winning_trades"]

    return {
        "cash":             round(cash, 2),
        "position_value":   round(position_value, 2),
        "total_value":      round(total, 2),
        "total_return_pct": round(total_return_pct, 2),
        "unrealized_pct":   round(unrealized_pct, 2),
        "holding":          holding,
        "total_trades":     n,
        "win_rate":         round(wins / n * 100, 1) if n > 0 else 0.0,
        "winning_trades":   wins,
        "max_drawdown":     state.get("max_drawdown", 0.0),
    }''',
    '''def get_fomo_value(current_price: float = None) -> dict:
    """current_price is accepted for backward compatibility but no longer
    determines valuation -- with multiple concurrent holdings across
    different tokens, each is valued at its own entry_price (matches
    get_fomo_stats)."""
    state    = load_fomo_portfolio()
    cash     = state["cash"]
    holdings = state.get("holdings", [])

    position_value = sum(h["units"] * h["entry_price"] for h in holdings)

    total            = cash + position_value
    total_return_pct = (total - FOMO_STARTING_CASH) / FOMO_STARTING_CASH * 100
    n                = state["total_trades"]
    wins             = state["winning_trades"]

    return {
        "cash":             round(cash, 2),
        "position_value":   round(position_value, 2),
        "total_value":      round(total, 2),
        "total_return_pct": round(total_return_pct, 2),
        "holdings":         holdings,
        "holding":          holdings[0] if holdings else None,  # backward compat
        "total_trades":     n,
        "win_rate":         round(wins / n * 100, 1) if n > 0 else 0.0,
        "winning_trades":   wins,
        "max_drawdown":     state.get("max_drawdown", 0.0),
    }''',
)

# ── 6. execute_fomo_buy: cap + duplicate-token check, append to list ─────────
add_edit(
    "execute_fomo_buy multi-position",
    '''    """Execute a FOMO copy trade buy. Returns holding dict or None if skipped."""
    sync_fomo_state_from_github()
    state = load_fomo_portfolio()

    if state.get("holding"):
        log.warning(f"FOMO: Already holding {state['holding']['token_ticker']} — skip buy")
        return None

    cash = state["cash"]''',
    '''    """Execute a FOMO copy trade buy. Returns holding dict or None if skipped.
    Multiple positions can be open at once (up to FOMO_MAX_CONCURRENT_POSITIONS),
    as long as they're in different tokens."""
    sync_fomo_state_from_github()
    state    = load_fomo_portfolio()
    holdings = state.setdefault("holdings", [])

    if len(holdings) >= FOMO_MAX_CONCURRENT_POSITIONS:
        log.warning(f"FOMO: At max concurrent positions ({FOMO_MAX_CONCURRENT_POSITIONS}) — skip buy")
        return None

    if contract_address and any((h.get("contract_address") or "") == contract_address for h in holdings):
        log.warning(f"FOMO: Already holding {token_ticker} — skip duplicate buy")
        return None

    cash = state["cash"]''',
)

add_edit(
    "execute_fomo_buy append + position_id",
    '''    holding = {
        "token_ticker":     token_ticker,
        "token_name":       token_name,
        "entry_price":      entry_price,
        "units":            units,
        "spent":            spend,
        "stop_loss":        stop_loss,
        "exit_target":      exit_target,
        "wallet_alias":     wallet_alias,
        "wallet_address":   wallet_address,
        "contract_address": contract_address,
        # Context captured at entry — used for post-mortem later
        "catalyst":         catalyst,
        "catalyst_score":   catalyst_score,
        "market_cap":       market_cap,
        "liquidity_usd":    liquidity_usd,
        "token_age_days":   token_age_days,
        "holder_count":     holder_count,
        "volume_spike_pct": volume_spike_pct,
        "entered_at":       datetime.now(timezone.utc).isoformat(),
        "auto_exit_at":     (datetime.now(timezone.utc) + timedelta(hours=FOMO_AUTO_EXIT_HOURS)).isoformat(),
        "source":           "fomo_copy",
        "partial_taken":    False,
    }

    state["cash"]    -= spend
    state["holding"] = holding
    save_fomo_portfolio(state)
    sync_fomo_state_to_github()

    log.info(f"FOMO BUY: {token_ticker} @ ${entry_price:.8f} | "
             f"${spend:.2f} | following {wallet_alias} | catalyst: {catalyst or 'none'}")
    return holding''',
    '''    holding = {
        "position_id":      uuid.uuid4().hex[:8],
        "token_ticker":     token_ticker,
        "token_name":       token_name,
        "entry_price":      entry_price,
        "units":            units,
        "spent":            spend,
        "stop_loss":        stop_loss,
        "exit_target":      exit_target,
        "wallet_alias":     wallet_alias,
        "wallet_address":   wallet_address,
        "contract_address": contract_address,
        # Context captured at entry — used for post-mortem later
        "catalyst":         catalyst,
        "catalyst_score":   catalyst_score,
        "market_cap":       market_cap,
        "liquidity_usd":    liquidity_usd,
        "token_age_days":   token_age_days,
        "holder_count":     holder_count,
        "volume_spike_pct": volume_spike_pct,
        "entered_at":       datetime.now(timezone.utc).isoformat(),
        "auto_exit_at":     (datetime.now(timezone.utc) + timedelta(hours=FOMO_AUTO_EXIT_HOURS)).isoformat(),
        "source":           "fomo_copy",
        "partial_taken":    False,
    }

    state["cash"] -= spend
    holdings.append(holding)
    save_fomo_portfolio(state)
    sync_fomo_state_to_github()

    log.info(f"FOMO BUY: {token_ticker} @ ${entry_price:.8f} | "
             f"${spend:.2f} | following {wallet_alias} | catalyst: {catalyst or 'none'} | "
             f"{len(holdings)}/{FOMO_MAX_CONCURRENT_POSITIONS} positions open")
    return holding''',
)

# ── 7. execute_fomo_sell: target by contract_address ──────────────────────────
add_edit(
    "execute_fomo_sell multi-position",
    '''def execute_fomo_sell(
    current_price: float,
    reason:        str = "tracker_exit",
    trader_held_hours: float = None,   # how long the original trader held
    exit_lag_minutes:  float = None,   # how many minutes after trader sold did we sell
) -> Optional[dict]:
    """Exit the active FOMO quick trade."""
    sync_fomo_state_from_github()
    state   = load_fomo_portfolio()
    holding = state.get("holding")
    if not holding:
        return None

    proceeds   = holding["units"] * current_price
    fee        = proceeds * FOMO_TAKER_FEE
    net        = proceeds - fee
    profit     = net - holding["spent"]
    profit_pct = profit / holding["spent"] * 100

    # Calculate hold duration
    entered_at  = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
    held_minutes = (datetime.now(timezone.utc) - entered_at).total_seconds() / 60

    state["cash"] += net

    # Update peak / drawdown
    total_val = state["cash"]
    if total_val > state.get("peak_value", FOMO_STARTING_CASH):
        state["peak_value"] = total_val
    else:
        drawdown = (state["peak_value"] - total_val) / state["peak_value"] * 100
        if drawdown > state.get("max_drawdown", 0):
            state["max_drawdown"] = round(drawdown, 2)

    trade_record = {
        **holding,
        "exit_price":         current_price,
        "exit_reason":        reason,
        "profit":             round(profit, 6),
        "profit_pct":         round(profit_pct, 2),
        "held_minutes":       round(held_minutes, 1),
        "trader_held_hours":  trader_held_hours,
        "exit_lag_minutes":   exit_lag_minutes,
        "exited_at":          datetime.now(timezone.utc).isoformat(),
        "postmortem_done":    False,
    }

    state["trade_history"].append(trade_record)
    state["total_trades"]  += 1
    if profit > 0:
        state["winning_trades"] += 1

    state["holding"] = None
    save_fomo_portfolio(state)
    sync_fomo_state_to_github()

    outcome = "WIN" if profit > 0 else "LOSS"
    log.info(f"FOMO SELL: {holding['token_ticker']} @ ${current_price:.8f} | "
             f"{profit_pct:+.1f}% | {outcome} | {reason}")
    return trade_record''',
    '''def execute_fomo_sell(
    contract_address:  str,             # which open position to close
    current_price:     float,
    reason:             str = "tracker_exit",
    trader_held_hours: float = None,   # how long the original trader held
    exit_lag_minutes:  float = None,   # how many minutes after trader sold did we sell
) -> Optional[dict]:
    """Exit one specific FOMO position, identified by contract_address."""
    sync_fomo_state_from_github()
    state    = load_fomo_portfolio()
    holdings = state.setdefault("holdings", [])
    holding  = next((h for h in holdings if (h.get("contract_address") or "") == contract_address), None)
    if not holding:
        return None
    holdings.remove(holding)

    proceeds   = holding["units"] * current_price
    fee        = proceeds * FOMO_TAKER_FEE
    net        = proceeds - fee
    profit     = net - holding["spent"]
    profit_pct = profit / holding["spent"] * 100

    # Calculate hold duration
    entered_at  = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
    held_minutes = (datetime.now(timezone.utc) - entered_at).total_seconds() / 60

    state["cash"] += net

    # Update peak / drawdown -- includes any other positions still open
    total_val = state["cash"] + sum(h["units"] * h["entry_price"] for h in holdings)
    if total_val > state.get("peak_value", FOMO_STARTING_CASH):
        state["peak_value"] = total_val
    else:
        drawdown = (state["peak_value"] - total_val) / state["peak_value"] * 100
        if drawdown > state.get("max_drawdown", 0):
            state["max_drawdown"] = round(drawdown, 2)

    trade_record = {
        **holding,
        "exit_price":         current_price,
        "exit_reason":        reason,
        "profit":             round(profit, 6),
        "profit_pct":         round(profit_pct, 2),
        "held_minutes":       round(held_minutes, 1),
        "trader_held_hours":  trader_held_hours,
        "exit_lag_minutes":   exit_lag_minutes,
        "exited_at":          datetime.now(timezone.utc).isoformat(),
        "postmortem_done":    False,
    }

    state["trade_history"].append(trade_record)
    state["total_trades"]  += 1
    if profit > 0:
        state["winning_trades"] += 1

    save_fomo_portfolio(state)
    sync_fomo_state_to_github()

    outcome = "WIN" if profit > 0 else "LOSS"
    log.info(f"FOMO SELL: {holding['token_ticker']} @ ${current_price:.8f} | "
             f"{profit_pct:+.1f}% | {outcome} | {reason} | {len(holdings)} position(s) remaining")
    return trade_record''',
)

# ── 8. check_fomo_auto_exits: loop over every open position, return a list ───
add_edit(
    "check_fomo_auto_exits multi-position",
    '''def check_fomo_auto_exits(price_map: dict = None) -> Optional[dict]:
    """
    Called during every 4-hour cycle and by the webhook server.
    Checks hard stop, take-profit target, and 24h time limit.
    Returns trade record if exited, None otherwise.
    """
    sync_fomo_state_from_github()
    state   = load_fomo_portfolio()
    holding = state.get("holding")
    if not holding:
        return None

    ticker        = holding["token_ticker"]
    current_price = (price_map or {}).get(ticker, holding["entry_price"])
    now           = datetime.now(timezone.utc)

    # Time exit — 24h limit
    auto_exit_at = datetime.fromisoformat(holding["auto_exit_at"].replace("Z", "+00:00"))
    if now >= auto_exit_at:
        log.warning(f"FOMO: Auto-exit {ticker} — 24h time limit reached")
        result = execute_fomo_sell(current_price, reason="time_exit_24h")
        if result:
            _notify_auto_exit(result)
        return result

    if current_price > 0:
        # Take profit — hit exit_target
        if current_price >= holding["exit_target"]:
            log.warning(f"FOMO: Take-profit {ticker} — target ${holding['exit_target']:.8f} reached")
            result = execute_fomo_sell(current_price, reason="take_profit")
            if result:
                _notify_auto_exit(result)
            return result

        # Hard stop — -15%
        pct = (current_price - holding["entry_price"]) / holding["entry_price"] * 100
        if pct <= FOMO_HARD_STOP_PCT * 100:
            log.warning(f"FOMO: Hard stop {ticker} — {pct:.1f}%")
            result = execute_fomo_sell(current_price, reason="hard_stop")
            if result:
                _notify_auto_exit(result)
            return result

    return None''',
    '''def check_fomo_auto_exits(price_map: dict = None) -> list:
    """
    Called during every cron cycle and by the webhook server.
    Checks hard stop, take-profit target, and 24h time limit for EVERY open
    position (multiple can be open at once). Returns a list of trade records
    for any positions that were exited (empty list if none fired).
    """
    sync_fomo_state_from_github()
    state    = load_fomo_portfolio()
    holdings = state.get("holdings", [])
    if not holdings:
        return []

    now   = datetime.now(timezone.utc)
    exits = []

    # Snapshot what to check up front -- execute_fomo_sell reloads state itself
    # on each call (and mutates the holdings list), so iterate over a fixed
    # copy rather than the live list.
    to_check = [(h["contract_address"], h["token_ticker"], h["entry_price"],
                 h["exit_target"], h["auto_exit_at"]) for h in holdings]

    for contract_address, ticker, entry_price, exit_target, auto_exit_at_str in to_check:
        current_price = (price_map or {}).get(ticker, entry_price)
        reason = None

        auto_exit_at = datetime.fromisoformat(auto_exit_at_str.replace("Z", "+00:00"))
        if now >= auto_exit_at:
            reason = "time_exit_24h"
        elif current_price > 0:
            if current_price >= exit_target:
                reason = "take_profit"
            else:
                pct = (current_price - entry_price) / entry_price * 100
                if pct <= FOMO_HARD_STOP_PCT * 100:
                    reason = "hard_stop"

        if reason:
            log.warning(f"FOMO: Auto-exit {ticker} — {reason}")
            result = execute_fomo_sell(contract_address, current_price, reason=reason)
            if result:
                _notify_auto_exit(result)
                exits.append(result)

    return exits''',
)

# ── 9. get_fomo_stats: aggregate across holdings ──────────────────────────────
add_edit(
    "get_fomo_stats multi-position",
    '''    holding    = state.get("holding")
    pos_value  = 0.0
    if holding:
        pos_value = holding["units"] * holding["entry_price"]
    total_val  = cash + pos_value

    return {
        "total_value":      round(total_val, 2),
        "total_return_pct": round((total_val - FOMO_STARTING_CASH) / FOMO_STARTING_CASH * 100, 2),
        "cash":             round(cash, 2),
        "total_trades":     n,
        "win_rate":         round(wins / n * 100, 1) if n > 0 else 0.0,
        "avg_return":       round(sum(t.get("profit_pct", 0) for t in history) / len(history), 2) if history else 0.0,
        "max_drawdown":     state.get("max_drawdown", 0.0),
        "wallet_stats":     wallet_stats,
        "holding":          holding,
    }''',
    '''    holdings  = state.get("holdings", [])
    pos_value = sum(h["units"] * h["entry_price"] for h in holdings)
    total_val = cash + pos_value

    return {
        "total_value":      round(total_val, 2),
        "total_return_pct": round((total_val - FOMO_STARTING_CASH) / FOMO_STARTING_CASH * 100, 2),
        "cash":             round(cash, 2),
        "total_trades":     n,
        "win_rate":         round(wins / n * 100, 1) if n > 0 else 0.0,
        "avg_return":       round(sum(t.get("profit_pct", 0) for t in history) / len(history), 2) if history else 0.0,
        "max_drawdown":     state.get("max_drawdown", 0.0),
        "wallet_stats":     wallet_stats,
        "holdings":         holdings,
        "holding":          holdings[0] if holdings else None,  # backward compat
        "open_positions":   len(holdings),
    }''',
)


def patch_file(path, edits):
    with open(path, encoding="utf-8") as f:
        content = f.read()

    applied, skipped = [], []
    for name, old, new in edits:
        if new in content:
            skipped.append(name)
            continue
        if old not in content:
            print(f"MISSING ANCHOR [{path} :: {name}]")
            print("---- expected to find ----")
            print(old)
            sys.exit(1)
        content = content.replace(old, new, 1)
        applied.append(name)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    for name in applied:
        print(f"APPLIED: {name}")
    for name in skipped:
        print(f"ALREADY APPLIED (skipped): {name}")


def main():
    patch_file(TARGET, EDITS)

    print("\nCompiling...")
    subprocess.run([sys.executable, "-m", "py_compile", TARGET], check=True)
    print("COMPILE OK")

    print("\nGit status:")
    subprocess.run(["git", "status", "--short"])

    print(
        "\nIMPORTANT -- run fomo_multiposition_patch_tracker.py too (same repo\n"
        "root) before committing -- fomo_tracker.py calls execute_fomo_sell()\n"
        "in several places and needs the matching contract_address argument\n"
        "added, or it will break once this file's signature changes.\n"
        "\n"
        "After BOTH patches are applied:\n"
        "  git add fomo_portfolio.py fomo_tracker.py\n"
        "  git commit -m \"Support multiple concurrent FOMO positions (cap 5)\"\n"
        "  git push origin master\n"
        "  git push origin main\n"
        "\n"
        "Your existing open position will be migrated automatically the first\n"
        "time this runs -- nothing is lost.\n"
    )


if __name__ == "__main__":
    main()
