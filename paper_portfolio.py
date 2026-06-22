#!/usr/bin/env python3
"""
paper_portfolio.py — Virtual $1,000 paper trading engine.

The agent treats every signal as a REAL trade decision.
No real money moves — but every buy, hold, and sell is recorded
as if it were live, so the learning loop has honest performance data.

Design:
  • Starts with $1,000 cash
  • Holds ONE position at a time (single best opportunity model)
  • Auto-buys on BUY / STRONG_BUY using confidence-weighted sizing
  • Auto-sells on SELL / STRONG_SELL (or stop-loss breach)
  • Enforces stop-loss on every cycle automatically
  • Full trade history saved to paper_portfolio.json

Position sizing (confidence-based):
  ≥ 80% confidence  →  40% of available cash
  60–79% confidence →  25% of available cash
  50–59% confidence →  15% of available cash (learning-mode small bet)
  < 50% confidence  →  skip (signal not strong enough to act)
"""

import os
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger("paper_portfolio")

PORTFOLIO_FILE  = "paper_portfolio.json"
STARTING_CASH   = 1_000.0

# Confidence → fraction of bankroll to risk
SIZING_TIERS = [
    (80, 0.40),   # ≥80% confidence → 40% of cash
    (60, 0.25),   # ≥60%            → 25% of cash
    (50, 0.15),   # ≥50%            → 15% of cash (learning mode — smaller bet)
    (0,  0.00),   # <50%            → skip
]


# ─── STATE I/O ────────────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "cash":            STARTING_CASH,
        "holding":         None,       # dict or None — only one position at a time
        "realized_pnl":    0.0,
        "peak_value":      STARTING_CASH,
        "total_trades":    0,
        "winning_trades":  0,
        "losing_trades":   0,
        "total_fees":      0.0,        # simulated 0.1% taker fee per side
        "trade_history":   [],
        "started_at":      datetime.now(timezone.utc).isoformat(),
    }


def load_portfolio() -> dict:
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Could not load portfolio: {e}")
    state = _default_state()
    save_portfolio(state)
    return state


def save_portfolio(state: dict):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


# ─── POSITION SIZING ──────────────────────────────────────────────────────────

def _position_size(cash: float, confidence: int) -> float:
    """Returns dollar amount to allocate based on confidence level."""
    for threshold, fraction in SIZING_TIERS:
        if confidence >= threshold:
            return round(cash * fraction, 2)
    return 0.0


# ─── TRADE EXECUTION ──────────────────────────────────────────────────────────

TAKER_FEE = 0.001   # 0.1% per side — realistic for most exchanges

def execute_buy(coin_ticker: str, coin_id: str, price: float,
                signal: str, confidence: int,
                entry_target: float = None, exit_target: float = None,
                stop_loss: float = None, expected_days: int = None) -> dict:
    """
    Auto-execute a virtual BUY.
    Returns a trade record dict, or None if the trade was skipped.

    Skips if:
      - Already holding a position
      - Confidence below 60%
      - Not enough cash (< $10)
    """
    state = load_portfolio()

    # Already holding — one position at a time
    if state["holding"]:
        held = state["holding"]["coin_ticker"]
        log.info(f"PAPER: Already holding {held} — skip BUY signal for {coin_ticker}")
        return None

    alloc = _position_size(state["cash"], confidence)
    if alloc < 10:
        log.info(f"PAPER: Confidence {confidence}% too low or cash too thin — skip buy {coin_ticker}")
        return None

    fee      = round(alloc * TAKER_FEE, 4)
    alloc_net = alloc - fee
    units    = alloc_net / price

    state["cash"]       = round(state["cash"] - alloc, 4)
    state["total_fees"] = round(state["total_fees"] + fee, 4)

    holding = {
        "coin_ticker":        coin_ticker,
        "coin_id":            coin_id,
        "units":              units,
        "entry_price":        price,
        "entry_date":         datetime.now(timezone.utc).isoformat(),
        "entry_signal":       signal,
        "entry_confidence":   confidence,
        "allocated_usd":      alloc,
        "fee_paid":           fee,
        "exit_target":        exit_target,
        "stop_loss":          stop_loss,
        "entry_target":       entry_target,
        "expected_days":      expected_days,
    }
    state["holding"] = holding
    save_portfolio(state)

    log.info(f"PAPER BUY  ▶ {coin_ticker}  ${price:,.4f}  ×{units:.4f} units  "
             f"(${alloc:.2f} at {confidence}% conf)")
    return holding


