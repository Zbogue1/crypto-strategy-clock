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
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fomo_wallet_stats import record_trade_outcome
from fomo_portfolio import run_fomo_postmortem, run_fomo_ai_postmortem

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

# Proactive rug-pull detection (fires before the -35% stop-loss)
RUG_LIQUIDITY_DROP_PCT = 0.65   # liquidity fell ≥65% from peak → rug signal
RUG_PRICE_CRASH_PCT    = 0.50   # price fell ≥50% since last 5-min check → rug signal

_rug_warned_positions: set = set()   # position_ids already warned (memory only, reset on restart)


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


def _send_telegram_button_local(message: str, button_text: str, callback_data: str):
    """Send a Telegram message with a single inline button (local copy — no circular import)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": button_text, "callback_data": callback_data}
                    ]]
                },
            },
            timeout=10,
        )
    except Exception as e:
        log.error(f"Exit monitor Telegram button error: {e}")


# ─── STOP-LOSS ALARM ──────────────────────────────────────────────────────────
# Repeats every 90 seconds until the user taps "GOT IT" in Telegram, or 30 min
# has elapsed. Auto-execution has already happened; this alarm just ensures the
# user knows ASAP so they can mentally update their P&L.

_stop_alarms: dict = {}   # ack_id → threading.Event (set() to silence)

ALARM_INTERVAL_SEC = 90
ALARM_MAX_REPEATS  = 20   # 20 × 90s ≈ 30 minutes max


def _fire_stop_alarm(ticker: str, net_usd: float, gain_pct: float):
    """
    Immediately send an urgent Telegram alarm for a stop-loss and begin repeating
    it every 90 seconds until the user taps the silence button.
    The trade has already been auto-executed before this is called.
    """
    ack_id     = uuid.uuid4().hex[:8]
    stop_event = threading.Event()
    _stop_alarms[ack_id] = stop_event

    net_str  = f"${net_usd:.2f}"
    pct_str  = f"{gain_pct:.0f}%"
    msg = (
        f"🚨🚨🚨 <b>STOP-LOSS FIRED: {ticker}</b> 🚨🚨🚨\n"
        f"Down <b>{pct_str}</b> from entry — <b>AUTO-EXITED</b>\n"
        f"Recovered: <b>{net_str}</b>\n"
        f"⚠️ Tap to silence this alarm"
    )
    button = "🔕 GOT IT — SILENCE ALARM"

    def _alarm_loop():
        repeat = 0
        while not stop_event.is_set() and repeat < ALARM_MAX_REPEATS:
            _send_telegram_button_local(msg, button, f"ack_stop:{ack_id}")
            repeat += 1
            # Sleep 90 s, checking for stop every second so it cancels quickly
            for _ in range(ALARM_INTERVAL_SEC):
                if stop_event.is_set():
                    break
                time.sleep(1)
        if repeat >= ALARM_MAX_REPEATS and not stop_event.is_set():
            _send_telegram(
                f"🔕 Stop-loss alarm for {ticker} auto-silenced after "
                f"{ALARM_MAX_REPEATS * ALARM_INTERVAL_SEC // 60} minutes."
            )
        _stop_alarms.pop(ack_id, None)
        log.info(f"Stop alarm {ack_id} ({ticker}) finished after {repeat} ping(s)")

    threading.Thread(
        target=_alarm_loop, daemon=True, name=f"stop-alarm-{ack_id}"
    ).start()
    log.info(f"Stop-loss alarm started for {ticker} (ack_id={ack_id})")


def silence_stop_alarm(ack_id: str) -> bool:
    """
    Called by the Telegram webhook when the user taps the silence button.
    Returns True if the alarm was found and silenced, False if already gone.
    """
    event = _stop_alarms.get(ack_id)
    if event:
        event.set()
        log.info(f"Stop alarm {ack_id} silenced by user")
        return True
    return False


# ─── PRICE FETCHING ───────────────────────────────────────────────────────────

def _get_price_and_liquidity(contract: str, chain: str = "solana") -> tuple:
    """
    Fetch current price and liquidity (USD) from DexScreener.
    Returns (price_float, liquidity_usd_float) or (None, None) on failure.
    """
    try:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{contract}"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return None, None
        pairs = resp.json().get("pairs") or []
        if not pairs:
            return None, None
        best  = max(pairs, key=lambda p: float(p.get("liquidity", {}).get("usd", 0) or 0))
        price = float(best.get("priceUsd", 0) or 0)
        liq   = float(best.get("liquidity", {}).get("usd", 0) or 0)
        return (price if price > 0 else None), (liq if liq > 0 else None)
    except Exception as e:
        log.debug(f"Exit monitor price fetch error ({contract[:8]}): {e}")
        return None, None


def _get_current_price(contract: str, chain: str = "solana") -> Optional[float]:
    """Compatibility wrapper — returns price only."""
    price, _ = _get_price_and_liquidity(contract, chain)
    return price


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

    # Reject impossible prices before they corrupt the portfolio
    from fomo_portfolio import is_price_sane
    if not is_price_sane(holding.get("entry_price"), current_price,
                         holding.get("token_ticker", "?")):
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
    from fomo_portfolio import (FOMO_TAKER_FEE, save_fomo_portfolio,
                                sync_fomo_state_to_github, is_price_sane)

    # Reject impossible prices before they corrupt the portfolio
    if not is_price_sane(holding.get("entry_price"), current_price,
                         holding.get("token_ticker", "?")):
        return 0.0

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

    # Record outcome against the wallet that triggered this trade
    try:
        record_trade_outcome(
            alias       = holding.get("wallet_alias", "unknown"),
            token       = holding.get("token_ticker", "?"),
            win         = gain_pct > 0,
            pnl_pct     = round(gain_pct, 2),
            pnl_usd     = round(net - holding.get("spent", 0), 2),
            exit_reason = reason,
        )
    except Exception as e:
        log.warning(f"WalletStats: record_trade_outcome failed: {e}")

    # Run postmortem — rule-based immediately, AI analysis in background thread
    try:
        # Build trade record for postmortem
        trade_record = {
            **holding,
            "exit_price":  current_price,
            "exit_reason": reason,
            "profit_pct":  round(gain_pct, 2),
            "pnl_usd":     round(net - holding.get("spent", 0), 2),
            "exited_at":   datetime.now(timezone.utc).isoformat(),
        }
        run_fomo_postmortem(trade_record)

        # AI postmortem runs in background so it never delays the exit
        def _ai_pm():
            try:
                run_fomo_ai_postmortem(trade_record)
            except Exception as e:
                log.warning(f"AI postmortem background error: {e}")
        threading.Thread(target=_ai_pm, daemon=True, name="fomo-ai-postmortem").start()

    except Exception as e:
        log.warning(f"Postmortem failed (non-fatal): {e}")

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

    current_price, current_liq = _get_price_and_liquidity(contract, chain)
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

    # Update peak liquidity (for rug detection)
    if current_liq:
        peak_liq = holding.get("peak_liquidity", current_liq)
        if current_liq > peak_liq:
            peak_liq = current_liq
        holding["peak_liquidity"] = peak_liq
    else:
        peak_liq = None

    gain_x   = current_price / entry_price
    gain_pct = (gain_x - 1) * 100

    # ── PRICE SANITY CHECK ────────────────────────────────────────────────
    # If DexScreener returns a price implying an impossible gain, the data is
    # corrupt or from the wrong pair. Skip this cycle entirely rather than
    # triggering phantom tranche exits with million-percent gains.
    from fomo_portfolio import MAX_REALISTIC_GAIN_X
    if gain_x > MAX_REALISTIC_GAIN_X:
        log.warning(
            f"Exit monitor {ticker}: suspicious price ${current_price:.8f} "
            f"implies {gain_x:.0f}x gain — likely bad DexScreener data, skipping cycle"
        )
        return

    # ── PROACTIVE RUG DETECTION (advisory — fires before stop-loss) ────────
    # Detects rug-pull signatures: liquidity collapse or sudden single-check
    # price crash. Sends a warning with a manual SELL button so the user can
    # exit before waiting for the -35% stop-loss to trigger automatically.
    position_id = holding.get("position_id", contract[:12])
    if position_id not in _rug_warned_positions:
        rug_reason = None
        if (peak_liq and current_liq
                and current_liq < peak_liq * (1 - RUG_LIQUIDITY_DROP_PCT)):
            rug_reason = (
                f"Liquidity collapsed: "
                f"${current_liq:,.0f} (was ${peak_liq:,.0f}, "
                f"−{(1 - current_liq/peak_liq)*100:.0f}%)"
            )
        last_price = holding.get("last_price_check")
        if (last_price and current_price < last_price * (1 - RUG_PRICE_CRASH_PCT)):
            rug_reason = rug_reason or (
                f"Price crashed {(1 - current_price/last_price)*100:.0f}% "
                f"in one check (${last_price:.8f} → ${current_price:.8f})"
            )
        if rug_reason:
            log.warning(f"Rug risk detected for {ticker}: {rug_reason}")
            _rug_warned_positions.add(position_id)

            # Log this event into the holding — postmortem reads it later to learn
            # whether acting on this warning would have saved money
            holding.setdefault("warning_events", []).append({
                "type":           "rug_risk_detector",
                "timestamp":      datetime.now(timezone.utc).isoformat(),
                "trigger":        rug_reason,
                "gain_pct":       round(gain_pct, 2),
                "liquidity_usd":  current_liq,
                "peak_liquidity": peak_liq,
            })

            # Golem's short recommendation — context-aware based on trigger + P&L
            is_liq_collapse = "Liquidity" in rug_reason
            if gain_pct > 5:
                advice = "You're still in profit. Sell now and keep the gain — this rarely recovers."
            elif gain_pct > 0:
                advice = "Barely green. Take it. Waiting for the stop-loss will cost you more."
            elif is_liq_collapse:
                advice = "Liquidity is gone. Price will follow. Exit now before it goes to zero."
            else:
                advice = "Price is in freefall. Cut the loss here — stop-loss is your floor, not your target."

            _send_telegram_button_local(
                f"🚩🚩 <b>RUG RISK DETECTED: {ticker}</b> 🚩🚩\n"
                f"{rug_reason}\n"
                f"📊 P&amp;L: <b>{gain_pct:+.0f}%</b> from entry\n"
                f"🧠 <i>{advice}</i>",
                "🚨 SELL NOW",
                f"rug_sell:{contract}",
            )

    # Track last seen price (for rug crash detection next cycle)
    holding["last_price_check"] = current_price

    # ── STOP-LOSS: -35% from entry ─────────────────────────────────────────
    if gain_pct <= STOP_LOSS_PCT * 100:
        net = _execute_full_sell(holding, current_price, "stop_loss", state)
        _fire_stop_alarm(ticker, net, gain_pct)   # repeating alarm until user ACKs
        return   # position closed, done

    # ── TRANCHE 1: 2x → sell 33% ──────────────────────────────────────────
    if gain_x >= TRANCHE_1_MULT and not holding.get("tranche_1_sold"):
        # Set flag BEFORE _execute_partial_sell so the save includes it —
        # otherwise the next 5-min cycle reloads from GitHub without the flag
        # and fires the tranche again (caused 4 repeat exits for GTA6).
        holding["tranche_1_sold"] = True
        net = _execute_partial_sell(holding, TRANCHE_SIZE, current_price, "tranche_1_2x", state)
        _send_telegram(
            f"💸 <b>TRANCHE 1 EXIT: {ticker}</b> hit 2x\n"
            f"Sold 33% @ ${current_price:.8f} (+{gain_pct:.0f}%)\n"
            f"Locked: ${net:.2f} | Remaining 67% riding 🚀"
        )
        return

    # ── TRANCHE 2: 3x → sell another 33% ─────────────────────────────────
    if gain_x >= TRANCHE_2_MULT and holding.get("tranche_1_sold") and not holding.get("tranche_2_sold"):
        # Same fix: set flags before save so they persist to GitHub.
        holding["tranche_2_sold"]       = True
        holding["trailing_stop_active"] = True   # arm the trailing stop
        net = _execute_partial_sell(holding, 0.5, current_price, "tranche_2_3x", state)
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
        # Log chart warning event before executing — captured in postmortem
        holding.setdefault("warning_events", []).append({
            "type":      "chart_distribution",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger":   "Distribution/wall pattern detected by chart analysis",
            "gain_pct":  round(gain_pct, 2),
        })
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
