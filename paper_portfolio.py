#!/usr/bin/env python3
"""
paper_portfolio.py — Virtual $1,000 paper trading engine.

The agent treats every signal as a REAL trade decision.
No real money moves — but every buy, hold, and sell is recorded
as if it were live, so the learning loop has honest performance data.

Design:
  • Starts with $1,000 cash
  • Holds up to MAX_POSITIONS simultaneous positions
  • Auto-buys on BUY / STRONG_BUY using confidence-weighted sizing
  • Auto-sells specific coins on SELL signal or stop-loss/take-profit breach
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
MAX_POSITIONS   = 3   # maximum simultaneous open positions

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
        "cash":                   STARTING_CASH,
        "holdings":               [],
        "realized_pnl":           0.0,
        "peak_value":             STARTING_CASH,
        "total_trades":           0,
        "winning_trades":         0,
        "losing_trades":          0,
        "total_fees":             0.0,
        "trade_history":          [],
        "started_at":             datetime.now(timezone.utc).isoformat(),
        # BTC benchmark — tracks what $1000 in BTC would be worth
        "btc_benchmark_price":    None,   # BTC price when portfolio started
        "btc_benchmark_units":    None,   # how many BTC $1000 would have bought
        # Bear market regime filter
        "consecutive_fear_cycles": 0,     # cycles where Fear & Greed < 20
    }


def load_portfolio() -> dict:
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                state = json.load(f)
            # ── Migrate from old single-holding format ──
            if "holding" in state and "holdings" not in state:
                old = state.pop("holding")
                state["holdings"] = [old] if old else []
                save_portfolio(state)
                log.info("Migrated portfolio: single holding → multi-holdings format")
            elif "holdings" not in state:
                state["holdings"] = []
            return state
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
    Returns a holding dict, or None if the trade was skipped.

    Skips if:
      - Already holding this coin
      - At MAX_POSITIONS
      - Confidence below 50%
      - Not enough cash (< $10)
    """
    state = load_portfolio()
    holdings = state.get("holdings", [])

    # Already holding this coin?
    if any(h["coin_ticker"] == coin_ticker for h in holdings):
        log.info(f"PAPER: Already holding {coin_ticker} — skip duplicate BUY")
        return None

    # At max positions?
    if len(holdings) >= MAX_POSITIONS:
        log.info(f"PAPER: At max {MAX_POSITIONS} positions — skip buy {coin_ticker}")
        return None

    alloc = _position_size(state["cash"], confidence)
    if alloc < 10:
        log.info(f"PAPER: Confidence {confidence}% too low or cash too thin — skip buy {coin_ticker}")
        return None

    fee       = round(alloc * TAKER_FEE, 4)
    alloc_net = alloc - fee
    units     = alloc_net / price

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
    state["holdings"].append(holding)
    save_portfolio(state)

    slots = f"[{len(state['holdings'])}/{MAX_POSITIONS} slots]"
    log.info(f"PAPER BUY  ▶ {coin_ticker}  ${price:,.4f}  ×{units:.4f} units  "
             f"(${alloc:.2f} at {confidence}% conf)  {slots}")
    return holding


def execute_sell(coin_ticker: str, price: float,
                 signal: str, reason: str = "signal") -> dict:
    """
    Auto-execute a virtual SELL of the named coin.
    Returns a completed trade record, or None if that coin isn't held.
    """
    state    = load_portfolio()
    holdings = state.get("holdings", [])
    h        = next((x for x in holdings if x["coin_ticker"] == coin_ticker), None)
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
        "hit_target":         bool(h.get("exit_target") and price >= h["exit_target"]),
        "hit_stop":           bool(h.get("stop_loss")   and price <= h["stop_loss"]),
        "result":             "WIN" if profit_pct > 0 else "LOSS",
    }

    # Remove this holding
    state["holdings"]    = [x for x in holdings if x["coin_ticker"] != coin_ticker]
    state["cash"]        = round(state["cash"] + net, 4)
    state["realized_pnl"]= round(state["realized_pnl"] + profit_usd, 4)
    state["total_trades"]+= 1
    state["total_fees"]  = round(state["total_fees"] + fee, 4)
    if profit_pct > 0:
        state["winning_trades"] += 1
    else:
        state["losing_trades"]  += 1

    # Update peak value (cash + remaining positions at cost basis)
    remaining_val = sum(x["allocated_usd"] for x in state["holdings"])
    total_val     = state["cash"] + remaining_val
    if total_val > state["peak_value"]:
        state["peak_value"] = total_val

    state["trade_history"].append(trade)
    save_portfolio(state)

    emoji = "✅" if profit_pct > 0 else "❌"
    slots = f"[{len(state['holdings'])}/{MAX_POSITIONS} slots]"
    log.info(f"PAPER SELL {emoji} {coin_ticker}  ${price:,.4f}  "
             f"{profit_pct:+.2f}%  (${profit_usd:+.2f})  reason={reason}  {slots}")
    return trade


