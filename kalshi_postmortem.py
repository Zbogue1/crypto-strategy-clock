#!/usr/bin/env python3
"""
kalshi_postmortem.py — Learning from mistakes on Kalshi perps.

Every trade we make gets logged with the full signal context.
When it closes, we record what actually happened vs what Golem said.

Over time this gives us:
  - Win rate by asset (which tickers is Golem good at?)
  - Win rate by confidence band (is 70+ confidence actually more accurate?)
  - Win rate by trend label (are UPTREND calls winning more than EARLY UPTREND?)
  - Win rate by funding sentiment (is the contrarian lean working?)
  - Performance by open interest direction
  - Calibration: if Golem says 80% confident, are we winning 80% of those?

The postmortem_summary() output is passed back into the research agent's
context so it can learn from its own history.

State persisted to kalshi_postmortem.json (persists across Railway deploys).
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

POSTMORTEM_FILE = os.getenv(
    "KALSHI_POSTMORTEM_FILE",
    os.path.join(os.path.dirname(__file__), "kalshi_postmortem.json"),
)

MIN_SAMPLE = 5   # need at least this many trades before we report a stat


# ─── PERSISTENCE ──────────────────────────────────────────────────────────────

def _load() -> dict:
    if os.path.exists(POSTMORTEM_FILE):
        try:
            with open(POSTMORTEM_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Kalshi postmortem: load error: {e}")
    return {"calls": []}


def _save(state: dict):
    try:
        with open(POSTMORTEM_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Kalshi postmortem: save error: {e}")


# ─── LOG CALL ─────────────────────────────────────────────────────────────────

def log_call(verdict: dict):
    """
    Record a research verdict at the time of signal generation.
    Called when we decide to paper-open a position.
    """
    state = _load()
    record = {
        "id":               len(state["calls"]) + 1,
        "ticker":           verdict["ticker"],
        "title":            verdict.get("title", verdict["ticker"]),
        "verdict":          verdict["verdict"],
        "confidence":       verdict["confidence"],
        "price_at_call":    verdict["price"],
        "trend_label":      verdict.get("trend_label", ""),
        "adx":              verdict.get("adx"),
        "composite_score":  verdict.get("composite_score", 0),
        "funding_rate_8h":  verdict.get("funding_rate_8h", 0.0),
        "funding_sentiment": verdict.get("funding_sentiment", ""),
        "oi_trend":         verdict.get("oi_trend", ""),
        "momentum_24h":     verdict.get("momentum_24h", 0.0),
        "reasoning":        verdict.get("reasoning", ""),
        "key_risk":         verdict.get("key_risk", ""),
        "called_at":        verdict.get("analyzed_at", datetime.now(timezone.utc).isoformat()),
        # Filled in when trade closes:
        "outcome":          None,    # "win" | "loss" | "liquidated"
        "exit_reason":      None,    # "stop_loss" | "take_profit" | "liquidated"
        "exit_price":       None,
        "realized_pnl":     None,
        "pnl_pct":          None,
        "held_hours":       None,
        "closed_at":        None,
        "correct_direction": None,  # did price go the direction we said?
    }
    state["calls"].append(record)
    _save(state)
    log.info(f"Kalshi postmortem: logged call #{record['id']} — {verdict['ticker']} {verdict['verdict']} {verdict['confidence']}/100")


def log_outcome(ticker: str, trade: dict):
    """
    Update a call record with the actual trade outcome.
    Called when a position closes.

    trade: output of kalshi_portfolio.close_position()
    """
    state  = _load()
    # Find the most recent open call for this ticker
    record = next(
        (c for c in reversed(state["calls"])
         if c["ticker"] == ticker and c["outcome"] is None),
        None,
    )
    if not record:
        log.warning(f"Kalshi postmortem: no open call found for {ticker}")
        return

    won         = trade.get("net_pnl", 0) > 0
    reason      = trade.get("reason", "")
    exit_price  = trade.get("exit_price", 0.0)
    entry_price = trade.get("entry_price", 0.0)
    direction   = trade.get("direction", "")

    # Was the price direction correct regardless of SL/TP?
    if entry_price > 0 and exit_price > 0:
        moved_up = exit_price > entry_price
        correct_direction = (
            (direction == "UP" and moved_up) or
            (direction == "DOWN" and not moved_up)
        )
    else:
        correct_direction = won

    record.update({
        "outcome":          "win" if won else ("liquidated" if reason == "liquidated" else "loss"),
        "exit_reason":      reason,
        "exit_price":       exit_price,
        "realized_pnl":     trade.get("net_pnl", 0.0),
        "pnl_pct":          trade.get("pnl_pct", 0.0),
        "held_hours":       trade.get("held_hours", 0.0),
        "closed_at":        datetime.now(timezone.utc).isoformat(),
        "correct_direction": correct_direction,
    })
    _save(state)
    log.info(
        f"Kalshi postmortem: outcome logged for {ticker} — "
        f"{'WIN' if won else 'LOSS'} ${trade.get('net_pnl', 0):+.2f}"
    )


# ─── ANALYSIS ─────────────────────────────────────────────────────────────────

def _completed_calls(state: dict) -> list[dict]:
    return [c for c in state["calls"] if c.get("outcome") is not None]


def _win_rate(calls: list[dict]) -> Optional[float]:
    if len(calls) < MIN_SAMPLE:
        return None
    wins = sum(1 for c in calls if c.get("outcome") == "win")
    return round(wins / len(calls) * 100, 1)


def get_stats() -> dict:
    """Full performance statistics across all dimensions."""
    state     = _load()
    completed = _completed_calls(state)
    total     = len(completed)

    if total == 0:
        return {"total": 0, "message": "No completed trades yet."}

    # Overall
    overall_wr    = _win_rate(completed)
    avg_pnl       = sum(c.get("realized_pnl", 0) for c in completed) / total
    avg_held      = sum(c.get("held_hours", 0) for c in completed) / total
    dir_correct   = sum(1 for c in completed if c.get("correct_direction")) / total * 100

    # By ticker
    tickers = {}
    for c in completed:
        t = c["ticker"]
        tickers.setdefault(t, []).append(c)
    by_ticker = {
        t: {"wr": _win_rate(calls), "n": len(calls)}
        for t, calls in tickers.items()
    }

    # By confidence band
    bands = {"50-59": [], "60-69": [], "70-79": [], "80+": []}
    for c in completed:
        conf = c.get("confidence", 0)
        if   conf >= 80: bands["80+"].append(c)
        elif conf >= 70: bands["70-79"].append(c)
        elif conf >= 60: bands["60-69"].append(c)
        else:            bands["50-59"].append(c)
    by_confidence = {
        band: {"wr": _win_rate(calls), "n": len(calls)}
        for band, calls in bands.items()
    }

    # By trend label
    trends = {}
    for c in completed:
        t = c.get("trend_label", "UNKNOWN")
        trends.setdefault(t, []).append(c)
    by_trend = {
        t: {"wr": _win_rate(calls), "n": len(calls)}
        for t, calls in trends.items()
    }

    # By funding sentiment
    sentiments = {}
    for c in completed:
        s = c.get("funding_sentiment", "unknown")
        sentiments.setdefault(s, []).append(c)
    by_funding = {
        s: {"wr": _win_rate(calls), "n": len(calls)}
        for s, calls in sentiments.items()
    }

    # By OI trend
    oi_groups = {}
    for c in completed:
        o = c.get("oi_trend", "unknown")
        oi_groups.setdefault(o, []).append(c)
    by_oi = {
        o: {"wr": _win_rate(calls), "n": len(calls)}
        for o, calls in oi_groups.items()
    }

    return {
        "total":            total,
        "overall_wr":       overall_wr,
        "avg_pnl":          round(avg_pnl, 2),
        "avg_held_hours":   round(avg_held, 1),
        "direction_accuracy": round(dir_correct, 1),
        "by_ticker":        by_ticker,
        "by_confidence":    by_confidence,
        "by_trend":         by_trend,
        "by_funding":       by_funding,
        "by_oi":            by_oi,
    }


def get_ticker_summary(ticker: str) -> str:
    """
    Return a plain-English postmortem summary for a specific ticker.
    This string is passed into the research agent's context.
    """
    state     = _load()
    completed = [c for c in _completed_calls(state) if c["ticker"] == ticker]

    if len(completed) < MIN_SAMPLE:
        total_calls = sum(1 for c in state["calls"] if c["ticker"] == ticker)
        if total_calls == 0:
            return ""
        return f"We've called {ticker} {total_calls}x but fewer than {MIN_SAMPLE} have closed — no calibration yet."

    wr          = _win_rate(completed)
    avg_pnl     = sum(c.get("realized_pnl", 0) for c in completed) / len(completed)
    dir_correct = sum(1 for c in completed if c.get("correct_direction")) / len(completed) * 100

    # Recent calls
    recent = completed[-5:]
    recent_results = ", ".join(
        f"{'✅' if c['outcome']=='win' else '❌'}{c['verdict']}@{c.get('confidence',0)}"
        for c in recent
    )

    # Best/worst conditions
    trend_wr = {}
    for c in completed:
        t = c.get("trend_label", "?")
        trend_wr.setdefault(t, []).append(c)
    trend_summary = " | ".join(
        f"{t}: {_win_rate(calls)}% ({len(calls)})" if _win_rate(calls) is not None else f"{t}: <{MIN_SAMPLE}"
        for t, calls in sorted(trend_wr.items())
    )

    return (
        f"Golem history on {ticker} ({len(completed)} closed trades):\n"
        f"Win rate: {wr}% | Avg P&L: ${avg_pnl:+.2f} | Direction accuracy: {dir_correct:.0f}%\n"
        f"Recent 5: {recent_results}\n"
        f"Win rate by trend: {trend_summary}"
    )


def get_all_summaries() -> dict:
    """Return {ticker: summary_str} for all tickers with closed trades."""
    state     = _load()
    completed = _completed_calls(state)
    tickers   = set(c["ticker"] for c in completed)
    return {t: get_ticker_summary(t) for t in tickers}


def format_stats_telegram() -> str:
    """Format full stats for /kalshi_stats Telegram command."""
    stats = get_stats()
    if stats["total"] == 0:
        return "📊 *KALSHI POSTMORTEM*\n\nNo completed trades yet. Golem is still learning."

    lines = [
        "📊 *KALSHI POSTMORTEM*\n",
        f"Completed trades: {stats['total']}",
        f"Win rate: *{stats['overall_wr']}%*",
        f"Direction accuracy: {stats['direction_accuracy']}%",
        f"Avg P&L per trade: ${stats['avg_pnl']:+.2f}",
        f"Avg hold time: {stats['avg_held_hours']}h",
        "",
        "*By confidence band:*",
    ]
    for band, d in stats.get("by_confidence", {}).items():
        if d["n"] > 0:
            wr_str = f"{d['wr']}%" if d["wr"] is not None else "<5 trades"
            lines.append(f"  {band}: {wr_str} ({d['n']} trades)")

    lines.append("")
    lines.append("*By trend label:*")
    for trend, d in stats.get("by_trend", {}).items():
        if d["n"] > 0:
            wr_str = f"{d['wr']}%" if d["wr"] is not None else "<5 trades"
            lines.append(f"  {trend}: {wr_str} ({d['n']} trades)")

    lines.append("")
    lines.append("*By funding sentiment (contrarian):*")
    for s, d in stats.get("by_funding", {}).items():
        if d["n"] > 0:
            wr_str = f"{d['wr']}%" if d["wr"] is not None else "<5 trades"
            lines.append(f"  {s}: {wr_str} ({d['n']} trades)")

    lines.append("")
    lines.append("*Best assets (≥5 trades):*")
    for t, d in sorted(
        stats.get("by_ticker", {}).items(),
        key=lambda x: (x[1]["wr"] or 0), reverse=True
    ):
        if d["n"] >= MIN_SAMPLE and d["wr"] is not None:
            lines.append(f"  {t}: {d['wr']}% ({d['n']} trades)")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(format_stats_telegram())
