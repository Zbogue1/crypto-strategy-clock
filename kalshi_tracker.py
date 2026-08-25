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
# Startup banners are noise — Railway restarts constantly.
ANNOUNCE_START        = os.getenv("KALSHI_ANNOUNCE_START", "false").lower() == "true"

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

# ─── CONCENTRATION GUARD ──────────────────────────────────────────────────────
# Every Kalshi perp is a crypto asset, so they all move with Bitcoin. Holding
# four longs is one leveraged bet on crypto direction, not four independent
# bets — and a single market-wide drop closes every stop at once. It also
# inflates the apparent sample size in calibration: 20 correlated "trades" is
# really about 5 independent observations.
MAX_SAME_DIRECTION = int(os.getenv("KALSHI_MAX_SAME_DIRECTION", "4"))
# Each position beyond the cap must clear a higher confidence bar to be worth
# adding to an existing bet. Set 0 to make the cap absolute.
CONCENTRATION_CONF_STEP = int(os.getenv("KALSHI_CONCENTRATION_CONF_STEP", "10"))
CONCENTRATION_CONF_MAX  = int(os.getenv("KALSHI_CONCENTRATION_CONF_MAX", "90"))


def _filter_by_concentration(verdicts: list, positions: list) -> tuple[list, list]:
    """
    Cap same-direction exposure. Verdicts are taken highest-confidence first;
    each one past the cap needs progressively more confidence to justify
    stacking onto a bet we already hold.

    Returns (approved, blocked_with_reasons).
    """
    longs  = sum(1 for p in positions if p.get("direction") == "UP")
    shorts = sum(1 for p in positions if p.get("direction") == "DOWN")

    approved, blocked = [], []

    for v in sorted(verdicts, key=lambda x: -x.get("confidence", 0)):
        direction = v["verdict"]
        held = longs if direction == "UP" else shorts

        if held < MAX_SAME_DIRECTION:
            approved.append(v)
            if direction == "UP":
                longs += 1
            else:
                shorts += 1
            continue

        # Past the cap — require escalating conviction
        if CONCENTRATION_CONF_STEP <= 0:
            blocked.append((v, f"already hold {held} {direction} (cap {MAX_SAME_DIRECTION})"))
            continue

        over    = held - MAX_SAME_DIRECTION + 1
        needed  = min(CONCENTRATION_CONF_MAX,
                      MIN_STACK_CONFIDENCE + (over - 1) * CONCENTRATION_CONF_STEP)
        if v.get("confidence", 0) >= needed:
            approved.append(v)
            if direction == "UP":
                longs += 1
            else:
                shorts += 1
        else:
            blocked.append((
                v,
                f"already hold {held} {direction}; needs {needed}+ confidence "
                f"to stack, has {v.get('confidence', 0)}"
            ))

    return approved, blocked


# Base bar for the first position past the cap
MIN_STACK_CONFIDENCE = int(os.getenv("KALSHI_STACK_MIN_CONF", "75"))


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
    "`/trades` — full bet-by-bet audit ledger\n"
    "`/report` — performance report (add days: `/report 30`)\n"
    "`/kalshi` — portfolio snapshot\n"
    "`/kalshi_stats` — track record & calibration\n"
    "`/kalshi_scan` — scan perps on demand (one-off)\n"
    "`/health` — storage diagnostics (Redis + record counts)\n"
    "`/archives` — list/restore archived portfolios\n"
    "`/reset` — start a fresh bank (archives first)\n"
    "`/help` — this message"
)

_last_update_id = 0


def _handle_deposit(text: str):
    """
    Add buying power without touching the track record.

        /deposit 10000
    """
    from kalshi_portfolio import deposit

    parts = text.split()
    if len(parts) < 2:
        s = get_portfolio_summary()
        send_telegram(
            "💵 *Add buying power*\n\n"
            "`/deposit 10000`\n\n"
            f"Current bank: ${s['starting_cash']:.2f} basis, "
            f"${s['cash']:.2f} cash\n"
            f"At ${DEFAULT_MARGIN:.0f}/trade that's "
            f"{int(s['cash'] // DEFAULT_MARGIN)} more position(s).\n\n"
            "_Unlike `/reset`, this keeps every trade and your W/L record. "
            "Percentage returns stay honest because the basis rises too._"
        )
        return

    try:
        target = float(parts[1])
    except ValueError:
        send_telegram("⚠️ Give a number, e.g. `/deposit 10000`")
        return

    before = get_portfolio_summary()
    result = deposit(target)
    if not result:
        send_telegram(
            f"ℹ️ Basis is already ${before['starting_cash']:.2f} — "
            f"nothing to add. Use a larger figure to top up further."
        )
        return

    after = get_portfolio_summary()
    send_telegram(
        "💵 *Deposit complete*\n\n"
        f"Bank: ${before['starting_cash']:.2f} → *${after['starting_cash']:.2f}*\n"
        f"Cash: ${before['cash']:.2f} → *${after['cash']:.2f}*\n"
        f"Capacity: {int(after['cash'] // DEFAULT_MARGIN)} concurrent positions "
        f"at ${DEFAULT_MARGIN:.0f}/trade\n\n"
        f"✅ Record preserved: *{after['winning_trades']}W / "
        f"{after['losing_trades']}L* across {after['total_trades']} trades"
    )


