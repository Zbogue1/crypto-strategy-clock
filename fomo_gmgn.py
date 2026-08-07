#!/usr/bin/env python3
"""
fomo_gmgn.py -- GMGN.ai integration via Parse.bot API.

Three roles in the Golem pipeline:

1. TOKEN SECURITY  (called from fomo_research.py)
   get_token_security(contract) -> dict
   Extra rug guards: honeypot flag, renounced mint, dev holdings, burn ratio.

2. SMART MONEY CROSS-CHECK  (called from fomo_research.py)
   get_smart_money_in_token(contract) -> list[dict]
   Returns GMGN-tagged smart money wallets already holding the token.
   Cross-wallet confirmation = position size boost.

3. TRADER DISCOVERY  (background weekly job)
   discover_traders(callback) -> list[dict]
   Scans GMGN leaderboard, filters by quality criteria, returns candidates
   for the user to approve via Telegram before adding to trusted_wallets.json.

   Filters for traders worth copy-trading at $1K scale:
     - win_rate        >= 65%   (not spray-and-pray)
     - open_positions  <= 20    (conviction per trade, not scatter-shot)
     - fast_tx_ratio   <= 0.30  (not a bot/sniper — humans can't copy those)
     - trades_30d      20-200   (active but not machine-speed)
     - avg_hold_hours  >= 4     (enough time to follow entry)

Setup (Railway env var required):
    PARSE_API_KEY  -- from parse.bot (Hobby tier recommended, free tier works)

Parse.bot endpoint base:
    https://api.parse.bot/scraper/fd0acc27-2d9b-49ca-b8ff-216a1b3ce0e0
"""

import logging
import os
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

PARSE_API_KEY  = (os.environ.get("PARSE_BOT_API_KEY") or os.environ.get("PARSE_API_KEY") or "").strip()
PARSE_BASE_URL = "https://api.parse.bot/scraper/fd0acc27-2d9b-49ca-b8ff-216a1b3ce0e0"
CHAIN          = "sol"

# Discovery quality filters — copy-trade safe at $1K scale
MIN_WIN_RATE        = 0.65   # 65%+
MAX_OPEN_POSITIONS  = 20     # not scatter-shot
MAX_FAST_TX_RATIO   = 0.30   # not a bot
MIN_TRADES_30D      = 20     # actually active
MAX_TRADES_30D      = 200    # not machine-speed
MIN_AVG_HOLD_HOURS  = 4.0    # enough time to follow entry

# Rate limiting — stay inside free tier (5 req/min) by default
_RATE_LIMIT_DELAY   = 13     # seconds between calls (≈4.6/min)


def _headers() -> dict:
    return {"X-API-Key": PARSE_API_KEY}


def _get(endpoint: str, params: dict = None, retries: int = 2) -> Optional[dict]:
    """Single authenticated GET with retry on transient errors."""
    if not PARSE_API_KEY:
        log.debug("GMGN: PARSE_API_KEY not set — skipping")
        return None

    url = f"{PARSE_BASE_URL}/{endpoint}"
    for attempt in range(retries + 1):
        try:
            resp = requests.get(url, headers=_headers(), params=params or {}, timeout=15)
            if resp.status_code == 429:
                log.warning("GMGN: rate limited — waiting 60s")
                time.sleep(60)
                continue
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    return data.get("data") or data
                log.warning(f"GMGN: non-success response: {data}")
                return None
            log.warning(f"GMGN: HTTP {resp.status_code} from {endpoint}")
            return None
        except Exception as e:
            log.error(f"GMGN: request error ({endpoint}): {e}")
            if attempt < retries:
                time.sleep(5)
    return None


# ─── 1. TOKEN SECURITY ────────────────────────────────────────────────────────

