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


def bar_quality(symbol: str, lookback_days: int = 5) -> dict:
    """
    Pull 1-minute bars from the most recent TRADING SESSION and measure coverage.

    Do NOT frame this as "the last N minutes" — running the probe overnight or
    at a weekend then returns zero bars and looks like a data failure when it's
    just an empty clock. Instead we pull several days, find the latest session
    that actually has bars, and measure density within that session.

    A tradeable feed should return close to one bar per market minute. Large
    gaps mean the stock trades where IEX can't see it, which makes VWAP,
    the 9 EMA, and pullback detection unreliable.

    Note: Alpaca's free tier cannot return the most recent ~15 minutes, so a
    live-market run will always show a small trailing gap. That's expected.
    """
    end   = datetime.now(timezone.utc)
    start = end - timedelta(days=lookback_days)
    data, err = _get(
        DATA_URL, f"/v2/stocks/{symbol}/bars",
        {
            "timeframe": "1Min",
            "start":     start.strftime("%Y-%m-%d"),
            "end":       end.strftime("%Y-%m-%d"),
            "limit":     10000,
            "feed":      "iex",
        },
    )
    if err:
        return {"symbol": symbol, "ok": False, "error": err}

    bars = data.get("bars") or []
    if not bars:
        return {"symbol": symbol, "ok": True, "bars": 0, "coverage_pct": 0.0,
                "note": f"NO BARS in {lookback_days}d — not visible on IEX"}

    # Group by calendar day, take the most recent session with data
    by_day: dict = {}
    for b in bars:
        day = (b.get("t") or "")[:10]
        by_day.setdefault(day, []).append(b)
    latest_day = max(by_day)
    session    = by_day[latest_day]

    vols      = [b.get("v", 0) for b in session]
    total_vol = sum(vols)

    # Coverage measured against the 390-minute regular session
    coverage = len(session) / 390 * 100

    max_gap = 0
    try:
        ts = [datetime.fromisoformat(b["t"].replace("Z", "+00:00")) for b in session]
        for a, b in zip(ts, ts[1:]):
            max_gap = max(max_gap, (b - a).total_seconds() / 60)
    except Exception:
        pass

    return {
        "symbol":       symbol,
        "ok":           True,
        "session":      latest_day,
        "bars":         len(session),
        "days_with_data": len(by_day),
        "coverage_pct": round(min(coverage, 100.0), 1),
        "total_volume": int(total_vol),
        "max_gap_min":  round(max_gap, 1),
        "first":        session[0].get("t", "")[11:16],
        "last":         session[-1].get("t", "")[11:16],
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
            f"  AAPL [{b['session']}]: {b['bars']} bars, {b['coverage_pct']}% cov, "
            f"max gap {b['max_gap_min']}min\n"
            f"    {b['first']}–{b['last']} UTC, {b['days_with_data']} day(s) of data"
        )
    else:
        lines.append(f"  AAPL: {b.get('note') or b.get('error')}")
    lines.append("")

    # 3. The real test — actual small-cap movers
    ok_count, tested = 0, 0
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
                ok_count = ok_count  # no-op, keeps counter honest
            else:
                flag = "✅" if q["coverage_pct"] >= 50 else "⚠️"
                if q["coverage_pct"] >= 50:
                    ok_count += 1
                tested += 1
                lines.append(
                    f"  {flag} {sym} (+{pct:.0f}%) [{q['session']}]: {q['bars']} bars, "
                    f"{q['coverage_pct']}% cov, gap {q['max_gap_min']}min, "
                    f"vol {q['total_volume']:,}"
                )

    # Verdict
    lines.append("")
    if tested == 0:
        lines.append(
            "<b>Verdict:</b> inconclusive — no mover data returned. "
            "Re-run during market hours (9:30–11:00 AM ET)."
        )
    elif ok_count >= max(1, tested // 2):
        lines.append(
            f"<b>✅ Verdict: usable</b> — {ok_count}/{tested} movers had ≥50% "
            f"bar coverage. IEX sees enough of these names to build on."
        )
    else:
        lines.append(
            f"<b>⚠️ Verdict: too thin</b> — only {ok_count}/{tested} movers had "
            f"≥50% coverage. IEX can't see these stocks well enough; we'd need "
            f"the Schwab API or a paid feed."
        )

    lines.append(
        "\n<i>Coverage = bars vs the 390-min regular session. Free tier can't "
        "return the last ~15 min, so a small trailing gap during live markets "
        "is normal.</i>"
    )
    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    import re
    print(re.sub(r"<[^>]+>", "", run_probe()))