def execute_sell(price: float, signal: str, reason: str = "signal") -> dict:
    """
    Auto-execute a virtual SELL of the current holding.
    Returns a completed trade record, or None if no position held.
    """
    state = load_portfolio()
    h = state.get("holding")
    if not h:
        return None

    fee        = round(h["units"] * price * TAKER_FEE, 4)
    gross      = round(h["units"] * price, 4)
    net        = round(gross - fee, 4)
    profit_usd = round(net - h["allocated_usd"], 4)
    profit_pct = round(profit_usd / h["allocated_usd"] * 100, 2)
    days_held  = _days_between(h["entry_date"], datetime.now(timezone.utc).isoformat())

    trade = {
        "trade_num":          state["total_trades"] + 1,
        "coin_ticker":        h["coin_ticker"],
        "coin_id":            h["coin_id"],
        "entry_price":        h["entry_price"],
        "entry_date":         h["entry_date"],
        "entry_signal":       h["entry_signal"],
        "entry_confidence":   h["entry_confidence"],
        "allocated_usd":      h["allocated_usd"],
        "exit_price":         price,
        "exit_date":          datetime.now(timezone.utc).isoformat(),
        "exit_signal":        signal,
        "exit_reason":        reason,
        "units":              h["units"],
        "gross_proceeds":     gross,
        "fees_total":         round(h["fee_paid"] + fee, 4),
        "net_proceeds":       net,
        "profit_usd":         profit_usd,
        "profit_pct":         profit_pct,
        "days_held":          days_held,
        "exit_target":        h.get("exit_target"),
        "stop_loss":          h.get("stop_loss"),
        "hit_target":         h.get("exit_target") and price >= h["exit_target"],
        "hit_stop":           h.get("stop_loss") and price <= h["stop_loss"],
        "result":             "WIN" if profit_pct > 0 else "LOSS",
    }

    state["cash"]           = round(state["cash"] + net, 4)
    state["realized_pnl"]   = round(state["realized_pnl"] + profit_usd, 4)
    state["total_trades"]  += 1
    state["total_fees"]     = round(state["total_fees"] + fee, 4)
    if profit_pct > 0:
        state["winning_trades"] += 1
    else:
        state["losing_trades"]  += 1

    total_val = state["cash"]  # no open position after sell
    if total_val > state["peak_value"]:
        state["peak_value"] = total_val

    state["trade_history"].append(trade)
    state["holding"] = None
    save_portfolio(state)

    emoji = "✅" if profit_pct > 0 else "❌"
    log.info(f"PAPER SELL {emoji} {h['coin_ticker']}  ${price:,.4f}  "
             f"{profit_pct:+.2f}%  (${profit_usd:+.2f})  reason={reason}")
    return trade


def check_stop_loss(current_price: float) -> dict:
    """
    Called every cycle. If current price breaches the stop-loss, auto-sells.
    Returns completed trade dict if stop triggered, else None.
    """
    state = load_portfolio()
    h = state.get("holding")
    if not h or not h.get("stop_loss"):
        return None

    if current_price <= h["stop_loss"]:
        log.warning(f"PAPER: Stop-loss hit! {h['coin_ticker']} "
                    f"${current_price:.4f} ≤ stop ${h['stop_loss']:.4f}")
        return execute_sell(current_price, "STOP_LOSS", reason="stop_loss")
    return None


def check_take_profit(current_price: float) -> dict:
    """
    Called every cycle. If price hits exit target, auto-sells.
    Returns completed trade dict if target hit, else None.
    """
    state = load_portfolio()
    h = state.get("holding")
    if not h or not h.get("exit_target"):
        return None

    if current_price >= h["exit_target"]:
        log.info(f"PAPER: Take-profit hit! {h['coin_ticker']} "
                 f"${current_price:.4f} ≥ target ${h['exit_target']:.4f}")
        return execute_sell(current_price, "TAKE_PROFIT", reason="take_profit")
    return None


# ─── PORTFOLIO VALUATION ──────────────────────────────────────────────────────