def check_stop_loss(coin_ticker: str, current_price: float) -> dict:
    """
    Check stop-loss for a specific coin.
    Returns completed trade dict if triggered, else None.
    """
    state    = load_portfolio()
    holdings = state.get("holdings", [])
    h        = next((x for x in holdings if x["coin_ticker"] == coin_ticker), None)
    if not h or not h.get("stop_loss"):
        return None
    if current_price <= h["stop_loss"]:
        log.warning(f"PAPER: Stop-loss hit! {coin_ticker} "
                    f"${current_price:.4f} ≤ stop ${h['stop_loss']:.4f}")
        return execute_sell(coin_ticker, current_price, "STOP_LOSS", reason="stop_loss")
    return None


def check_time_exit(coin_ticker: str, current_price: float) -> dict:
    """
    Exit a position that has exceeded 1.5x its expected hold time with no target/stop hit.
    Prevents capital sitting idle in a stalled trade indefinitely.
    """
    state    = load_portfolio()
    holdings = state.get("holdings", [])
    h        = next((x for x in holdings if x["coin_ticker"] == coin_ticker), None)
    if not h or not h.get("expected_days"):
        return None
    days_held = _days_between(h["entry_date"], datetime.now(timezone.utc).isoformat())
    max_days  = h["expected_days"] * 1.5
    if days_held >= max_days:
        log.info(
            f"PAPER: Time exit — {coin_ticker} held {days_held:.0f}d "
            f"(expected {h['expected_days']}d, max {max_days:.0f}d)"
        )
        return execute_sell(coin_ticker, current_price, "HOLD", reason="time_exit")
    return None


def check_take_profit(coin_ticker: str, current_price: float) -> dict:
    """
    Check take-profit for a specific coin.
    Returns completed trade dict if triggered, else None.
    """
    state    = load_portfolio()
    holdings = state.get("holdings", [])
    h        = next((x for x in holdings if x["coin_ticker"] == coin_ticker), None)
    if not h or not h.get("exit_target"):
        return None
    if current_price >= h["exit_target"]:
        log.info(f"PAPER: Take-profit hit! {coin_ticker} "
                 f"${current_price:.4f} ≥ target ${h['exit_target']:.4f}")
        return execute_sell(coin_ticker, current_price, "TAKE_PROFIT", reason="take_profit")
    return None


# ─── PORTFOLIO VALUATION ──────────────────────────────────────────────────────

