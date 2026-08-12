#!/usr/bin/env python3
"""
fomo_convergence.py — Multi-wallet convergence detection.

When 2+ tracked wallets buy the same token within a short window, that is
a significantly stronger signal than a single wallet acting alone. This module
tracks recent BUY signals in memory and flags convergence events.

Convergence tiers:
  2 wallets in 2h  → CONVERGENCE  (+10% position size boost)
  3+ wallets in 2h → ULTRA        (+20% position size boost, new Telegram alert)

State is in-memory only — convergence is a short-term signal. Signals older
than WINDOW_HOURS are pruned automatically on each call.
"""

import logging
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger(__name__)

WINDOW_HOURS = 2      # how far back to look for matching buys
BOOST_2X     = 10.0  # extra position % when 2 wallets align
BOOST_3X     = 20.0  # extra position % when 3+ wallets align

# Thread-safe in-memory log: { contract: [ {alias, ts}, ... ] }
_lock     = threading.Lock()
_buy_log: dict[str, list] = {}


def record_signal(alias: str, contract: str):
    """
    Record that `alias` just bought `contract`.
    Called on every BUY signal before convergence check.
    """
    if not alias or not contract:
        return
    now = datetime.now(timezone.utc)
    with _lock:
        if contract not in _buy_log:
            _buy_log[contract] = []
        _buy_log[contract].append({"alias": alias, "ts": now})
        # Prune old entries while we're here
        cutoff = now - timedelta(hours=WINDOW_HOURS)
        _buy_log[contract] = [e for e in _buy_log[contract] if e["ts"] > cutoff]


def check_convergence(alias: str, contract: str) -> dict:
    """
    Check if other tracked wallets have recently bought the same contract.

    Returns a dict:
      {
        "count":       int,           # total unique wallets in window (including current)
        "other_names": list[str],     # aliases of OTHER wallets (excluding current)
        "boost_pct":   float,         # extra position % to add
        "label":       str,           # human-readable label for Telegram
        "is_convergence": bool,       # True if 2+ wallets
      }
    """
    now     = datetime.now(timezone.utc)
    cutoff  = now - timedelta(hours=WINDOW_HOURS)

    with _lock:
        entries = _buy_log.get(contract, [])
        recent  = [e for e in entries if e["ts"] > cutoff]

    # Deduplicate by alias, exclude current caller
    seen       = {}
    other_names = []
    for e in recent:
        a = e["alias"]
        if a not in seen:
            seen[a] = e["ts"]
            if a != alias:
                other_names.append(a)

    total_count = len(seen) + (0 if alias in seen else 1)  # include current wallet

    if total_count >= 3:
        return {
            "count":          total_count,
            "other_names":    other_names,
            "boost_pct":      BOOST_3X,
            "label":          f"\U0001f525\U0001f525 ULTRA CONVERGENCE: {', '.join(other_names)} also buying",
            "is_convergence": True,
        }
    elif total_count == 2:
        return {
            "count":          total_count,
            "other_names":    other_names,
            "boost_pct":      BOOST_2X,
            "label":          f"\U0001f525 CONVERGENCE: {', '.join(other_names)} also in this token",
            "is_convergence": True,
        }
    else:
        return {
            "count":          1,
            "other_names":    [],
            "boost_pct":      0.0,
            "label":          "",
            "is_convergence": False,
        }


def get_active_convergences() -> list[dict]:
    """Return all contracts currently showing convergence (2+ wallets). For debugging."""
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=WINDOW_HOURS)
    result = []
    with _lock:
        for contract, entries in _buy_log.items():
            recent = [e for e in entries if e["ts"] > cutoff]
            aliases = list({e["alias"] for e in recent})
            if len(aliases) >= 2:
                result.append({"contract": contract, "wallets": aliases, "count": len(aliases)})
    return result
