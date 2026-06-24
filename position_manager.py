#!/usr/bin/env python3
"""
position_manager.py — Multi-position lifecycle manager.

Tracks which phase the portfolio is in:

  HUNTING  — Has open position slots. Actively looking for new entries.
  HOLDING  — All MAX_POSITIONS slots are full. Managing existing positions only.

Mode is derived live from paper_portfolio.json so it never drifts out of sync.
position_state.json is written each cycle as a lightweight summary for the dashboard.
"""

import os
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("position_manager")

POSITION_FILE = "position_state.json"

# Import MAX_POSITIONS from paper_portfolio to keep them in sync
try:
    from paper_portfolio import MAX_POSITIONS, load_portfolio
except ImportError:
    MAX_POSITIONS = 3
    def load_portfolio():
        return {"cash": 1000.0, "holdings": []}


# ─── STATE I/O ────────────────────────────────────────────────────────────────

def load_position() -> dict:
    if os.path.exists(POSITION_FILE):
        try:
            with open(POSITION_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load position state: {e}")
    return _make_summary()


def save_position(state: dict):
    with open(POSITION_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _make_summary() -> dict:
    """Build a position_state.json summary from the live portfolio."""
    portfolio = load_portfolio()
    holdings  = portfolio.get("holdings", [])
    mode      = "HOLDING" if len(holdings) >= MAX_POSITIONS else "HUNTING"
    return {
        "mode":             mode,
        "positions":        holdings,
        "position_count":   len(holdings),
        "max_positions":    MAX_POSITIONS,
        "cash_available":   portfolio.get("cash", 0),
        "last_updated":     datetime.now(timezone.utc).isoformat(),
    }


# ─── MODE QUERY ───────────────────────────────────────────────────────────────

def get_mode() -> str:
    """
    Returns HUNTING (has open slots) or HOLDING (all slots full).
    Derived live from portfolio so it's always accurate.
    """
    portfolio = load_portfolio()
    holdings  = portfolio.get("holdings", [])
    return "HOLDING" if len(holdings) >= MAX_POSITIONS else "HUNTING"


def process_signal(signal: str, current_price: float, confidence: int,
                   coin_ticker: str = None) -> str:
    """
    Called after each trade execution. Saves an updated position summary
    to position_state.json and returns the current mode.
    """
    summary = _make_summary()
    save_position(summary)
    return summary["mode"]


# ─── CONTEXT FOR CLAUDE ───────────────────────────────────────────────────────

def get_position_context(current_price: float) -> dict:
    """
    Returns a dict injected into Claude's analysis payload each cycle.
    Shows all open positions with entry info so Claude can make HOLD/SELL decisions.
    current_price is BTC price (used as approximate fallback for non-BTC coins).
    """
    portfolio = load_portfolio()
    holdings  = portfolio.get("holdings", [])
    mode      = "HOLDING" if len(holdings) >= MAX_POSITIONS else "HUNTING"

    ctx = {
        "investment_mode":  mode,
        "position_count":   len(holdings),
        "max_positions":    MAX_POSITIONS,
        "slots_available":  MAX_POSITIONS - len(holdings),
        "cash_available":   round(portfolio.get("cash", 0), 2),
    }

    if holdings:
        positions_detail = []
        for h in holdings:
            entry = h["entry_price"]
            positions_detail.append({
                "coin_ticker":      h["coin_ticker"],
                "entry_price":      entry,
                "entry_date":       h["entry_date"],
                "entry_confidence": h.get("entry_confidence"),
                "allocated_usd":    h["allocated_usd"],
                "exit_target":      h.get("exit_target"),
                "stop_loss":        h.get("stop_loss"),
                "expected_days":    h.get("expected_days"),
            })
        ctx["open_positions"] = positions_detail
        tickers = ", ".join(h["coin_ticker"] for h in holdings)
        ctx["note"] = (
            f"Currently holding {len(holdings)}/{MAX_POSITIONS} positions: {tickers}. "
            f"For each held coin, evaluate whether to HOLD or include in sell_coins. "
            f"{'No new entries — all slots full.' if mode == 'HOLDING' else f'Can open {MAX_POSITIONS - len(holdings)} more position(s) if a strong setup appears.'}"
        )
    else:
        ctx["open_positions"] = []
        ctx["note"] = f"No positions held. {MAX_POSITIONS} slots available. Find the best entry opportunities."

    return ctx


def build_mode_prompt(mode: str, pos_ctx: dict) -> str:
    """
    Returns a mode-specific block appended to Claude's system prompt.
    Tells Claude exactly what role it's playing this cycle.
    """
    n       = pos_ctx.get("position_count", 0)
    slots   = pos_ctx.get("slots_available", MAX_POSITIONS)
    cash    = pos_ctx.get("cash_available", 0)
    positions = pos_ctx.get("open_positions", [])

    lines = [
        "━" * 55,
        f"PORTFOLIO STATE: {n}/{MAX_POSITIONS} positions open | ${cash:,.2f} cash",
        "━" * 55,
    ]

    if positions:
        lines.append("\nOPEN POSITIONS (evaluate each — HOLD or add to sell_coins):")
        for p in positions:
            lines.append(
                f"  • {p['coin_ticker']:6s} entry ${p['entry_price']:,.2f} | "
                f"target ${p.get('exit_target') or 0:,.2f} | "
                f"stop ${p.get('stop_loss') or 0:,.2f}"
            )

    lines.append("")

    if mode == "HOLDING":
        lines += [
            "ALL SLOTS FULL — focus on managing open positions.",
            "",
            "SELL when (for each held coin):",
            "  - Stop-loss is close or thesis has changed",
            "  - RSI overbought + bearish divergence",
            "  - Better opportunity exists that justifies rotation",
            "  - Target price reached or price action deteriorating",
            "",
            "signal must be: HOLD (keep everything) or BUY (rotate — sell something first)",
            "sell_coins: list any tickers to EXIT this cycle (can be [])",
        ]
    else:
        lines += [
            f"{slots} SLOT(S) AVAILABLE — manage existing + find new entry if warranted.",
            "",
            "For EXISTING positions: evaluate each coin — add to sell_coins if thesis broken.",
            "For NEW entries: find the best swing setup from candidates.",
            "",
            "SWING ENTRY criteria:",
            "  - Oversold RSI bounce, MACD bullish cross, volume breakout, or news catalyst",
            "  - Clear stop-loss level (defined downside)",
            "  - 5-20% upside target over days to 2 weeks",
            "",
            "signal: BUY (open new position) | HOLD (no new entry this cycle)",
            "sell_coins: list any tickers to EXIT this cycle (can be [])",
        ]

    return "\n".join(lines)


# ─── DISPLAY ──────────────────────────────────────────────────────────────────

def get_display_state(current_price: float) -> dict:
    ctx = get_position_context(current_price)
    ctx["raw_state"] = load_position()
    return ctx


# ─── MANUAL OVERRIDES ─────────────────────────────────────────────────────────

def reset_to_hunting():
    """Force-write a HUNTING summary (used after manual reset)."""
    save_position({
        "mode":           "HUNTING",
        "positions":      [],
        "position_count": 0,
        "max_positions":  MAX_POSITIONS,
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    })
    log.info("Position state reset to HUNTING.")