def get_portfolio_value(prices: dict = None) -> dict:
    """
    Returns a full snapshot of current portfolio value.
    prices: dict mapping ticker -> current_price for unrealized P&L computation.
    Also accepts a single float (legacy) for BTC-only backward compat.
    """
    state    = load_portfolio()
    holdings = state.get("holdings", [])
    if isinstance(prices, (int, float)):
        # Legacy: single price passed — try to map to first holding's ticker
        if holdings:
            prices = {holdings[0]["coin_ticker"]: prices}
        else:
            prices = {}
    prices = prices or {}

    total_holding_value = 0.0
    total_unrealized    = 0.0
    holdings_detail     = []

    for h in holdings:
        ticker    = h["coin_ticker"]
        curr      = prices.get(ticker)
        hv        = round(h["units"] * curr, 4)  if curr else 0.0
        u_usd     = round(hv - h["allocated_usd"], 4) if curr else 0.0
        u_pct     = round(u_usd / h["allocated_usd"] * 100, 2) if curr and h["allocated_usd"] else 0.0
        total_holding_value += hv if curr else h["allocated_usd"]  # cost basis if no price
        total_unrealized    += u_usd
        holdings_detail.append({
            **h,
            "current_value":   hv,
            "current_price":   curr,
            "unrealized_usd":  u_usd,
            "unrealized_pct":  u_pct,
        })

    total_value  = round(state["cash"] + total_holding_value, 4)
    total_return = round((total_value - STARTING_CASH) / STARTING_CASH * 100, 2)
    drawdown     = round((total_value - state["peak_value"]) / state["peak_value"] * 100, 2) \
                   if state["peak_value"] > 0 else 0.0

    n        = state["total_trades"]
    win_rate = round(state["winning_trades"] / n * 100, 1) if n > 0 else 0.0

    trades  = state.get("trade_history", [])
    avg_pnl = round(sum(t["profit_pct"] for t in trades) / len(trades), 2) if trades else 0.0
    best    = max((t["profit_pct"] for t in trades), default=0.0)
    worst   = min((t["profit_pct"] for t in trades), default=0.0)

    result = {
        "cash":             state["cash"],
        "holdings":         holdings_detail,               # full list
        "holding":          holdings_detail[0] if holdings_detail else None,  # backward compat
        "holding_value":    total_holding_value,
        "total_value":      total_value,
        "starting_cash":    STARTING_CASH,
        "realized_pnl":     state["realized_pnl"],
        "unrealized_usd":   round(total_unrealized, 4),
        "unrealized_pct":   round(total_unrealized / max(total_holding_value, 1) * 100, 2)
                            if total_holding_value > 0 else 0.0,
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
        "position_count":   len(holdings),
        "max_positions":    MAX_POSITIONS,
    }

    # BTC benchmark comparison
    btc_benchmark_price = state.get("btc_benchmark_price")
    btc_benchmark_units = state.get("btc_benchmark_units")
    if btc_benchmark_price and btc_benchmark_units:
        current_btc = prices.get("BTC") if isinstance(prices, dict) else None
        if current_btc:
            benchmark_value  = round(btc_benchmark_units * current_btc, 2)
            benchmark_return = round((benchmark_value - STARTING_CASH) / STARTING_CASH * 100, 2)
        else:
            benchmark_value  = None
            benchmark_return = None
        result["btc_benchmark_value"]  = benchmark_value
        result["btc_benchmark_return"] = benchmark_return
        result["btc_benchmark_price"]  = btc_benchmark_price
    else:
        result["btc_benchmark_value"]  = None
        result["btc_benchmark_return"] = None
        result["btc_benchmark_price"]  = None

    # Graduation readiness
    result["graduation"] = get_graduation_status()

    return result


# ─── DISPLAY HELPERS ──────────────────────────────────────────────────────────

def get_portfolio_summary_lines(prices: dict = None) -> list:
    """Returns a list of display-ready strings for terminal output."""
    p     = get_portfolio_value(prices)
    RESET = "\033[0m"

    ret_col = "\033[92m" if p["total_return_pct"] >= 0 else "\033[91m"
    lines = [
        f"  💼  Paper Portfolio  ·  Started $1,000",
        f"  Total value:  ${p['total_value']:,.2f}  "
        f"({ret_col}{p['total_return_pct']:+.2f}%{RESET}  overall)",
        f"  Cash:         ${p['cash']:,.2f}  "
        f"[{p['position_count']}/{p['max_positions']} positions]",
    ]

    for h in p["holdings"]:
        u_col = "\033[92m" if h["unrealized_pct"] >= 0 else "\033[91m"
        lines.append(
            f"  ▶ {h['coin_ticker']}  ×{h['units']:.4f}  "
            f"entry ${h['entry_price']:,.4f}  "
            f"now {u_col}{h['unrealized_pct']:+.1f}%{RESET}"
        )
        if h.get("exit_target"):
            lines.append(f"      Target ${h['exit_target']:,.4f}  |  Stop ${h.get('stop_loss', 0):,.4f}")

    if p["total_trades"] > 0:
        lines.append(
            f"  Trades: {p['total_trades']}  |  Win rate: {p['win_rate']:.0f}%  |  "
            f"Avg: {p['avg_trade_pct']:+.1f}%  |  "
            f"Best: +{p['best_trade_pct']:.1f}%  Worst: {p['worst_trade_pct']:+.1f}%"
        )
    else:
        lines.append("  Trades: 0  —  waiting for first signal")

    # BTC benchmark comparison
    if p.get("btc_benchmark_return") is not None:
        bret = p["btc_benchmark_return"]
        aret = p["total_return_pct"]
        diff = round(aret - bret, 2)
        diff_col = "\033[92m" if diff >= 0 else "\033[91m"
        lines.append(
            f"  vs BTC hold: agent {ret_col}{aret:+.2f}%{RESET}  "
            f"/ BTC {bret:+.2f}%  "
            f"({diff_col}{diff:+.2f}% edge{RESET})"
        )

    # Graduation readiness
    grad = p.get("graduation", {})
    grad_icon = "🎓" if grad.get("ready") else "📚"
    lines.append(
        f"  {grad_icon} Real-money readiness: {grad.get('score', '0/5')}  "
        f"({grad.get('days_running', 0):.0f} days running)"
    )

    return lines


