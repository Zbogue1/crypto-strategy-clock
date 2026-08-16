#!/usr/bin/env python3
"""
kalshi_telegram.py — Plain-English Telegram alerts for Kalshi perp signals.

All messages are titled "KALSHI" and written like a friend who trades crypto
is texting you. No jargon, no percentages for stop loss — plain dollar terms.

Message types:
  1. NEW SIGNAL  — Golem found a good bet, here's why
  2. EXIT ALERT  — position closed (stop hit / take profit / liquidated)
  3. FUNDING REMINDER — daily funding cost check on open positions
  4. PORTFOLIO SNAPSHOT — /kalshi command summary

The KALSHI_TELEGRAM_TOKEN and KALSHI_CHAT_ID env vars are the same bot used
by the FOMO system, or you can set separate ones.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_TOKEN = (os.getenv("KALSHI_TELEGRAM_TOKEN")
                  or os.getenv("TELEGRAM_TOKEN")
                  or os.getenv("TELEGRAM_BOT_TOKEN", ""))
CHAT_ID        = (os.getenv("KALSHI_CHAT_ID")
                  or os.getenv("TELEGRAM_CHAT_ID", ""))
TELEGRAM_URL   = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"


# ─── SEND ─────────────────────────────────────────────────────────────────────

def send_telegram(text: str, parse_mode: str = "Markdown") -> bool:
    if not TELEGRAM_TOKEN or not CHAT_ID:
        log.warning("Kalshi Telegram: no token/chat_id configured — printing instead")
        print(f"\n{'='*60}\n{text}\n{'='*60}\n")
        return False
    try:
        r = requests.post(
            TELEGRAM_URL,
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if r.status_code == 200:
            return True
        log.warning(f"Kalshi Telegram: HTTP {r.status_code}: {r.text[:200]}")
        return False
    except Exception as e:
        log.error(f"Kalshi Telegram: send error: {e}")
        return False


# ─── SIGNAL ALERT ─────────────────────────────────────────────────────────────

def format_signal_alert(verdict: dict, margin: float = 50.0) -> str:
    """
    Format a new signal alert.

    verdict: output of kalshi_research.analyze_market()
    margin:  how much we're putting in (for dollar-amount TP/SL estimates)
    """
    ticker    = verdict["ticker"]
    title     = verdict.get("title", ticker)
    direction = verdict["verdict"]   # "UP" or "DOWN"
    confidence = verdict["confidence"]
    price     = verdict["price"]
    leverage  = verdict.get("suggested_leverage", 2.0)
    stop_pct  = verdict.get("stop_pct", 5.0)
    tp_pct    = verdict.get("take_profit_pct", 10.0)
    reasoning = verdict.get("reasoning", "")
    key_risk  = verdict.get("key_risk", "")
    fund_note = verdict.get("funding_cost_note", "")

    # Dollar amounts
    notional    = margin * leverage
    stop_loss_usd = round(margin * stop_pct / 100, 2)
    take_profit_usd = round(notional * tp_pct / 100, 2)

    direction_emoji = "🟢" if direction == "UP" else "🔴"
    direction_word  = "bet UP" if direction == "UP" else "bet DOWN"
    tp_word         = "pumps" if direction == "UP" else "dumps"

    # Confidence bar
    bar_filled = int(confidence / 10)
    conf_bar   = "█" * bar_filled + "░" * (10 - bar_filled)

    msg = (
        f"📡 *KALSHI* — {direction_emoji} New Signal\n"
        f"\n"
        f"*{title}* (`{ticker}`)\n"
        f"Price now: `${price:.4f}`\n"
        f"\n"
        f"Golem says: *{direction_word}* at {confidence}/100 confidence\n"
        f"`{conf_bar}`\n"
        f"\n"
        f"💬 *Why:* {reasoning}\n"
        f"\n"
        f"📐 *The setup:*\n"
        f"• Put in: ${margin:.0f} at {leverage}x leverage (${notional:.0f} total exposure)\n"
        f"• Walk away if it hits: ${stop_loss_usd:.2f} loss\n"
        f"• Take the money at: ${take_profit_usd:.2f} gain\n"
        f"\n"
        f"💸 *Funding cost:* {fund_note}\n"
        f"\n"
        f"⚠️ *Watch out for:* {key_risk}\n"
        f"\n"
        f"_Paper trade only. Real money coming after calibration._"
    )
    return msg


# ─── EXIT ALERT ───────────────────────────────────────────────────────────────

def format_exit_alert(trade: dict) -> str:
    """
    Format a position close alert.
    trade: output of kalshi_portfolio.close_position()
    """
    ticker    = trade["ticker"]
    direction = trade["direction"]
    net_pnl   = trade.get("net_pnl", 0.0)
    reason    = trade.get("reason", "closed")
    entry     = trade.get("entry_price", 0)
    exit_p    = trade.get("exit_price", 0)
    leverage  = trade.get("leverage", 1)
    funding   = trade.get("funding_paid", 0.0)
    held      = trade.get("held_hours", 0.0)

    won = net_pnl > 0

    # Plain-English reason
    reason_map = {
        "stop_loss":   "walked away (stop loss hit)",
        "take_profit": "cashed out (take profit hit) 🎉",
        "liquidated":  "got liquidated 💀",
        "manual":      "closed manually",
    }
    reason_text = reason_map.get(reason, reason)

    emoji = "✅" if won else "❌"
    direction_emoji = "🟢" if direction == "UP" else "🔴"

    msg = (
        f"{emoji} *KALSHI* — Position Closed\n"
        f"\n"
        f"{direction_emoji} *{ticker}* {direction} {leverage}x\n"
        f"We {reason_text}\n"
        f"\n"
        f"Entry: `${entry:.4f}` → Exit: `${exit_p:.4f}`\n"
        f"Net result: *${net_pnl:+.2f}*"
        + (f" (after ${funding:.2f} in funding fees)" if funding > 0.01 else "")
        + f"\n"
        f"Held for {held:.1f} hours\n"
    )

    if reason == "liquidated":
        msg += "\n⚠️ Position was fully liquidated — leverage was too high for the move."
    elif won:
        msg += f"\nSolid call. Golem nailed it."
    else:
        msg += f"\nMarket had other plans. Adding to postmortem."

    return msg


# ─── FUNDING REMINDER ─────────────────────────────────────────────────────────

def format_funding_reminder(charges: list[dict]) -> Optional[str]:
    """
    Format funding payment notification. Only sent if total charge > $0.10.
    charges: output of kalshi_portfolio.apply_funding()
    """
    if not charges:
        return None

    total_charge = sum(c["charge"] for c in charges)
    if abs(total_charge) < 0.10:
        return None  # too small to bother the user

    lines = ["💸 *KALSHI* — Funding payment applied\n"]
    for c in charges:
        dir_emoji = "🟢" if c["direction"] == "UP" else "🔴"
        paid_or_received = "paid" if c["charge"] > 0 else "received"
        lines.append(
            f"{dir_emoji} {c['ticker']}: {paid_or_received} ${abs(c['charge']):.4f} "
            f"(total: ${c['cumulative_paid']:.2f})"
        )
    lines.append(f"\nTotal this cycle: ${total_charge:+.4f}")
    return "\n".join(lines)


# ─── PORTFOLIO SNAPSHOT ───────────────────────────────────────────────────────

def format_portfolio_snapshot(summary: dict) -> str:
    """
    Full portfolio summary for /kalshi command.
    summary: output of kalshi_portfolio.get_portfolio_summary()
    """
    pnl_emoji = "📈" if summary["total_pnl"] >= 0 else "📉"
    lines = [
        f"📊 *KALSHI PAPER PORTFOLIO*\n",
        f"Bank: ${summary['cash']:.2f} cash + ${summary['total_value'] - summary['cash']:.2f} in positions",
        f"Total value: *${summary['total_value']:.2f}* (started ${summary['starting_cash']:.2f})",
        f"{pnl_emoji} All-time P&L: *${summary['total_pnl']:+.2f}*",
        f"Win rate: {summary['win_rate']:.0f}% ({summary['winning_trades']}W / {summary['losing_trades']}L)",
        f"Funding paid total: ${summary['total_funding_paid']:.2f}",
        "",
    ]

    if summary["positions"]:
        lines.append("*Open bets:*")
        for p in summary["positions"]:
            emoji = "🟢" if p["direction"] == "UP" else "🔴"
            pnl   = p["unrealized_pnl"]
            pnl_s = f"${pnl:+.2f}"
            lines.append(
                f"{emoji} {p['ticker']} {p['direction']} {p['leverage']}x "
                f"| In at {p['entry']:.4f}, now {p['current']:.4f} "
                f"| Unrealized: {pnl_s}"
            )
    else:
        lines.append("No open bets right now.")

    return "\n".join(lines)


# ─── SCAN SUMMARY (no viable signals) ────────────────────────────────────────

def format_no_signals_summary(markets_checked: int) -> str:
    """Sent when a scan cycle finds nothing worth betting on."""
    now = datetime.now(timezone.utc).strftime("%H:%M UTC")
    return (
        f"🔍 *KALSHI* — Scan complete ({now})\n"
        f"Checked {markets_checked} markets. Nothing worth betting on right now.\n"
        f"Golem is watching."
    )


# ─── CONVENIENCE WRAPPERS ─────────────────────────────────────────────────────

def send_signal(verdict: dict, margin: float = 50.0) -> bool:
    return send_telegram(format_signal_alert(verdict, margin))


def send_exit(trade: dict) -> bool:
    return send_telegram(format_exit_alert(trade))


def send_funding(charges: list[dict]) -> bool:
    msg = format_funding_reminder(charges)
    if msg:
        return send_telegram(msg)
    return True


def send_portfolio(summary: dict = None) -> bool:
    from kalshi_portfolio import get_portfolio_summary
    if summary is None:
        summary = get_portfolio_summary()
    return send_telegram(format_portfolio_snapshot(summary))


if __name__ == "__main__":
    # Preview all message formats
    sample_verdict = {
        "ticker":            "BTC-PERP",
        "title":             "Bitcoin Perpetual",
        "verdict":           "UP",
        "confidence":        73,
        "price":             95420.50,
        "suggested_leverage": 3.0,
        "stop_pct":          5.0,
        "take_profit_pct":   12.0,
        "reasoning":         "Bitcoin is in a confirmed uptrend on the hourly — ADX at 28, +DI crushing -DI. Funding is slightly negative which means shorts are paying longs, so the crowd is actually betting against us which is a green flag. OI is rising with price — people are piling in on the right side.",
        "key_risk":          "If BTC breaks below $93k support the whole structure falls apart.",
        "funding_cost_note": "Funding rate is slightly negative, so you'd actually earn about $0.05/day holding the long.",
    }

    sample_trade = {
        "ticker":      "BTC-PERP",
        "direction":   "UP",
        "net_pnl":     18.50,
        "reason":      "take_profit",
        "entry_price": 95000.0,
        "exit_price":  106400.0,
        "leverage":    3.0,
        "funding_paid": 0.15,
        "held_hours":  14.5,
    }

    print(format_signal_alert(sample_verdict, margin=50.0))
    print("\n" + "="*60 + "\n")
    print(format_exit_alert(sample_trade))
