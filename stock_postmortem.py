#!/usr/bin/env python3
"""
stock_postmortem.py — Learning loop for Stock Golem.

Every entry is logged with the full context that produced it. When the trade
closes, the outcome is written back against that context. Over time this answers
questions the strategy documents cannot:

  - Does the AI veto actually improve results, or is it rejecting winners?
  - Which catalyst types produce winners? Is "FDA approval" really better than
    "contract win", or does that just sound better?
  - Does the 5x RVOL threshold matter, or would 3x work equally well?
  - Is Ross's 10:30 cutoff right for OUR execution, or should it be earlier?
  - Do 5-pillar setups actually beat 4-pillar ones?

The summaries feed back into the research prompt, so the AI sees its own track
record on similar setups before approving the next one.

DESIGN NOTE: this records what the system BELIEVED at entry, not just what
happened. A trade log tells you P&L. A postmortem tells you which of your
beliefs were wrong — which is the only thing that lets you improve.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests as _requests

log = logging.getLogger(__name__)

MIN_SAMPLE = int(os.getenv("STOCK_PM_MIN_SAMPLE", "5"))


# ─── STORAGE ──────────────────────────────────────────────────────────────────

def _first_env(names: tuple) -> str:
    for n in names:
        v = os.getenv(n, "").strip()
        if v:
            return v
    return ""


_REDIS_URL = _first_env((
    "UPSTASH_REDIS_URL", "UPSTASH_REDIS_REST_URL", "KV_REST_API_URL", "REDIS_URL"))
_REDIS_TOKEN = _first_env((
    "UPSTASH_REDIS_TOKEN", "UPSTASH_REDIS_REST_TOKEN", "KV_REST_API_TOKEN", "REDIS_TOKEN"))
_KEY = "stock_postmortem"

_LOCAL = os.path.join(
    os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__)),
    "stock_postmortem.json")


def _redis_get(key: str):
    if not (_REDIS_URL and _REDIS_TOKEN):
        return None
    try:
        r = _requests.post(_REDIS_URL,
                           headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                           json=["GET", key], timeout=5)
        if r.status_code == 200:
            v = r.json().get("result")
            return json.loads(v) if v else None
    except Exception as e:
        log.error(f"Stock postmortem: Redis GET failed: {e}")
    return None


def _redis_set(key: str, data: dict) -> bool:
    if not (_REDIS_URL and _REDIS_TOKEN):
        return False
    try:
        r = _requests.post(_REDIS_URL,
                           headers={"Authorization": f"Bearer {_REDIS_TOKEN}"},
                           json=["SET", key, json.dumps(data, default=str)],
                           timeout=5)
        return r.status_code == 200
    except Exception as e:
        log.error(f"Stock postmortem: Redis SET failed: {e}")
    return False


def _load() -> dict:
    d = _redis_get(_KEY)
    if d:
        return d
    if os.path.exists(_LOCAL):
        try:
            with open(_LOCAL) as f:
                return json.load(f)
        except Exception:
            pass
    return {"calls": [], "vetoes": []}


def _save(state: dict):
    if _redis_set(_KEY, state):
        return
    try:
        with open(_LOCAL, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Stock postmortem: save failed: {e}")


# ─── LOGGING ──────────────────────────────────────────────────────────────────

def log_entry(pos: dict, snap: dict, pillars: dict,
              pullback: dict, review: dict) -> int:
    """Record the full belief-state at entry. Returns the call id."""
    state = _load()
    cat = snap.get("catalyst") or {}

    record = {
        "id":        len(state["calls"]) + 1,
        "symbol":    pos["symbol"],
        "entered_at": pos.get("opened_at", datetime.now(timezone.utc).isoformat()),
        "entry_hour_et": _hour_et(pos.get("opened_at", "")),

        # what we believed
        "pillars_passed":   pillars.get("passed"),
        "grade":            pillars.get("grade"),
        "rvol":             snap.get("rvol"),
        "pct_change":       snap.get("pct_change"),
        "float_m":          snap.get("float_m"),
        "price":            snap.get("price"),
        "catalyst_quality": cat.get("quality"),
        "catalyst_type":    cat.get("catalyst_type"),
        "catalyst_score":   cat.get("score"),
        "retrace_pct":      pullback.get("retrace_pct"),
        "surge_candles":    pullback.get("surge_candles"),
        "pullback_candles": pullback.get("pullback_candles"),
        "risk_per_share":   pullback.get("risk_per_share"),
        "ai_confidence":    review.get("confidence"),
        "ai_reasoning":     review.get("reasoning", "")[:300],
        "front_side":       review.get("front_side"),
        "setup":            pos.get("setup"),
        "shares":           pos.get("shares"),
        "entry":            pos.get("entry"),
        "stop":             pos.get("stop"),
        "target":           pos.get("target"),

        # filled on close
        "outcome":     None,
        "exit":        None,
        "pnl":         None,
        "r_multiple":  None,
        "exit_reason": None,
        "held_minutes": None,
        "closed_at":   None,
    }
    state["calls"].append(record)
    _save(state)
    log.info(f"Postmortem: logged entry #{record['id']} {pos['symbol']} "
             f"grade {pillars.get('grade')} conf {review.get('confidence')}")
    return record["id"]


def log_outcome(symbol: str, trade: dict):
    """Write the result back against the most recent open call for this symbol."""
    state = _load()
    rec = next((c for c in reversed(state["calls"])
                if c["symbol"] == symbol and c["outcome"] is None), None)
    if not rec:
        log.warning(f"Postmortem: no open call for {symbol}")
        return

    risk_ps = (rec.get("entry") or 0) - (rec.get("stop") or 0)
    pnl_ps  = (trade.get("exit") or 0) - (rec.get("entry") or 0)

    rec.update({
        "outcome":      "win" if trade.get("won") else "loss",
        "exit":         trade.get("exit"),
        "pnl":          trade.get("pnl"),
        "r_multiple":   round(pnl_ps / risk_ps, 2) if risk_ps else 0,
        "exit_reason":  trade.get("reason"),
        "held_minutes": trade.get("held_minutes"),
        "closed_at":    trade.get("closed_at"),
    })
    _save(state)
    log.info(f"Postmortem: outcome for {symbol} — {rec['outcome']} "
             f"{rec['r_multiple']:+.2f}R")


def log_veto(symbol: str, snap: dict, pillars: dict,
             pullback: dict, review: dict):
    """
    Record rejections too.

    Without this you can only measure the trades you took, which makes it
    impossible to tell whether the AI veto is protecting you or costing you.
    We store the setup so a later pass can check what the stock actually did.
    """
    state = _load()
    cat = snap.get("catalyst") or {}
    state.setdefault("vetoes", []).append({
        "symbol":     symbol,
        "at":         datetime.now(timezone.utc).isoformat(),
        "hour_et":    _hour_et(datetime.now(timezone.utc).isoformat()),
        "reason":     review.get("veto_reason", ""),
        "confidence": review.get("confidence"),
        "grade":      pillars.get("grade"),
        "pillars_passed": pillars.get("passed"),
        "catalyst_quality": cat.get("quality"),
        "entry_would_be": pullback.get("entry"),
        "stop_would_be":  pullback.get("stop"),
        "target_would_be": pullback.get("target"),
        "price_at_veto":  snap.get("price"),
        "reasoning":  review.get("reasoning", "")[:200],
    })
    # Keep the veto log bounded — it's diagnostic, not an audit trail
    state["vetoes"] = state["vetoes"][-300:]
    _save(state)


def _hour_et(iso: str) -> Optional[int]:
    try:
        from zoneinfo import ZoneInfo
        t = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return t.astimezone(ZoneInfo("America/New_York")).hour
    except Exception:
        return None


# ─── ANALYSIS ─────────────────────────────────────────────────────────────────

def _closed(state: dict) -> list:
    return [c for c in state["calls"] if c.get("outcome")]


def _stats(calls: list) -> Optional[dict]:
    if len(calls) < MIN_SAMPLE:
        return None
    wins = [c for c in calls if c["outcome"] == "win"]
    rs   = [c.get("r_multiple") or 0 for c in calls]
    return {
        "n":        len(calls),
        "win_rate": round(len(wins) / len(calls) * 100, 1),
        "avg_r":    round(sum(rs) / len(rs), 2),
        "total_r":  round(sum(rs), 2),
    }


def _bucket(calls: list, keyfn) -> dict:
    out: dict = {}
    for c in calls:
        k = keyfn(c)
        if k is None:
            continue
        out.setdefault(k, []).append(c)
    return {k: _stats(v) or {"n": len(v), "win_rate": None,
                             "avg_r": None, "total_r": None}
            for k, v in sorted(out.items(), key=lambda x: str(x[0]))}


def get_stats() -> dict:
    state  = _load()
    closed = _closed(state)
    if not closed:
        return {"total": 0, "vetoes": len(state.get("vetoes", []))}

    def rvol_band(c):
        v = c.get("rvol")
        if v is None: return None
        return "5-10x" if v < 10 else ("10-25x" if v < 25 else "25x+")

    def conf_band(c):
        v = c.get("ai_confidence")
        if v is None: return None
        return "60-69" if v < 70 else ("70-79" if v < 80 else "80+")

    def float_band(c):
        v = c.get("float_m")
        if v is None: return None
        return "<5M" if v < 5 else ("5-10M" if v < 10 else "10-20M")

    def retrace_band(c):
        v = c.get("retrace_pct")
        if v is None: return None
        return "<25%" if v < 25 else ("25-40%" if v < 40 else "40-50%")

    return {
        "total":       len(closed),
        "vetoes":      len(state.get("vetoes", [])),
        "overall":     _stats(closed),
        "by_grade":    _bucket(closed, lambda c: c.get("grade")),
        "by_catalyst": _bucket(closed, lambda c: c.get("catalyst_quality")),
        "by_hour_et":  _bucket(closed, lambda c: c.get("entry_hour_et")),
        "by_rvol":     _bucket(closed, rvol_band),
        "by_confidence": _bucket(closed, conf_band),
        "by_float":    _bucket(closed, float_band),
        "by_retrace":  _bucket(closed, retrace_band),
        "by_exit":     _bucket(closed, lambda c: c.get("exit_reason")),
    }


def get_context_summary() -> str:
    """
    Compact track record injected into the research prompt, so the AI sees how
    similar setups have actually performed before approving another one.
    """
    s = get_stats()
    if not s.get("total") or not s.get("overall"):
        return ("No closed trades yet — no calibration data. Judge this setup "
                "on its merits.")

    o = s["overall"]
    lines = [
        f"TRACK RECORD ({o['n']} closed trades):",
        f"  Overall: {o['win_rate']}% win rate, {o['avg_r']:+.2f}R average, "
        f"{o['total_r']:+.1f}R total",
    ]

    def add(label, bucket, fmt="{}"):
        rows = [(k, v) for k, v in bucket.items() if v.get("win_rate") is not None]
        if not rows:
            return
        lines.append(f"  {label}:")
        for k, v in rows:
            lines.append(f"    {fmt.format(k)}: {v['win_rate']}% "
                         f"({v['avg_r']:+.2f}R avg, n={v['n']})")

    add("By catalyst quality", s.get("by_catalyst", {}))
    add("By grade",            s.get("by_grade", {}))
    add("By hour (ET)",        s.get("by_hour_et", {}), "{}:00")
    add("By AI confidence",    s.get("by_confidence", {}))
    add("By retrace depth",    s.get("by_retrace", {}))

    lines.append(
        "\nUse this. If a bucket is consistently losing, weight against similar "
        "setups. If a bucket is strong, that's evidence — but respect small "
        f"samples (n<{MIN_SAMPLE} is shown without stats)."
    )
    return "\n".join(lines)


def format_telegram() -> str:
    s = get_stats()
    if not s.get("total"):
        return (f"📊 *STOCK POSTMORTEM*\n\nNo closed trades yet.\n"
                f"{s.get('vetoes',0)} setup(s) vetoed so far.")

    o = s["overall"]
    lines = [
        "📊 *STOCK POSTMORTEM*\n",
        f"Closed trades: {o['n']}  ·  Vetoed: {s['vetoes']}",
        f"Win rate: *{o['win_rate']}%*",
        f"Avg: *{o['avg_r']:+.2f}R*  ·  Total: *{o['total_r']:+.1f}R*",
        "",
    ]

    def section(title, bucket, fmt="{}"):
        rows = [(k, v) for k, v in bucket.items() if v.get("win_rate") is not None]
        if not rows:
            return
        lines.append(f"*{title}:*")
        for k, v in rows:
            lines.append(f"  {fmt.format(k)}: {v['win_rate']}% "
                         f"({v['avg_r']:+.2f}R, n={v['n']})")
        lines.append("")

    section("By catalyst",  s.get("by_catalyst", {}))
    section("By grade",     s.get("by_grade", {}))
    section("By hour ET",   s.get("by_hour_et", {}), "{}:00")
    section("By RVOL",      s.get("by_rvol", {}))
    section("By confidence", s.get("by_confidence", {}))
    section("By exit",      s.get("by_exit", {}))

    lines.append(f"_Buckets with fewer than {MIN_SAMPLE} trades are hidden — "
                 f"too small to mean anything._")
    return "\n".join(lines)


def reset() -> dict:
    """Archive then clear."""
    old = _load()
    if old.get("calls"):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        if not _redis_set(f"stock_postmortem_archive_{stamp}", old):
            log.error("ARCHIVE WRITE FAILED — refusing to reset without a backup.")
            return {"ok": False, "error": "archive write failed; nothing was reset"}
        log.warning(f"Postmortem: archived {len(old['calls'])} calls")
    fresh = {"calls": [], "vetoes": []}
    _save(fresh)
    return fresh


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(format_telegram().replace("*", ""))
