#!/usr/bin/env python3
"""
stock_telegram.py — Plain-English alerts for Stock Golem.

Same principle as the Kalshi bot: describe the trade the way a person would,
not the way the code stores it. Share counts and stop distances in dollars,
not percentages, because that's how you'd actually think about a $2k account.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_TOKEN = (os.getenv("STOCK_TELEGRAM_TOKEN")
                  or os.getenv("TELEGRAM_BOT_TOKEN", ""))
CHAT_ID        = (os.getenv("STOCK_CHAT_ID")
                  or os.getenv("TELEGRAM_CHAT_ID", "")).strip()
URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

# Learned at runtime from any incoming message. A configured CHAT_ID that
# belongs to a different bot's chat produces "chat not found", which is
# indistinguishable from a typo and wastes time. Whoever messages the bot IS
# the chat it should reply to, so we capture that and prefer it.
_learned_chat_id: str = ""


def remember_chat(chat_id) -> None:
    """Called by the command poller for every incoming message."""
    global _learned_chat_id
    cid = str(chat_id).strip()
    if cid and cid != _learned_chat_id:
        _learned_chat_id = cid
        if cid != CHAT_ID:
            log.warning(
                f"Stock Telegram: learned chat_id {cid} from an incoming "
                f"message (configured value was {CHAT_ID or 'unset'}). "
                f"Using the learned one."
            )


def _target_chat() -> str:
    return _learned_chat_id or CHAT_ID


def whoami() -> dict:
    """
    Which bot does this token actually belong to?

    With several bots in play it's easy to configure one token while messaging
    a different bot — the symptom is 'chat not found', which looks like a bad
    chat ID rather than a bot mix-up. This removes the ambiguity.
    """
    if not TELEGRAM_TOKEN:
        return {"ok": False, "error": "no token set"}
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getMe", timeout=10)
        if r.status_code != 200:
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        d = r.json().get("result", {}) or {}
        return {
            "ok":        True,
            "username":  d.get("username", "?"),
            "name":      d.get("first_name", "?"),
            "id":        d.get("id"),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def send(text: str, parse_mode: str = "Markdown") -> bool:
    chat = _target_chat()
    if not TELEGRAM_TOKEN or not chat:
        log.warning("Stock Telegram not configured — printing instead")
        print(f"\n{'='*60}\n{text}\n{'='*60}\n")
        return False
    try:
        r = requests.post(URL, json={"chat_id": chat, "text": text,
                                     "parse_mode": parse_mode}, timeout=10)
        if r.status_code == 200:
            return True
        if r.status_code == 400 and "chat not found" in r.text.lower():
            log.error(
                f"Stock Telegram: chat_id {chat} not reachable. Send this bot "
                f"any message so it can learn the right one."
            )
        else:
            log.warning(f"Stock Telegram HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.error(f"Stock Telegram send failed: {e}")
    return False


# ─── ENTRY ALERT ──────────────────────────────────────────────────────────────

def format_entry(pos: dict, snap: dict, pillars: dict,
                 review: dict, pullback: dict) -> str:
    sym    = pos["symbol"]
    shares = pos["shares"]
    entry  = pos["entry"]
    stop   = pos["stop"]
    target = pos["target"]
    risk   = pos["risk"]
    reward = shares * (target - entry)

    conf = review.get("confidence", 0)
    bar  = "█" * int(conf / 10) + "░" * (10 - int(conf / 10))

    p = pillars["pillars"]
    pillar_lines = []
    for key, label in [("rvol", "Volume"), ("momentum", "Up today"),
                       ("catalyst", "News"), ("price", "Price"), ("float", "Float")]:
        d = p.get(key, {})
        icon = "✅" if d.get("pass") is True else ("❌" if d.get("pass") is False else "❔")
        pillar_lines.append(f"{icon} {label}: {d.get('note','')}")

    heads = snap.get("headlines") or []
    head = heads[0] if heads else "no catalyst found"

    return (
        f"📈 *STOCK GOLEM* — Entry\n\n"
        f"*{sym}* @ `${entry:.2f}`  ({snap.get('pct_change',0):+.0f}% today)\n"
        f"Grade {pillars['grade']} — {pillars['passed']}/5 pillars\n\n"
        f"*The trade:*\n"
        f"• Buy *{shares} shares* at ${entry:.2f}  (${pos['cost']:.0f})\n"
        f"• Stop at ${stop:.2f} — you lose *${risk:.0f}* if wrong\n"
        f"• Target ${target:.2f} — you make *${reward:.0f}* if right\n"
        f"• That's {pos['reward_ratio']:.1f}:1\n\n"
        f"*Why it qualified:*\n" + "\n".join(pillar_lines) + "\n\n"
        f"*The setup:* pullback retraced {pullback['retrace_pct']:.0f}%, held "
        f"above VWAP (${pullback['vwap']:.2f}) and the 9 EMA "
        f"(${pullback['ema']:.2f}). Entry on the crossing candle.\n\n"
        f"💬 *Golem:* {review.get('reasoning','')}\n"
        f"`{bar}` {conf}/100\n\n"
        f"📰 {head}\n"
        f"⚠️ {review.get('key_risk','')}\n\n"
        f"_Paper trade._"
    )


# ─── EXIT ALERT ───────────────────────────────────────────────────────────────

def format_exit(trade: dict) -> str:
    won  = trade["won"]
    pnl  = trade["pnl"]
    sym  = trade["symbol"]
    cps  = trade.get("cents_per_share", 0)

    reason_text = {
        "stop_loss":      "stopped out",
        "target":         "hit target 🎯",
        "exit_signal":    "exit indicator fired",
        "eod":            "closed at end of day",
        "max_hold":       "held too long, closed",
        "trailing":       "trailing stop",
    }.get(trade.get("reason", ""), trade.get("reason", "closed"))

    icon = "✅" if won else "❌"
    return (
        f"{icon} *STOCK GOLEM* — Closed\n\n"
        f"*{sym}* {reason_text}\n"
        f"{trade['shares']} sh: `${trade['entry']:.2f}` → `${trade['exit']:.2f}`\n"
        f"*${pnl:+.2f}*  ({cps:+.2f}/share, {trade['pnl_pct']:+.1f}%)\n"
        f"Held {trade['held_minutes']:.0f} min\n"
    )


# ─── HALT ALERT ───────────────────────────────────────────────────────────────

def format_halt(reason: str, summary: dict) -> str:
    return (
        f"🛑 *STOCK GOLEM* — Done for the day\n\n"
        f"*{reason}*\n\n"
        f"Today: {summary['day_trades']} trades, *${summary['day_pnl']:+.2f}*\n"
        f"Account: ${summary['total_value']:.2f}\n\n"
        f"_No more entries until tomorrow. This is the rule working, not a bug._"
    )


# ─── PORTFOLIO SNAPSHOT ───────────────────────────────────────────────────────

def format_portfolio(s: dict) -> str:
    icon = "📈" if s["total_pnl"] >= 0 else "📉"
    lines = [
        "📊 *STOCK GOLEM PORTFOLIO*\n",
        f"Value: *${s['total_value']:.2f}*  (started ${s['starting_cash']:.0f})",
        f"{icon} P&L: *${s['total_pnl']:+.2f}*  ({s['total_pnl_pct']:+.1f}%)",
        f"Cash: ${s['cash']:.2f}",
        "",
    ]

    if s["total_trades"]:
        lines += [
            f"Record: *{s['winning_trades']}W / {s['losing_trades']}L*  "
            f"({s['win_rate']:.0f}%)",
            f"Avg win ${s['avg_win']:+.2f} | Avg loss ${s['avg_loss']:+.2f}",
            f"Profit ratio: {s['profit_ratio']:.2f}  _(target ≥2.0)_",
            "",
        ]

    lines.append(f"*Today:* {s['day_trades']} trades, ${s['day_pnl']:+.2f}")
    if s["consecutive_losses"]:
        lines.append(f"⚠️ {s['consecutive_losses']} consecutive losses")
    if s["halted_reason"]:
        lines.append(f"🛑 Halted: {s['halted_reason']}")
    lines.append("")

    if s["positions"]:
        lines.append("*Open:*")
        for p in s["positions"]:
            arrow = "🟢" if p["unrealized"] >= 0 else "🔴"
            lines.append(
                f"{arrow} {p['symbol']} {p['shares']}sh @ ${p['entry']:.2f} "
                f"→ ${p['current']:.2f}  *${p['unrealized']:+.2f}*\n"
                f"   stop ${p['stop']:.2f} | target ${p['target']:.2f}"
            )
    else:
        lines.append("_No open positions._")

    return "\n".join(lines)


# ─── CANDIDATE SUMMARY ────────────────────────────────────────────────────────

def format_scan(candidates: list, rejected: int, market_note: str = "") -> str:
    if not candidates:
        return (
            f"🔍 *STOCK GOLEM* — Scan complete\n\n"
            f"No qualifying setups.{' ' + market_note if market_note else ''}\n"
            f"{rejected} candidate(s) screened out.\n\n"
            f"_A day with no trades is a normal outcome._"
        )

    lines = [f"🔍 *STOCK GOLEM* — {len(candidates)} candidate(s)\n"]
    for c in candidates:
        s, p = c["snap"], c["pillars"]
        lines.append(
            f"*{s['symbol']}* ${s['price']:.2f} ({s['pct_change']:+.0f}%) — "
            f"grade {p['grade']}, {p['passed']}/5"
        )
    lines.append(f"\n{rejected} screened out.")
    return "\n".join(lines)


HELP = (
    "🤖 *STOCK GOLEM*\n\n"
    "Momentum day trading on small caps — Ross Cameron's 5-pillar method, "
    "paper traded.\n\n"
    "*Commands:*\n"
    "`/stock` — portfolio & open positions\n"
    "`/scan` — scan for setups now\n"
    "`/trades` — trade-by-trade ledger\n"
    "`/report` — performance report\n"
    "`/postmortem` — what's actually working (by catalyst, hour, grade)\n"
    "`/rules` — the strategy rules in force\n"
    "`/health` — data & storage diagnostics\n"
    "`/help` — this message"
)


def format_rules() -> str:
    import stock_signals as sig
    import stock_portfolio as pf
    return (
        "📋 *STRATEGY RULES IN FORCE*\n\n"
        "*5 Pillars* (need ≥%d):\n"
        "1. RVOL ≥ %.0fx\n"
        "2. Up ≥ %.0f%% today\n"
        "3. News catalyst\n"
        "4. Price $%.0f–$%.0f\n"
        "5. Float < %.0fM\n\n"
        "*Pullback entry:*\n"
        "• Retrace ≤ %.0f%%\n"
        "• Volume heavier on green than red\n"
        "• Holds VWAP and %d EMA\n"
        "• Enter on the crossing candle\n"
        "• Stop = pullback low\n\n"
        "*Risk:*\n"
        "• Risk $%.0f/trade, min %.0f:1\n"
        "• Daily max loss $%.0f\n"
        "• %d consecutive losers → done\n"
        % (sig.MIN_PILLARS, sig.MIN_RVOL, sig.MIN_PCT_CHANGE,
           sig.SMALL_ACCT_MIN, sig.SMALL_ACCT_MAX, sig.FLOAT_MAX_HOT_M,
           sig.MAX_RETRACE_PCT, sig.EMA_PERIOD,
           pf.RISK_PER_TRADE, pf.MIN_PROFIT_RATIO,
           pf.DAILY_MAX_LOSS, pf.MAX_CONSECUTIVE_LOSS)
    )