def _handle_archives(text: str):
    """List archived portfolios, or restore one: /archives restore <key>"""
    from kalshi_portfolio import list_archives, restore_archive

    parts = text.split()

    if len(parts) >= 3 and parts[1].lower() == "restore":
        key = parts[2]
        data = restore_archive(key)
        if not data:
            send_telegram(f"⚠️ Archive `{key}` not found. Send `/archives` to list them.")
            return
        send_telegram(
            "♻️ *Portfolio restored*\n\n"
            f"From: `{key}`\n"
            f"Trades: {len(data.get('trade_history', []))}\n"
            f"Record: {data.get('winning_trades',0)}W / {data.get('losing_trades',0)}L\n"
            f"Open positions: {len(data.get('holdings', []))}\n\n"
            "_Your previous state was archived first — this is reversible._"
        )
        return

    archives = list_archives()
    if not archives:
        send_telegram(
            "📦 *No archives*\n\nNothing has been archived yet. "
            "Archives are created automatically before any reset or restore."
        )
        return

    lines = [f"📦 *PORTFOLIO ARCHIVES ({len(archives)})*\n"]
    for a in archives[:10]:
        when = (a.get("archived_at", "") or "")[:16].replace("T", " ")
        note = f" — _{a['note']}_" if a.get("note") else ""
        lines.append(
            f"\n`{a['key']}`\n"
            f"  {when} UTC{note}\n"
            f"  {a.get('trades',0)} trades | "
            f"{a.get('winning',0)}W / {a.get('losing',0)}L | "
            f"{a.get('holdings',0)} open"
        )
    lines.append("\n\n_Restore with:_\n`/archives restore <key>`")
    send_telegram("\n".join(lines))


def _handle_reset(text: str):
    """
    Wipe the paper portfolio and postmortem, restarting with a fresh bank.
    Requires explicit confirmation: `/reset confirm`
    """
    from kalshi_portfolio  import reset_portfolio
    from kalshi_postmortem import reset_postmortem

    parts = text.split()
    new_cash = float(os.getenv("KALSHI_STARTING_CASH", "500.0"))

    # Optional override: /reset confirm 10000
    if len(parts) > 2:
        try:
            new_cash = float(parts[2])
        except ValueError:
            pass

    if len(parts) < 2 or parts[1].lower() != "confirm":
        summary = get_portfolio_summary()
        send_telegram(
            "⚠️ *Reset paper portfolio?*\n\n"
            f"This clears:\n"
            f"• {len(summary.get('positions', []))} open position(s)\n"
            f"• {summary.get('winning_trades', 0) + summary.get('losing_trades', 0)} closed trade(s)\n"
            f"• Postmortem/calibration history\n\n"
            f"New bank would be *${new_cash:,.2f}*\n\n"
            "✅ Your current data is *archived first* and can be brought back "
            "with `/archives restore <key>`.\n\n"
            "To proceed, send:\n"
            "`/reset confirm`\n\n"
            "Or set a custom amount:\n"
            "`/reset confirm 10000`"
        )
        return

    try:
        fresh = reset_portfolio(new_cash)
        reset_postmortem()
        _research_rejects.clear()
        _last_alerted.clear()
        send_telegram(
            "♻️ *Portfolio reset complete*\n\n"
            f"Fresh paper bank: *${fresh['starting_cash']:,.2f}*\n"
            f"At ${DEFAULT_MARGIN:.0f}/trade that's "
            f"{int(fresh['starting_cash'] // DEFAULT_MARGIN)} concurrent positions.\n\n"
            "All positions closed, history cleared. Starting clean."
        )
        log.warning(f"Kalshi: portfolio reset via Telegram — new bank ${new_cash:,.2f}")
    except Exception as e:
        log.error(f"Kalshi reset failed: {e}", exc_info=True)
        send_telegram(f"⚠️ Reset failed: {e}")


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

            # Screenshot sent from the phone. The bot it was sent TO is the
            # routing decision — Kalshi reads images as event/odds context,
            # not as memecoin chatter.
            try:
                import vision
                if vision.has_image(msg):
                    send_telegram("Reading screenshot...", parse_mode=None)
                    res = vision.process_screenshot(msg, "kalshi", TELEGRAM_TOKEN)
                    if res:
                        send_telegram(res["text"], parse_mode=None)
                    else:
                        send_telegram("Couldn't download that image.",
                                      parse_mode=None)
                    continue
            except Exception as e:
                log.error(f"Kalshi: screenshot handling failed: {e}")
            if text.startswith("/trades") or text.startswith("/ledger"):
                parts = text.split()
                lim = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 20
                off = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                # Plain text: exit reasons like "take_profit"/"stop_loss" and
                # ticker names contain underscores, which Markdown treats as
                # italic markers. An odd count makes Telegram reject the whole
                # message — which is why the ledger silently never arrived.
                send_telegram(build_trade_ledger(limit=lim, offset=off),
                              parse_mode=None)
            elif text.startswith("/health"):
                send_telegram(build_health_report(), parse_mode=None)
            elif text.startswith("/reconcile"):
                from reconcile import reconcile_all, format_report
                send_telegram(format_report(reconcile_all(), html=False),
                              parse_mode=None)
            elif text.startswith("/inbox"):
                from fomo_inbox import build_report
                send_telegram(build_report(), parse_mode=None)
            elif text.startswith("/events"):
                send_telegram(build_event_book(), parse_mode=None)
            elif text.startswith("/event_scan"):
                send_telegram("🔍 Scanning event markets — this takes a minute...")
                Thread(target=lambda: _run_event_scan_cycle(force=True),
                       daemon=True, name="kalshi-event-scan-manual").start()
            elif text.startswith("/archives"):
                _handle_archives(text)
            elif text.startswith("/deposit"):
                _handle_deposit(text)
            elif text.startswith("/reset"):
                _handle_reset(text)
            elif text.startswith("/report"):
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


