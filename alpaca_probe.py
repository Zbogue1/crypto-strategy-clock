#!/usr/bin/env python3
"""
alpaca_probe.py — Data quality check for Alpaca's free tier.

THE QUESTION THIS ANSWERS:
Alpaca's free plan serves IEX data only — roughly 2-3% of consolidated US equity
volume. For liquid large caps that's fine. But Stock Golem's strategy targets
sub-20M-float small caps up 10%+ on 5x relative volume, and those often trade
almost entirely away from IEX. If the 1-minute bars come back sparse or empty
exactly when a stock is running, the strategy cannot be executed on this data.

Better to find that out in one command than after building a scanner.

Checks:
  1. Auth + account state
  2. Bar density for a liquid benchmark (AAPL) — establishes what "good" looks like
  3. Bar density for actual small-cap movers pulled from Alpaca's own screener
  4. Gap analysis — how many minutes are missing during market hours
"""

import logging
import os
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

API_KEY    = os.getenv("ALPACA_API_KEY", "").strip()
SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "").strip()

DATA_URL   = "https://data.alpaca.markets"
PAPER_URL  = "https://paper-api.alpaca.markets"


def _headers() -> dict:
    return {
        "APCA-API-KEY-ID":     API_KEY,
        "APCA-API-SECRET-KEY": SECRET_KEY,
        "accept":              "application/json",
    }


def _get(base: str, path: str, params: dict = None, timeout: int = 15):
    try:
        r = requests.get(f"{base}{path}", headers=_headers(), params=params or {}, timeout=timeout)
        if r.status_code == 200:
            return r.json(), None
        return None, f"HTTP {r.status_code}: {r.text[:160]}"
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


# ─── CHECKS ───────────────────────────────────────────────────────────────────

def check_auth() -> dict:
    data, err = _get(PAPER_URL, "/v2/account")
    if err:
        return {"ok": False, "error": err}
    return {
        "ok":       True,
        "status":   data.get("status"),
        "cash":     float(data.get("cash", 0) or 0),
        "equity":   float(data.get("equity", 0) or 0),
        "currency": data.get("currency", "USD"),
        "pattern_day_trader": data.get("pattern_day_trader"),
    }


def get_movers(limit: int = 10) -> tuple:
    """
    Alpaca's screener — the closest free equivalent to a gap scanner.
    Returns (gainers, error).
    """
    data, err = _get(DATA_URL, "/v1beta1/screener/stocks/movers", {"top": limit})
    if err:
        return [], err
    return data.get("gainers", []), None


def bar_quality(symbol: str, minutes_back: int = 180) -> dict:
    """
    Pull 1-minute bars and measure how complete they are.

    A tradeable feed should return close to one bar per market minute. Large
    gaps mean the stock is trading somewhere IEX can't see, which makes VWAP,
    the 9 EMA, and pullback detection unreliable.
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes_back)
    data, err = _get(
        DATA_URL, f"/v2/stocks/{symbol}/bars",
        {
            "timeframe": "1Min",
            "start":     start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end":       end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit":     10000,
            "feed":      "iex",
        },
    )
    if err:
        return {"symbol": symbol, "ok": False, "error": err}

    bars = data.get("bars") or []
    if not bars:
        return {"symbol": symbol, "ok": True, "bars": 0, "coverage_pct": 0.0,
                "note": "NO BARS — not visible on IEX in this window"}

    # Coverage: bars returned vs minutes elapsed
    coverage = len(bars) / max(minutes_back, 1) * 100
    vols  = [b.get("v", 0) for b in bars]
    total_vol = sum(vols)
    zero_vol  = sum(1 for v in vols if not v)

    # Largest consecutive gap between bars
    max_gap = 0
    try:
        ts = [datetime.fromisoformat(b["t"].replace("Z", "+00:00")) for b in bars]
        for a, b in zip(ts, ts[1:]):
            gap = (b - a).total_seconds() / 60
            max_gap = max(max_gap, gap)
    except Exception:
        pass

    return {
        "symbol":       symbol,
        "ok":           True,
        "bars":         len(bars),
        "coverage_pct": round(coverage, 1),
        "total_volume": int(total_vol),
        "zero_vol_bars": zero_vol,
        "max_gap_min":  round(max_gap, 1),
        "first":        bars[0].get("t", "")[:16],
        "last":         bars[-1].get("t", "")[:16],
    }


def run_probe() -> str:
    """Full probe, formatted for Telegram."""
    if not API_KEY or not SECRET_KEY:
        return ("❌ <b>Alpaca not configured</b>\n\n"
                "Set <code>ALPACA_API_KEY</code> and <code>ALPACA_SECRET_KEY</code> "
                "in Railway variables.")

    lines = ["🔬 <b>ALPACA FREE-TIER DATA PROBE</b>\n"]

    # 1. Auth
    acct = check_auth()
    if not acct["ok"]:
        return "\n".join(lines) + f"\n❌ <b>Auth failed</b>\n{acct['error']}"
    lines.append(
        f"✅ <b>Connected</b> — {acct['status']}\n"
        f"   Cash ${acct['cash']:,.2f} | Equity ${acct['equity']:,.2f}\n"
    )

    # 2. Benchmark — what good coverage looks like
    lines.append("<b>Benchmark (liquid large cap)</b>")
    b = bar_quality("AAPL")
    if b.get("bars"):
        lines.append(
            f"  AAPL: {b['bars']} bars, {b['coverage_pct']}% coverage, "
            f"max gap {b['max_gap_min']}min"
        )
    else:
        lines.append(f"  AAPL: {b.get('note') or b.get('error')}")
    lines.append("")

    # 3. The real test — actual small-cap movers
    lines.append("<b>Today's movers (the real test)</b>")
    movers, err = get_movers(8)
    if err:
        lines.append(f"  ⚠️ Screener unavailable: {err}")
    elif not movers:
        lines.append("  (no movers returned — market may be closed)")
    else:
        for m in movers[:6]:
            sym = m.get("symbol", "?")
            pct = m.get("percent_change", 0)
            q   = bar_quality(sym)
            if not q.get("ok"):
                lines.append(f"  {sym} (+{pct:.0f}%): ERROR {q.get('error','')[:40]}")
            elif q["bars"] == 0:
                lines.append(f"  ❌ {sym} (+{pct:.0f}%): NO BARS on IEX")
            else:
                flag = "✅" if q["coverage_pct"] >= 50 else "⚠️"
                lines.append(
                    f"  {flag} {sym} (+{pct:.0f}%): {q['bars']} bars, "
                    f"{q['coverage_pct']}% cov, gap {q['max_gap_min']}min, "
                    f"vol {q['total_volume']:,}"
                )

    lines.append(
        "\n<i>Coverage is bars returned vs minutes elapsed. Below ~50% on the "
        "movers means IEX can't see enough of the tape to trade this strategy, "
        "and we'd need Schwab or a paid feed.</i>"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import re
    print(re.sub(r"<[^>]+>", "", run_probe()))