def get_portfolio_value(current_price: float = None) -> dict:
    """
    Returns a full snapshot of current portfolio value.
    Pass current_price to compute unrealized P&L on open position.
    """
    state = load_portfolio()
    h     = state.get("holding")

    unrealized_usd = 0.0
    unrealized_pct = 0.0
    holding_value  = 0.0

    if h and current_price:
        holding_value  = round(h["units"] * current_price, 4)
        unrealized_usd = round(holding_value - h["allocated_usd"], 4)
        unrealized_pct = round(unrealized_usd / h["allocated_usd"] * 100, 2)

    total_value   = round(state["cash"] + holding_value, 4)
    total_return  = round((total_value - STARTING_CASH) / STARTING_CASH * 100, 2)
    drawdown      = round((total_value - state["peak_value"]) / state["peak_value"] * 100, 2) \
                    if state["peak_value"] > 0 else 0.0

    n       = state["total_trades"]
    win_rate = round(state["winning_trades"] / n * 100, 1) if n > 0 else 0.0

    # Average profit per completed trade
    trades  = state.get("trade_history", [])
    avg_pnl = round(sum(t["profit_pct"] for t in trades) / len(trades), 2) if trades else 0.0
    best    = max((t["profit_pct"] for t in trades), default=0.0)
    worst   = min((t["profit_pct"] for t in trades), default=0.0)

    return {
        "cash":             state["cash"],
        "holding":          h,
        "holding_value":    holding_value,
        "total_value":      total_value,
        "starting_cash":    STARTING_CASH,
        "realized_pnl":     state["realized_pnl"],
        "unrealized_usd":   unrealized_usd,
        "unrealized_pct":   unrealized_pct,
        "total_return_pct": total_return,
        "peak_value":       state["peak_value"],
        "drawdown_pct":     drawdown,
        "total_trades":     n,
        "winning_trades":   state["winning_trades"],
        "losing_trades":    state["losing_trades"],
        "win_rate":         win_rate,
        "avg_trade_pct":    avg_pnl,
        "best_trade_pct":   best,
        "worst_trade_pct":  worst,
        "total_fees":       state["total_fees"],
        "trade_history":    trades,
    }


# ─── DISPLAY HELPERS ──────────────────────────────────────────────────────────

def get_portfolio_summary_lines(current_price: float = None) -> list:
    """Returns a list of display-ready strings for terminal output."""
    p = get_portfolio_value(current_price)
    h = p["holding"]

    ret_col  = "\033[92m" if p["total_return_pct"] >= 0 else "\033[91m"
    RESET    = "\033[0m"

    lines = [
        f"  💼  Paper Portfolio  ·  Started $1,000",
        f"  Total value:  ${p['total_value']:,.2f}  "
        f"({ret_col}{p['total_return_pct']:+.2f}%{RESET}  overall)",
        f"  Cash:         ${p['cash']:,.2f}",
    ]

    if h:
        u_col = "\033[92m" if p["unrealized_pct"] >= 0 else "\033[91m"
        lines.append(
            f"  Holding:      {h['coin_ticker']}  ×{h['units']:.4f} units  "
            f"entry ${h['entry_price']:,.4f}  "
            f"now {u_col}{p['unrealized_pct']:+.1f}%{RESET}"
        )
        if h.get("exit_target"):
            lines.append(f"  Target:       ${h['exit_target']:,.4f}  |  "
                         f"Stop: ${h.get('stop_loss', 0):,.4f}")

    if p["total_trades"] > 0:
        lines.append(
            f"  Trades: {p['total_trades']}  |  Win rate: {p['win_rate']:.0f}%  |  "
            f"Avg: {p['avg_trade_pct']:+.1f}%  |  "
            f"Best: +{p['best_trade_pct']:.1f}%  Worst: {p['worst_trade_pct']:+.1f}%"
        )
    else:
        lines.append("  Trades: 0  —  waiting for first signal")

    return lines