# ─── RESEARCH CACHE ───────────────────────────────────────────────────────────
# The signal engine surfaces the same borderline setups every cycle (NEAR -43,
# SUI -40, LINK +40...). Re-running the AI on an unchanged setup costs money and
# always returns the same "not actionable" answer. Cache those rejections and
# only re-analyze when the score actually moves or the cache expires.
RESEARCH_CACHE_HOURS = float(os.getenv("KALSHI_RESEARCH_CACHE_HOURS", "2"))
RESEARCH_SCORE_DELTA = int(os.getenv("KALSHI_RESEARCH_DELTA", "8"))

# ticker → {"score": int, "ts": float}
_research_rejects: dict = {}


def _filter_cached_rejects(viable: list[dict]) -> tuple[list[dict], int]:
    """
    Drop candidates we recently analyzed and rejected, whose score hasn't
    meaningfully moved. Returns (candidates_to_analyze, n_skipped).
    """
    now = time.time()
    keep, skipped = [], 0

    for v in viable:
        ticker = v["ticker"]
        score  = v["composite_score"]
        cached = _research_rejects.get(ticker)

        if cached:
            age_h = (now - cached["ts"]) / 3600
            moved = abs(score - cached["score"])
            if age_h < RESEARCH_CACHE_HOURS and moved < RESEARCH_SCORE_DELTA:
                skipped += 1
                log.debug(
                    f"Kalshi research cache: skip {ticker} "
                    f"(score {score:+d}, moved {moved}, age {age_h:.1f}h)"
                )
                continue
        keep.append(v)

    return keep, skipped


def _cache_rejects(analyzed: list[dict], actionable_tickers: set):
    """Remember which analyzed candidates came back with nothing to trade."""
    now = time.time()
    for v in analyzed:
        ticker = v["ticker"]
        if ticker in actionable_tickers:
            # Actionable — don't cache, we want it re-evaluated freely later
            _research_rejects.pop(ticker, None)
        else:
            _research_rejects[ticker] = {"score": v["composite_score"], "ts": now}


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

    # 4b. Skip setups we already analyzed and rejected while unchanged
    if not force:
        viable, skipped = _filter_cached_rejects(viable)
        if skipped:
            log.info(
                f"Kalshi scan: skipped {skipped} unchanged setup(s) from research cache "
                f"— saved {skipped} AI call(s)"
            )
        if not viable:
            log.info("Kalshi scan: all candidates already analyzed and unchanged")
            return

    # 5. Load postmortem context
    pm_summaries = get_all_summaries()

    # 6. Research agent analysis — now portfolio-aware, so each call knows what
    #    we already hold AND what was approved earlier in this same cycle.
    verdicts = scan_all_viable(viable, snapshots_by_ticker, pm_summaries,
                               portfolio=summary)

    # 6b. Cache the ones that produced nothing tradeable
    _cache_rejects(viable, {v["ticker"] for v in verdicts})

    # 6c. Hard concentration guard — the AI can be persuaded, this cannot.
    verdicts, blocked = _filter_by_concentration(
        verdicts, summary.get("positions", [])
    )
    for v, why in blocked:
        log.info(
            f"Kalshi scan: BLOCKED {v['ticker']} {v['verdict']} "
            f"({v.get('confidence')}/100) — {why}"
        )

    # 7. Take the highest-confidence verdicts, up to the remaining daily slots
    verdicts.sort(key=lambda v: -v.get("confidence", 0))
    if not force:
        verdicts = verdicts[:max(0, slots_left)]

    # Assign conviction groups: positions opened by THIS scan in the SAME
    # direction are one logical bet. They stay separate trades for P&L, but
    # calibration counts the group once — four correlated longs is one
    # directional call, not four independent observations.
    _scan_stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    _dir_counts: dict = {}
    for v in verdicts:
        _dir_counts[v["verdict"]] = _dir_counts.get(v["verdict"], 0) + 1

    for verdict in verdicts:
        ticker = verdict["ticker"]
        _grp_id   = f"{_scan_stamp}_{verdict['verdict']}"
        _grp_size = _dir_counts.get(verdict["verdict"], 1)
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
            group_id=    _grp_id,
            group_size=  _grp_size,
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

