#!/usr/bin/env python3
"""
stock_tracker.py — Stock Golem main loop.

Unlike Kalshi and FOMO, this one is time-bound. Equities have hours, and the
strategy is explicitly a morning strategy:

  07:00–11:00 ET   Ross's stated trading window
  10:30 ET         his own metrics review: "if I stop at 10 a.m., that would
                   have improved my performance for the last 30 days"
  16:00 ET         everything closed — no overnight holds, ever

So the loop does nothing for most of the day, scans hard for a few hours, and
force-closes before the bell. Positions are monitored far more often than they
are opened: entries happen every few minutes, but a 15¢ stop on a stock moving
30¢/minute needs checking constantly.

Threads:
  SCAN     — during the entry window only, every SCAN_INTERVAL seconds
  MONITOR  — whenever positions are open, every MONITOR_INTERVAL seconds
  COMMAND  — Telegram polling, always
"""

import logging
import os
import time
from datetime import datetime, time as dtime, timezone
from threading import Thread, Event
from typing import Optional

import requests

import stock_data       as sd
import stock_signals    as sig
import stock_portfolio  as pf
import stock_telegram   as tg
import stock_postmortem as pm
from stock_research import review_setup

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
log = logging.getLogger("stock_tracker")

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                       # pragma: no cover
    ET = timezone.utc
    log.warning("zoneinfo unavailable — falling back to UTC for market hours")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
SCAN_INTERVAL    = int(os.getenv("STOCK_SCAN_INTERVAL", "120"))     # 2 min
MONITOR_INTERVAL = int(os.getenv("STOCK_MONITOR_INTERVAL", "20"))   # 20 sec
COMMAND_INTERVAL = int(os.getenv("STOCK_COMMAND_INTERVAL", "5"))

ENTRY_START_ET   = os.getenv("STOCK_ENTRY_START", "07:00")
ENTRY_END_ET     = os.getenv("STOCK_ENTRY_END", "10:30")
FORCE_CLOSE_ET   = os.getenv("STOCK_FORCE_CLOSE", "15:50")

MAX_HOLD_MINUTES = float(os.getenv("STOCK_MAX_HOLD_MIN", "120"))
SILENT           = os.getenv("STOCK_SILENT", "false").lower() == "true"
AUTO_TRADE       = os.getenv("STOCK_AUTO_TRADE", "true").lower() == "true"

TELEGRAM_TOKEN = tg.TELEGRAM_TOKEN


def _parse_hhmm(s: str) -> dtime:
    try:
        h, m = s.split(":")
        return dtime(int(h), int(m))
    except Exception:
        return dtime(9, 30)


def now_et() -> datetime:
    return datetime.now(ET)


def in_entry_window() -> tuple:
    """(open, note) — is it inside the entry window?"""
    t = now_et()
    if t.weekday() >= 5:
        return False, "weekend"
    start, end = _parse_hhmm(ENTRY_START_ET), _parse_hhmm(ENTRY_END_ET)
    cur = t.time()
    if cur < start:
        return False, f"before {ENTRY_START_ET} ET"
    if cur > end:
        return False, f"past {ENTRY_END_ET} ET entry cutoff"
    return True, f"{t.strftime('%H:%M')} ET"


def past_force_close() -> bool:
    t = now_et()
    return t.weekday() < 5 and t.time() >= _parse_hhmm(FORCE_CLOSE_ET)


# ─── SCAN ─────────────────────────────────────────────────────────────────────

_last_entry: dict = {}      # symbol → epoch, avoid re-entering same name
REENTRY_COOLDOWN = 900


