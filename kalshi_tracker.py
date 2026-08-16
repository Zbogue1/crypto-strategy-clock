#!/usr/bin/env python3
"""
kalshi_tracker.py — Main loop for the Kalshi Perps autonomous trading system.

Two loops running in parallel:
  SCAN loop (every 15 min):
    1. Fetch all active Kalshi perp markets
    2. Get full snapshots (candles + funding + OI) for each
    3. Run kalshi_signals to score every market
    4. Pass viable candidates to kalshi_research (Claude Sonnet 5)
    5. Send Telegram alerts for actionable signals
    6. Paper-open positions for UP/DOWN verdicts

  MONITOR loop (every 5 min):
    1. Fetch current prices for all open positions
    2. Check stop loss / take profit / liquidation
    3. Close any triggered positions
    4. Send Telegram exit alerts
    5. Log outcomes to postmortem
    6. Check if funding payment is due (every 8H) → apply and notify

Telegram commands handled:
  /kalshi          → portfolio snapshot
  /kalshi_stats    → postmortem stats
  /kalshi_scan     → force immediate scan

Deploy on Railway alongside the FOMO Golem.
Required env vars:
  ANTHROPIC_API_KEY      — Claude API key
  TELEGRAM_TOKEN         — Bot token (or KALSHI_TELEGRAM_TOKEN)
  TELEGRAM_CHAT_ID       — Your chat ID (or KALSHI_CHAT_ID)
  KALSHI_API_KEY         — Optional: only needed for real-money mode
  KALSHI_USE_DEMO        — Set to "true" for demo API (paper trading default)
  KALSHI_STARTING_CASH   — Starting paper balance (default $500)
  KALSHI_DEFAULT_MARGIN  — Margin per trade (default $50)
"""

import logging
import os
import time
from datetime import datetime, timezone
from threading import Thread, Event

import requests

from kalshi_data       import get_all_markets, get_full_market_snapshot
from kalshi_signals    import get_viable_signals, score_all_markets
from kalshi_research   import scan_all_viable
from kalshi_portfolio  import (
    open_position, close_position, update_prices,
    apply_funding, get_portfolio_summary, format_portfolio_telegram,
)
from kalshi_postmortem import (
    log_call, log_outcome, get_all_summaries, format_stats_telegram,
)
from kalshi_telegram   import (
    send_signal, send_exit, send_funding, send_telegram,
    format_no_signals_summary, format_portfolio_snapshot,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("kalshi_tracker")

# ─── CONFIG ───────────────────────────────────────────────────────────────────

SCAN_INTERVAL_SEC     = int(os.getenv("KALSHI_SCAN_INTERVAL",    "900"))    # 15 min
MONITOR_INTERVAL_SEC  = int(os.getenv("KALSHI_MONITOR_INTERVAL", "300"))    # 5 min
FUNDING_INTERVAL_SEC  = 8 * 3600   # 8 hours

DEFAULT_MARGIN        = float(os.getenv("KALSHI_DEFAULT_MARGIN", "50.0"))
NOTIFY_NO_SIGNALS     = os.getenv("KALSHI_NOTIFY_NO_SIGNALS", "false").lower() == "true"

TELEGRAM_TOKEN = os.getenv("KALSHI_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("KALSHI_CHAT_ID")        or os.getenv("TELEGRAM_CHAT_ID", "")


# ─── TELEGRAM COMMAND HANDLER ─────────────────────────────────────────────────

_last_update_id = 0

def _poll_telegram_commands():
    """Check for incoming Telegram commands (long-poll)."""
    global _last_update_id
    if not TELEGRAM_TOKEN:
        return

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 5},
            timeout=10,
        )
        if r.status_code != 200:
            return
        for update in r.json().get("result", []):
            _last_update_id = max(_last_update_id, update["update_id"])
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            if text.startswith("/kalshi_stats"):
                send_telegram(format_stats_telegram())
            elif text.startswith("/kalshi_scan"):
                send_telegram("🔍 Manual scan triggered...")
                _run_scan_cycle(force=True)
            elif text.startswith("/kalshi"):
                summary = get_portfolio_summary()
                send_telegram(format_portfolio_snapshot(summary))
    except Exception as e:
        log.warning(f"Telegram poll error: {e}")