# ─── EVENT MARKET CYCLE ───────────────────────────────────────────────────────
#
# Runs alongside the perp scan, NOT instead of it. Separate daily budget,
# separate book, separate monitor — so adding sports and weather bets does not
# reduce the number of crypto perp trades, which was the explicit requirement.

EVENT_TRADING       = os.getenv("KALSHI_EVENT_TRADING", "true").lower() == "true"
EVENT_DAILY_TARGET  = int(os.getenv("KALSHI_EVENT_DAILY_TRADES", "4"))
EVENT_SCAN_INTERVAL = int(os.getenv("KALSHI_EVENT_SCAN_SEC", "3600"))
EVENT_SETTLE_INTERVAL = int(os.getenv("KALSHI_EVENT_SETTLE_SEC", "900"))


def _run_event_scan_cycle(force: bool = False):
    """Screen event markets → analyst → edge gate → paper bet."""
    if not EVENT_TRADING and not force:
        return

    from kalshi_event_scanner import screen_markets
    from kalshi_event_trader import evaluate, check_domain_limit, format_bet_alert
    from kalshi_event_portfolio import (
        open_bet, get_summary as event_summary, bets_opened_today,
    )
    from kalshi_analyst import analyze_question

    log.info("=== KALSHI EVENT SCAN START ===")

    opened = bets_opened_today()
    slots  = EVENT_DAILY_TARGET - opened
    if not force and slots <= 0:
        log.info(f"Kalshi events: daily target reached ({opened}/{EVENT_DAILY_TARGET})")
        return

    res        = screen_markets()
    candidates = res.get("candidates", [])
    stats      = res.get("stats", {})
    log.info(f"Kalshi events: screened {stats.get('total', 0)} markets → "
             f"{len(candidates)} candidate(s), {slots} slot(s) left")

    if not candidates:
        if force:
            from kalshi_event_scanner import format_scan_summary
            send_telegram(format_scan_summary(res))
        return

    summary   = event_summary()
    held      = {p["ticker"] for p in summary["positions"]}
    open_pos  = summary["positions"]
    stamp     = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    taken     = 0

    for m in candidates:
        if taken >= slots and not force:
            break
        ticker = m["ticker"]
        if ticker in held:
            continue

        # Correlation cap BEFORE spending an AI call — six same-evening games
        # are one bet on "favourites hold up tonight".
        ok, why = check_domain_limit(m.get("domain", ""), open_pos)
        if not ok:
            log.info(f"Kalshi events: skip {ticker} — {why}")
            continue

        try:
            analysis = analyze_question(m.get("title", ""), ticker=ticker)
        except Exception as e:
            log.error(f"Kalshi events: analysis failed for {ticker}: {e}")
            continue
        if not analysis or analysis.get("error"):
            log.warning(f"Kalshi events: no analysis for {ticker} — "
                        f"{(analysis or {}).get('error', 'empty result')}")
            continue

        decision = evaluate(m, analysis)
        if not decision.get("trade"):
            log.info(f"Kalshi events: pass {ticker} — {decision.get('reason')}")
            continue

        sizing = decision["sizing"]
        pos = open_bet(
            ticker=      ticker,
            title=       m.get("title", ticker),
            side=        decision["side"],
            price_cents= decision["implied"],
            contracts=   sizing["contracts"],
            cost_per=    sizing["cost_per"],
            domain=      m.get("domain", ""),
            close_time=  m.get("close_time", ""),
            confidence=  decision["confidence"],
            edge=        decision["edge"],
            our_prob=    decision["our_prob"],
            reasoning=   analysis.get("reasoning", ""),
            group_id=    f"{stamp}_event",
            group_size=  1,
        )
        if not pos:
            continue

        taken += 1
        open_pos.append(pos)
        if force or not SILENT:
            send_telegram(format_bet_alert(m, decision, analysis))
        time.sleep(1.5)

    log.info(f"=== KALSHI EVENT SCAN END — {taken} bet(s) placed ===")


_stale_alerted: set = set()


def _alert_stale_events(stale: list):
    """
    Tell the user once per stuck position, not every cycle.

    Alerting every 15 minutes would train him to ignore it, which defeats the
    purpose — the whole failure mode here is a problem nobody notices.
    """
    for p in stale:
        t = p["ticker"]
        if t in _stale_alerted:
            continue
        _stale_alerted.add(t)
        log.warning(f"Kalshi events: {t} is {p['overdue_hours']:.0f}h past its "
                    f"close time and still unsettled — ${p['cost_basis']:.2f} tied up")
        send_telegram(
            "STUCK EVENT BET\n\n"
            f"{p['title'][:80]}\n{t}\n\n"
            f"Closed {p['overdue_hours']:.0f}h ago but Kalshi has not reported a "
            f"result, so it cannot be settled.\n"
            f"${p['cost_basis']:.2f} is tied up in it.\n\n"
            "Nothing was auto-closed — the outcome is unknown and guessing it "
            "would invent P&L. Check the market on Kalshi.",
            parse_mode=None,
        )


