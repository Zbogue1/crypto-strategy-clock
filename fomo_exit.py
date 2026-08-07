#!/usr/bin/env python3
"""
fomo_exit.py -- Automated exit monitor for FOMO Golem positions.

Runs as a background thread, checking all open positions every 5 minutes.
Executes exits based on a tiered tranche system:

  TRANCHE 1  — Position hits 2x entry price  → sell 33%, lock profit
  TRANCHE 2  — Position hits 3x entry price  → sell another 33%
  TRANCHE 3  — Final 33% rides until:
                 (a) Tracked wallet sells     → follow them out
                 (b) Trailing stop -30% from peak → protect gains
                 (c) Chart shows distribution  → technical exit
  STOP-LOSS  — Position drops -35% from entry → full exit, no exceptions

Telegram messages sent for every action so nothing is silent.
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Exit thresholds
STOP_LOSS_PCT        = -0.35   # -35% from entry → full exit
TRANCHE_1_MULT       = 2.0     # 2x → sell 33%
TRANCHE_2_MULT       = 3.0     # 3x → sell 33%
TRAILING_STOP_PCT    = 0.30    # -30% from peak → exit final 33%
TRANCHE_SIZE         = 1 / 3   # each tranche is 33% of original units

POLL_INTERVAL_SEC    = 300     # check every 5 minutes
STARTUP_DELAY_SEC    = 120     # wait 2 min after Flask starts


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def _send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.error(f"Exit monitor Telegram error: {e}")


# ─── PRICE FETCHING ───────────────────────────────────────────────────────────

def _get_current_price(contract: str, chain: str = "solana") -> Optional[float]:
    """Fetch current price from DexScreener for a contract address."""
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return None
        # Pick the most liquid pair
        best = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        price = float(best.get("priceUsd", 0) or 0)
        return price if price > 0 else None
    except Exception as e:
        log.debug(f"Exit monitor price fetch error ({contract[:8]}): {e}")
        return None


# ─── CHART EXIT CHECK ─────────────────────────────────────────────────────────

def _chart_says_exit(contract: str, chain: str = "solana") -> bool:
    """Quick chart exit check — returns True if distribution/wall detected."""
    try:
        from fomo_chart import analyze_chart, chart_should_exit
        sig = analyze_chart(contract, chain=chain)
        return chart_should_exit(sig)
    except Exception:
        return False


# ─── PARTIAL SELL ─────────────────────────────────────────────────────────────

def _execute_partial_sell(
    holding: dict,
    fraction: float,
    current_price: float,
    reason: str,
    state: dict,
) -> float:
    """
    Sell `fraction` of the holding's remaining units.
    Updates holding in-place. Returns USD proceeds.
    state is the live portfolio dict (mutated directly).
    """
    from fomo_portfolio import FOMO_TAKER_FEE, save_fomo_portfolio, sync_fomo_state_to_github

    units_to_sell = holding["units"] * fraction
    if units_to_sell <= 0:
        return 0.0

    proceeds = units_to_sell * current_price
    fee      = proceeds * FOMO_TAKER_FEE
    net      = proceeds - fee

    # Update holding — reduce units in place
    holding["units"]        -= units_to_sell
    holding["spent"]        *= (1 - fraction)   # proportional cost basis reduction
    holding["peak_price"]    = max(holding.get("peak_price", current_price), current_price)

    state["cash"] += net

    entry   = holding["entry_price"]
    gain_x  = current_price / entry if entry > 0 else 1
    gain_pct = (gain_x - 1) * 100

    log.info(
        f"EXIT [{reason}] {holding['token_ticker']}: "
        f"sold {fraction*100:.0f}% @ ${current_price:.8f} "
        f"({gain_pct:+.0f}%) → +${net:.2f}"
    )

    save_fomo_portfolio(state)
    sync_fomo_state_to_github()
    return net


def _execute_full_sell(
    holding: dict,
    current_price: float,
    reason: str,
    state: dict,
) -> float:
    """Full exit — removes holding from portfolio entirely."""
    from fomo_portfolio import FOMO_TAKER_FEE, save_fomo_portfolio, sync_fomo_state_to_github

    proceeds = holding["units"] * current_price
    fee      = proceeds * FOMO_TAKER_FEE
    net      = proceeds - fee

    entry     = holding["entry_price"]
    gain_x    = current_price / entry if entry > 0 else 1
    gain_pct  = (gain_x - 1) * 100

    state["cash"] += net
    state["total_trades"] = state.get("total_trades", 0) + 1
    if gain_pct > 0:
        state["winning_trades"] = state.get("winning_trades", 0) + 1

    # Record in trade history
    state.setdefault("trade_history", []).append({
        **holding,
        "exit_price":   current_price,
        "exit_reason":  reason,
        "pnl_usd":      round(net - holding.get("spent", 0), 2),
        "pnl_pct":      round(gain_pct, 2),
        "exited_at":    datetime.now(timezone.utc).isoformat(),
    })

    state["holdings"] = [h for h in state["holdings"] if h.get("position_id") != holding.get("position_id")]

    log.info(
        f"FULL EXIT [{reason}] {holding['token_ticker']}: "
        f"@ ${current_price:.8f} ({gain_pct:+.0f}%) → +${net:.2f}"
    )

    save_fomo_portfolio(state)
    sync_fomo_state_to_github()
    return net


# ─── CONDITION CHECKS ─────────────────────────────────────────────────────────

def _check_holding(holding: dict, state: dict):
    """
    Evaluate all exit conditions for one holding.
    Mutates holding and state in-place when action taken.
    """
    contract = holding.get("contract_address")
    ticker   = holding.get("token_ticker", "?")
    chain    = holding.get("chain", "solana")

    if not contract:
        return

    current_price = _get_current_price(contract, chain)
    if not current_price:
        log.debug(f"Exit monitor: couldn't fetch price for {ticker}")
        return

    entry_price = holding.get("entry_price", 0)
    if not entry_price:
        return

    # Update peak price
    peak = holding.get("peak_price", entry_price)
    if current_price > peak:
        peak = current_price
        holding["peak_price"] = peak

    gain_x   = current_price / entry_price
    gain_pct = (gain_x - 1) * 100

    # ── STOP-LOSS: -35% from entry ─────────────────────────────────────────
    if gain_pct <= STOP_LOSS_PCT * 100:
        net = _execute_full_sell(holding, current_price, "stop_loss", state)
        _send_telegram(
            f"🛑 <b>STOP-LOSS: {ticker}</b>\n"
            f"Down {gain_pct:.0f}% from entry — auto-exited full position.\n"
            f"Recovered: ${net:.2f}"
        )
        return   # position closed, done

    # ── TRANCHE 1: 2x → sell 33% ──────────────────────────────────────────
    if gain_x >= TRANCHE_1_MULT and not holding.get("tranche_1_sold"):
        net = _execute_partial_sell(holding, TRANCHE_SIZE, current_price, "tranche_1_2x", state)
        holding["tranche_1_sold"] = True
        _send_telegram(
            f"💸 <b>TRANCHE 1 EXIT: {ticker}</b> hit 2x\n"
            f"Sold 33% @ ${current_price:.8f} (+{gain_pct:.0f}%)\n"
            f"Locked: ${net:.2f} | Remaining 67% riding 🚀"
        )
        return

    # ── TRANCHE 2: 3x → sell another 33% ─────────────────────────────────
    if gain_x >= TRANCHE_2_MULT and holding.get("tranche_1_sold") and not holding.get("tranche_2_sold"):
        net = _execute_partial_sell(holding, 0.5, current_price, "tranche_2_3x", state)
        # sell half of what remains (which is 33% of original)
        holding["tranche_2_sold"]       = True
        holding["trailing_stop_active"] = True   # arm the trailing stop
        _send_telegram(
            f"💸 <b>TRANCHE 2 EXIT: {ticker}</b> hit 3x\n"
            f"Sold another 33% @ ${current_price:.8f} (+{gain_pct:.0f}%)\n"
            f"Locked: ${net:.2f} | Final 33% riding — trailing stop armed at -30% from peak 🎯"
        )
        return

    # ── TRAILING STOP on final 33% ────────────────────────────────────────
    if holding.get("trailing_stop_active") and peak > 0:
        drop_from_peak = (current_price - peak) / peak
        if drop_from_peak <= -TRAILING_STOP_PCT:
            net = _execute_full_sell(holding, current_price, "trailing_stop", state)
            _send_telegram(
                f"📉 <b>TRAILING STOP: {ticker}</b>\n"
                f"Dropped {drop_from_peak*100:.0f}% from peak — final 33% auto-exited.\n"
                f"Recovered: ${net:.2f}"
            )
            return

    # ── CHART EXIT SIGNAL on final 33% ───────────────────────────────────
    if holding.get("tranche_2_sold") and _chart_says_exit(contract, chain):
        net = _execute_full_sell(holding, current_price, "chart_distribution", state)
        _send_telegram(
            f"📊 <b>CHART EXIT: {ticker}</b>\n"
            f"Distribution/wall pattern detected — final 33% auto-exited.\n"
            f"Proceeds: ${net:.2f} | Gain: {gain_pct:+.0f}%"
        )
        return

    log.debug(
        f"Exit monitor {ticker}: {gain_pct:+.1f}% | "
        f"T1={'✅' if holding.get('tranche_1_sold') else '⏳'} "
        f"T2={'✅' if holding.get('tranche_2_sold') else '⏳'} "
        f"peak={peak:.8f}"
    )


# ─── MAIN MONITOR LOOP ────────────────────────────────────────────────────────

def run_exit_checks():
    """Single pass — check all open holdings for exit conditions."""
    from fomo_portfolio import load_fomo_portfolio, save_fomo_portfolio, sync_fomo_state_from_github

    sync_fomo_state_from_github()
    state    = load_fomo_portfolio()
    holdings = state.get("holdings", [])

    if not holdings:
        log.debug("Exit monitor: no open positions")
        return

    log.info(f"Exit monitor: checking {len(holdings)} position(s)")

    # Iterate over a copy — holdings may be removed during iteration
    for holding in list(holdings):
        # Re-check that position still exists in state (may have been closed)
        if not any(h.get("position_id") == holding.get("position_id") for h in state["holdings"]):
            continue
        try:
            _check_holding(holding, state)
            time.sleep(3)   # small gap between price fetches
        except Exception as e:
            log.error(f"Exit monitor error on {holding.get('token_ticker','?')}: {e}")


def start_exit_monitor() -> threading.Thread:
    """
    Start the background exit monitor thread.
    Polls all open positions every 5 minutes.
    """
    def _loop():
        log.info(f"Exit monitor started (checking every {POLL_INTERVAL_SEC//60} min)")
        time.sleep(STARTUP_DELAY_SEC)
        while True:
            try:
                run_exit_checks()
            except Exception as e:
                log.error(f"Exit monitor loop error: {e}")
            time.sleep(POLL_INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-exit-monitor")
    t.start()
    return t


# ─── TRANCHE-AWARE SELL (called when tracked wallet sells) ────────────────────

def handle_tracker_sell(contract: str, current_price: float, ticker: str = "?") -> Optional[float]:
    """
    Called when a tracked wallet sells a token we're holding.
    Exits only the remaining position (respects already-sold tranches).
    Returns USD net proceeds or None if not holding.
    """
    from fomo_portfolio import load_fomo_portfolio, sync_fomo_state_from_github

    sync_fomo_state_from_github()
    state    = load_fomo_portfolio()
    holdings = state.get("holdings", [])
    holding  = next((h for h in holdings if (h.get("contract_address") or "") == contract), None)

    if not holding:
        return None

    entry    = holding.get("entry_price", 0)
    gain_pct = ((current_price / entry) - 1) * 100 if entry > 0 else 0

    net = _execute_full_sell(holding, current_price, "tracker_sold", state)

    t1 = holding.get("tranche_1_sold", False)
    t2 = holding.get("tranche_2_sold", False)

    if t1 and t2:
        msg = (
            f"🏁 <b>FINAL EXIT: {ticker}</b> — tracker sold\n"
            f"Final 33% closed @ {gain_pct:+.0f}%\n"
            f"Proceeds: ${net:.2f} | All tranches complete ✅"
        )
    elif t1:
        msg = (
            f"🏁 <b>EXIT: {ticker}</b> — tracker sold\n"
            f"Remaining 67% closed @ {gain_pct:+.0f}%\n"
            f"Proceeds: ${net:.2f}"
        )
    else:
        msg = (
            f"🏁 <b>FULL EXIT: {ticker}</b> — tracker sold before 2x\n"
            f"Closed full position @ {gain_pct:+.0f}%\n"
            f"Proceeds: ${net:.2f} | Following trader's early exit"
        )
    _send_telegram(msg)
    return net
