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
from typing import Optional

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

# Autonomous perp scanning — ON. Golem keeps hunting and paper trading so we
# build a real track record. Set KALSHI_AUTO_SCAN=false to stop trading entirely.
AUTO_SCAN             = os.getenv("KALSHI_AUTO_SCAN", "true").lower() == "true"

# SILENT MODE — trade autonomously but don't send unsolicited Telegram alerts.
# Everything is still recorded to the portfolio and postmortem, so /kalshi and
# /kalshi_stats show you exactly what happened whenever you want to look.
# Set KALSHI_SILENT=false to get live entry/exit notifications again.
SILENT                = os.getenv("KALSHI_SILENT", "true").lower() == "true"

# Keep managing positions that are already open.
MONITOR_POSITIONS     = os.getenv("KALSHI_MONITOR_POSITIONS", "true").lower() == "true"

# ─── DAY-TRADING MODE ─────────────────────────────────────────────────────────
# Target N new bets per UTC day, every one closed out the same day.
# This produces a clean, comparable daily sample for the weekly report.
DAILY_TRADE_TARGET    = int(os.getenv("KALSHI_DAILY_TRADES", "4"))
# Force-close any position still open at/after this UTC hour.
DAY_CLOSE_UTC_HOUR    = int(os.getenv("KALSHI_DAY_CLOSE_HOUR", "23"))
# Stop opening new trades this many hours before the close, so every position
# gets a fair run instead of being opened at 22:55 and killed at 23:00.
ENTRY_CUTOFF_BUFFER_H = float(os.getenv("KALSHI_ENTRY_BUFFER_HOURS", "3"))
# Hard cap on how long a single day trade can run, regardless of clock.
MAX_HOLD_HOURS        = float(os.getenv("KALSHI_MAX_HOLD_HOURS", "20"))

# ─── WEEKLY REPORT ────────────────────────────────────────────────────────────
WEEKLY_REPORT_ENABLED = os.getenv("KALSHI_WEEKLY_REPORT", "true").lower() == "true"
REPORT_INTERVAL_DAYS  = float(os.getenv("KALSHI_REPORT_DAYS", "7"))
_REPORT_STATE_KEY     = "kalshi_report_state"

TELEGRAM_TOKEN = os.getenv("KALSHI_TELEGRAM_TOKEN") or os.getenv("TELEGRAM_TOKEN", "")
CHAT_ID        = os.getenv("KALSHI_CHAT_ID")        or os.getenv("TELEGRAM_CHAT_ID", "")


# ─── TELEGRAM COMMAND HANDLER ─────────────────────────────────────────────────

HELP_TEXT = (
    "🤖 *KALSHI Golem*\n\n"
    "I stay quiet unless you ask. Just send me any bet question:\n\n"
    "`Will Bitcoin be above $120,000 this week?`\n"
    "`Will the Fed cut rates in December?`\n"
    "`Who wins, Parry or Boisson?`\n\n"
    "Any message with a `?` gets analyzed. Use `/ask <question>` if it "
    "doesn't end in a question mark.\n\n"
    "*Commands:*\n"
    "`/report` — performance report (add days: `/report 30`)\n"
    "`/kalshi` — portfolio snapshot\n"
    "`/kalshi_stats` — track record & calibration\n"
    "`/kalshi_scan` — scan perps on demand (one-off)\n"
    "`/help` — this message"
)

_last_update_id = 0