def _run_event_settle_cycle():
    """
    Close event positions whose markets have actually resolved.

    Deliberately does nothing else. There is no mark-to-market and no stop
    loss: the reason for moving to event markets was that they end with a real
    answer instead of a timer, so the only exit is settlement.
    """
    if not EVENT_TRADING:
        return

    from kalshi_events import get_market_settlement
    from kalshi_event_portfolio import (
        get_summary as event_summary, settle_bet, stale_positions,
    )

    positions = event_summary()["positions"]
    if not positions:
        return

    # Capital that should have been freed and wasn't. Settlement is the only
    # exit in this book, so a market that never reports a result would hold
    # money indefinitely with nothing saying so.
    _alert_stale_events(stale_positions())

    for pos in positions:
        ticker = pos["ticker"]
        try:
            s = get_market_settlement(ticker)
        except Exception as e:
            log.error(f"Kalshi events: settlement check failed for {ticker}: {e}")
            continue
        if s is None:
            # Couldn't ask. NOT the same as "not resolved" — say so, so a
            # persistently unreachable ticker shows up in the logs instead of
            # looking like a market that never settles.
            log.warning(f"Kalshi events: no settlement data for {ticker} "
                        f"(API unreachable?) — will retry")
            continue
        if not s["settled"]:
            continue

        trade = settle_bet(ticker, s["result"])
        if trade and not SILENT:
            icon = "✅" if trade["won"] else "❌"
            send_telegram(
                f"{icon} *EVENT SETTLED* — {'WON' if trade['won'] else 'LOST'}\n\n"
                f"{trade['title'][:90]}\n`{ticker}`\n\n"
                f"Bet {trade['side']} at {trade['entry_cents']:.0f}c · "
                f"resolved {s['result'].upper()}\n"
                f"P&L: *${trade['net_pnl']:+.2f}* ({trade['return_pct']:+.0f}%)"
            )


def run_event_scan_loop(stop_event: Event):
    while not stop_event.is_set():
        try:
            _run_event_scan_cycle()
        except Exception as e:
            log.error(f"Kalshi event scan loop error: {e}", exc_info=True)
        for _ in range(EVENT_SCAN_INTERVAL):
            if stop_event.is_set():
                break
            time.sleep(1)


def run_event_settle_loop(stop_event: Event):
    while not stop_event.is_set():
        try:
            _run_event_settle_cycle()
        except Exception as e:
            log.error(f"Kalshi event settle loop error: {e}", exc_info=True)
        for _ in range(EVENT_SETTLE_INTERVAL):
            if stop_event.is_set():
                break
            time.sleep(1)


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


def build_event_book(limit: int = 15) -> str:
    """
    The event book — open bets and settled results.

    Plain text on purpose: Kalshi tickers are full of underscores and dashes
    (KXNBAGAME-25AUG24-LAL), which Markdown reads as formatting and Telegram
    then rejects outright. That is how the perp ledger silently failed to send.
    """
    try:
        from kalshi_event_portfolio import get_summary
    except Exception as e:
        return f"Event book unavailable: {e}"

    s = get_summary()
    L = [
        "KALSHI EVENT BOOK",
        "",
        f"Cash:        ${s['cash']:,.2f}",
        f"At risk:     ${s['at_risk']:,.2f}  ({s['n_positions']} open)",
        f"Book value:  ${s['total_value']:,.2f}   (basis ${s['starting_cash']:,.2f})",
        f"Realized:    ${s['realized_pnl']:+,.2f}",
    ]
    if s["total_trades"]:
        L.append(f"Record:      {s['wins']}W / {s['losses']}L "
                 f"({s['win_rate']:.0f}%) over {s['total_trades']} settled")
    else:
        L.append("Record:      no bets settled yet")

    if s["positions"]:
        L += ["", "OPEN BETS"]
        for p in s["positions"]:
            L.append(
                f"\n  {p['side']} {p['ticker']}\n"
                f"    {p['title'][:64]}\n"
                f"    {p['contracts']} contracts @ {p['entry_cents']:.0f}c  "
                f"= ${p['cost_basis']:.2f} risked to win ${p['max_gain']:.2f}\n"
                f"    our estimate {p['our_prob']:.0f}% vs market "
                f"{p['entry_cents']:.0f}% | edge {p['edge']:.1f}pts | "
                f"conf {p['confidence']}\n"
                f"    resolves {str(p.get('close_time',''))[:16].replace('T',' ')} UTC"
            )

    from kalshi_event_portfolio import _load
    hist = _load().get("trade_history", [])
    if hist:
        L += ["", f"SETTLED (last {min(limit, len(hist))})"]
        for t in reversed(hist[-limit:]):
            mark = "WON " if t.get("won") else "LOST"
            L.append(
                f"\n  {mark} {t['side']} {t['ticker']}  "
                f"-> resolved {str(t.get('result','?')).upper()}\n"
                f"    {t['title'][:64]}\n"
                f"    {t['contracts']} @ {t['entry_cents']:.0f}c  "
                f"P&L ${t['net_pnl']:+.2f} ({t['return_pct']:+.0f}%)"
            )
    return "\n".join(L)