# ─── SCAN CYCLE ───────────────────────────────────────────────────────────────

_last_alerted: dict = {}  # ticker → timestamp, suppress re-alerts within 4H

def _run_scan_cycle(force: bool = False):
    """Full market scan → signal detection → research → alerts → open positions."""
    log.info("=== KALSHI SCAN CYCLE START ===")
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # 1. Fetch markets
    markets = get_all_markets(use_cache=False)
    if not markets:
        log.warning("Kalshi scan: no markets returned from API")
        return

    log.info(f"Kalshi scan: {len(markets)} active markets")

    # 2. Fetch snapshots (in serial to be kind to the API)
    snapshots = []
    snapshots_by_ticker = {}
    for m in markets:
        snap = get_full_market_snapshot(m["ticker"])
        if snap:
            snapshots.append(snap)
            snapshots_by_ticker[m["ticker"]] = snap
        time.sleep(0.3)  # gentle rate limit

    log.info(f"Kalshi scan: {len(snapshots)} snapshots fetched")

    # 3. Signal scoring
    viable = get_viable_signals(snapshots)
    if not viable:
        log.info("Kalshi scan: no viable signals this cycle")
        if NOTIFY_NO_SIGNALS:
            send_telegram(format_no_signals_summary(len(markets)))
        return

    log.info(f"Kalshi scan: {len(viable)} viable signals — running research agent")

    # 4. Suppress re-alerts for tickers we already hold or alerted recently
    now = time.time()
    summary = get_portfolio_summary()
    open_tickers = {p["ticker"] for p in summary.get("positions", [])}
    if not force:
        viable = [
            v for v in viable
            if v["ticker"] not in open_tickers
            and (now - _last_alerted.get(v["ticker"], 0)) > 4 * 3600
        ]
        if not viable:
            log.info("Kalshi scan: all viable signals already held or alerted recently")
            return

    # 5. Load postmortem context
    pm_summaries = get_all_summaries()

    # 6. Research agent analysis
    verdicts = scan_all_viable(viable, snapshots_by_ticker, pm_summaries)

    # 7. For each actionable verdict, send alert + open paper position
    for verdict in verdicts:
        ticker = verdict["ticker"]
        log.info(f"Kalshi: {ticker} → {verdict['verdict']} {verdict['confidence']}/100")

        # Send Telegram signal
        send_signal(verdict, margin=DEFAULT_MARGIN)

        # Log to postmortem
        log_call(verdict)

        # Open paper position
        funding_rate = 0.0
        snap = snapshots_by_ticker.get(ticker)
        if snap and snap.get("funding"):
            funding_rate = snap["funding"].get("funding_rate", 0.0) or 0.0

        open_position(
            ticker=      ticker,
            title=       verdict.get("title", ticker),
            direction=   verdict["verdict"],
            entry_price= verdict["price"],
            leverage=    verdict.get("suggested_leverage", 2.0),
            margin=      DEFAULT_MARGIN,
            stop_pct=    verdict.get("stop_pct", 5.0),
            tp_pct=      verdict.get("take_profit_pct", 10.0),
            funding_rate= funding_rate,
            confidence=  verdict["confidence"],
            reasoning=   verdict.get("reasoning", ""),
            signal_source= "kalshi_research",
        )

        _last_alerted[ticker] = now
        time.sleep(1.5)  # stagger Telegram sends

    log.info("=== KALSHI SCAN CYCLE END ===")


# ─── MONITOR CYCLE ────────────────────────────────────────────────────────────

_last_funding_check: float = 0.0