def build_portfolio_html(current_price: float = None) -> str:
    """Returns an HTML card for the dashboard."""
    p = get_portfolio_value(current_price)
    h = p["holding"]

    ret_col  = "#00c853" if p["total_return_pct"] >= 0 else "#f44336"
    u_col    = "#00c853" if p["unrealized_pct"] >= 0 else "#f44336"
    fill_pct = min(max((p["total_value"] - STARTING_CASH) / STARTING_CASH * 100 + 50, 0), 100)

    holding_html = ""
    if h:
        holding_html = f"""
      <div class="port-row">
        <span class="port-label">Holding</span>
        <span class="port-val">
          {h['coin_ticker']} &nbsp;·&nbsp; {h['units']:.4f} units &nbsp;·&nbsp;
          entry ${h['entry_price']:,.4f} &nbsp;·&nbsp;
          <span style="color:{u_col}">{p['unrealized_pct']:+.1f}% unrealized</span>
        </span>
      </div>
      <div class="port-row">
        <span class="port-label">Targets</span>
        <span class="port-val">
          🎯 Exit ${h.get('exit_target', 0):,.4f} &nbsp;&nbsp;
          🛑 Stop ${h.get('stop_loss', 0):,.4f}
        </span>
      </div>"""

    trade_rows = ""
    for t in reversed(p["trade_history"][-5:]):
        col  = "#00c853" if t["profit_pct"] > 0 else "#f44336"
        icon = "✅" if t["profit_pct"] > 0 else "❌"
        trade_rows += f"""
      <div class="trade-row">
        <span class="trade-coin">{icon} {t['coin_ticker']}</span>
        <span class="trade-dates">{t['entry_date'][:10]} → {t['exit_date'][:10]}</span>
        <span class="trade-pnl" style="color:{col}">{t['profit_pct']:+.2f}%</span>
        <span class="trade-usd" style="color:{col}">${t['profit_usd']:+.2f}</span>
        <span class="trade-reason">{t['exit_reason']}</span>
      </div>"""

    no_trades = "<div style='color:#666;font-style:italic'>No completed trades yet — waiting for first signal.</div>" \
                if not p["trade_history"] else ""

    win_color = "#00c853" if p["win_rate"] >= 50 else "#f44336"

    return f"""
  <div class="card portfolio-card">
    <div class="card-label">💼 Paper Portfolio — Virtual $1,000</div>

    <div class="port-hero">
      <div class="port-total">${p['total_value']:,.2f}</div>
      <div class="port-return" style="color:{ret_col}">{p['total_return_pct']:+.2f}% total return</div>
    </div>

    <div class="port-bar-track">
      <div class="port-bar-fill" style="width:{fill_pct:.0f}%;background:{ret_col}"></div>
    </div>

    <div class="port-stats">
      <div class="port-stat"><div class="ps-val">${p['cash']:,.2f}</div><div class="ps-label">Cash</div></div>
      <div class="port-stat"><div class="ps-val">${p['realized_pnl']:+,.2f}</div><div class="ps-label">Realized P&L</div></div>
      <div class="port-stat"><div class="ps-val" style="color:{win_color}">{p['win_rate']:.0f}%</div><div class="ps-label">Win Rate</div></div>
      <div class="port-stat"><div class="ps-val">{p['total_trades']}</div><div class="ps-label">Trades</div></div>
    </div>

    {holding_html}

    <div class="port-section-label">Last 5 Trades</div>
    {no_trades}
    <div class="trades-list">{trade_rows}</div>
  </div>

  <style>
    .portfolio-card {{ background: #1a1a2e; border: 1px solid #2a2a4a; }}
    .port-hero {{ text-align:center; padding: 12px 0 4px; }}
    .port-total {{ font-size:2.2rem; font-weight:700; color:#fff; }}
    .port-return {{ font-size:1rem; margin-top:2px; }}
    .port-bar-track {{ height:6px; background:#2a2a4a; border-radius:3px; margin:10px 0; }}
    .port-bar-fill {{ height:100%; border-radius:3px; transition:width .4s; }}
    .port-stats {{ display:flex; gap:8px; margin:12px 0; }}
    .port-stat {{ flex:1; background:#12122a; border-radius:8px; padding:8px; text-align:center; }}
    .ps-val {{ font-size:1.1rem; font-weight:700; color:#e0e0e0; }}
    .ps-label {{ font-size:.7rem; color:#888; margin-top:2px; }}
    .port-row {{ display:flex; gap:12px; margin:6px 0; font-size:.85rem; }}
    .port-label {{ color:#888; min-width:60px; }}
    .port-val {{ color:#ccc; }}
    .port-section-label {{ font-size:.75rem; color:#555; text-transform:uppercase;
                            letter-spacing:.08em; margin:14px 0 6px; }}
    .trades-list {{ display:flex; flex-direction:column; gap:4px; }}
    .trade-row {{ display:flex; gap:10px; font-size:.82rem; align-items:center;
                  padding:4px 0; border-bottom:1px solid #1e1e3a; }}
    .trade-coin {{ min-width:60px; font-weight:600; }}
    .trade-dates {{ color:#666; font-size:.75rem; flex:1; }}
    .trade-pnl {{ font-weight:700; min-width:55px; text-align:right; }}
    .trade-usd {{ min-width:60px; text-align:right; }}
    .trade-reason {{ color:#555; font-size:.75rem; }}
  </style>"""


# ─── UTILITIES ────────────────────────────────────────────────────────────────

def _days_between(date_str1: str, date_str2: str) -> float:
    try:
        from datetime import datetime, timezone
        d1 = datetime.fromisoformat(date_str1.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(date_str2.replace("Z", "+00:00"))
        return round(abs((d2 - d1).total_seconds() / 86400), 2)
    except Exception:
        return 0.0


def reset_portfolio():
    """Wipe portfolio and start fresh with $1,000. USE WITH CAUTION."""
    save_portfolio(_default_state())
    log.info("Paper portfolio reset to $1,000.")