def run_scan(force: bool = False, announce: bool = False) -> list:
    open_now, note = in_entry_window()
    if not force and not open_now:
        log.info(f"Scan skipped — {note}")
        return []

    allowed, why = pf.can_trade()
    if not force and not allowed:
        log.info(f"Scan skipped — {why}")
        return []

    movers = sd.get_movers(top=50, min_pct=sig.MIN_PCT_CHANGE)
    if not movers:
        log.info("Scan: no gainers returned")
        if announce:
            tg.send(tg.format_scan([], 0, note))
        return []

    log.info(f"Scan: {len(movers)} gainer(s) to screen")
    candidates, rejected = [], 0

    for m in movers[:20]:
        sym = m["symbol"]
        if time.time() - _last_entry.get(sym, 0) < REENTRY_COOLDOWN:
            continue

        snap = sd.get_full_snapshot(sym)
        if not snap:
            rejected += 1
            continue

        pillars = sig.score_pillars(snap, market_hot=True)
        if not pillars["qualifies"]:
            rejected += 1
            log.debug(f"{sym}: {pillars['passed']}/5 pillars — skip")
            continue

        bars = sd.get_bars(sym, "1Min", lookback_days=1)
        if len(bars) < 20:
            rejected += 1
            continue

        pb = sig.detect_pullback(bars)
        if not pb or not pb["valid"]:
            rejected += 1
            continue

        candidates.append({"snap": snap, "pillars": pillars,
                           "pullback": pb, "bars": bars})
        log.info(f"CANDIDATE {sym}: grade {pillars['grade']}, "
                 f"pullback valid, ready={pb['ready']}")
        time.sleep(0.3)

    log.info(f"Scan complete: {len(candidates)} candidate(s), {rejected} rejected")

    if announce:
        tg.send(tg.format_scan(candidates, rejected, note))

    if AUTO_TRADE:
        for c in candidates:
            if c["pullback"]["ready"]:
                _try_enter(c, note)

    return candidates


def _try_enter(c: dict, clock_note: str = ""):
    snap, pillars, pb = c["snap"], c["pillars"], c["pullback"]
    sym = snap["symbol"]

    allowed, why = pf.can_trade()
    if not allowed:
        log.info(f"{sym}: entry blocked — {why}")
        return

    sizing = pf.calc_shares(pb["entry"], pb["stop"])
    if sizing["shares"] < 1:
        log.info(f"{sym}: sizing failed — {sizing.get('reason')}")
        return

    # Feed the AI its own track record on similar setups
    track_record = pm.get_context_summary()
    review = review_setup(snap, pillars, pb, sizing,
                          clock_note, track_record=track_record)
    if not review["approve"]:
        log.info(f"{sym}: AI veto — {review.get('veto_reason')} | "
                 f"{review.get('reasoning','')[:90]}")
        # Record the rejection — without this we can't tell whether the veto
        # is protecting us or costing us winners.
        pm.log_veto(sym, snap, pillars, pb, review)
        return

    pos = pf.open_position(
        symbol=sym, entry=pb["entry"], stop=pb["stop"], target=pb["target"],
        shares=sizing["shares"], setup="first_pullback",
        confidence=review["confidence"], reasoning=review["reasoning"],
        pillars_passed=pillars["passed"],
        rvol=snap.get("rvol"), float_m=snap.get("float_m"),
        pct_change=snap.get("pct_change"),
        catalyst_quality=review.get("catalyst_quality"),
    )
    if pos:
        _last_entry[sym] = time.time()
        pm.log_entry(pos, snap, pillars, pb, review)
        if not SILENT:
            tg.send(tg.format_entry(pos, snap, pillars, review, pb))


# ─── MONITOR ──────────────────────────────────────────────────────────────────

