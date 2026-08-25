#!/usr/bin/env python3
"""
fomo_intel.py — Memory for forwarded intel.

WHAT WAS WRONG
The screenshot reader extracted PoorGoat's CATE thesis correctly — stance,
conviction, the bias warning about a poster sitting on a +1,834% position —
formatted it, sent it, and threw all of it away. Nothing was stored. Nothing
changed. The next decision about CATE would start from zero.

That is a display, not a system. Reading is not learning; learning means the
next decision is different because of what was read.

WHAT THIS ADDS
A persistent record keyed by token symbol. Every forwarded screenshot writes
here, and three things read it back:

  1. RE-ENTRY DETECTION. If intel arrives on a token we recently exited —
     which is exactly the CATE case — that is its own signal. We have history:
     an entry price, an exit price, a reason, and the aftermath watchlist
     already tracks what it did next. A bullish thesis on something we sold
     two days ago deserves a different response than a cold call.

  2. RESEARCH CONTEXT. research_token() gets the accumulated intel, so a buy
     decision can weigh what the trader actually said rather than starting
     blind.

  3. STALENESS. Intel decays. A thesis from six days ago is history, not
     information, and is scored accordingly.

WHAT IT STILL DOESN'T DO
Buy anything. Screenshots are unverifiable and a poster with a 1,834% gain has
every incentive to talk their book — the extraction flagged that itself. This
raises candidates and informs decisions; the trigger stays human.
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

_DATA_DIR  = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", os.path.dirname(__file__))
STATE_FILE = os.path.join(_DATA_DIR, "fomo_intel.json")
STATE_KEY  = "fomo_intel"

# Intel older than this is history, not signal.
FRESH_HOURS   = float(os.getenv("FOMO_INTEL_FRESH_H", "48"))
# Keep this many entries per token before dropping the oldest.
MAX_PER_TOKEN = int(os.getenv("FOMO_INTEL_MAX_PER_TOKEN", "12"))


def _redis():
    try:
        from kalshi_portfolio import _redis_get, _redis_set
        return _redis_get, _redis_set
    except Exception:
        return None, None


def _load() -> dict:
    get, _ = _redis()
    if get:
        d = get(STATE_KEY)
        if d:
            return d
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.error(f"Intel: load error: {e}")
    return {"tokens": {}}


def _save(state: dict):
    _, put = _redis()
    if put:
        put(STATE_KEY, state)
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Intel: save error: {e}")


# ─── WRITE ────────────────────────────────────────────────────────────────────

def record(item: dict, extracted: dict, note: str = "") -> dict:
    """
    Store one token's worth of intel from a screenshot.

    Returns the stored entry, including whether this token is one we recently
    held — the caller uses that to decide whether it's a re-entry case.
    """
    symbol = (item.get("symbol") or "").lstrip("$").upper()
    if not symbol:
        return {}

    state = _load()
    entry = {
        "at":          datetime.now(timezone.utc).isoformat(),
        "source":      "screenshot",
        "poster":      extracted.get("poster_handle") or extracted.get("poster_name"),
        "platform":    extracted.get("platform"),
        "posted_when": extracted.get("posted_when"),
        "stance":      item.get("stance"),
        "conviction":  item.get("conviction"),
        "quote":       (item.get("key_quote") or "")[:400],
        "is_new_call": bool(item.get("is_new_call")),
        "identifier":  item.get("identifier"),
        "identifier_unverified": item.get("identifier_unverified"),
        "commentary":  extracted.get("market_commentary"),
        # The extraction's own scepticism is worth keeping. It flagged that a
        # poster up 1,834% is talking their book — that caveat should reach
        # the buy decision, not just the Telegram message.
        "caution":     extracted.get("caution"),
        "user_note":   note[:300],
    }

    lst = state["tokens"].setdefault(symbol, [])
    lst.append(entry)
    if len(lst) > MAX_PER_TOKEN:
        state["tokens"][symbol] = lst[-MAX_PER_TOKEN:]
    _save(state)

    log.info(f"Intel: recorded {symbol} — {entry['stance']} "
             f"({entry['conviction']}) from {entry['poster']}")
    return entry


# ─── READ ─────────────────────────────────────────────────────────────────────

def get_intel(symbol: str, fresh_only: bool = True) -> list:
    """Everything we've been told about this token, newest last."""
    symbol = (symbol or "").lstrip("$").upper()
    entries = _load()["tokens"].get(symbol, [])
    if not fresh_only:
        return entries
    cutoff = datetime.now(timezone.utc) - timedelta(hours=FRESH_HOURS)
    out = []
    for e in entries:
        try:
            if datetime.fromisoformat(str(e["at"]).replace("Z", "+00:00")) >= cutoff:
                out.append(e)
        except Exception:
            pass
    return out