def _handle_ask(question: str):
    """Run the expert bet analyzer on a free-text question and reply."""
    if not question:
        send_telegram(
            "Ask me about any Kalshi market, like:\n"
            "`/ask Will Bitcoin be above $120,000 this week?`"
        )
        return

    send_telegram(f"🔎 Analyzing: _{question}_\nRunning base rates, market pricing, volatility model, and news search...")
    try:
        from kalshi_analyst import analyze_question
        result = analyze_question(question)
        if result.get("error"):
            send_telegram(f"⚠️ Couldn't complete that analysis: {result['error']}")
            return
        send_telegram(result["telegram"])
        log.info(
            f"Kalshi /ask: '{question[:60]}' → {result.get('verdict')} "
            f"P(yes)={result.get('probability_yes')}% edge={result.get('edge_points')}"
        )
    except Exception as e:
        log.error(f"Kalshi /ask error: {e}", exc_info=True)
        send_telegram(f"⚠️ Analysis failed: {e}")

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
            if text.startswith("/report"):
                parts = text.split()
                days = REPORT_INTERVAL_DAYS
                if len(parts) > 1:
                    try:
                        days = float(parts[1])
                    except ValueError:
                        pass
                send_telegram(build_weekly_report(days))
            elif text.startswith("/kalshi_stats"):
                send_telegram(format_stats_telegram())
            elif text.startswith("/kalshi_scan"):
                send_telegram("🔍 Manual scan triggered...")
                _run_scan_cycle(force=True)
            elif text.startswith("/ask"):
                question = text[len("/ask"):].strip()
                _handle_ask(question)
            elif text.startswith("/kalshi"):
                summary = get_portfolio_summary()
                send_telegram(format_portfolio_snapshot(summary))
            elif text.startswith("/help") or text.startswith("/start"):
                send_telegram(HELP_TEXT)
            elif text and not text.startswith("/"):
                # Free text that looks like a bet question → analyze it
                if "?" in text or text.lower().startswith(("will ", "is ", "does ", "can ", "who ")):
                    _handle_ask(text)
    except Exception as e:
        log.warning(f"Telegram poll error: {e}")


# ─── DAY-TRADE HELPERS ────────────────────────────────────────────────────────

def _utc_day(ts: str = None) -> str:
    """YYYY-MM-DD in UTC for a timestamp string, or today if None."""
    if ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).strftime("%Y-%m-%d")
        except Exception:
            return ""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _trades_opened_today() -> int:
    """
    How many positions we've opened during the current UTC day.
    Counts both still-open holdings and trades already closed today, so the
    daily cap survives restarts and same-day round trips.
    """
    today = _utc_day()
    count = 0
    try:
        from kalshi_portfolio import _load as _load_portfolio
        state = _load_portfolio()
        for h in state.get("holdings", []):
            if _utc_day(h.get("opened_at", "")) == today:
                count += 1
        for t in state.get("trade_history", []):
            if _utc_day(t.get("opened_at", "")) == today:
                count += 1
    except Exception as e:
        log.warning(f"Kalshi: could not count today's trades: {e}")
    return count


def _should_force_close(pos: dict) -> Optional[str]:
    """Return a close reason if this day trade has run out of time."""
    opened_at = pos.get("opened_at", "")
    if not opened_at:
        return None
    try:
        opened = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
    except Exception:
        return None

    now   = datetime.now(timezone.utc)
    hours = (now - opened).total_seconds() / 3600

    if hours >= MAX_HOLD_HOURS:
        return "max_hold"
    # Same-day rule: if we've crossed into the close window, or into a new day
    if now.strftime("%Y-%m-%d") != opened.strftime("%Y-%m-%d"):
        return "day_end"
    if now.hour >= DAY_CLOSE_UTC_HOUR:
        return "day_end"
    return None


# ─── SCAN CYCLE ───────────────────────────────────────────────────────────────

_last_alerted: dict = {}  # ticker → timestamp, suppress re-alerts within 4H

