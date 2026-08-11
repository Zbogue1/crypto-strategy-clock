#!/usr/bin/env python3
"""
fomo_wallet_stats.py — Outcome tracking and auto-promotion/demotion for tracked wallets.

Called after every trade close. Updates per-wallet win/loss records, recalculates
rolling win rate, and promotes or demotes wallets based on observed performance.

Promotion to Tier A:   5+ trades followed AND rolling win rate >= 70%
Demotion from Tier A:  3 consecutive losses OR rolling win rate < 45% over 5+ trades

Performance data is stored in fomo_wallet_performance.json on the GitHub data branch
so it survives Railway redeploys. trusted_wallets.json tier assignments are updated
locally and committed automatically via the data branch sync mechanism.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO        = "Zbogue1/crypto-strategy-clock"
GITHUB_DATA_BRANCH = "data"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

PERFORMANCE_FILE    = "fomo_wallet_performance.json"
TRUSTED_WALLETS_FILE = "trusted_wallets.json"

PROMOTE_MIN_TRADES   = 5     # need at least this many trades before promoting
PROMOTE_WIN_RATE     = 0.70  # 70%+ rolling win rate → promote to Tier A
DEMOTE_WIN_RATE      = 0.45  # below 45% over 5+ trades → demote to Tier B
DEMOTE_CONSEC_LOSSES = 3     # 3 consecutive losses → demote regardless of overall rate


# ─── GITHUB HELPERS ───────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }


def _pull_performance() -> dict:
    """Load performance data from GitHub data branch, or return empty dict."""
    if not GITHUB_TOKEN:
        # Fallback: try local file
        try:
            if os.path.exists(PERFORMANCE_FILE):
                with open(PERFORMANCE_FILE) as f:
                    return json.load(f)
        except Exception:
            pass
        return {}
    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PERFORMANCE_FILE}?ref={GITHUB_DATA_BRANCH}",
            headers=_gh_headers(), timeout=10,
        )
        if r.status_code == 200:
            import base64
            content = base64.b64decode(r.json()["content"]).decode("utf-8")
            return json.loads(content)
    except Exception as e:
        log.warning(f"WalletStats: could not pull performance from GitHub: {e}")
    return {}


def _push_performance(data: dict):
    """Push performance data to GitHub data branch."""
    # Always write locally first
    try:
        with open(PERFORMANCE_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"WalletStats: could not write local performance file: {e}")

    if not GITHUB_TOKEN:
        return
    try:
        import base64
        content_bytes = json.dumps(data, indent=2, default=str).encode("utf-8")
        content_b64   = base64.b64encode(content_bytes).decode("utf-8")

        # Get current SHA if file exists
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PERFORMANCE_FILE}?ref={GITHUB_DATA_BRANCH}",
            headers=_gh_headers(), timeout=10,
        )
        sha = r.json().get("sha") if r.status_code == 200 else None

        payload = {
            "message": "auto: update wallet performance stats",
            "content": content_b64,
            "branch":  GITHUB_DATA_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{PERFORMANCE_FILE}",
            headers=_gh_headers(), json=payload, timeout=15,
        )
    except Exception as e:
        log.warning(f"WalletStats: could not push performance to GitHub: {e}")


# ─── TELEGRAM NOTIFICATION ────────────────────────────────────────────────────

def _send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"WalletStats: Telegram notify failed: {e}")


# ─── TRUSTED WALLETS TIER UPDATE ─────────────────────────────────────────────

def _update_wallet_tier(alias: str, new_tier: str, reason: str):
    """Update a wallet's tier in trusted_wallets.json."""
    try:
        with open(TRUSTED_WALLETS_FILE) as f:
            data = json.load(f)

        for tier_key in ("tier_a", "tier_b"):
            for w in data.get(tier_key, []):
                if w.get("alias") == alias:
                    old_tier = w.get("tier")
                    if old_tier == new_tier:
                        return  # already correct tier
                    w["tier"] = new_tier
                    if new_tier == "A":
                        w["promoted_at"] = datetime.now(timezone.utc).isoformat()
                        w["demoted_at"]  = None
                    else:
                        w["demoted_at"]  = datetime.now(timezone.utc).isoformat()
                        w["promoted_at"] = None

                    # Move between tier lists
                    data[tier_key].remove(w)
                    target_key = "tier_a" if new_tier == "A" else "tier_b"
                    data[target_key].append(w)

                    with open(TRUSTED_WALLETS_FILE, "w") as f:
                        json.dump(data, f, indent=2)

                    log.info(f"WalletStats: {alias} moved {old_tier} → {new_tier} | {reason}")
                    return

    except Exception as e:
        log.warning(f"WalletStats: could not update tier for {alias}: {e}")


# ─── MAIN OUTCOME RECORDING ───────────────────────────────────────────────────