def check_reentry(symbol: str) -> dict:
    """
    Did we hold this before, and what happened after we sold?

    This is the CATE question. The aftermath watcher already tracks every
    token we exited and the peak it reached since — so when intel arrives on
    a name we sold, we can answer "how did that exit age?" instead of
    treating it as a cold call.
    """
    symbol = (symbol or "").lstrip("$").upper()
    out = {"previously_held": False}

    try:
        from fomo_aftermath import _load as af_load
        af = af_load()
        for w in af.get("watching", []) + af.get("completed", []):
            if (w.get("ticker") or "").lstrip("$").upper() != symbol:
                continue
            exit_px = float(w.get("exit_price") or 0)
            peak    = float(w.get("peak_after") or 0)
            last    = float(w.get("last_price") or 0)
            out.update({
                "previously_held": True,
                "entry_price":  w.get("entry_price"),
                "exit_price":   exit_px,
                "exit_reason":  w.get("exit_reason"),
                "exited_at":    w.get("exited_at"),
                "peak_since":   peak,
                "last_price":   last,
                "peak_multiple": round(peak / exit_px, 2) if exit_px else None,
                "now_multiple":  round(last / exit_px, 2) if exit_px else None,
            })
            break
    except Exception as e:
        out["error"] = str(e)
    return out


def format_for_research(symbol: str) -> str:
    """
    Compact intel summary for injection into research_token's context.

    Deliberately includes the cautions. A buy decision that sees only the
    bullish quotes and not "poster may be heavily biased due to a large
    profitable position" is worse informed than one with no intel at all.
    """
    entries = get_intel(symbol)
    if not entries:
        return ""

    lines = [f"FORWARDED INTEL on {symbol} ({len(entries)} recent):"]
    for e in entries[-5:]:
        lines.append(
            f"  [{str(e['at'])[:16]}] {e.get('poster') or 'unknown'}: "
            f"{e.get('stance')} ({e.get('conviction')}) — "
            f"\"{(e.get('quote') or '')[:140]}\"")
        if e.get("caution"):
            lines.append(f"      CAUTION: {e['caution'][:140]}")
        if e.get("user_note"):
            lines.append(f"      user note: {e['user_note'][:120]}")

    re_entry = check_reentry(symbol)
    if re_entry.get("previously_held"):
        lines.append(
            f"  WE HELD THIS BEFORE: exited at ${re_entry['exit_price']:.8f} "
            f"({re_entry['exit_reason']}); peak since is "
            f"{re_entry.get('peak_multiple')}x that exit.")
    return "\n".join(lines)


def build_report(limit: int = 10) -> str:
    """Plain text — tickers and quotes break Markdown."""
    state = _load()
    tokens = state.get("tokens", {})
    if not tokens:
        return ("INTEL — forwarded screenshots\n\n"
                "Nothing recorded yet. Forward a screenshot to any bot and it "
                "starts here.")

    scored = []
    for sym, entries in tokens.items():
        if entries:
            scored.append((entries[-1].get("at", ""), sym, entries))
    scored.sort(reverse=True)

    L = ["INTEL — forwarded screenshots", ""]
    for _, sym, entries in scored[:limit]:
        latest = entries[-1]
        fresh  = len(get_intel(sym))
        L.append(f"{sym}  ({len(entries)} total, {fresh} fresh)")
        L.append(f"  latest: {latest.get('stance')} ({latest.get('conviction')}) "
                 f"from {latest.get('poster') or 'unknown'} "
                 f"at {str(latest.get('at'))[:16]}")
        r = check_reentry(sym)
        if r.get("previously_held"):
            L.append(f"  WE SOLD THIS: exit {r['exit_reason']}, "
                     f"peak since {r.get('peak_multiple')}x, "
                     f"now {r.get('now_multiple')}x")
        if latest.get("caution"):
            L.append(f"  caution: {latest['caution'][:100]}")
        L.append("")
    return "\n".join(L)