def _run_scan_cycle(force: bool = False):
    """Full market scan → signal detection → research → alerts → open positions."""
    log.info("=== KALSHI SCAN CYCLE START ===")
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")

    # 0. Day-trade budget check — stop once we've hit the daily target
    opened_today = _trades_opened_today()
    slots_left   = DAILY_TRADE_TARGET - opened_today
    if not force and slots_left <= 0:
        log.info(
            f"Kalshi scan: daily target reached ({opened_today}/{DAILY_TRADE_TARGET}) "
            "— no new entries until tomorrow UTC"
        )
        return
    log.info(f"Kalshi scan: {opened_today}/{DAILY_TRADE_TARGET} trades today, {slots_left} slot(s) left")

    # Don't open a day trade so late it can't run before the day-end close
    now_utc = datetime.now(timezone.utc)
    entry_cutoff = DAY_CLOSE_UTC_HOUR - ENTRY_CUTOFF_BUFFER_H
    if not force and now_utc.hour >= entry_cutoff:
        log.info(
            f"Kalshi scan: past {entry_cutoff:.0f}:00 UTC entry cutoff "
            f"(day closes {DAY_CLOSE_UTC_HOUR}:00) — no new entries"
        )
        return

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
        if force or NOTIFY_NO_SIGNALS:
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

    # 7. Take the highest-confidence verdicts, up to the remaining daily slots
    verdicts.sort(key=lambda v: -v.get("confidence", 0))
    if not force:
        verdicts = verdicts[:max(0, slots_left)]

    for verdict in verdicts:
        ticker = verdict["ticker"]
        log.info(f"Kalshi: {ticker} → {verdict['verdict']} {verdict['confidence']}/100")

        # Telegram signal — suppressed in silent mode, but ALWAYS sent when the
        # user explicitly asked for a scan (force=True via /kalshi_scan).
        if force or not SILENT:
            send_signal(verdict, margin=DEFAULT_MARGIN)

        # Log to postmortem — ALWAYS, this is how Golem learns
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

    if not MONITOR_POSITIONS:
        return

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

    # Day-trade rule: force-close anything that's run past its same-day window
    for pos in positions:
        ticker = pos["ticker"]
        reason = _should_force_close(pos)
        if not reason:
            continue
        price = prices_by_ticker.get(ticker)
        if not price:
            continue
        trade = close_position(ticker, price, reason=reason)
        if trade:
            if not SILENT:
                send_exit(trade)
            log_outcome(ticker, trade)
            log.info(
                f"Kalshi monitor: day-trade close {ticker} ({reason}) @ {price:.4f} "
                f"| net ${trade.get('net_pnl', 0):+.2f}"
            )

    # Re-read after any forced closes so we don't double-process
    positions = get_portfolio_summary().get("positions", [])
    if not positions:
        return

    # Check exits
    exits = update_prices(prices_by_ticker)
    for exit_event in exits:
        ticker     = exit_event["ticker"]
        reason     = exit_event["reason"]
        exit_price = exit_event["exit_price"]

        trade = close_position(ticker, exit_price, reason=reason)
        if trade:
            if not SILENT:
                send_exit(trade)
            log_outcome(ticker, trade)   # always — this is the learning signal
            log.info(
                f"Kalshi monitor: closed {ticker} via {reason} @ {exit_price:.4f} "
                f"| net ${trade.get('net_pnl', 0):+.2f}"
            )

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
            if not SILENT:
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
        except Exception as e:
            log.error(f"Kalshi monitor loop error: {e}", exc_info=True)
        for _ in range(MONITOR_INTERVAL_SEC):
            if stop_event.is_set():
                break
            time.sleep(1)