def run_monitor():
    s = pf.get_summary()
    if not s["positions"]:
        return

    force_close = past_force_close()

    for p in s["positions"]:
        sym = p["symbol"]
        snap = sd.get_snapshot(sym)
        if not snap or not snap["price"]:
            log.warning(f"Monitor: no price for {sym} — position unchecked")
            continue
        px = snap["price"]
        pf.update_high_water(sym, px)

        reason = None
        if px <= p["stop"]:
            reason = "stop_loss"
        elif px >= p["target"]:
            reason = "target"
        elif force_close:
            reason = "eod"
        else:
            held = _held_minutes(p.get("opened_at", ""))
            if held >= MAX_HOLD_MINUTES:
                reason = "max_hold"
            else:
                bars = sd.get_bars(sym, "1Min", lookback_days=1)
                if len(bars) >= 6:
                    exits = sig.check_exit_signals(bars)
                    high = [e for e in exits if e["severity"] == "high"]
                    if high:
                        reason = "exit_signal"
                        log.info(f"{sym}: {high[0]['indicator']} — {high[0]['note']}")

        if reason:
            trade = pf.close_position(sym, px, reason=reason)
            if trade:
                pm.log_outcome(sym, trade)      # always — this is the learning
                if not SILENT:
                    tg.send(tg.format_exit(trade))
            after = pf.get_summary()
            if after["halted_reason"] and not SILENT:
                tg.send(tg.format_halt(after["halted_reason"], after))
        time.sleep(0.2)


def _held_minutes(opened_at: str) -> float:
    try:
        t = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - t).total_seconds() / 60
    except Exception:
        return 0.0


# ─── TELEGRAM COMMANDS ────────────────────────────────────────────────────────

_last_update_id = 0


_poll_fail_logged = False


def clear_webhook() -> None:
    """
    Delete any registered webhook before polling.

    Telegram allows EITHER a webhook OR getUpdates, never both. If a webhook is
    set — even one registered by accident, or left over from another service
    sharing a token — getUpdates returns 409 Conflict and polling receives
    nothing, forever, with no visible error. Sending still works, so the bot
    looks alive while ignoring every command.
    """
    if not TELEGRAM_TOKEN:
        return
    try:
        info = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getWebhookInfo",
            timeout=10)
        url = ""
        if info.status_code == 200:
            url = (info.json().get("result") or {}).get("url", "")
        if url:
            log.warning(f"Telegram: webhook registered ({url}) — deleting so "
                        f"getUpdates polling can work")
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/deleteWebhook",
                json={"drop_pending_updates": False}, timeout=10)
        else:
            log.info("Telegram: no webhook set — polling is clear")
    except Exception as e:
        log.warning(f"Telegram webhook check failed: {e}")


def poll_commands():
    global _last_update_id, _poll_fail_logged
    if not TELEGRAM_TOKEN:
        return
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params={"offset": _last_update_id + 1, "timeout": 5}, timeout=10)
        if r.status_code != 200:
            # Log ONCE rather than every 5s, but never silently. A 409 here is
            # a webhook conflict and means no command will ever be received.
            if not _poll_fail_logged:
                _poll_fail_logged = True
                log.error(f"Telegram getUpdates HTTP {r.status_code}: "
                          f"{r.text[:200]}")
                if r.status_code == 409:
                    log.error("409 = webhook conflict. Calling deleteWebhook.")
                    clear_webhook()
            return
        _poll_fail_logged = False
        for upd in r.json().get("result", []):
            _last_update_id = max(_last_update_id, upd["update_id"])
            msg  = upd.get("message", {}) or {}
            chat = (msg.get("chat") or {}).get("id")
            if chat:
                # Whoever messages the bot is the chat it should reply to —
                # this self-corrects a wrong or stale TELEGRAM_CHAT_ID.
                tg.remember_chat(chat)
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            # Each command gets its own guard. Previously one exception killed
            # the whole poll iteration and was logged at DEBUG — invisible at
            # normal log level, so a single broken command silently disabled
            # ALL command handling with no trace.
            try:
                _handle_command(text)
            except Exception as e:
                log.error(f"Command '{text[:30]}' failed: {e}", exc_info=True)
                tg.send(f"⚠️ `{text.split()[0]}` failed: {type(e).__name__}: {e}")
    except Exception as e:
        log.warning(f"Command poll error: {e}")