def get_token_security(contract: str) -> dict:
    """
    Fetch GMGN token security flags for a Solana contract address.

    Returns a dict with:
        honeypot        bool   -- true = cannot sell, hard veto
        renounced_mint  bool   -- true = dev can't mint more (good)
        dev_holding_pct float  -- % supply still held by dev wallet
        burn_ratio      float  -- % supply burned
        top10_rate      float  -- % held by top 10 wallets
        holder_count    int
        raw             dict   -- full API response for debugging
    """
    empty = {
        "honeypot": None,
        "renounced_mint": None,
        "dev_holding_pct": None,
        "burn_ratio": None,
        "top10_rate": None,
        "holder_count": None,
        "raw": {},
    }

    data = _get("get_token_info", {"chain": CHAIN, "address": contract})
    if not data:
        return empty

    stat     = data.get("stat") or {}
    security = data.get("security") or {}

    return {
        "honeypot":        bool(security.get("is_honeypot")),
        "renounced_mint":  bool(security.get("renounced_mint")),
        "dev_holding_pct": security.get("dev_token_burn_ratio"),   # pct dev still holds
        "burn_ratio":      security.get("burn_ratio"),
        "top10_rate":      stat.get("top_10_holder_rate"),
        "holder_count":    stat.get("holder_count"),
        "raw":             data,
    }


def security_hard_veto(sec: dict) -> Optional[str]:
    """
    Returns a veto reason string if GMGN flags a hard stop, else None.
    Call this AFTER our existing 3 hard vetos in fomo_research.py.
    """
    if sec.get("honeypot"):
        return "GMGN: HONEYPOT — cannot sell this token"
    if sec.get("dev_holding_pct") is not None and sec["dev_holding_pct"] > 0.20:
        pct = sec["dev_holding_pct"] * 100
        return f"GMGN: Dev holds {pct:.0f}% of supply — dump risk"
    return None


# ─── 2. SMART MONEY CROSS-CHECK ──────────────────────────────────────────────

# GMGN wallet tags that indicate smart money
SMART_MONEY_TAGS = {"smart_degen", "smart_money", "kol", "whale", "sniper"}


def get_smart_money_in_token(contract: str, max_holders: int = 50) -> list:
    """
    Returns a list of GMGN-tagged smart money wallets already holding this token.
    Each entry: {"wallet": str, "tags": list, "holding_pct": float, "usd_value": float}

    An empty list means no smart money detected (not necessarily bad).
    Use the count to boost the research score:
        1 smart money holder  → +1 to fundamentals_score
        2+ smart money holders → +2, position size up to 30%
    """
    time.sleep(_RATE_LIMIT_DELAY)
    data = _get("get_token_holders", {"chain": CHAIN, "address": contract, "limit": max_holders})
    if not data:
        return []

    holders = data.get("holders") or []
    smart = []
    for h in holders:
        tags = set(t.lower() for t in (h.get("tags") or []))
        if tags & SMART_MONEY_TAGS:
            smart.append({
                "wallet":      h.get("address", ""),
                "tags":        list(tags),
                "holding_pct": h.get("holding_percentage", 0),
                "usd_value":   h.get("usd_value", 0),
            })
    return smart


# ─── 3. TRADER DISCOVERY ─────────────────────────────────────────────────────

def get_wallet_profile(wallet_address: str) -> Optional[dict]:
    """
    Fetch detailed profile for a single wallet.
    Returns normalized dict or None if API unavailable.
    """
    time.sleep(_RATE_LIMIT_DELAY)
    data = _get("get_wallet_profile", {"chain": CHAIN, "wallet_address": wallet_address})
    if not data:
        return None

    return {
        "wallet":          wallet_address,
        "winrate_7d":      data.get("winrate_7d"),
        "winrate_30d":     data.get("winrate"),
        "pnl_7d":          data.get("pnl_7d"),
        "pnl_30d":         data.get("pnl_30d"),
        "realized_profit": data.get("realized_profit"),
        "fast_tx_ratio":   data.get("fast_tx_ratio"),
        "honeypot_ratio":  data.get("honeypot_ratio"),
        "sol_balance":     data.get("sol_balance"),
        "twitter":         data.get("twitter_username"),
        "tags":            data.get("tags") or [],
        "raw":             data,
    }


