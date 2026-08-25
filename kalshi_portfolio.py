#!/usr/bin/env python3
"""
kalshi_portfolio.py — Accurate paper trade tracker for Kalshi perps.

Perp mechanics implemented:
  - Margin × leverage = notional position size
  - Liquidation price: entry × (1 - 1/leverage) for longs
                       entry × (1 + 1/leverage) for shorts
  - Funding deducted every 8H on notional (longs pay when rate > 0)
  - Stop loss and take profit checked on every monitoring cycle
  - Running P&L = (current_price / entry_price - 1) × notional × direction
  - All state persisted to JSON file for Railway/cross-session durability

Paper trading only — no real orders placed.
When we go live, the trade execution hooks in kalshi_tracker.py call the real API.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests as _requests

log = logging.getLogger(__name__)

_DATA_DIR      = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__))
PORTFOLIO_FILE = os.getenv(
    "KALSHI_PORTFOLIO_FILE",
    os.path.join(_DATA_DIR, "kalshi_portfolio.json"),
)

# ─── REDIS (Upstash) ──────────────────────────────────────────────────────────
# Upstash's own dashboard exports these as UPSTASH_REDIS_REST_URL /
# UPSTASH_REDIS_REST_TOKEN, Vercel uses KV_REST_API_*, and people commonly
# shorten to REDIS_*. Accept every variant — a name mismatch here silently
# disables persistence and loses all trade history on the next redeploy.
_URL_VARS = (
    "UPSTASH_REDIS_URL", "UPSTASH_REDIS_REST_URL",
    "KV_REST_API_URL", "REDIS_URL", "REDIS_REST_URL",
)
_TOKEN_VARS = (
    "UPSTASH_REDIS_TOKEN", "UPSTASH_REDIS_REST_TOKEN",
    "KV_REST_API_TOKEN", "REDIS_TOKEN", "REDIS_REST_TOKEN",
)


def _first_env(names: tuple) -> tuple:
    """Return (value, var_name_that_matched) for the first populated var."""
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v, n
    return "", ""


_REDIS_URL,   _REDIS_URL_VAR   = _first_env(_URL_VARS)
_REDIS_TOKEN, _REDIS_TOKEN_VAR = _first_env(_TOKEN_VARS)
_PORTFOLIO_KEY = "kalshi_portfolio"

if _REDIS_URL and _REDIS_TOKEN:
    log.info(f"Kalshi portfolio: Redis configured via {_REDIS_URL_VAR} / {_REDIS_TOKEN_VAR}")
else:
    log.error(
        "Kalshi portfolio: NO REDIS CONFIGURED — trade history will be LOST on "
        f"every redeploy. Set one of {_URL_VARS[0]} or {_URL_VARS[1]} (+ token)."
    )


def _redis_get(key: str):
    if not _REDIS_URL or not _REDIS_TOKEN:
        return None
    try:
        r = _requests.post(
            _REDIS_URL,
            headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
            json=["GET", key],
            timeout=5,
        )
        if r.status_code == 200:
            val = r.json().get("result")
            return json.loads(val) if val else None
        log.error(f"Kalshi portfolio: Redis GET HTTP {r.status_code} — "
                  f"falling back to local file, history may appear empty")
    except Exception as e:
        log.error(f"Kalshi portfolio: Redis GET error: {e} — "
                  f"falling back to local file, history may appear empty")
    return None


def redis_health() -> dict:
    """
    Diagnose whether Redis is reachable and what's actually stored.
    Used by /health so an apparent 'reset' can be distinguished from a
    connection failure before anyone panics or overwrites good data.
    """
    out = {
        "configured": bool(_REDIS_URL and _REDIS_TOKEN),
        "reachable":  False,
        "keys":       {},
        "error":      None,
        "url_var":    _REDIS_URL_VAR,
        "token_var":  _REDIS_TOKEN_VAR,
        "searched":   {"url": list(_URL_VARS), "token": list(_TOKEN_VARS)},
        "found_any":  sorted(
            n for n in (_URL_VARS + _TOKEN_VARS) if os.getenv(n, "").strip()
        ),
    }
    if not out["configured"]:
        missing = []
        if not _REDIS_URL:
            missing.append("URL")
        if not _REDIS_TOKEN:
            missing.append("TOKEN")
        out["error"] = f"Redis {' and '.join(missing)} not found in any known env var name"
        return out

    for key in (_PORTFOLIO_KEY, "kalshi_postmortem"):
        try:
            r = _requests.post(
                _REDIS_URL,
                headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                json=["GET", key],
                timeout=5,
            )
            if r.status_code != 200:
                out["error"] = f"HTTP {r.status_code}"
                continue
            out["reachable"] = True
            raw = r.json().get("result")
            if not raw:
                out["keys"][key] = {"exists": False}
                continue
            data = json.loads(raw)
            if key == _PORTFOLIO_KEY:
                out["keys"][key] = {
                    "exists":        True,
                    "bytes":         len(raw),
                    "holdings":      len(data.get("holdings", [])),
                    "trade_history": len(data.get("trade_history", [])),
                    "total_trades":  data.get("total_trades", 0),
                    "winning":       data.get("winning_trades", 0),
                    "losing":        data.get("losing_trades", 0),
                    "created_at":    data.get("created_at", "?"),
                }
            else:
                out["keys"][key] = {
                    "exists": True,
                    "bytes":  len(raw),
                    "calls":  len(data.get("calls", [])),
                }
        except Exception as e:
            out["error"] = str(e)
    return out


def _redis_set(key: str, data: dict) -> bool:
    if not _REDIS_URL or not _REDIS_TOKEN:
        return False
    try:
        r = _requests.post(
            _REDIS_URL,
            headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
            json=["SET", key, json.dumps(data, default=str)],
            timeout=5,
        )
        return r.status_code == 200
    except Exception as e:
        log.warning(f"Kalshi portfolio: Redis SET error: {e}")
    return False

STARTING_CASH   = float(os.getenv("KALSHI_STARTING_CASH", "500.0"))   # paper bank
DEFAULT_MARGIN  = float(os.getenv("KALSHI_DEFAULT_MARGIN", "50.0"))    # $ margin per trade
FUNDING_PERIOD_HOURS = 8
# No practical cap while paper trading — we want maximum sample size for
# calibration. Cash is the real constraint. Set KALSHI_MAX_POSITIONS to limit.
MAX_OPEN_POSITIONS  = int(os.getenv("KALSHI_MAX_POSITIONS", "999"))


# ─── PERSISTENCE ──────────────────────────────────────────────────────────────

def _load() -> dict:
    # Try Redis first (survives redeploys)
    data = _redis_get(_PORTFOLIO_KEY)
    if data:
        log.debug("Kalshi portfolio: loaded from Redis")
        return data

    # Fallback: local file (dev / no Redis configured)
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Kalshi portfolio: file load error: {e}")

    return {
        "version":        "kalshi-v1",
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "cash":           STARTING_CASH,
        "starting_cash":  STARTING_CASH,
        "peak_value":     STARTING_CASH,
        "total_trades":   0,
        "winning_trades": 0,
        "losing_trades":  0,
        "total_pnl":      0.0,
        "total_funding_paid": 0.0,
        "holdings":       [],
        "trade_history":  [],
    }


def reset_portfolio(starting_cash: float = None) -> dict:
    """
    Wipe the paper portfolio and start fresh.

    Reads KALSHI_STARTING_CASH at call time so a Railway variable change takes
    effect without a code deploy. Destroys all open positions and trade history.
    """
    if starting_cash is None:
        starting_cash = float(os.getenv("KALSHI_STARTING_CASH", "500.0"))

    # ARCHIVE FIRST — never destroy trade history. A reset that deletes the
    # record makes it impossible to audit past bets or recover from a mistaken
    # wipe. The archive is keyed by timestamp and kept indefinitely.
    try:
        old = _load()
        if old.get("trade_history") or old.get("holdings"):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            _redis_set(f"kalshi_portfolio_archive_{stamp}", old)
            # Track archive keys so they can be listed/restored later
            index = _redis_get("kalshi_archive_index") or {"archives": []}
            index["archives"].append({
                "key":           f"kalshi_portfolio_archive_{stamp}",
                "archived_at":   datetime.now(timezone.utc).isoformat(),
                "trades":        len(old.get("trade_history", [])),
                "holdings":      len(old.get("holdings", [])),
                "winning":       old.get("winning_trades", 0),
                "losing":        old.get("losing_trades", 0),
            })
            _redis_set("kalshi_archive_index", index)
            log.warning(
                f"Kalshi portfolio: ARCHIVED {len(old.get('trade_history', []))} trades "
                f"to kalshi_portfolio_archive_{stamp} before reset"
            )
    except Exception as e:
        log.error(f"Kalshi portfolio: archive before reset FAILED: {e}")

    fresh = {
        "version":        "kalshi-v1",
        "created_at":     datetime.now(timezone.utc).isoformat(),
        "cash":           starting_cash,
        "starting_cash":  starting_cash,
        "peak_value":     starting_cash,
        "total_trades":   0,
        "winning_trades": 0,
        "losing_trades":  0,
        "total_pnl":      0.0,
        "total_funding_paid": 0.0,
        "holdings":       [],
        "trade_history":  [],
    }
    _save(fresh)
    log.warning(f"Kalshi portfolio: RESET — fresh bank ${starting_cash:,.2f}")
    return fresh


def deposit(target_bank: float) -> Optional[dict]:
    """
    Add cash WITHOUT touching trade history, positions, or the W/L record.

    Use this instead of reset_portfolio() when the goal is simply more buying
    power. A reset destroys the calibration data that took weeks to accumulate;
    a deposit just raises the bank.

    Treated as a real deposit: cash AND starting_cash both rise by the same
    amount, so percentage returns stay honest — depositing money is not a gain.
    Idempotent: once the basis reaches the target, further calls no-op.
    """
    state = _load()
    basis = float(state.get("starting_cash", STARTING_CASH))

    if basis >= target_bank:
        log.info(f"Kalshi deposit: basis already ${basis:,.2f} — no-op")
        return None

    # Ledger guard — protects against repeated deposits if a save fails to
    # persist and the basis appears to revert on the next boot.
    ledger = state.setdefault("deposits", [])
    if any(abs(float(d.get("target", 0)) - target_bank) < 0.01 for d in ledger):
        log.warning(
            f"Kalshi deposit: target ${target_bank:,.2f} already in ledger but "
            f"basis is ${basis:,.2f} — state didn't persist. Skipping to avoid "
            f"double-funding."
        )
        return None

    delta = target_bank - basis
    state["cash"]          = state.get("cash", 0.0) + delta
    state["starting_cash"] = basis + delta
    state["peak_value"]    = state.get("peak_value", 0.0) + delta
    ledger.append({
        "target": target_bank,
        "amount": round(delta, 2),
        "at":     datetime.now(timezone.utc).isoformat(),
    })
    _save(state)

    log.warning(
        f"Kalshi: DEPOSITED ${delta:,.2f} — cash now ${state['cash']:,.2f}, "
        f"basis ${state['starting_cash']:,.2f}. "
        f"{len(state.get('trade_history', []))} trades and "
        f"{state.get('winning_trades',0)}W/{state.get('losing_trades',0)}L preserved."
    )
    return state


def list_archives() -> list:
    """Every archived portfolio snapshot, newest first."""
    index = _redis_get("kalshi_archive_index") or {"archives": []}
    return list(reversed(index.get("archives", [])))


def restore_archive(archive_key: str) -> Optional[dict]:
    """
    Restore a previously archived portfolio. The CURRENT state is archived
    first, so a restore is itself reversible.
    """
    data = _redis_get(archive_key)
    if not data:
        log.error(f"Kalshi portfolio: archive {archive_key} not found")
        return None

    # Archive what we're about to replace
    try:
        current = _load()
        if current.get("trade_history") or current.get("holdings"):
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            _redis_set(f"kalshi_portfolio_archive_{stamp}", current)
            index = _redis_get("kalshi_archive_index") or {"archives": []}
            index["archives"].append({
                "key":         f"kalshi_portfolio_archive_{stamp}",
                "archived_at": datetime.now(timezone.utc).isoformat(),
                "trades":      len(current.get("trade_history", [])),
                "holdings":    len(current.get("holdings", [])),
                "winning":     current.get("winning_trades", 0),
                "losing":      current.get("losing_trades", 0),
                "note":        "auto-archived before restore",
            })
            _redis_set("kalshi_archive_index", index)
    except Exception as e:
        log.warning(f"Kalshi portfolio: pre-restore archive failed: {e}")

    _save(data)
    log.warning(
        f"Kalshi portfolio: RESTORED from {archive_key} — "
        f"{len(data.get('trade_history', []))} trades, "
        f"{data.get('winning_trades',0)}W/{data.get('losing_trades',0)}L"
    )
    return data


def _save(state: dict):
    # Primary: Redis
    if _redis_set(_PORTFOLIO_KEY, state):
        log.debug("Kalshi portfolio: saved to Redis")
        return
    # Fallback: local file
    try:
        with open(PORTFOLIO_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Kalshi portfolio: save error: {e}")


# ─── POSITION MATH ────────────────────────────────────────────────────────────

def calc_liquidation_price(entry: float, leverage: float, direction: str) -> float:
    """
    Liquidation price for a perp position.
    direction: "UP" (long) or "DOWN" (short)

    Long  liq = entry × (1 - 1/leverage)  — price drops this much → bust
    Short liq = entry × (1 + 1/leverage)  — price rises this much → bust
    """
    if leverage <= 0:
        return 0.0
    if direction == "UP":
        return round(entry * (1.0 - 1.0 / leverage), 6)
    else:
        return round(entry * (1.0 + 1.0 / leverage), 6)


def calc_pnl(entry: float, current: float, notional: float, direction: str) -> float:
    """Unrealized P&L in dollars."""
    if entry <= 0:
        return 0.0
    pct = (current - entry) / entry
    if direction == "DOWN":
        pct = -pct
    return round(pct * notional, 4)


def calc_stop_price(entry: float, stop_pct: float, direction: str) -> float:
    """Dollar stop price from entry and stop %."""
    if direction == "UP":
        return round(entry * (1.0 - stop_pct / 100.0), 6)
    else:
        return round(entry * (1.0 + stop_pct / 100.0), 6)


def calc_tp_price(entry: float, tp_pct: float, direction: str) -> float:
    """Dollar take-profit price from entry and tp %."""
    if direction == "UP":
        return round(entry * (1.0 + tp_pct / 100.0), 6)
    else:
        return round(entry * (1.0 - tp_pct / 100.0), 6)


def calc_funding_charge(notional: float, funding_rate: float, direction: str) -> float:
    """
    Funding charge for one 8H period.
    Positive rate → longs pay shorts.
    Negative rate → shorts pay longs.
    Returns dollar amount PAID by this position (negative = received).
    """
    if direction == "UP":
        return round(notional * funding_rate, 6)   # longs pay when rate > 0
    else:
        return round(-notional * funding_rate, 6)  # shorts pay when rate < 0


# ─── OPEN / CLOSE ─────────────────────────────────────────────────────────────

def open_position(
    ticker:     str,
    title:      str,
    direction:  str,            # "UP" or "DOWN"
    entry_price: float,
    leverage:   float,
    margin:     float = DEFAULT_MARGIN,
    stop_pct:   float = 5.0,
    tp_pct:     float = 10.0,
    funding_rate: float = 0.0,  # current 8H rate
    confidence: int = 0,
    reasoning:  str = "",
    signal_source: str = "kalshi_research",
    group_id:   str = "",   # conviction group — see below
    group_size: int = 1,
) -> Optional[dict]:
    """
    Paper-open a new perp position.
    Returns the position dict, or None if not enough cash / too many positions.
    """
    state = _load()

    if len(state["holdings"]) >= MAX_OPEN_POSITIONS:
        log.warning(f"Kalshi portfolio: max positions ({MAX_OPEN_POSITIONS}) reached — skipping {ticker}")
        return None

    if state["cash"] < margin:
        log.warning(f"Kalshi portfolio: insufficient cash ${state['cash']:.2f} for ${margin:.2f} margin")
        return None

    # Already holding this ticker?
    for h in state["holdings"]:
        if h["ticker"] == ticker:
            log.info(f"Kalshi portfolio: already holding {ticker} — skipping duplicate")
            return None

    notional    = margin * leverage
    liq_price   = calc_liquidation_price(entry_price, leverage, direction)
    stop_price  = calc_stop_price(entry_price, stop_pct, direction)
    tp_price    = calc_tp_price(entry_price, tp_pct, direction)
    now         = datetime.now(timezone.utc).isoformat()

    position = {
        "ticker":           ticker,
        "title":            title,
        "direction":        direction,
        "entry_price":      entry_price,
        "current_price":    entry_price,
        "leverage":         leverage,
        "margin":           margin,
        "notional":         notional,
        "liquidation_price": liq_price,
        "stop_price":       stop_price,
        "take_profit_price": tp_price,
        "stop_pct":         stop_pct,
        "tp_pct":           tp_pct,
        "funding_rate_8h":  funding_rate,
        "funding_paid":     0.0,
        "unrealized_pnl":   0.0,
        "opened_at":        now,
        "last_funding_at":  now,
        "confidence":       confidence,
        "reasoning":        reasoning,
        "signal_source":    signal_source,
        # Conviction group: correlated positions opened by the same scan in the
        # same direction share an ID. They're separate trades for P&L, but ONE
        # observation for calibration — four correlated longs is one directional
        # call, and counting it as four inflates the apparent sample size.
        "group_id":         group_id,
        "group_size":       group_size,
    }

    state["cash"]    -= margin
    state["holdings"].append(position)
    _save(state)

    log.info(
        f"Kalshi portfolio: OPENED {direction} {ticker} @ {entry_price:.4f} "
        f"| margin=${margin:.2f} lev={leverage}x notional=${notional:.2f} "
        f"| liq={liq_price:.4f} stop={stop_price:.4f} tp={tp_price:.4f}"
    )
    return position


def close_position(
    ticker:        str,
    exit_price:    float,
    reason:        str = "manual",   # "stop_loss" | "take_profit" | "manual" | "liquidated"
) -> Optional[dict]:
    """
    Paper-close a position. Returns closed trade dict or None if not found.
    """
    state = _load()

    idx = next((i for i, h in enumerate(state["holdings"]) if h["ticker"] == ticker), None)
    if idx is None:
        log.warning(f"Kalshi portfolio: no open position for {ticker}")
        return None

    pos        = state["holdings"].pop(idx)
    entry      = pos["entry_price"]
    direction  = pos["direction"]
    notional   = pos["notional"]
    margin     = pos["margin"]

    realized_pnl   = calc_pnl(entry, exit_price, notional, direction)
    funding_paid   = pos.get("funding_paid", 0.0)
    net_pnl        = realized_pnl - funding_paid

    # Return margin + net profit to cash
    state["cash"]         += margin + net_pnl
    state["total_pnl"]    += net_pnl
    state["total_trades"] += 1

    # NOTE: total_funding_paid is deliberately NOT incremented here.
    # apply_funding() already added every charge to the running total at the
    # moment it was levied. Adding the position's accumulated funding_paid
    # again on close counted the whole thing twice, roughly doubling the
    # reported funding figure. The cash math was always right — net_pnl above
    # carries the real deduction — but the /health and report funding lines
    # were inflated.

    won = net_pnl > 0
    if won:
        state["winning_trades"] += 1
    else:
        state["losing_trades"] += 1

    # Track peak
    total_val = state["cash"] + sum(h.get("unrealized_pnl", 0) for h in state["holdings"])
    if total_val > state["peak_value"]:
        state["peak_value"] = total_val

    trade_record = {
        "ticker":        ticker,
        "title":         pos.get("title", ticker),
        "direction":     direction,
        "entry_price":   entry,
        "exit_price":    exit_price,
        "leverage":      pos["leverage"],
        "margin":        margin,
        "notional":      notional,
        "realized_pnl":  round(realized_pnl, 4),
        "funding_paid":  round(funding_paid, 4),
        "net_pnl":       round(net_pnl, 4),
        "pnl_pct":       round(net_pnl / margin * 100, 2) if margin > 0 else 0,
        "won":           won,
        "reason":        reason,
        "opened_at":     pos.get("opened_at", ""),
        "closed_at":     datetime.now(timezone.utc).isoformat(),
        "held_hours":    _hours_held(pos.get("opened_at", "")),
        "confidence":    pos.get("confidence", 0),
        "reasoning":     pos.get("reasoning", ""),
        "signal_source": pos.get("signal_source", ""),
        "group_id":      pos.get("group_id", ""),
        "group_size":    pos.get("group_size", 1),
        "trend_label":   pos.get("trend_label", ""),
        "funding_sentiment": pos.get("funding_sentiment", ""),
    }

    state["trade_history"].append(trade_record)
    _save(state)

    log.info(
        f"Kalshi portfolio: CLOSED {direction} {ticker} @ {exit_price:.4f} "
        f"| pnl=${net_pnl:+.2f} ({trade_record['pnl_pct']:+.1f}%) | reason={reason}"
    )
    return trade_record


def _hours_held(opened_at: str) -> float:
    try:
        opened = datetime.fromisoformat(opened_at)
        now    = datetime.now(timezone.utc)
        return round((now - opened).total_seconds() / 3600, 1)
    except Exception:
        return 0.0


# ─── MONITORING ───────────────────────────────────────────────────────────────

def update_prices(prices_by_ticker: dict) -> list[dict]:
    """
    Update current prices for all holdings. Check SL/TP/liquidation.
    Called every 5 minutes by the monitoring loop.

    prices_by_ticker: {ticker: current_price}

    Returns list of exit events: [{ticker, reason, exit_price, ...}]
    """
    state  = _load()
    exits  = []
    changed = False

    for pos in state["holdings"]:
        ticker  = pos["ticker"]
        price   = prices_by_ticker.get(ticker)
        if price is None or price <= 0:
            continue

        pos["current_price"] = price
        pos["unrealized_pnl"] = calc_pnl(
            pos["entry_price"], price, pos["notional"], pos["direction"]
        )
        changed = True

        direction = pos["direction"]
        liq       = pos["liquidation_price"]
        stop      = pos["stop_price"]
        tp        = pos["take_profit_price"]

        # Liquidation check (takes priority)
        liquidated = (
            (direction == "UP"   and price <= liq) or
            (direction == "DOWN" and price >= liq)
        )
        if liquidated:
            exits.append({"ticker": ticker, "reason": "liquidated", "exit_price": liq})
            continue

        # Stop loss
        stop_hit = (
            (direction == "UP"   and price <= stop) or
            (direction == "DOWN" and price >= stop)
        )
        if stop_hit:
            exits.append({"ticker": ticker, "reason": "stop_loss", "exit_price": price})
            continue

        # Take profit
        tp_hit = (
            (direction == "UP"   and price >= tp) or
            (direction == "DOWN" and price <= tp)
        )
        if tp_hit:
            exits.append({"ticker": ticker, "reason": "take_profit", "exit_price": price})

    if changed:
        _save(state)

    return exits


def apply_funding(funding_rates_by_ticker: dict) -> list[dict]:
    """
    Deduct funding payments from open positions. Call every 8H.

    funding_rates_by_ticker: {ticker: rate_float (e.g. 0.001 = 0.1%/8H)}

    Returns list of {ticker, charge, direction, cumulative_paid} for logging.
    """
    state   = _load()
    charges = []
    now     = datetime.now(timezone.utc).isoformat()

    for pos in state["holdings"]:
        ticker = pos["ticker"]
        rate   = funding_rates_by_ticker.get(ticker)
        if rate is None:
            continue

        charge = calc_funding_charge(pos["notional"], rate, pos["direction"])
        pos["funding_paid"]    = round(pos.get("funding_paid", 0.0) + charge, 6)
        pos["last_funding_at"] = now
        pos["unrealized_pnl"]  = calc_pnl(
            pos["entry_price"], pos["current_price"], pos["notional"], pos["direction"]
        ) - pos["funding_paid"]

        state["total_funding_paid"] += charge
        charges.append({
            "ticker":           ticker,
            "charge":           round(charge, 6),
            "direction":        pos["direction"],
            "cumulative_paid":  round(pos["funding_paid"], 4),
        })

    _save(state)
    return charges


# ─── REPORTING ────────────────────────────────────────────────────────────────

def get_portfolio_summary() -> dict:
    """Return current portfolio state for Telegram / display."""
    state         = _load()
    holdings      = state["holdings"]
    total_margin  = sum(h["margin"] for h in holdings)
    total_unreal  = sum(h.get("unrealized_pnl", 0) for h in holdings)
    total_value   = state["cash"] + total_margin + total_unreal

    win_rate = 0.0
    if state["total_trades"] > 0:
        win_rate = state["winning_trades"] / state["total_trades"] * 100

    return {
        "cash":              round(state["cash"], 2),
        "total_value":       round(total_value, 2),
        # Authoritative P&L — derived from the account, not accumulated.
        # state["total_pnl"] is a running counter incremented only in
        # close_position(), so funding payments (which move cash directly)
        # never reach it and the figure drifts further from truth over time.
        # Same class of bug as FOMO's tranche harvests: money moved, nothing
        # recorded it. account − basis cannot omit a category.
        "true_pnl":          round(total_value - state["starting_cash"], 2),
        "true_pnl_pct":      round(
            (total_value / state["starting_cash"] - 1) * 100, 2
        ) if state.get("starting_cash") else 0.0,
        "counter_pnl":       round(state["total_pnl"], 2),
        "funding_paid":      round(state.get("total_funding_paid", 0), 2),
        "starting_cash":     state["starting_cash"],
        "peak_value":        round(state["peak_value"], 2),
        "total_pnl":         round(state["total_pnl"], 2),
        "total_funding_paid": round(state["total_funding_paid"], 4),
        "total_trades":      state["total_trades"],
        "winning_trades":    state["winning_trades"],
        "losing_trades":     state["losing_trades"],
        "win_rate":          round(win_rate, 1),
        "open_positions":    len(holdings),
        "positions":         [
            {
                "ticker":    h["ticker"],
                "direction": h["direction"],
                "entry":     h["entry_price"],
                "current":   h.get("current_price", h["entry_price"]),
                "leverage":  h["leverage"],
                "margin":    h["margin"],
                "unrealized_pnl": round(h.get("unrealized_pnl", 0), 2),
                "stop":      h["stop_price"],
                "tp":        h["take_profit_price"],
                "liq":       h["liquidation_price"],
                "funding_paid": round(h.get("funding_paid", 0), 4),
                "opened_at": h.get("opened_at", ""),
            }
            for h in holdings
        ],
    }


def format_portfolio_telegram(summary: dict = None) -> str:
    """Format portfolio status for Telegram."""
    if summary is None:
        summary = get_portfolio_summary()

    lines = [
        "📊 *KALSHI PAPER PORTFOLIO*",
        f"Cash: ${summary['cash']:.2f}  |  Total: ${summary['total_value']:.2f}",
        f"P&L: ${summary['total_pnl']:+.2f}  |  Win rate: {summary['win_rate']:.0f}% ({summary['winning_trades']}/{summary['total_trades']})",
        f"Funding paid: ${summary['total_funding_paid']:.2f}",
        "",
    ]

    if summary["positions"]:
        lines.append("*Open Positions:*")
        for p in summary["positions"]:
            emoji = "🟢" if p["direction"] == "UP" else "🔴"
            pnl   = p["unrealized_pnl"]
            pnl_s = f"${pnl:+.2f}"
            lines.append(
                f"{emoji} {p['ticker']} {p['direction']} {p['leverage']}x "
                f"| Entry {p['entry']:.4f} → {p['current']:.4f} "
                f"| P&L {pnl_s}"
            )
    else:
        lines.append("No open positions.")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import json as _json

    # Demo: open a fake position and print summary
    pos = open_position(
        ticker="BTC-USD-PERP",
        title="Bitcoin Perpetual",
        direction="UP",
        entry_price=95000.0,
        leverage=3.0,
        margin=50.0,
        stop_pct=5.0,
        tp_pct=12.0,
        funding_rate=0.0005,
        confidence=72,
        reasoning="Strong uptrend, balanced funding, OI rising.",
    )
    print(_json.dumps(get_portfolio_summary(), indent=2))