def build_trade_ledger(limit: int = 20, offset: int = 0) -> str:
    """
    Full audit trail — every individual bet with the details needed to verify
    it was a real market position, not a hallucinated one.

    Shows: ticker, direction, leverage, exact entry/exit prices, timestamps,
    exit reason, P&L, and the confidence the AI assigned at entry.
    """
    from kalshi_portfolio import _load as _load_portfolio

    state   = _load_portfolio()
    history = list(state.get("trade_history", []))
    open_   = state.get("holdings", [])

    if not history and not open_:
        return (
            "📒 *KALSHI TRADE LEDGER*\n\n"
            "No trades recorded.\n\n"
            "If you expected history here, run `/health` — an unreachable "
            "Redis will show as an empty ledger while the data is still intact."
        )

    lines = [f"📒 *KALSHI TRADE LEDGER*\n"]

    # ── Open positions ────────────────────────────────────────────────────
    if open_:
        lines.append(f"*OPEN ({len(open_)})*")
        for h in open_:
            opened = (h.get("opened_at", "") or "")[:16].replace("T", " ")
            lines.append(
                f"\n🔵 `{h.get('ticker','?')}` {h.get('direction','?')} "
                f"{h.get('leverage',1)}x\n"
                f"   Entry `{h.get('entry_price',0):.4f}` @ {opened} UTC\n"
                f"   Margin ${h.get('margin',0):.0f} | notional ${h.get('notional',0):.0f}\n"
                f"   Stop `{h.get('stop_price',0):.4f}` | TP `{h.get('take_profit_price',0):.4f}`\n"
                f"   Conf {h.get('confidence','?')}/100"
            )
        lines.append("")

    # ── Closed trades, newest first ───────────────────────────────────────
    history.reverse()
    page = history[offset:offset + limit]

    if page:
        shown_to = offset + len(page)
        lines.append(f"*CLOSED ({offset+1}-{shown_to} of {len(history)})*")
        for i, t in enumerate(page, start=offset + 1):
            pnl    = t.get("net_pnl", 0)
            mark   = "✅" if pnl > 0 else "❌"
            opened = (t.get("opened_at", "") or "")[:16].replace("T", " ")
            closed = (t.get("closed_at", "") or "")[:16].replace("T", " ")
            reason = t.get("reason", "?")
            lines.append(
                f"\n{mark} *#{i}* `{t.get('ticker','?')}` {t.get('direction','?')} "
                f"{t.get('leverage',1)}x\n"
                f"   `{t.get('entry_price',0):.4f}` → `{t.get('exit_price',0):.4f}`  "
                f"(*${pnl:+.2f}*, {t.get('pnl_pct',0):+.1f}%)\n"
                f"   In  {opened} UTC\n"
                f"   Out {closed} UTC  ({t.get('held_hours',0):.1f}h, {reason})\n"
                f"   Margin ${t.get('margin',0):.0f} | conf {t.get('confidence','?')}/100"
            )

        if shown_to < len(history):
            lines.append(f"\n_Next page: `/trades {limit} {shown_to}`_")

    # ── Reconciliation — do the parts add up? ─────────────────────────────
    sum_pnl = sum(t.get("net_pnl", 0) for t in history)
    wins    = sum(1 for t in history if t.get("net_pnl", 0) > 0)
    losses  = len(history) - wins
    lines += [
        "",
        "*RECONCILIATION*",
        f"Ledger entries: {len(history)} closed, {len(open_)} open",
        f"Sum of ledger P&L: ${sum_pnl:+.2f}",
        f"Portfolio counter: {state.get('total_trades',0)} trades, "
        f"{state.get('winning_trades',0)}W / {state.get('losing_trades',0)}L",
        f"Ledger recount:    {len(history)} trades, {wins}W / {losses}L",
    ]
    if state.get("total_trades", 0) != len(history):
        lines.append("⚠️ Counter and ledger disagree — counter may be stale.")
    else:
        lines.append("✓ Counter matches ledger.")

    return "\n".join(lines)