def build_portfolio_html(prices: dict = None) -> str:
    """Returns an HTML card for the dashboard."""
    p = get_portfolio_value(prices)

    ret_col  = "#00c853" if p["total_return_pct"] >= 0 else "#f44336"
    fill_pct = min(max((p["total_value"] - STARTING_CASH) / STARTING_CASH * 100 + 50, 0), 100)

    holdings_html = ""
    for h in p["holdings"]:
        u_col = "#00c853" if h["unrealized_pct"] >= 0 else "#f44336"
        holdings_html += f"""
      <div class="port-row">
        <span class="port-label">{h['coin_ticker']}</span>
        <span class="port-val">
          {h['units']:.4f} units &nbsp;·&nbsp; entry ${h['entry_price']:,.4f} &nbsp;·&nbsp;
          <span style="color:{u_col}">{h['unrealized_pct']:+.1f}% unrealized</span>
        </span>
      </div>
      <div class="port-row">
        <span class="port-label">Targets</span>
        <span class="port-val">
          🎯 ${h.get('exit_target', 0):,.4f} &nbsp;&nbsp; 🛑 ${h.get('stop_loss', 0):,.4f}
        </span>
      </div>"""

    if not holdings_html:
        holdings_html = "<div style='color:#666;font-style:italic'>No open positions — hunting for entry.</div>"

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

    win_color   = "#00c853" if p["win_rate"] >= 50 else "#f44336"
    slots_color = "#ffab40" if p["position_count"] >= MAX_POSITIONS else "#00c853"

    # BTC benchmark HTML section
    btc_bret = p.get("btc_benchmark_return")
    if btc_bret is not None:
        aret     = p["total_return_pct"]
        edge     = round(aret - btc_bret, 2)
        btc_col  = "#00c853" if btc_bret >= 0 else "#f44336"
        edge_col = "#00c853" if edge >= 0 else "#f44336"
        benchmark_html = (
            '<div class="port-section-label">vs BTC Buy-and-Hold</div>'
            f'<div class="port-row"><span class="port-label">Agent</span>'
            f'<span class="port-val" style="color:{ret_col}">{aret:+.2f}%</span></div>'
            f'<div class="port-row"><span class="port-label">BTC hold</span>'
            f'<span class="port-val" style="color:{btc_col}">{btc_bret:+.2f}%</span></div>'
            f'<div class="port-row"><span class="port-label">Edge</span>'
            f'<span class="port-val" style="color:{edge_col}">{edge:+.2f}%</span></div>'
        )
    else:
        benchmark_html = ""

    # Graduation readiness HTML section
    grad      = p.get("graduation", {})
    grad_icon = "&#127891; READY" if grad.get("ready") else "&#128218; Learning"
    grad_html = (
        '<div class="port-section-label">Real-Money Readiness</div>'
        f'<div class="port-row"><span class="port-label">{grad_icon}</span>'
        f'<span class="port-val">{grad.get("score", "0/5")} criteria'
        f' &middot; {grad.get("days_running", 0):.0f} days running</span></div>'
    )

    return f"""
  <div class="card portfolio-card">
    <div class="card-label">&#128188; Paper Portfolio &mdash; Virtual $1,000</div>

    <div class="port-hero">
      <div class="port-total">${p['total_value']:,.2f}</div>
      <div class="port-return" style="color:{ret_col}">{p['total_return_pct']:+.2f}% total return</div>
    </div>

    <div class="port-bar-track">
      <div class="port-bar-fill" style="width:{fill_pct:.0f}%;background:{ret_col}"></div>
    </div>

    <div class="port-stats">
      <div class="port-stat"><div class="ps-val">${p['cash']:,.2f}</div><div class="ps-label">Cash</div></div>
      <div class="port-stat"><div class="ps-val">${p['realized_pnl']:+,.2f}</div><div class="ps-label">Realized P&amp;L</div></div>
      <div class="port-stat"><div class="ps-val" style="color:{win_color}">{p['win_rate']:.0f}%</div><div class="ps-label">Win Rate</div></div>
      <div class="port-stat"><div class="ps-val" style="color:{slots_color}">{p['position_count']}/{MAX_POSITIONS}</div><div class="ps-label">Positions</div></div>
    </div>

    <div class="port-section-label">Open Positions</div>
    {holdings_html}

    <div class="port-section-label">Last 5 Trades</div>
    {no_trades}
    <div class="trades-list">{trade_rows}</div>

    {benchmark_html}
    {grad_html}
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
        d1 = datetime.fromisoformat(date_str1.replace("Z", "+00:00"))
        d2 = datetime.fromisoformat(date_str2.replace("Z", "+00:00"))
        return round(abs((d2 - d1).total_seconds() / 86400), 2)
    except Exception:
        return 0.0


def reset_portfolio():
    """Wipe portfolio and start fresh with $1,000. USE WITH CAUTION."""
    save_portfolio(_default_state())
    log.info("Paper portfolio reset to $1,000.")


# ─── BTC BENCHMARK ────────────────────────────────────────────────────────────

def update_btc_benchmark(btc_price: float):
    """
    Record the BTC price at portfolio start so we can compare agent vs buy-and-hold.
    No-op after the first call — benchmark is set once and never updated.
    """
    if not btc_price or btc_price <= 0:
        return
    state = load_portfolio()
    if state.get("btc_benchmark_price"):
        return  # already set
    state["btc_benchmark_price"] = btc_price
    state["btc_benchmark_units"] = round(STARTING_CASH / btc_price, 8)
    save_portfolio(state)
    log.info(f"BTC benchmark set: ${btc_price:,.2f}  ({state['btc_benchmark_units']:.8f} BTC)")


# ─── BEAR MARKET REGIME FILTER ────────────────────────────────────────────────

def update_fear_counter(fear_value) -> int:
    """
    Track consecutive cycles where Fear & Greed index < 20 (extreme fear).
    Resets to 0 when fear_value >= 20. Returns current consecutive count.
    """
    state = load_portfolio()
    try:
        val = int(fear_value) if fear_value is not None else 50
    except (TypeError, ValueError):
        val = 50
    if val < 20:
        state["consecutive_fear_cycles"] = state.get("consecutive_fear_cycles", 0) + 1
    else:
        state["consecutive_fear_cycles"] = 0
    save_portfolio(state)
    return state["consecutive_fear_cycles"]


# ─── GRADUATION READINESS ─────────────────────────────────────────────────────

def get_graduation_status() -> dict:
    """
    Evaluate whether the agent is ready to graduate from paper trading to real money.
    Returns a dict with score (X/5), individual criteria, and a ready flag.

    Criteria:
      1. At least 10 completed trades
      2. Win rate > 50%
      3. Average trade return > 0%
      4. Portfolio running for at least 30 days
      5. No losing streak of 3+ consecutive trades in recent history
    """
    state    = load_portfolio()
    history  = state.get("trade_history", [])
    n_trades = state.get("total_trades", 0)
    wins     = state.get("winning_trades", 0)

    criteria = {}

    # 1. Trade count
    criteria["min_trades"] = n_trades >= 10

    # 2. Win rate
    win_rate = (wins / n_trades * 100) if n_trades > 0 else 0.0
    criteria["win_rate"] = win_rate > 50

    # 3. Average trade return positive
    if history:
        avg_pct = sum(t.get("profit_pct", 0) for t in history) / len(history)
    else:
        avg_pct = 0.0
    criteria["positive_avg"] = avg_pct > 0

    # 4. Running at least 30 days
    started = state.get("started_at", datetime.now(timezone.utc).isoformat())
    days_running = _days_between(started, datetime.now(timezone.utc).isoformat())
    criteria["days_30"] = days_running >= 30

    # 5. No streak of 3+ consecutive losses in recent 10 trades
    recent = history[-10:] if len(history) >= 10 else history
    max_streak = 0
    streak     = 0
    for t in recent:
        if t.get("profit_pct", 0) < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    criteria["no_bad_streak"] = max_streak < 3

    score     = sum(criteria.values())
    score_str = f"{score}/5"
    ready     = score == 5

    return {
        "score":        score_str,
        "ready":        ready,
        "criteria":     criteria,
        "days_running": days_running,
        "win_rate":     win_rate,
        "avg_trade":    round(avg_pct, 2),
        "n_trades":     n_trades,
    }