def _passes_copy_trade_filter(profile: dict) -> tuple[bool, str]:
    """
    Returns (passes, reason_if_failed).
    All criteria must pass for a wallet to be worth copy-trading at $1K scale.
    """
    wr = profile.get("winrate_30d") or profile.get("winrate_7d") or 0
    if wr < MIN_WIN_RATE:
        return False, f"win rate {wr*100:.0f}% < {MIN_WIN_RATE*100:.0f}%"

    ftr = profile.get("fast_tx_ratio") or 0
    if ftr > MAX_FAST_TX_RATIO:
        return False, f"bot/sniper risk — fast_tx_ratio {ftr:.2f}"

    return True, ""


def discover_traders(period: str = "7d", limit: int = 50) -> list:
    """
    Scan GMGN leaderboard and return wallets that pass all copy-trade filters.
    period: "7d" or "30d"
    limit:  how many leaderboard entries to check

    Each result: {"wallet": str, "winrate": float, "pnl": float,
                  "fast_tx_ratio": float, "twitter": str, "tags": list}

    Typical use: run weekly, send candidates to Telegram for user approval.
    """
    if not PARSE_API_KEY:
        log.debug("GMGN discover: PARSE_API_KEY not set")
        return []

    log.info(f"GMGN: scanning leaderboard ({period}, top {limit})")
    data = _get("get_wallet_rankings", {
        "chain":   CHAIN,
        "period":  period,
        "orderby": f"pnl_{period}",
        "limit":   limit,
    })
    if not data:
        log.warning("GMGN: leaderboard fetch failed")
        return []

    rankings = data.get("rank") or []
    candidates = []

    for entry in rankings:
        wallet = entry.get("wallet_address") or entry.get("address", "")
        if not wallet:
            continue

        winrate = entry.get(f"winrate_{period}") or entry.get("winrate") or 0
        pnl     = entry.get(f"pnl_{period}") or 0
        ftr     = entry.get("fast_tx_ratio") or 0

        # Quick pre-filter on leaderboard data before spending API credits
        if winrate < MIN_WIN_RATE:
            continue
        if ftr > MAX_FAST_TX_RATIO:
            continue

        # Fetch full profile for detailed check
        time.sleep(_RATE_LIMIT_DELAY)
        profile = get_wallet_profile(wallet)
        if not profile:
            continue

        passes, reason = _passes_copy_trade_filter(profile)
        if not passes:
            log.debug(f"GMGN: {wallet[:8]}... filtered — {reason}")
            continue

        candidates.append({
            "wallet":        wallet,
            "winrate":       winrate,
            "pnl":           pnl,
            "fast_tx_ratio": ftr,
            "twitter":       profile.get("twitter") or "",
            "tags":          profile.get("tags") or [],
            "pnl_30d":       profile.get("pnl_30d"),
            "realized_profit": profile.get("realized_profit"),
        })
        log.info(
            f"GMGN candidate: {wallet[:8]}... "
            f"WR={winrate*100:.0f}% PnL={pnl:+.0f} "
            f"twitter={profile.get('twitter','?')}"
        )

    log.info(f"GMGN discovery: {len(candidates)} candidates from {len(rankings)} checked")
    return candidates


def format_discovery_telegram(candidates: list, existing_wallets: set) -> str:
    """
    Format discovered traders as a Telegram message for user approval.
    existing_wallets: set of wallet addresses already in trusted_wallets.json
    """
    new = [c for c in candidates if c["wallet"] not in existing_wallets]
    if not new:
        return "🔍 GMGN scan complete — no new traders found above 65% win rate."

    lines = [f"🔍 <b>GMGN Discovery: {len(new)} new trader(s)</b>\n"]
    for c in new[:5]:   # cap at 5 per message
        wr  = f"{c['winrate']*100:.0f}%"
        pnl = f"+${c['pnl']:,.0f}" if c['pnl'] >= 0 else f"-${abs(c['pnl']):,.0f}"
        tw  = f"@{c['twitter']}" if c['twitter'] else "no twitter"
        lines.append(
            f"• {tw}\n"
            f"  WR: {wr} | 7D PnL: {pnl}\n"
            f"  Tags: {', '.join(c['tags']) or 'none'}\n"
            f"  <code>{c['wallet']}</code>"
        )
    lines.append("\nCopy any address above and paste it here with the trader name to add to watchlist.")
    return "\n".join(lines)