def build_weekly_report(days: float = 7.0) -> str:
    """Performance report over the trailing N days of closed trades."""
    from kalshi_portfolio import _load as _load_portfolio

    state   = _load_portfolio()
    history = state.get("trade_history", [])
    cutoff  = datetime.now(timezone.utc).timestamp() - days * 86400

    recent = []
    for t in history:
        try:
            closed = datetime.fromisoformat(
                (t.get("closed_at") or "").replace("Z", "+00:00")
            ).timestamp()
        except Exception:
            continue
        if closed >= cutoff:
            recent.append(t)

    starting = state.get("starting_cash", 500.0)
    summary  = get_portfolio_summary()
    total_val = summary["total_value"]
    all_time_pct = (total_val / starting - 1) * 100 if starting else 0.0

    if not recent:
        return (
            f"📊 *KALSHI — {days:.0f} Day Report*\n\n"
            "No trades closed in this period.\n\n"
            f"Account value: *${total_val:.2f}* (started ${starting:.2f})\n"
            f"All-time: *{all_time_pct:+.2f}%*"
        )

    wins    = [t for t in recent if t.get("net_pnl", 0) > 0]
    losses  = [t for t in recent if t.get("net_pnl", 0) <= 0]
    net     = sum(t.get("net_pnl", 0) for t in recent)
    staked  = sum(t.get("margin", 0) for t in recent)
    win_rate = len(wins) / len(recent) * 100
    roi      = (net / staked * 100) if staked else 0.0

    avg_win  = (sum(t["net_pnl"] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t["net_pnl"] for t in losses) / len(losses)) if losses else 0.0
    best     = max(recent, key=lambda t: t.get("net_pnl", 0))
    worst    = min(recent, key=lambda t: t.get("net_pnl", 0))
    avg_hold = sum(t.get("held_hours", 0) for t in recent) / len(recent)

    # Profit factor — gross wins / gross losses
    gross_w = sum(t["net_pnl"] for t in wins)
    gross_l = abs(sum(t["net_pnl"] for t in losses))
    pf = (gross_w / gross_l) if gross_l > 0 else float("inf")
    pf_str = "∞" if pf == float("inf") else f"{pf:.2f}"

    # Per-day breakdown
    by_day: dict = {}
    for t in recent:
        d = _utc_day(t.get("closed_at", ""))
        by_day.setdefault(d, []).append(t)

    emoji = "📈" if net > 0 else "📉"
    lines = [
        f"{emoji} *KALSHI — {days:.0f} DAY REPORT*\n",
        f"*Return on money staked: {roi:+.2f}%*",
        f"Net P&L: *${net:+.2f}* across {len(recent)} closed trades",
        "",
        f"Win rate: *{win_rate:.0f}%* ({len(wins)}W / {len(losses)}L)",
        f"Profit factor: {pf_str}  _(above 1.0 = profitable)_",
        f"Avg win: ${avg_win:+.2f}  |  Avg loss: ${avg_loss:+.2f}",
        f"Avg hold: {avg_hold:.1f}h",
        "",
        f"Best:  {best['ticker']} ${best.get('net_pnl',0):+.2f}",
        f"Worst: {worst['ticker']} ${worst.get('net_pnl',0):+.2f}",
        "",
        "*Daily breakdown:*",
    ]
    for d in sorted(by_day):
        day_trades = by_day[d]
        day_net = sum(t.get("net_pnl", 0) for t in day_trades)
        day_w   = sum(1 for t in day_trades if t.get("net_pnl", 0) > 0)
        mark    = "🟢" if day_net > 0 else "🔴"
        lines.append(f"{mark} {d}: {len(day_trades)} trades, {day_w}W — ${day_net:+.2f}")

    lines += [
        "",
        f"*Account: ${total_val:.2f}* (started ${starting:.2f})",
        f"All-time: *{all_time_pct:+.2f}%*",
        "",
        "_Paper trading. Run /kalshi_stats for calibration detail._",
    ]
    return "\n".join(lines)


def _report_due() -> bool:
    """True if REPORT_INTERVAL_DAYS have passed since the last report."""
    try:
        from kalshi_portfolio import _redis_get, _redis_set
        state = _redis_get(_REPORT_STATE_KEY) or {}
        last  = state.get("last_report_ts", 0)
        now   = time.time()
        if not last:
            # First run — anchor now so the first report lands a full period out
            _redis_set(_REPORT_STATE_KEY, {"last_report_ts": now})
            return False
        if (now - last) >= REPORT_INTERVAL_DAYS * 86400:
            _redis_set(_REPORT_STATE_KEY, {"last_report_ts": now})
            return True
    except Exception as e:
        log.warning(f"Kalshi: report scheduler error: {e}")
    return False


def run_report_loop(stop_event: Event):
    """Fire the periodic performance report."""
    while not stop_event.is_set():
        try:
            if WEEKLY_REPORT_ENABLED and _report_due():
                log.info("Kalshi: sending scheduled performance report")
                send_telegram(build_weekly_report(REPORT_INTERVAL_DAYS))
        except Exception as e:
            log.error(f"Kalshi report loop error: {e}", exc_info=True)
        for _ in range(1800):   # check every 30 min
            if stop_event.is_set():
                break
            time.sleep(1)


def _warm_event_market_cache():
    """
    Pre-load the open event market list so the first /ask doesn't wait ~30s
    paging through all of Kalshi. Runs once in the background at startup.
    """
    try:
        from kalshi_events import fetch_all_open_markets
        markets = fetch_all_open_markets()
        log.info(f"Kalshi: event market cache warmed — {len(markets)} open markets ready for /ask")
    except Exception as e:
        log.warning(f"Kalshi: event cache warm failed (first /ask will be slower): {e}")


def run_command_loop(stop_event: Event):
    """Dedicated fast poller so /ask feels responsive."""
    while not stop_event.is_set():
        try:
            _poll_telegram_commands()
        except Exception as e:
            log.error(f"Kalshi command loop error: {e}", exc_info=True)
        for _ in range(5):
            if stop_event.is_set():
                break
            time.sleep(1)