def _handle_command(text: str):
    low = text.lower()

    if low.startswith(("/stock", "/portfolio")):
        prices = {}
        for p in pf.get_summary()["positions"]:
            snap = sd.get_snapshot(p["symbol"])
            if snap:
                prices[p["symbol"]] = snap["price"]
        tg.send(tg.format_portfolio(pf.get_summary(prices)))

    elif low.startswith("/scan"):
        tg.send("🔍 Scanning...")
        Thread(target=lambda: run_scan(force=True, announce=True), daemon=True).start()

    elif low.startswith("/rules"):
        tg.send(tg.format_rules())

    elif low.startswith("/trades"):
        tg.send(_format_ledger())

    elif low.startswith("/deposit"):
        _handle_deposit(text)

    elif low.startswith(("/postmortem", "/calibration", "/stats")):
        tg.send(pm.format_telegram())

    elif low.startswith("/report"):
        tg.send(_format_report())

    elif low.startswith("/health"):
        # Plain text: this report is full of env var names like
        # UPSTASH_REDIS_URL, and underscores are Markdown italic markers.
        # An odd number of them makes Telegram reject the entire message.
        tg.send(_format_health(), parse_mode=None)

    elif low.startswith(("/help", "/start")):
        tg.send(tg.HELP)


def _handle_deposit(text: str):
    """Add buying power without touching the track record: /deposit 10000"""
    parts = text.split()
    before = pf.get_summary()

    if len(parts) < 2:
        tg.send(
            "💵 *Add buying power*\n\n"
            "`/deposit 10000`\n\n"
            f"Current: ${before['starting_cash']:.2f} basis, "
            f"${before['cash']:.2f} cash\n\n"
            "_Keeps every trade and your W/L record. Percentage returns stay "
            "honest because the basis rises too._\n\n"
            "⚠️ A bigger bank also means risk-based sizing can finally reach the "
            f"full ${pf.RISK_PER_TRADE:.0f}/trade — on $2k, tight stops were "
            "capital-capped well below that."
        )
        return

    try:
        target = float(parts[1])
    except ValueError:
        tg.send("⚠️ Give a number, e.g. `/deposit 10000`")
        return

    if not pf.deposit(target):
        tg.send(f"ℹ️ Basis is already ${before['starting_cash']:.2f} — nothing added.")
        return

    after = pf.get_summary()
    tg.send(
        "💵 *Deposit complete*\n\n"
        f"Bank: ${before['starting_cash']:.2f} → *${after['starting_cash']:.2f}*\n"
        f"Cash: ${before['cash']:.2f} → *${after['cash']:.2f}*\n\n"
        f"✅ Record preserved: *{after['winning_trades']}W / "
        f"{after['losing_trades']}L* across {after['total_trades']} trades"
    )


def _format_ledger(limit: int = 15) -> str:
    s = pf._load()
    hist = list(reversed(s.get("trade_history", [])))[:limit]
    if not hist:
        return "📒 *STOCK LEDGER*\n\nNo closed trades yet."
    lines = [f"📒 *STOCK LEDGER* (last {len(hist)})\n"]
    for i, t in enumerate(hist, 1):
        icon = "✅" if t["won"] else "❌"
        lines.append(
            f"{icon} *{t['symbol']}* {t['shares']}sh\n"
            f"   `${t['entry']:.2f}` → `${t['exit']:.2f}`  "
            f"*${t['pnl']:+.2f}* ({t.get('cents_per_share',0):+.2f}/sh)\n"
            f"   {t.get('reason','')} · {t['held_minutes']:.0f}min · "
            f"conf {t.get('confidence','?')}"
        )
    return "\n".join(lines)


