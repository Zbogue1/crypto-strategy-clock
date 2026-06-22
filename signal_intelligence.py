"""
signal_intelligence.py
======================
The "crystal ball" credibility engine.

Tracks which specific signals, news sources, and technical indicators
actually lead to profitable trades vs losses — then feeds that learned
credibility back into Claude before every prediction.

How it works:
  1. On BUY  → log_trade_entry(coin, key_signals, confidence)
               Saves which signals drove the entry.

  2. On SELL → log_trade_exit(profit_pct, won)
               Scores each of those signals as correct or misleading.

  3. Claude prompt → build_credibility_context()
               Returns a block of text summarizing reliable vs
               misleading signals, injected before Claude decides.

Over time Claude learns to weight CoinDesk differently than Reddit,
RSI signals differently than funding-rate signals, etc — all based
on actual trade outcomes, not guesswork.

Data file: signal_credibility.json
"""

import os
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("signal_intelligence")

CREDIBILITY_FILE = "signal_credibility.json"

# Minimum trades before we report credibility.
# Avoids overconfident ratings from 1-2 data points.
MIN_TRADES_FOR_RATING = 3


# ─── PERSISTENCE ──────────────────────────────────────────────────────────────

def _load() -> dict:
    """Load credibility data from disk."""
    if os.path.exists(CREDIBILITY_FILE):
        try:
            with open(CREDIBILITY_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load signal_credibility.json: {e}")
    return {
        "open_trade":   None,
        "signal_stats": {},
        "trade_count":  0,
        "last_updated": None,
    }


def _save(data: dict):
    """Save credibility data to disk."""
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(CREDIBILITY_FILE, "w") as f:
        json.dump(data, f, indent=2, default=str)


# ─── TRADE ENTRY LOGGING ──────────────────────────────────────────────────────

def log_trade_entry(coin: str, key_signals: list, confidence: int):
    """
    Call right after execute_buy() to save which signals drove this entry.

    key_signals comes from Claude's JSON response — the "key_signals" field:
    [
      {"source": "CoinDesk", "signal_type": "news",      "description": "ETF approval article"},
      {"source": "RSI",      "signal_type": "technical", "description": "RSI at 28, oversold bounce"},
      {"source": "Reddit",   "signal_type": "sentiment", "description": "High bullish post volume"},
    ]

    These get scored as reliable or misleading when the trade closes.
    """
    if not key_signals:
        log.info("Signal intelligence: no key_signals from Claude — skipping entry log")
        return

    data = _load()
    data["open_trade"] = {
        "coin":        coin,
        "entry_time":  datetime.now(timezone.utc).isoformat(),
        "key_signals": key_signals,
        "confidence":  confidence,
    }
    _save(data)
    sources = [s.get("source", "?") for s in key_signals]
    log.info(f"Signal intelligence: logged entry for {coin} | signals: {sources}")


# ─── TRADE EXIT SCORING ───────────────────────────────────────────────────────

def log_trade_exit(profit_pct: float, won: bool):
    """
    Call right after execute_sell() / check_stop_loss() / check_take_profit()
    returns a completed trade.

    Scores the signals from the open entry:
      - If trade WON  → each signal gets a WIN (it was reliable)
      - If trade LOST → each signal gets a LOSS (it was misleading this time)

    profit_pct: actual return, e.g. +8.5 or -3.2
    won:        True if net profitable
    """
    data       = _load()
    open_trade = data.get("open_trade")

    if not open_trade:
        log.info("Signal intelligence: no open trade on record — nothing to score")
        return

    coin        = open_trade.get("coin", "?")
    key_signals = open_trade.get("key_signals", [])

    if not key_signals:
        data["open_trade"] = None
        _save(data)
        return

    stats = data.get("signal_stats", {})

    for sig in key_signals:
        source      = sig.get("source", "unknown").strip()
        signal_type = sig.get("signal_type", "general").strip()
        key         = f"{source}__{signal_type}"

        if key not in stats:
            stats[key] = {
                "source":      source,
                "signal_type": signal_type,
                "wins":        0,
                "losses":      0,
                "total":       0,
                "win_rate":    0.0,
                "total_pct":   0.0,   # running sum of profit_pct
                "avg_return":  0.0,   # avg profit per trade when this signal fired
                "last_seen":   None,
            }

        entry                = stats[key]
        entry["total"]      += 1
        entry["total_pct"]   = round(entry.get("total_pct", 0.0) + profit_pct, 4)
        entry["avg_return"]  = round(entry["total_pct"] / entry["total"], 4)
        entry["last_seen"]   = datetime.now(timezone.utc).isoformat()

        if won:
            entry["wins"] += 1
        else:
            entry["losses"] += 1

        entry["win_rate"] = round(entry["wins"] / entry["total"], 3)

    result_str = f"WIN +{profit_pct:.1f}%" if won else f"LOSS {profit_pct:.1f}%"
    log.info(
        f"Signal intelligence: scored {len(key_signals)} signal(s) for {coin} → {result_str}"
    )

    data["signal_stats"] = stats
    data["trade_count"]  = data.get("trade_count", 0) + 1
    data["open_trade"]   = None   # clear — trade closed
    _save(data)


# ─── CREDIBILITY CONTEXT BUILDER ──────────────────────────────────────────────

def build_credibility_context() -> str:
    """
    Returns a text block for injection into Claude's system prompt.
    Summarizes which signals have been historically reliable vs misleading
    based on actual trade outcomes.

    Returns empty string if there's not enough data yet.
    """
    data  = _load()
    stats = data.get("signal_stats", {})

    if not stats:
        return ""   # not enough data yet — silent

    total_trades = data.get("trade_count", 0)

    # Split into rated (enough data) vs unrated (still collecting)
    rated   = [(k, v) for k, v in stats.items()
               if v.get("total", 0) >= MIN_TRADES_FOR_RATING]
    unrated = [(k, v) for k, v in stats.items()
               if v.get("total", 0) < MIN_TRADES_FOR_RATING]

    if not rated:
        # Early stage — just note we're tracking
        n_signals = len(stats)
        return (
            f"\n[SIGNAL LEARNING IN PROGRESS: {n_signals} signal type(s) tracked across "
            f"{total_trades} trade(s). Need {MIN_TRADES_FOR_RATING}+ trades per signal "
            f"before credibility ratings appear. Keep trading!]\n"
        )

    # Sort rated signals by win_rate descending
    rated.sort(key=lambda x: x[1]["win_rate"], reverse=True)

    reliable   = [(k, v) for k, v in rated if v["win_rate"] >= 0.65]
    unreliable = [(k, v) for k, v in rated if v["win_rate"] <  0.40]
    mixed      = [(k, v) for k, v in rated if 0.40 <= v["win_rate"] < 0.65]

    lines = [
        "",
        "━━ SIGNAL CREDIBILITY INTELLIGENCE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Learned from {total_trades} completed paper trade(s). "
        "Apply this to weight signals in your current analysis:",
    ]

    if reliable:
        lines.append("")
        lines.append("✅ RELIABLE signals — these have a strong track record (weight more heavily):")
        for k, v in reliable[:6]:
            lines.append(
                f"   • {v['source']} [{v['signal_type']}] — "
                f"{v['wins']}/{v['total']} trades won "
                f"({v['win_rate']*100:.0f}% win rate), "
                f"avg return {v['avg_return']:+.1f}%"
            )

    if unreliable:
        lines.append("")
        lines.append("❌ MISLEADING signals — historically unreliable (discount these):")
        for k, v in unreliable[:6]:
            lines.append(
                f"   • {v['source']} [{v['signal_type']}] — "
                f"only {v['wins']}/{v['total']} trades won "
                f"({v['win_rate']*100:.0f}% win rate), "
                f"avg return {v['avg_return']:+.1f}%"
            )

    if mixed:
        lines.append("")
        lines.append("⚠️  MIXED signals — use with caution:")
        for k, v in mixed[:4]:
            lines.append(
                f"   • {v['source']} [{v['signal_type']}] — "
                f"{v['wins']}/{v['total']} ({v['win_rate']*100:.0f}%)"
            )

    if unrated:
        lines.append("")
        still_tracking = ", ".join(
            f"{v['source']} ({v['total']} trade{'s' if v['total'] != 1 else ''})"
            for k, v in unrated[:5]
        )
        lines.append(f"📊 Still collecting data on: {still_tracking}")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("")

    return "\n".join(lines)


# ─── STATS SUMMARY ────────────────────────────────────────────────────────────

def get_signal_stats_summary() -> dict:
    """
    Returns the raw stats dict for display in the dashboard or logs.
    """
    data = _load()
    return {
        "trade_count":  data.get("trade_count", 0),
        "signal_count": len(data.get("signal_stats", {})),
        "open_trade":   data.get("open_trade"),
        "signal_stats": data.get("signal_stats", {}),
    }