def build_health_report() -> str:
    """Storage diagnostics — distinguishes a real reset from a Redis outage."""
    from kalshi_portfolio import redis_health

    h = redis_health()

    # Identify exactly which Railway project/service this bot is running in.
    # With similarly-named projects AND services, this removes the guesswork
    # about where to add variables.
    proj    = os.getenv("RAILWAY_PROJECT_NAME", "?")
    svc     = os.getenv("RAILWAY_SERVICE_NAME", "?")
    env_nm  = os.getenv("RAILWAY_ENVIRONMENT_NAME", "?")

    lines = [
        "🩺 *KALSHI STORAGE HEALTH*\n",
        "📍 *This bot is running in:*",
        f"   Project: `{proj}`",
        f"   Service: `{svc}`",
        f"   Env: `{env_nm}`",
        "   _Add variables to THIS project + service._\n",
    ]

    if not h["configured"]:
        lines.append("❌ *Redis not configured*")
        lines.append(f"   {h['error']}\n")
        lines.append("*Variable names I check:*")
        lines.append("  URL: " + ", ".join(f"`{n}`" for n in h["searched"]["url"][:3]))
        lines.append("  Token: " + ", ".join(f"`{n}`" for n in h["searched"]["token"][:3]))
        if h["found_any"]:
            lines.append("\n*Found in Railway:* " + ", ".join(f"`{n}`" for n in h["found_any"]))
            lines.append("_Partial config — one half is missing._")
        else:
            lines.append("\n*Found in Railway:* none of them")
            lines.append(
                "\nCheck the exact variable names on crypto-strategy-clock. "
                "Upstash labels them `UPSTASH_REDIS_REST_URL` and "
                "`UPSTASH_REDIS_REST_TOKEN` — both now accepted."
            )
        lines.append("\n⚠️ Until fixed, data lives only in the container and is LOST on every redeploy.")
        return "\n".join(lines)

    if not h["reachable"]:
        lines.append("❌ *Redis unreachable*")
        lines.append(f"   Error: {h.get('error')}")
        lines.append(
            "\n⚠️ The bot is falling back to local files, so history will look "
            "empty even though your data is probably still safe in Redis. "
            "Do NOT reset — fix the connection first."
        )
        return "\n".join(lines)

    lines.append(f"✅ *Redis reachable* via `{h.get('url_var','?')}`\n")
    for key, info in h["keys"].items():
        name = "Portfolio" if "portfolio" in key else "Postmortem"
        if not info.get("exists"):
            lines.append(f"*{name}* (`{key}`)\n   ⚠️ Key does not exist — never written, or deleted.")
            continue
        lines.append(f"*{name}* (`{key}`)")
        lines.append(f"   Size: {info['bytes']:,} bytes")
        if "trade_history" in info:
            lines.append(
                f"   Closed trades: {info['trade_history']} | Open: {info['holdings']}\n"
                f"   Counters: {info['total_trades']} total, "
                f"{info['winning']}W / {info['losing']}L\n"
                f"   Created: {info['created_at'][:16].replace('T',' ')} UTC"
            )
        else:
            lines.append(f"   Logged calls: {info['calls']}")
        lines.append("")

    lines.append("_Use `/trades` to inspect individual bets._")
    return "\n".join(lines)