def record_trade_outcome(
    alias:      str,
    token:      str,
    win:        bool,
    pnl_pct:    float,
    pnl_usd:    float,
    exit_reason: str = "unknown",
):
    """
    Called after every trade close. Updates wallet performance stats,
    checks for promotion/demotion, and notifies via Telegram if tier changes.

    Args:
        alias:       wallet alias from trusted_wallets.json (e.g. "Binkieee")
        token:       token ticker (e.g. "PUMP")
        win:         True if trade was profitable
        pnl_pct:     P&L as a percentage (e.g. +42.5 or -15.0)
        pnl_usd:     P&L in dollars
        exit_reason: why the trade closed (stop_loss, tranche_1_2x, trailing_stop, etc.)
    """
    if not alias or alias == "unknown":
        return

    perf = _pull_performance()

    if alias not in perf:
        perf[alias] = {
            "trades_followed":   0,
            "wins":              0,
            "losses":            0,
            "total_pnl_pct":     0.0,
            "total_pnl_usd":     0.0,
            "consecutive_losses": 0,
            "win_rate":          None,
            "last_trade_at":     None,
            "last_outcome":      None,
            "last_token":        None,
            "trade_log":         [],   # last 20 trades for rolling calc
        }

    w = perf[alias]
    w["trades_followed"] += 1
    w["total_pnl_pct"]   = round(w["total_pnl_pct"] + pnl_pct, 2)
    w["total_pnl_usd"]   = round(w["total_pnl_usd"] + pnl_usd, 2)
    w["last_trade_at"]   = datetime.now(timezone.utc).isoformat()
    w["last_outcome"]    = "WIN" if win else "LOSS"
    w["last_token"]      = token

    if win:
        w["wins"]             += 1
        w["consecutive_losses"] = 0
    else:
        w["losses"]           += 1
        w["consecutive_losses"] = w.get("consecutive_losses", 0) + 1

    # Keep rolling log of last 20 trades
    w["trade_log"].append({
        "token":      token,
        "win":        win,
        "pnl_pct":   round(pnl_pct, 2),
        "pnl_usd":   round(pnl_usd, 2),
        "reason":    exit_reason,
        "at":        w["last_trade_at"],
    })
    w["trade_log"] = w["trade_log"][-20:]

    # Rolling win rate over last 10 trades (or all trades if fewer)
    recent = w["trade_log"][-10:]
    recent_wins = sum(1 for t in recent if t["win"])
    w["win_rate"] = round(recent_wins / len(recent), 3) if recent else None

    n = w["trades_followed"]

    # ── Promotion check ─────────────────────────────────────────────────────
    if n >= PROMOTE_MIN_TRADES and w["win_rate"] and w["win_rate"] >= PROMOTE_WIN_RATE:
        _update_wallet_tier(alias, "A", f"{w['win_rate']*100:.0f}% win rate over {n} trades")
        _send_telegram(
            f"\U0001f31f <b>WALLET PROMOTED: {alias} → Tier A</b>\n"
            f"Win rate: {w['win_rate']*100:.0f}% over {n} trades followed\n"
            f"Total P&L: ${w['total_pnl_usd']:+.2f}"
        )

    # ── Demotion check ───────────────────────────────────────────────────────
    elif w["consecutive_losses"] >= DEMOTE_CONSEC_LOSSES:
        _update_wallet_tier(alias, "B", f"{w['consecutive_losses']} consecutive losses")
        _send_telegram(
            f"\U0001f4c9 <b>WALLET DEMOTED: {alias} → Tier B</b>\n"
            f"{w['consecutive_losses']} consecutive losses\n"
            f"Win rate: {w['win_rate']*100:.0f}% | Last: {token} {'+' if win else ''}{pnl_pct:.1f}%"
        )

    elif n >= PROMOTE_MIN_TRADES and w["win_rate"] and w["win_rate"] < DEMOTE_WIN_RATE:
        _update_wallet_tier(alias, "B", f"win rate {w['win_rate']*100:.0f}% below {DEMOTE_WIN_RATE*100:.0f}% floor")
        _send_telegram(
            f"\U0001f4c9 <b>WALLET DEMOTED: {alias} → Tier B</b>\n"
            f"Win rate dropped to {w['win_rate']*100:.0f}% over last {len(recent)} trades\n"
            f"Total P&L: ${w['total_pnl_usd']:+.2f}"
        )

    _push_performance(perf)
    log.info(
        f"WalletStats: {alias} | {'WIN' if win else 'LOSS'} {token} {pnl_pct:+.1f}% | "
        f"Record: {w['wins']}W/{w['losses']}L | WR: {w['win_rate']*100:.0f}%" if w['win_rate'] else
        f"WalletStats: {alias} | {'WIN' if win else 'LOSS'} {token} {pnl_pct:+.1f}% | "
        f"Record: {w['wins']}W/{w['losses']}L"
    )


# ─── LEADERBOARD SUMMARY ─────────────────────────────────────────────────────

def get_wallet_leaderboard() -> str:
    """Return a Telegram-formatted leaderboard of tracked wallet performance."""
    perf = _pull_performance()
    if not perf:
        return "No trade outcomes recorded yet."

    rows = []
    for alias, w in perf.items():
        n  = w.get("trades_followed", 0)
        if n == 0:
            continue
        wr = w.get("win_rate")
        wr_str = f"{wr*100:.0f}%" if wr is not None else "n/a"
        pnl = w.get("total_pnl_usd", 0)
        rows.append((pnl, alias, n, wr_str, pnl))

    if not rows:
        return "No completed trades yet."

    rows.sort(reverse=True)
    lines = ["<b>📊 WALLET LEADERBOARD (our observed trades)</b>\n"]
    for _, alias, n, wr_str, pnl in rows:
        emoji = "🟢" if pnl >= 0 else "🔴"
        lines.append(f"{emoji} <b>{alias}</b> — {n} trades | WR: {wr_str} | P&L: ${pnl:+.2f}")

    return "\n".join(lines)