def _run_monitor_cycle():
    """Check open positions for SL/TP/liquidation. Apply funding if due."""
    global _last_funding_check

    summary = get_portfolio_summary()
    positions = summary.get("positions", [])
    if not positions:
        return

    # Fetch current prices for all open tickers
    prices_by_ticker = {}
    for pos in positions:
        ticker = pos["ticker"]
        snap   = get_full_market_snapshot(ticker)
        if snap:
            prices_by_ticker[ticker] = snap["price"]
        time.sleep(0.2)

    if not prices_by_ticker:
        return

    # Check exits
    exits = update_prices(prices_by_ticker)
    for exit_event in exits:
        ticker     = exit_event["ticker"]
        reason     = exit_event["reason"]
        exit_price = exit_event["exit_price"]

        trade = close_position(ticker, exit_price, reason=reason)
        if trade:
            send_exit(trade)
            log_outcome(ticker, trade)
            log.info(f"Kalshi monitor: closed {ticker} via {reason} @ {exit_price:.4f}")

    # Funding check (every 8H)
    now = time.time()
    if (now - _last_funding_check) >= FUNDING_INTERVAL_SEC:
        _last_funding_check = now

        # Re-fetch funding rates for all open positions
        funding_rates = {}
        for pos in positions:
            ticker = pos["ticker"]
            snap   = get_full_market_snapshot(ticker)
            if snap and snap.get("funding"):
                funding_rates[ticker] = snap["funding"].get("funding_rate", 0.0) or 0.0

        if funding_rates:
            charges = apply_funding(funding_rates)
            send_funding(charges)
            log.info(f"Kalshi monitor: funding applied for {len(charges)} positions")


# ─── MAIN ─────────────────────────────────────────────────────────────────────

def run_scan_loop(stop_event: Event):
    while not stop_event.is_set():
        try:
            _run_scan_cycle()
        except Exception as e:
            log.error(f"Kalshi scan loop error: {e}", exc_info=True)
        # Wait SCAN_INTERVAL_SEC, checking stop_event every second
        for _ in range(SCAN_INTERVAL_SEC):
            if stop_event.is_set():
                break
            time.sleep(1)


def run_monitor_loop(stop_event: Event):
    while not stop_event.is_set():
        try:
            _run_monitor_cycle()
            _poll_telegram_commands()
        except Exception as e:
            log.error(f"Kalshi monitor loop error: {e}", exc_info=True)
        for _ in range(MONITOR_INTERVAL_SEC):
            if stop_event.is_set():
                break
            time.sleep(1)


def main():
    log.info("=" * 60)
    log.info("KALSHI PERPS TRACKER — starting up")
    log.info(f"Scan interval: {SCAN_INTERVAL_SEC}s | Monitor: {MONITOR_INTERVAL_SEC}s")
    log.info(f"Default margin: ${DEFAULT_MARGIN:.0f} | Max positions: 6")
    log.info("=" * 60)

    # Send startup message
    send_telegram(
        "🚀 *KALSHI Tracker* started\n"
        f"Watching all active perp markets. Scanning every {SCAN_INTERVAL_SEC//60} min.\n"
        f"Paper trading with ${DEFAULT_MARGIN:.0f}/trade.\n"
        "Commands: /kalshi /kalshi_stats /kalshi_scan"
    )

    stop_event = Event()

    scan_thread    = Thread(target=run_scan_loop,    args=(stop_event,), daemon=True, name="kalshi-scan")
    monitor_thread = Thread(target=run_monitor_loop, args=(stop_event,), daemon=True, name="kalshi-monitor")

    scan_thread.start()
    monitor_thread.start()

    log.info("Both loops running. Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Stopping Kalshi tracker...")
        stop_event.set()
        scan_thread.join(timeout=10)
        monitor_thread.join(timeout=10)
        log.info("Kalshi tracker stopped.")


if __name__ == "__main__":
    main()
