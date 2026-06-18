#!/usr/bin/env python3
"""
dashboard_server.py — Crypto Strategy Clock local dashboard.

Run:  python dashboard_server.py
Then open: http://localhost:8888

Or just double-click "Open Dashboard.bat"
"""

import os, json, math, webbrowser, threading, time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
PORT = 8888

# ─── DATA READERS ─────────────────────────────────────────────────────────────

def read_json(filename, default=None):
    path = os.path.join(PROJECT_DIR, filename)
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default or {}

def read_signal_history(n=40):
    path = os.path.join(PROJECT_DIR, "crypto_history.jsonl")
    if not os.path.exists(path):
        return []
    rows = []
    with open(path) as f:
        for line in f:
            try:
                rows.append(json.loads(line.strip()))
            except Exception:
                pass
    return rows[-n:]

def build_api_data():
    port  = read_json("paper_portfolio.json")
    pos   = read_json("position_state.json")
    hist  = read_signal_history(40)
    last  = hist[-1] if hist else {}

    # Compute portfolio value history from trade history
    trades     = port.get("trade_history", [])
    val_labels = []
    val_data   = []
    running    = 1000.0
    for t in trades:
        running += t.get("profit_usd", 0)
        val_labels.append(t.get("exit_date", "")[:10])
        val_data.append(round(running, 2))

    # Holding P&L if open position exists
    holding      = port.get("holding")
    unrealized   = 0.0
    unrealized_pct = 0.0
    if holding and last.get("price"):
        curr = last["price"]
        if holding.get("units") and holding.get("allocated_usd"):
            gross = holding["units"] * curr
            unrealized = round(gross - holding["allocated_usd"], 2)
            unrealized_pct = round(unrealized / holding["allocated_usd"] * 100, 2)

    # Overall stats
    cash         = port.get("cash", 1000.0)
    hold_val     = (holding["units"] * last["price"]) if holding and last.get("price") else 0.0
    total_val    = round(cash + hold_val, 2)
    total_ret    = round((total_val - 1000) / 1000 * 100, 2)
    n_trades     = port.get("total_trades", 0)
    winners      = port.get("winning_trades", 0)
    win_rate     = round(winners / n_trades * 100, 1) if n_trades else 0
    avg_pnl      = round(sum(t["profit_pct"] for t in trades) / len(trades), 2) if trades else 0
    best         = max((t["profit_pct"] for t in trades), default=0)
    worst        = min((t["profit_pct"] for t in trades), default=0)
    realized_pnl = port.get("realized_pnl", 0)

    # Signal history for chart
    sig_labels   = [r.get("ts", "")[:10] for r in hist[-20:]]
    sig_conf     = [r.get("confidence", 0) for r in hist[-20:]]
    sig_signals  = [r.get("signal", "HOLD") for r in hist[-20:]]
    sig_colors   = []
    for s in sig_signals:
        if s in ("BUY", "STRONG_BUY"):   sig_colors.append("#00c853")
        elif s in ("SELL", "STRONG_SELL"): sig_colors.append("#f44336")
        else:                              sig_colors.append("#ff9800")

    return {
        "last_run":        last.get("ts", "Never"),
        "current_signal":  last.get("signal", "—"),
        "current_coin":    last.get("coin", "—"),
        "current_price":   last.get("price", 0),
        "current_conf":    last.get("confidence", 0),
        "investment_mode": pos.get("mode", "HUNTING"),
        "coin_ticker_held":pos.get("coin_ticker"),

        "portfolio": {
            "cash":          round(cash, 2),
            "total_value":   total_val,
            "total_return":  total_ret,
            "realized_pnl":  round(realized_pnl, 2),
            "unrealized":    unrealized,
            "unrealized_pct":unrealized_pct,
            "n_trades":      n_trades,
            "win_rate":      win_rate,
            "avg_pnl":       avg_pnl,
            "best":          round(best, 2),
            "worst":         round(worst, 2),
            "holding":       holding,
        },

        "charts": {
            "val_labels":    val_labels,
            "val_data":      val_data,
            "sig_labels":    sig_labels,
            "sig_conf":      sig_conf,
            "sig_colors":    sig_colors,
        },

        "trade_history":   list(reversed(trades[-10:])),
        "signal_history":  list(reversed(hist[-8:])),
    }