def main():
    log.info("=" * 60)
    log.info("KALSHI GOLEM — starting up")
    mode = ("SILENT AUTO-TRADE" if (AUTO_SCAN and SILENT)
            else "AUTO-SCAN + ALERTS" if AUTO_SCAN else "ON-DEMAND ONLY")
    log.info(f"Mode: {mode}")
    log.info(f"Scan interval: {SCAN_INTERVAL_SEC}s | Monitor: {MONITOR_INTERVAL_SEC}s")
    from kalshi_portfolio import MAX_OPEN_POSITIONS as _maxpos
    _maxpos_str = "unlimited" if _maxpos >= 999 else str(_maxpos)
    log.info(
        f"Default margin: ${DEFAULT_MARGIN:.0f} | Max positions: {_maxpos_str} | "
        f"Day-trade target: {DAILY_TRADE_TARGET}/day"
    )
    log.info("=" * 60)

    # Send startup message
    if AUTO_SCAN and SILENT:
        send_telegram(
            "🤖 *KALSHI Golem* — _silent day-trading mode_\n\n"
            f"Taking up to *{DAILY_TRADE_TARGET} bets per day* at ${DEFAULT_MARGIN:.0f} each, "
            f"all closed out same day. Running quietly — no trade alerts.\n\n"
            f"📊 *Your report lands in {REPORT_INTERVAL_DAYS:.0f} days* with the full "
            "win/loss percentage.\n\n"
            "Check anytime:\n"
            "`/report` — performance summary\n"
            "`/kalshi` — open positions & P&L\n"
            "`/kalshi_stats` — calibration detail\n\n"
            "Or ask me about any bet:\n"
            "`Will Bitcoin be above $120,000 this week?`"
        )
    elif AUTO_SCAN:
        send_telegram(
            "🚀 *KALSHI Golem* started\n"
            f"Auto-scanning crypto perps every {SCAN_INTERVAL_SEC//60} min.\n"
            f"Paper trading with ${DEFAULT_MARGIN:.0f}/trade.\n\n"
            "Ask me about any Kalshi bet —\n"
            "`Will Bitcoin be above $120,000 this week?`\n\n"
            "Commands: /ask /kalshi /kalshi_stats /kalshi_scan /help"
        )
    else:
        send_telegram(
            "🤖 *KALSHI Golem* ready — _on-demand mode_\n\n"
            "Not trading. I'll stay quiet until you ask me something.\n\n"
            "Just send me any bet question:\n"
            "`Will Bitcoin be above $120,000 this week?`"
        )

    stop_event = Event()

    monitor_thread = Thread(target=run_monitor_loop, args=(stop_event,), daemon=True, name="kalshi-monitor")
    command_thread = Thread(target=run_command_loop, args=(stop_event,), daemon=True, name="kalshi-commands")

    monitor_thread.start()
    command_thread.start()

    scan_thread = None
    if AUTO_SCAN:
        scan_thread = Thread(target=run_scan_loop, args=(stop_event,), daemon=True, name="kalshi-scan")
        scan_thread.start()
        if SILENT:
            log.info("Auto-scan ENABLED, SILENT — trading + learning, no Telegram alerts")
        else:
            log.info("Auto-scan ENABLED — will alert on new signals")
    else:
        log.info("Auto-scan DISABLED — on-demand only. Set KALSHI_AUTO_SCAN=true to enable.")

    report_thread = Thread(target=run_report_loop, args=(stop_event,), daemon=True, name="kalshi-report")
    report_thread.start()
    if WEEKLY_REPORT_ENABLED:
        log.info(f"Performance report scheduled every {REPORT_INTERVAL_DAYS} days")

    # Warm the event-market cache in the background so /ask is fast immediately
    Thread(target=_warm_event_market_cache, daemon=True, name="kalshi-cache-warm").start()

    log.info("All loops running (scan / monitor / commands / report). Ctrl+C to stop.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Stopping Kalshi tracker...")
        stop_event.set()
        if scan_thread:
            scan_thread.join(timeout=10)
        monitor_thread.join(timeout=10)
        command_thread.join(timeout=10)
        log.info("Kalshi tracker stopped.")


if __name__ == "__main__":
    main()