def _format_report() -> str:
    s = pf.get_summary()
    hist = pf._load().get("trade_history", [])
    if not hist:
        return "📊 *STOCK REPORT*\n\nNo closed trades yet."

    setups: dict = {}
    for t in hist:
        k = t.get("setup", "unknown")
        setups.setdefault(k, []).append(t)

    lines = [
        "📊 *STOCK GOLEM REPORT*\n",
        f"Account: *${s['total_value']:.2f}* ({s['total_pnl_pct']:+.1f}%)",
        f"Record: *{s['winning_trades']}W / {s['losing_trades']}L* ({s['win_rate']:.0f}%)",
        f"Avg win ${s['avg_win']:+.2f} | avg loss ${s['avg_loss']:+.2f}",
        f"Profit ratio: {s['profit_ratio']:.2f}",
        "",
        "*Ross's benchmarks:*",
        f"  Accuracy {s['win_rate']:.0f}% — "
        f"{'Pro >70%' if s['win_rate']>70 else 'Advanced 60-70%' if s['win_rate']>=60 else 'Beginner 50-60%' if s['win_rate']>=50 else 'Novice 40-50%'}",
        f"  P/L ratio {s['profit_ratio']:.2f} — "
        f"{'Advanced' if s['profit_ratio']>=1.5 else 'Beginner' if s['profit_ratio']>=1.0 else 'Novice'}",
        "",
        "*By setup:*",
    ]
    for k, ts in setups.items():
        w = sum(1 for t in ts if t["won"])
        pnl = sum(t["pnl"] for t in ts)
        lines.append(f"  {k}: {w}/{len(ts)} — ${pnl:+.2f}")
    return "\n".join(lines)


def _format_health() -> str:
    lines = ["🩺 *STOCK GOLEM HEALTH*\n"]
    lines.append(
        f"📍 project `{os.getenv('RAILWAY_PROJECT_NAME','?')}` / "
        f"service `{os.getenv('RAILWAY_SERVICE_NAME','?')}`"
    )
    me = tg.whoami()
    if me.get("ok"):
        lines.append(f"🤖 bot: @{me['username']} ({me['name']})")
    lines.append("")

    if not sd.is_configured():
        lines.append("❌ Alpaca keys not set")
    else:
        acct = sd.get_account()
        if acct:
            lines.append(f"✅ Alpaca — {acct['status']}, "
                         f"${acct['equity']:,.2f} equity")
        else:
            lines.append("❌ Alpaca auth failed")

    clock = sd.get_clock()
    if clock:
        lines.append(f"🕐 Market {'OPEN' if clock['is_open'] else 'closed'}")
    open_now, note = in_entry_window()
    lines.append(f"🎯 Entry window: {'OPEN' if open_now else 'closed'} ({note})")

    s = pf.get_summary()
    lines.append(
        f"\n💾 Portfolio: {s['total_trades']} trades, "
        f"{len(s['positions'])} open, ${s['total_value']:.2f}"
    )
    if s["halted_reason"]:
        lines.append(f"🛑 {s['halted_reason']}")

    # Storage diagnostics — a write that silently falls back to the container
    # filesystem looks fine until the next redeploy erases it.
    rh = pf.redis_health()
    lines.append("")
    if not rh["configured"]:
        lines.append("❌ *Redis not configured* — data LOST on every redeploy")
        lines.append(f"   {rh.get('error','')}")
    elif not rh["readable"]:
        lines.append(f"❌ *Redis unreadable* — {rh.get('error','')}")
    elif not rh["writable"]:
        lines.append(f"⚠️ *Redis reads but does NOT write*")
        lines.append(f"   {rh.get('error','')}")
        lines.append("   _Deposits and trades won't survive a restart._")
    else:
        lines.append(f"✅ Redis OK via `{rh['url_var']}`")
        if rh["key_exists"]:
            # Coerce explicitly — a stored null would make an f-string numeric
            # format raise TypeError, which is what silently broke /health.
            cash  = rh.get("stored_cash")  or 0
            basis = rh.get("stored_basis") or 0
            lines.append(
                f"   stored: ${float(cash):,.2f} cash / "
                f"${float(basis):,.2f} basis · "
                f"{rh.get('stored_trades') or 0} trades · "
                f"{rh.get('stored_deposits') or 0} deposit(s) · {rh['bytes']:,}B"
            )
        else:
            lines.append("   ⚠️ portfolio key does not exist yet — nothing saved")

    return "\n".join(lines)