# ─── HTML TEMPLATE ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>🔮 Crypto Strategy Clock</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  :root {
    --bg:       #07071a;
    --bg2:      #0e0e2a;
    --bg3:      #141430;
    --card:     #111128;
    --border:   #1e1e42;
    --accent:   #5c6bc0;
    --green:    #00e676;
    --red:      #ff5252;
    --orange:   #ffab40;
    --text:     #e8eaf6;
    --muted:    #5c6080;
    --dim:      #3a3a6a;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body { height: 100%; background: var(--bg); color: var(--text);
               font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; }

  /* ── SCROLLBAR ── */
  ::-webkit-scrollbar { width: 6px; }
  ::-webkit-scrollbar-track { background: var(--bg2); }
  ::-webkit-scrollbar-thumb { background: var(--dim); border-radius: 3px; }

  /* ── LAYOUT ── */
  .app { display: flex; flex-direction: column; min-height: 100vh; }
  .topbar {
    background: linear-gradient(135deg, #0d0d2b 0%, #131340 100%);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex; align-items: center; justify-content: space-between;
    position: sticky; top: 0; z-index: 100;
  }
  .topbar-left { display: flex; align-items: center; gap: 14px; }
  .logo { font-size: 1.4rem; font-weight: 800; letter-spacing: -.02em;
          background: linear-gradient(90deg, #7c83ff, #00e5ff);
          -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .version { font-size: .72rem; color: var(--muted); background: var(--bg3);
             padding: 2px 8px; border-radius: 20px; border: 1px solid var(--border); }
  .topbar-right { display: flex; align-items: center; gap: 16px; font-size: .8rem; }
  .last-run { color: var(--muted); }
  .last-run span { color: var(--text); }
  .refresh-btn {
    background: var(--bg3); border: 1px solid var(--border); color: var(--text);
    padding: 6px 14px; border-radius: 8px; cursor: pointer; font-size: .8rem;
    transition: all .2s;
  }
  .refresh-btn:hover { background: var(--accent); border-color: var(--accent); }

  /* ── GRID ── */
  .grid { display: grid; grid-template-columns: repeat(12, 1fr);
          gap: 16px; padding: 20px 24px; flex: 1; }
  .col-4 { grid-column: span 4; }
  .col-3 { grid-column: span 3; }
  .col-6 { grid-column: span 6; }
  .col-8 { grid-column: span 8; }
  .col-12{ grid-column: span 12; }

  /* ── CARD ── */
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 14px; padding: 18px; overflow: hidden; position: relative;
  }
  .card-label {
    font-size: .7rem; text-transform: uppercase; letter-spacing: .1em;
    color: var(--muted); margin-bottom: 12px;
  }

  /* ── SIGNAL HERO ── */
  .signal-hero {
    background: linear-gradient(135deg, #0e0e30 0%, #131350 100%);
    border: 1px solid var(--border);
  }
  .signal-coin { font-size: 1rem; color: var(--muted); margin-bottom: 4px; }
  .signal-price { font-size: 1.8rem; font-weight: 700; color: var(--text); }
  .signal-badge {
    display: inline-block; padding: 6px 20px; border-radius: 30px;
    font-size: 1.4rem; font-weight: 800; letter-spacing: .1em;
    margin: 12px 0; text-transform: uppercase;
  }
  .badge-buy  { background: #00e67622; color: var(--green); border: 2px solid var(--green);
                box-shadow: 0 0 20px #00e67640; animation: pulse-green 2s infinite; }
  .badge-hold { background: #ffab4022; color: var(--orange); border: 2px solid var(--orange); }
  .badge-sell { background: #ff525222; color: var(--red);   border: 2px solid var(--red);
                box-shadow: 0 0 20px #ff525240; animation: pulse-red 2s infinite; }
  .badge-wait { background: #ff525215; color: #ff7070;     border: 2px solid #ff5252aa; }
  @keyframes pulse-green {
    0%,100% { box-shadow: 0 0 15px #00e67640; }
    50%      { box-shadow: 0 0 35px #00e67680; }
  }
  @keyframes pulse-red {
    0%,100% { box-shadow: 0 0 15px #ff525240; }
    50%      { box-shadow: 0 0 35px #ff525270; }
  }
  .conf-row { display: flex; align-items: center; gap: 10px; margin-top: 6px; }
  .conf-label { font-size: .78rem; color: var(--muted); }
  .conf-track { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
  .conf-fill  { height: 100%; border-radius: 3px; transition: width .6s; }
  .conf-num   { font-size: .85rem; font-weight: 700; min-width: 36px; text-align: right; }
  .mode-badge {
    display: inline-block; padding: 3px 10px; border-radius: 20px;
    font-size: .7rem; font-weight: 600; letter-spacing: .06em;
    margin-top: 10px;
  }
  .mode-hunting { background: #5c6bc022; color: #7c83ff; border: 1px solid #5c6bc055; }
  .mode-holding { background: #00e67622; color: var(--green); border: 1px solid #00e67655; }
  .mode-waiting { background: #ffab4022; color: var(--orange); border: 1px solid #ffab4055; }

  /* ── PORTFOLIO VALUE ── */
  .port-total { font-size: 2.4rem; font-weight: 800; line-height: 1; }
  .port-ret   { font-size: 1rem; margin-top: 4px; font-weight: 600; }
  .port-bar-track { height: 5px; background: var(--bg3); border-radius: 3px; margin: 12px 0; }
  .port-bar-fill  { height: 100%; border-radius: 3px; transition: width .6s; }
  .stat-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
  .stat-item { background: var(--bg3); border-radius: 10px; padding: 10px; text-align: center; }
  .stat-val  { font-size: 1.1rem; font-weight: 700; }
  .stat-lbl  { font-size: .68rem; color: var(--muted); margin-top: 2px; }

  /* ── POSITION CARD ── */
  .pos-row { display: flex; justify-content: space-between; align-items: center;
             padding: 8px 0; border-bottom: 1px solid var(--border); }
  .pos-row:last-child { border-bottom: none; }
  .pos-key   { color: var(--muted); font-size: .82rem; }
  .pos-val   { font-weight: 600; font-size: .9rem; }
  .target-row{ display: flex; gap: 10px; margin-top: 10px; }
  .target-pill {
    flex: 1; text-align: center; padding: 6px; border-radius: 8px;
    font-size: .78rem; font-weight: 600;
  }
  .target-exit { background: #00e67618; color: var(--green); border: 1px solid #00e67640; }
  .target-stop { background: #ff525218; color: var(--red);   border: 1px solid #ff525240; }
  .no-position { text-align: center; color: var(--muted); padding: 20px 0;
                  font-size: .88rem; line-height: 1.6; }

  /* ── CHART ── */
  .chart-wrap { position: relative; height: 160px; margin-top: 8px; }

  /* ── TRADE HISTORY ── */
  .trade-row {
    display: grid;
    grid-template-columns: 28px 52px 1fr 60px 60px 70px;
    gap: 8px; padding: 8px 4px; border-bottom: 1px solid var(--border);
    align-items: center; font-size: .82rem;
  }
  .trade-row:last-child { border-bottom: none; }
  .trade-icon { font-size: 1rem; text-align: center; }
  .trade-coin { font-weight: 700; }
  .trade-dates{ color: var(--muted); font-size: .72rem; }
  .trade-pnl  { font-weight: 700; text-align: right; }
  .trade-usd  { text-align: right; }
  .trade-why  { color: var(--muted); font-size: .72rem; text-align: right; }
  .no-trades  { text-align: center; color: var(--muted); padding: 20px 0;
                 font-size: .88rem; }

  /* ── SIGNAL LOG ── */
  .sig-row {
    display: grid; grid-template-columns: 80px 58px 70px 1fr;
    gap: 8px; padding: 7px 4px; border-bottom: 1px solid var(--border);
    font-size: .82rem; align-items: center;
  }
  .sig-row:last-child { border-bottom: none; }
  .sig-ts    { color: var(--muted); font-size: .72rem; }
  .sig-coin  { font-weight: 700; }
  .sig-tag   { display: inline-block; padding: 2px 8px; border-radius: 12px;
               font-size: .72rem; font-weight: 700; }
  .sig-buy   { background: #00e67620; color: var(--green); }
  .sig-sell  { background: #ff525220; color: var(--red); }
  .sig-hold  { background: #ffab4020; color: var(--orange); }
  .sig-conf  { color: var(--muted); font-size: .78rem; }

  /* ── EMPTY / LOADING ── */
  .loading { display: flex; align-items: center; justify-content: center;
             min-height: 300px; color: var(--muted); font-size: 1rem; }
  .green { color: var(--green); }
  .red   { color: var(--red); }
  .orange{ color: var(--orange); }

  /* ── FOOTER ── */
  .footer { text-align: center; color: var(--dim); font-size: .72rem;
            padding: 16px; border-top: 1px solid var(--border); }
</style>
</head>
<body>
<div class="app">

  <!-- TOP BAR -->
  <div class="topbar">
    <div class="topbar-left">
      <div class="logo">🔮 Crypto Strategy Clock</div>
      <div class="version">v3.0</div>
    </div>
    <div class="topbar-right">
      <div class="last-run">Last run: <span id="last-run">—</span></div>
      <button class="refresh-btn" onclick="loadData()">↻ Refresh</button>
    </div>
  </div>

  <!-- MAIN GRID -->
  <div class="grid" id="grid">
    <div class="loading col-12">Loading your portfolio…</div>
  </div>

  <div class="footer">Crypto Strategy Clock · Powered by Claude AI · Not financial advice · Paper trading only</div>
</div>

<script>
let portfolioChart = null;
let signalChart    = null;

async function loadData() {
  try {
    const res  = await fetch('/api/data');
    const data = await res.json();
    render(data);
  } catch(e) {
    document.getElementById('grid').innerHTML =
      '<div class="loading col-12">⚠ Could not load data. Is dashboard_server.py running?</div>';
  }
}

function fmt(n, dec=2) {
  return Number(n).toLocaleString('en-US', {minimumFractionDigits:dec, maximumFractionDigits:dec});
}
function fmtDate(s) {
  if (!s || s === 'Never') return '—';
  try {
    const d = new Date(s);
    return d.toLocaleDateString('en-US', {month:'short', day:'numeric'}) + ' ' +
           d.toLocaleTimeString('en-US', {hour:'2-digit', minute:'2-digit'});
  } catch { return s.slice(0,16); }
}
function signalClass(sig) {
  if (!sig) return 'badge-hold';
  const s = sig.toUpperCase();
  if (s.includes('STRONG_BUY') || s==='BUY') return 'badge-buy';
  if (s.includes('SELL')) return 'badge-sell';
  if (s==='WAIT_LONGER' || s==='WAIT') return 'badge-wait';
  return 'badge-hold';
}
function signalLabel(sig) {
  const map = {
    'STRONG_BUY':'STRONG BUY', 'BUY':'BUY', 'HOLD':'HOLD',
    'SELL':'SELL', 'STRONG_SELL':'SELL NOW',
    'RE_ENTER':'BUY BACK', 'WAIT_LONGER':'NOT YET',
    'STOP_LOSS':'STOP HIT', 'TAKE_PROFIT':'TARGET HIT'
  };
  return map[sig] || sig || '—';
}
function modeClass(m) {
  if (!m) return 'mode-hunting';
  return 'mode-' + m.toLowerCase();
}
function pnlSpan(val, suffix='%') {
  const n = Number(val);
  const cls = n > 0 ? 'green' : n < 0 ? 'red' : '';
  const sign = n > 0 ? '+' : '';
  return `<span class="${cls}">${sign}${fmt(n)}${suffix}</span>`;
}

function render(d) {
  const p   = d.portfolio || {};
  const ch  = d.charts || {};
  const holding = p.holding;

  // Last run timestamp
  document.getElementById('last-run').textContent = fmtDate(d.last_run);

  // ── SIGNAL HERO
  const sClass = signalClass(d.current_signal);
  const confColor = d.current_conf >= 70 ? 'var(--green)' : d.current_conf >= 50 ? 'var(--orange)' : 'var(--red)';
  const modeLabel = d.investment_mode || 'HUNTING';
  const signalHtml = `
    <div class="card signal-hero col-4">
      <div class="card-label">Current Signal</div>
      <div class="signal-coin">${d.current_coin || '—'}
        ${d.current_price ? ' &nbsp;·&nbsp; $' + fmt(d.current_price, 4) : ''}
      </div>
      <div>
        <div class="signal-badge ${sClass}">${signalLabel(d.current_signal)}</div>
      </div>
      <div class="conf-row">
        <span class="conf-label">Confidence</span>
        <div class="conf-track">
          <div class="conf-fill" style="width:${d.current_conf}%;background:${confColor}"></div>
        </div>
        <span class="conf-num" style="color:${confColor}">${d.current_conf}%</span>
      </div>
      <div class="mode-badge ${modeClass(modeLabel)}">${modeLabel}</div>
    </div>`;

  // ── PORTFOLIO VALUE
  const retColor = p.total_return >= 0 ? 'var(--green)' : 'var(--red)';
  const barPct   = Math.min(Math.max((p.total_value - 1000) / 1000 * 100 + 50, 0), 100);
  const portHtml = `
    <div class="card col-4">
      <div class="card-label">Paper Portfolio</div>
      <div class="port-total" style="color:${retColor}">$${fmt(p.total_value || 1000)}</div>
      <div class="port-ret" style="color:${retColor}">${pnlSpan(p.total_return)} &nbsp;·&nbsp; Started $1,000.00</div>
      <div class="port-bar-track">
        <div class="port-bar-fill" style="width:${barPct}%;background:${retColor}"></div>
      </div>
      <div class="stat-grid">
        <div class="stat-item">
          <div class="stat-val">$${fmt(p.cash || 1000)}</div>
          <div class="stat-lbl">Cash</div>
        </div>
        <div class="stat-item">
          <div class="stat-val">${pnlSpan(p.realized_pnl,'')}</div>
          <div class="stat-lbl">Realized P&L</div>
        </div>
        <div class="stat-item">
          <div class="stat-val" style="color:${p.win_rate>=50?'var(--green)':'var(--red)'}">${fmt(p.win_rate,0)}%</div>
          <div class="stat-lbl">Win Rate</div>
        </div>
        <div class="stat-item">
          <div class="stat-val">${p.n_trades || 0}</div>
          <div class="stat-lbl">Trades</div>
        </div>
      </div>
    </div>`;

  // ── OPEN POSITION
  let posHtml;
  if (holding) {
    const uCol = p.unrealized_pct >= 0 ? 'var(--green)' : 'var(--red)';
    posHtml = `
    <div class="card col-4">
      <div class="card-label">Open Position</div>
      <div class="pos-row">
        <span class="pos-key">Coin</span>
        <span class="pos-val">${holding.coin_ticker || '—'}</span>
      </div>
      <div class="pos-row">
        <span class="pos-key">Units</span>
        <span class="pos-val">${fmt(holding.units, 4)}</span>
      </div>
      <div class="pos-row">
        <span class="pos-key">Entry price</span>
        <span class="pos-val">$${fmt(holding.entry_price, 4)}</span>
      </div>
      <div class="pos-row">
        <span class="pos-key">Unrealized P&L</span>
        <span class="pos-val" style="color:${uCol}">
          ${pnlSpan(p.unrealized_pct)} &nbsp; $${fmt(p.unrealized, 2)}
        </span>
      </div>
      <div class="pos-row">
        <span class="pos-key">Entry signal</span>
        <span class="pos-val">${holding.entry_signal || '—'} &nbsp; (${holding.entry_confidence || 0}% conf)</span>
      </div>
      <div class="target-row">
        <div class="target-pill target-exit">🎯 Exit &nbsp; $${fmt(holding.exit_target || 0, 4)}</div>
        <div class="target-pill target-stop">🛑 Stop &nbsp; $${fmt(holding.stop_loss || 0, 4)}</div>
      </div>
    </div>`;
  } else {
    posHtml = `
    <div class="card col-4">
      <div class="card-label">Open Position</div>
      <div class="no-position">
        🔍 No open position<br>
        <span style="font-size:.78rem">Agent is ${modeLabel === 'WAITING' ? 'waiting for re-entry' : 'hunting for the next trade'}</span>
      </div>
    </div>`;
  }

  // ── PORTFOLIO CHART
  const hasValHistory = ch.val_data && ch.val_data.length > 0;
  const portChartHtml = `
    <div class="card col-6">
      <div class="card-label">Portfolio Value History</div>
      <div class="chart-wrap">
        <canvas id="portChart"></canvas>
      </div>
      ${!hasValHistory ? '<div style="color:var(--muted);font-size:.82rem;text-align:center;padding:8px">Charts appear after first completed trades</div>' : ''}
    </div>`;

  // ── SIGNAL CONFIDENCE CHART
  const sigChartHtml = `
    <div class="card col-6">
      <div class="card-label">Signal History — Confidence</div>
      <div class="chart-wrap">
        <canvas id="sigChart"></canvas>
      </div>
      ${(!ch.sig_conf || !ch.sig_conf.length) ? '<div style="color:var(--muted);font-size:.82rem;text-align:center;padding:8px">Charts appear after first runs</div>' : ''}
    </div>`;

  // ── TRADE HISTORY
  let tradeRows = '';
  const trades = d.trade_history || [];
  if (trades.length === 0) {
    tradeRows = '<div class="no-trades">No completed trades yet.<br>The agent will execute its first trade on the next strong signal.</div>';
  } else {
    tradeRows = `<div class="trade-row" style="color:var(--muted);font-size:.72rem;font-weight:600">
      <span></span><span>COIN</span><span>DATES</span>
      <span style="text-align:right">%</span>
      <span style="text-align:right">P&L</span>
      <span style="text-align:right">REASON</span></div>` +
    trades.map(t => {
      const win  = t.profit_pct > 0;
      const col  = win ? 'var(--green)' : 'var(--red)';
      const icon = win ? '✅' : '❌';
      const sign = t.profit_pct > 0 ? '+' : '';
      return `<div class="trade-row">
        <span class="trade-icon">${icon}</span>
        <span class="trade-coin">${t.coin_ticker||'—'}</span>
        <span class="trade-dates">${(t.entry_date||'').slice(0,10)} → ${(t.exit_date||'').slice(0,10)}</span>
        <span class="trade-pnl" style="color:${col}">${sign}${fmt(t.profit_pct)}%</span>
        <span class="trade-usd"  style="color:${col}">$${fmt(t.profit_usd,2)}</span>
        <span class="trade-why">${t.exit_reason||'—'}</span>
      </div>`;
    }).join('');
  }
  const tradeHtml = `
    <div class="card col-7">
      <div class="card-label">Trade History</div>
      ${tradeRows}
    </div>`;

  // ── SIGNAL LOG
  const sigRows = (d.signal_history || []).map(r => {
    const s   = r.signal || '—';
    const cls = s.includes('BUY') ? 'sig-buy' : s.includes('SELL') ? 'sig-sell' : 'sig-hold';
    const lbl = signalLabel(s);
    return `<div class="sig-row">
      <span class="sig-ts">${(r.ts||'').slice(0,10)}</span>
      <span class="sig-coin">${r.coin||'—'}</span>
      <span class="sig-tag ${cls}">${lbl}</span>
      <span class="sig-conf">${r.confidence||0}% conf</span>
    </div>`;
  }).join('');
  const sigLogHtml = `
    <div class="card col-5">
      <div class="card-label">Recent Signals</div>
      ${sigRows || '<div class="no-trades">No signals yet. Run the agent to start.</div>'}
    </div>`;

  // ── STATS ROW
  const statsHtml = `
    <div class="card col-3">
      <div class="card-label">Best Trade</div>
      <div style="font-size:1.8rem;font-weight:800" class="green">+${fmt(p.best||0)}%</div>
      <div style="font-size:.78rem;color:var(--muted);margin-top:4px">Highest single-trade return</div>
    </div>
    <div class="card col-3">
      <div class="card-label">Worst Trade</div>
      <div style="font-size:1.8rem;font-weight:800" class="${(p.worst||0)<0?'red':'green'}">${fmt(p.worst||0)}%</div>
      <div style="font-size:.78rem;color:var(--muted);margin-top:4px">Largest single-trade loss</div>
    </div>
    <div class="card col-3">
      <div class="card-label">Avg Trade</div>
      <div style="font-size:1.8rem;font-weight:800" class="${(p.avg_pnl||0)>=0?'green':'red'}">${pnlSpan(p.avg_pnl||0)}</div>
      <div style="font-size:.78rem;color:var(--muted);margin-top:4px">Average return per trade</div>
    </div>
    <div class="card col-3">
      <div class="card-label">Win Rate</div>
      <div style="font-size:1.8rem;font-weight:800;color:${(p.win_rate||0)>=50?'var(--green)':'var(--red)'}">${fmt(p.win_rate||0,0)}%</div>
      <div style="font-size:.78rem;color:var(--muted);margin-top:4px">${p.n_trades||0} total trades</div>
    </div>`;

  // ── INJECT
  document.getElementById('grid').innerHTML =
    signalHtml + portHtml + posHtml +
    portChartHtml + sigChartHtml +
    statsHtml +
    tradeHtml + sigLogHtml;

  // ── CHARTS
  if (portfolioChart) { portfolioChart.destroy(); portfolioChart = null; }
  if (signalChart)    { signalChart.destroy();    signalChart    = null; }

  const chartDefaults = {
    responsive: true, maintainAspectRatio: false,
    plugins: { legend: { display: false } },
    scales: {
      x: { grid: { color: '#1e1e42' }, ticks: { color: '#5c6080', font: { size: 10 } } },
      y: { grid: { color: '#1e1e42' }, ticks: { color: '#5c6080', font: { size: 10 } } }
    }
  };

  if (hasValHistory) {
    const ctx1 = document.getElementById('portChart').getContext('2d');
    portfolioChart = new Chart(ctx1, {
      type: 'line',
      data: {
        labels: ch.val_labels,
        datasets: [{
          data: ch.val_data,
          borderColor: '#00e676', backgroundColor: '#00e67618',
          borderWidth: 2, pointRadius: 4, pointBackgroundColor: '#00e676',
          fill: true, tension: .3
        }]
      },
      options: { ...chartDefaults }
    });
  }

  if (ch.sig_conf && ch.sig_conf.length) {
    const ctx2 = document.getElementById('sigChart').getContext('2d');
    signalChart = new Chart(ctx2, {
      type: 'bar',
      data: {
        labels: ch.sig_labels,
        datasets: [{
          data: ch.sig_conf,
          backgroundColor: ch.sig_colors,
          borderRadius: 4
        }]
      },
      options: {
        ...chartDefaults,
        scales: {
          ...chartDefaults.scales,
          y: { ...chartDefaults.scales.y, min: 0, max: 100 }
        }
      }
    });
  }
}

// Auto-refresh every 5 minutes
loadData();
setInterval(loadData, 5 * 60 * 1000);
</script>
</body>
</html>"""


# ─── HTTP HANDLER ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, format, *args):
        pass  # suppress access logs

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/data":
            self._serve_json()
        elif self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_html(self):
        body = HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self):
        try:
            data = build_api_data()
        except Exception as e:
            data = {"error": str(e)}
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

def open_browser():
    time.sleep(1.2)
    webbrowser.open(f"http://localhost:{PORT}")

if __name__ == "__main__":
    print(f"""
  ╔══════════════════════════════════════════════╗
  ║  🔮  Crypto Strategy Clock Dashboard         ║
  ║                                              ║
  ║  Opening:  http://localhost:{PORT}            ║
  ║                                              ║
  ║  Keep this window open while using           ║
  ║  the dashboard. Close it to stop.            ║
  ╚══════════════════════════════════════════════╝
""")
    os.chdir(PROJECT_DIR)
    threading.Thread(target=open_browser, daemon=True).start()
    server = HTTPServer(("localhost", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")