def _group_trades(trades: list) -> list:
    """
    Collapse trades into conviction groups.

    A group is one researched directional call. Its sub-positions are separate
    trades for P&L, but the GROUP is the unit of calibration: if Golem said
    "crypto up" and backed it with four tickers, that's one call that was right
    or wrong — counting it four times would inflate the sample.

    Group wins if its NET P&L is positive, regardless of the sub-position split.
    Trades with no group_id (legacy or manual) are groups of one.
    """
    groups: dict = {}
    for t in trades:
        gid = t.get("group_id") or f"solo_{t.get('ticker','?')}_{t.get('closed_at','')}"
        g = groups.setdefault(gid, {
            "group_id":   gid,
            "direction":  t.get("direction", "?"),
            "subs":       [],
            "net_pnl":    0.0,
            "margin":     0.0,
            "confidence": t.get("confidence", 0),
            "opened_at":  t.get("opened_at", ""),
            "closed_at":  t.get("closed_at", ""),
        })
        g["subs"].append(t)
        g["net_pnl"] += t.get("net_pnl", 0) or 0
        g["margin"]  += t.get("margin", 0) or 0
        # group closes when its last sub closes
        if (t.get("closed_at") or "") > (g["closed_at"] or ""):
            g["closed_at"] = t.get("closed_at", "")

    out = []
    for g in groups.values():
        g["sub_wins"]   = sum(1 for s in g["subs"] if (s.get("net_pnl", 0) or 0) > 0)
        g["sub_losses"] = len(g["subs"]) - g["sub_wins"]
        g["won"]        = g["net_pnl"] > 0
        g["size"]       = len(g["subs"])
        out.append(g)
    return sorted(out, key=lambda x: x.get("closed_at", ""))


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

    # ── Conviction groups — the real calibration unit ────────────────────
    groups     = _group_trades(recent)
    g_wins     = [g for g in groups if g["won"]]
    g_losses   = [g for g in groups if not g["won"]]
    g_win_rate = len(g_wins) / len(groups) * 100 if groups else 0.0
    multi      = [g for g in groups if g["size"] > 1]

    emoji = "📈" if net > 0 else "📉"
    lines = [
        f"{emoji} *KALSHI — {days:.0f} DAY REPORT*\n",
        f"*Return on money staked: {roi:+.2f}%*",
        f"Net P&L: *${net:+.2f}*",
        "",
        "*━━ CALL ACCURACY ━━*",
        f"_One researched call = one observation, even when backed by several tickers._",
        f"Record: *{len(g_wins)}W / {len(g_losses)}L*  ({g_win_rate:.0f}%)",
        f"Researched calls: {len(groups)}",
    ]
    if multi:
        avg_sz = sum(g["size"] for g in multi) / len(multi)
        lines.append(
            f"Multi-position calls: {len(multi)} (avg {avg_sz:.1f} tickers each)"
        )
    lines += [
        "",
        "*━━ SUB-POSITIONS ━━*",
        f"_Every individual ticker traded._",
        f"Record: *{len(wins)}W / {len(losses)}L*  ({win_rate:.0f}%)",
        f"Positions: {len(recent)}",
        f"Profit factor: {pf_str}  _(above 1.0 = profitable)_",
        f"Avg win: ${avg_win:+.2f}  |  Avg loss: ${avg_loss:+.2f}",
        f"Avg hold: {avg_hold:.1f}h",
        "",
        f"Best:  {best['ticker']} ${best.get('net_pnl',0):+.2f}",
        f"Worst: {worst['ticker']} ${worst.get('net_pnl',0):+.2f}",
        "",
        "*Calls placed:*",
    ]
    for g in reversed(groups[-10:]):
        mark  = "✅" if g["won"] else "❌"
        when  = (g.get("closed_at", "") or "")[:10]
        if g["size"] > 1:
            lines.append(
                f"{mark} {when} {g['direction']} ×{g['size']} tickers "
                f"→ *${g['net_pnl']:+.2f}*  "
                f"_(subs: {g['sub_wins']}W/{g['sub_losses']}L)_"
            )
        else:
            s = g["subs"][0]
            lines.append(
                f"{mark} {when} {g['direction']} {s.get('ticker','?')} "
                f"→ *${g['net_pnl']:+.2f}*"
            )
    if len(groups) > 10:
        lines.append(f"_…and {len(groups)-10} earlier calls_")

    lines += [
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
    log.info(
        f"Running in: project='{os.getenv('RAILWAY_PROJECT_NAME','?')}' "
        f"service='{os.getenv('RAILWAY_SERVICE_NAME','?')}' "
        f"env='{os.getenv('RAILWAY_ENVIRONMENT_NAME','?')}'"
    )
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

    # ── Startup message rate limit ────────────────────────────────────────
    # Both services deploy from the same repo, so every push restarts this bot
    # and fires a startup message. During active development that's a message
    # every few minutes — which directly contradicts "silent mode". Only
    # announce if we haven't in the last STARTUP_MSG_COOLDOWN_H hours.
    # Also catches crash-loops, where the same message would repeat endlessly.
    _startup_cooldown_h = float(os.getenv("KALSHI_STARTUP_MSG_COOLDOWN_H", "12"))
    _announce = True
    try:
        from kalshi_portfolio import _redis_get, _redis_set
        _last = (_redis_get("kalshi_last_startup_msg") or {}).get("ts", 0)
        if _last and (time.time() - _last) < _startup_cooldown_h * 3600:
            _announce = False
            log.info(
                f"Startup message suppressed — last sent "
                f"{(time.time()-_last)/60:.0f} min ago (cooldown {_startup_cooldown_h}h)"
            )
        else:
            _redis_set("kalshi_last_startup_msg", {"ts": time.time()})
    except Exception as e:
        log.warning(f"Startup message cooldown check failed: {e}")

    # Send startup message
    if not _announce:
        pass
    # Same reasoning as Stock Golem: Railway restarts on every redeploy,
    # crash and failed health check, so a startup banner is noise that
    # teaches you to ignore the bot. Off unless explicitly enabled.
    elif not ANNOUNCE_START:
        log.info("Startup announcement suppressed "
                 "(set KALSHI_ANNOUNCE_START=true to enable)")
    elif AUTO_SCAN and SILENT:
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

    # Event markets run on their own threads with their own daily budget, so
    # sports/weather bets are ADDITIONAL to the perp trades rather than
    # competing for the same slots.
    event_threads = []
    if EVENT_TRADING:
        event_threads = [
            Thread(target=run_event_scan_loop, args=(stop_event,),
                   daemon=True, name="kalshi-event-scan"),
            Thread(target=run_event_settle_loop, args=(stop_event,),
                   daemon=True, name="kalshi-event-settle"),
        ]
        for t in event_threads:
            t.start()
        log.info(f"Event trading ENABLED — up to {EVENT_DAILY_TARGET} bet(s)/day, "
                 f"scan every {EVENT_SCAN_INTERVAL//60} min, "
                 f"settlement check every {EVENT_SETTLE_INTERVAL//60} min")
    else:
        log.info("Event trading DISABLED — set KALSHI_EVENT_TRADING=true to enable.")

    report_thread = Thread(target=run_report_loop, args=(stop_event,), daemon=True, name="kalshi-report")
    report_thread.start()
    if WEEKLY_REPORT_ENABLED:
        log.info(f"Performance report scheduled every {REPORT_INTERVAL_DAYS} days")

    # Warm the event-market cache in the background so /ask is fast immediately
    Thread(target=_warm_event_market_cache, daemon=True, name="kalshi-cache-warm").start()

    log.info("All loops running (scan / monitor / commands / report). Ctrl+C to stop.")
    try:
        from reconcile import check_self
    except Exception as e:
        log.warning(f"reconcile self-check unavailable: {e}")
        check_self = None

    try:
        while True:
            time.sleep(60)
            # Books check on a timer. Silent unless the account moved by an
            # amount the trade records can't explain — only then does it
            # message, and only once per distinct gap.
            if check_self:
                # Both books this service owns: the perp portfolio and the
                # event book. They have separate cash, so a gap in one would
                # not show up in the other.
                for _book in ("kalshi", "events"):
                    try:
                        check_self(_book,
                                   lambda m: send_telegram(m, parse_mode=None),
                                   html=False)
                    except Exception as e:
                        log.error(f"reconcile self-check ({_book}) error: {e}")
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