# ─── LOOPS ────────────────────────────────────────────────────────────────────

def scan_loop(stop: Event):
    while not stop.is_set():
        try:
            run_scan()
        except Exception as e:
            log.error(f"Scan loop error: {e}", exc_info=True)
        _sleep(stop, SCAN_INTERVAL)


def monitor_loop(stop: Event):
    while not stop.is_set():
        try:
            run_monitor()
        except Exception as e:
            log.error(f"Monitor loop error: {e}", exc_info=True)
        _sleep(stop, MONITOR_INTERVAL)


def command_loop(stop: Event):
    while not stop.is_set():
        try:
            poll_commands()
        except Exception as e:
            log.debug(f"Command loop error: {e}")
        _sleep(stop, COMMAND_INTERVAL)


def _sleep(stop: Event, seconds: int):
    for _ in range(seconds):
        if stop.is_set():
            return
        time.sleep(1)


def main():
    log.info("=" * 60)
    log.info("STOCK GOLEM — starting")
    log.info(f"project={os.getenv('RAILWAY_PROJECT_NAME','?')} "
             f"service={os.getenv('RAILWAY_SERVICE_NAME','?')}")
    log.info(f"Entry window {ENTRY_START_ET}-{ENTRY_END_ET} ET | "
             f"force close {FORCE_CLOSE_ET} ET")
    log.info(f"Risk ${pf.RISK_PER_TRADE:.0f}/trade | daily max "
             f"${pf.DAILY_MAX_LOSS:.0f} | {pf.MAX_CONSECUTIVE_LOSS} losers = done")
    log.info(f"Auto-trade: {AUTO_TRADE} | Silent: {SILENT}")
    log.info("=" * 60)

    if not sd.is_configured():
        log.error("ALPACA_API_KEY / ALPACA_SECRET_KEY not set — data layer dead")

    # Identify which bot this token belongs to — with several bots configured
    # it's easy to set one token while messaging a different bot, and the only
    # symptom is a confusing "chat not found".
    me = tg.whoami()
    if me.get("ok"):
        log.info(f"Telegram bot identity: @{me['username']} ({me['name']})")
        log.info(f"Configured chat_id: {tg.CHAT_ID or 'unset'} — "
                 f"message @{me['username']} directly if replies don't arrive")
    else:
        log.error(f"Telegram getMe failed: {me.get('error')}")

    # Must run before polling starts — a stale webhook silently blocks every
    # command while outbound messages continue to work normally.
    clear_webhook()

    if not SILENT:
        s = pf.get_summary()
        open_now, note = in_entry_window()
        tg.send(
            f"📈 *STOCK GOLEM* online\n\n"
            f"Account: *${s['total_value']:.2f}*\n"
            f"Entry window: {ENTRY_START_ET}–{ENTRY_END_ET} ET "
            f"({'open now' if open_now else note})\n"
            f"Risk ${pf.RISK_PER_TRADE:.0f}/trade, max "
            f"${pf.DAILY_MAX_LOSS:.0f}/day, {pf.MAX_CONSECUTIVE_LOSS} losers = done\n\n"
            f"`/help` for commands"
        )

    stop = Event()
    threads = [
        Thread(target=scan_loop,    args=(stop,), daemon=True, name="stock-scan"),
        Thread(target=monitor_loop, args=(stop,), daemon=True, name="stock-monitor"),
        Thread(target=command_loop, args=(stop,), daemon=True, name="stock-commands"),
    ]
    for t in threads:
        t.start()

    log.info("All loops running.")
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        log.info("Stopping...")
        stop.set()
        for t in threads:
            t.join(timeout=5)


if __name__ == "__main__":
    main()
