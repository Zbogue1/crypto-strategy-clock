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
MIN_WIN_RATE        = 0.60   # 60%+ (precision traders rarely top raw-PnL leaderboards)
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

    # avg_hold_time may come back as seconds — normalise to minutes
    hold_raw = (
        data.get("avg_holding_time")       # seconds (most common)
        or data.get("avg_hold_duration")   # alternative field name
        or data.get("avg_hold_time")
    )
    avg_hold_minutes = None
    if hold_raw is not None:
        try:
            avg_hold_minutes = float(hold_raw) / 60   # assume seconds
        except (TypeError, ValueError):
            pass

    return {
        "wallet":            wallet_address,
        "winrate_7d":        data.get("winrate_7d"),
        "winrate_30d":       data.get("winrate"),
        "pnl_7d":            data.get("pnl_7d"),
        "pnl_30d":           data.get("pnl_30d"),
        "realized_profit":   data.get("realized_profit"),
        "fast_tx_ratio":     data.get("fast_tx_ratio"),
        "honeypot_ratio":    data.get("honeypot_ratio"),
        "sol_balance":       data.get("sol_balance"),
        "twitter":           data.get("twitter_username"),
        "tags":              data.get("tags") or [],
        "avg_hold_minutes":  avg_hold_minutes,
        "raw":               data,
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


def discover_traders(period: str = "7d", limit: int = 100) -> list:
    """
    Scan GMGN leaderboard and return wallets that pass all copy-trade filters.
    Uses leaderboard data directly — no per-wallet profile credit spend.

    Filters applied from leaderboard data:
        winrate_7d  >= 60%          (precision trader)
        buy_7d      <= 2000         (not machine-speed)
        tags        no wash_trader  (exclude manipulators)
        realized_profit_7d > $1K   (actually making money)
    """
    if not PARSE_API_KEY:
        log.debug("GMGN discover: PARSE_API_KEY not set")
        return []

    log.info(f"GMGN: scanning leaderboard ({period}, top {limit})")
    data = _get("get_wallet_rankings", {
        "chain":   CHAIN,
        "period":  period,
        "orderby": "winrate",
        "limit":   limit,
    })
    if not data:
        log.warning("GMGN: leaderboard fetch failed")
        return []

    rankings = data.get("rank") or []
    candidates = []
    BAD_TAGS = {"wash_trader", "bot", "sniper", "arbitrager", "top_renamed"}

    for entry in rankings:
        wallet = entry.get("wallet_address") or entry.get("address", "")
        if not wallet:
            continue

        winrate  = entry.get("winrate_7d") or entry.get("winrate_30d") or 0
        buys_7d  = entry.get("buy_7d") or 0
        tags     = set(t.lower() for t in (entry.get("tags") or []))
        realized = float(entry.get("realized_profit_7d") or 0)
        pnl      = float(entry.get("pnl_7d") or 0)
        twitter  = entry.get("twitter_username") or entry.get("name") or ""

        if winrate < MIN_WIN_RATE:
            continue
        if buys_7d > 2000:
            log.debug(f"GMGN: {wallet[:8]}... filtered — {buys_7d} buys/7d (bot-speed)")
            continue
        if tags & BAD_TAGS:
            log.debug(f"GMGN: {wallet[:8]}... filtered — bad tags: {tags & BAD_TAGS}")
            continue
        if realized < 1000:
            log.debug(f"GMGN: {wallet[:8]}... filtered — realized profit ${realized:.0f} too low")
            continue

        # Fetch full profile to get hold time and fast_tx_ratio
        profile = get_wallet_profile(wallet)
        fast_tx = float((profile or {}).get("fast_tx_ratio") or 0)
        hold_min = (profile or {}).get("avg_hold_minutes")   # None if unavailable
        honeypot_ratio = float((profile or {}).get("honeypot_ratio") or 0)

        candidate = {
            "wallet":             wallet,
            "winrate":            winrate,
            "win_rate":           winrate,
            "pnl":                pnl,
            "realized_profit":    realized,
            "realized_pnl_7d":   realized,
            "fast_tx_ratio":      fast_tx,
            "honeypot_ratio":     honeypot_ratio,
            "twitter":            twitter,
            "has_twitter":        bool(twitter),
            "tags":               list(tags),
            "buys_7d":            buys_7d,
            "trades_7d":          buys_7d,   # best proxy from leaderboard data
            "avg_trades_per_day": round(buys_7d / 7, 1),
            "avg_hold_minutes":   hold_min,
        }
        candidates.append(candidate)
        log.info(
            f"GMGN candidate: {wallet[:8]}... "
            f"WR={winrate*100:.0f}% realized=${realized:,.0f} "
            f"hold={f'{hold_min:.0f}min' if hold_min else '?'} "
            f"twitter=@{twitter}"
        )

    # Run every candidate through the vetting engine
    try:
        from fomo_vetting import vet_and_annotate
        candidates = [vet_and_annotate(c) for c in candidates]
    except Exception as e:
        log.warning(f"GMGN: vetting failed (non-fatal): {e}")

    log.info(f"GMGN discovery: {len(candidates)} candidates from {len(rankings)} checked")
    return candidates


def discover_narrative_whales(period: str = "30d", limit: int = 50) -> list:
    """
    Find big-fish narrative traders — whales who move markets but aren't
    ideal for direct copy-trading at $1K scale (high trade count, diverse positions).

    These get added as copy_trade: false (👁️ narrative watch), not copy targets.
    They tell us WHAT narratives smart money is loading up on.

    Filters (opposite of copy-trade filters):
        realized_profit  >= $100K   (genuinely big fish)
        trades_30d       >= 100     (very active, spreading across narratives)
        win_rate         < 60%      (spray-and-pray confirms narrative style)
        fast_tx_ratio    <= 0.5     (not a pure bot)
    """
    if not PARSE_API_KEY:
        return []

    log.info("GMGN: scanning for narrative whales...")
    # Sort by realized profit to find the biggest players
    data = _get("get_wallet_rankings", {
        "chain":   CHAIN,
        "period":  period,
        "orderby": "realized_profit",
        "limit":   limit,
    })
    if not data:
        return []

    rankings = data.get("rank") or []
    whales = []

    for entry in rankings:
        wallet = entry.get("wallet_address") or entry.get("address", "")
        if not wallet:
            continue

        realized = entry.get("realized_profit") or 0
        winrate  = entry.get("winrate") or 0
        ftr      = entry.get("fast_tx_ratio") or 0

        # Pre-filter on leaderboard data
        if realized < 100_000:
            continue
        if ftr > 0.5:
            continue
        if winrate >= 0.65:
            continue   # this is a copy-trade candidate, not a whale

        time.sleep(_RATE_LIMIT_DELAY)
        profile = get_wallet_profile(wallet)
        if not profile:
            continue

        whales.append({
            "wallet":           wallet,
            "realized_profit":  realized,
            "winrate":          winrate,
            "fast_tx_ratio":    ftr,
            "twitter":          profile.get("twitter") or "",
            "tags":             profile.get("tags") or [],
            "pnl_30d":          profile.get("pnl_30d"),
            "copy_trade":       False,
            "copy_trade_reason": (
                f"Narrative whale — ${realized:,.0f} realized profit, "
                f"{winrate*100:.0f}% win rate. "
                f"Watch for narrative themes, not copy-trade entries."
            ),
        })
        log.info(f"GMGN whale: {wallet[:8]}... realized=${realized:,.0f} WR={winrate*100:.0f}%")

    log.info(f"GMGN whale discovery: {len(whales)} found")
    return whales


def format_whale_telegram(whales: list, existing_wallets: set) -> str:
    """Format narrative whale discoveries as a Telegram message."""
    new = [w for w in whales if w["wallet"] not in existing_wallets]
    if not new:
        return ""

    lines = [f"🐋 <b>Narrative Whales Found: {len(new)}</b>\n"
             f"<i>These move markets — watch their narratives, don't copy entries</i>\n"]
    for w in new[:5]:
        profit = f"${w['realized_profit']:,.0f}"
        wr     = f"{w['winrate']*100:.0f}%"
        tw     = f"@{w['twitter']}" if w['twitter'] else "no twitter"
        lines.append(
            f"• {tw}\n"
            f"  Realized: {profit} | WR: {wr} (spray & pray)\n"
            f"  <code>{w['wallet']}</code>"
        )
    lines.append("\nPaste address + name here to add as 👁️ narrative watch.")
    return "\n".join(lines)


RECOMMENDATION_ICONS = {
    "COPY_TRADE":      "✅",
    "NARRATIVE_WATCH": "👁️",
    "TWITTER_ONLY":    "🐦",
    "REJECT":          "❌",
}


def format_discovery_telegram(candidates: list, existing_wallets: set) -> str:
    """
    Format discovered traders as a Telegram message for user approval.
    Includes vetting scores and recommendation if available.
    existing_wallets: set of wallet addresses already in trusted_wallets.json
    """
    new = [c for c in candidates if c["wallet"] not in existing_wallets]
    if not new:
        return f"🔍 GMGN scan complete — no new traders found above {MIN_WIN_RATE*100:.0f}% win rate."

    lines = [f"🔍 <b>GMGN Discovery: {len(new)} new trader(s)</b>\n"]
    for c in new[:5]:   # cap at 5 per message
        wr       = f"{c['winrate']*100:.0f}%"
        realized = c.get("realized_profit") or 0
        pnl_str  = f"+${realized:,.0f}" if realized >= 0 else f"-${abs(realized):,.0f}"
        tw       = f"@{c['twitter']}" if c['twitter'] else "no twitter"
        hold     = c.get("avg_hold_minutes")
        hold_str = f"{hold:.0f}min hold" if hold else "hold unknown"

        vetting  = c.get("vetting") or {}
        rec      = vetting.get("recommendation", "UNVETTED")
        score    = vetting.get("score", "?")
        icon     = RECOMMENDATION_ICONS.get(rec, "❓")
        top_flag = vetting.get("flags", [])
        flag_str = f"\n  ⚠️ {top_flag[0]}" if top_flag else ""
        disq     = vetting.get("disqualifiers", [])
        disq_str = f"\n  ⛔ {disq[0]}" if disq else ""

        lines.append(
            f"{icon} {tw} — <b>{rec}</b> (score {score}/100)\n"
            f"  WR: {wr} | 7D: {pnl_str} | {hold_str}\n"
            f"  Tags: {', '.join(c['tags']) or 'none'}"
            f"{disq_str}{flag_str}\n"
            f"  <code>{c['wallet']}</code>"
        )
    lines.append("\nPaste address + name here to add to watchlist.")
    return "\n".join(lines)


# ─── WEEKLY WATCHLIST RE-VETTING ──────────────────────────────────────────────

def revett_watchlist(watchlist: list) -> dict:
    """
    Re-fetch and re-score every wallet in the current watchlist using live GMGN data.

    Args:
        watchlist: list of wallet dicts from trusted_wallets.json (both tiers merged).

    Returns:
        {
          "upgraded":  [change_dicts],   ← recommendation improved
          "degraded":  [change_dicts],   ← recommendation dropped
          "rejected":  [change_dicts],   ← now REJECT — caller should remove these
          "unchanged": [change_dicts],   ← score changed but recommendation same
          "errors":    [error_dicts],    ← GMGN fetch failed
        }

    change_dict keys: alias, wallet, old_rec, new_rec, old_score, new_score, flags
    """
    try:
        from fomo_vetting import score_wallet
    except ImportError:
        log.warning("revett_watchlist: fomo_vetting not available — skipping")
        return {"upgraded": [], "degraded": [], "rejected": [], "unchanged": [], "errors": []}

    REC_RANK = {"COPY_TRADE": 3, "NARRATIVE_WATCH": 2, "TWITTER_ONLY": 1, "REJECT": 0}

    results = {"upgraded": [], "degraded": [], "rejected": [], "unchanged": [], "errors": []}

    for w in watchlist:
        address = w.get("wallet", "")
        alias   = w.get("alias", address[:8] or "unknown")

        # Skip placeholder addresses added before a real address was known
        if not address or address.startswith("fill_in") or len(address) < 20:
            continue

        old_vetting = w.get("vetting") or {}
        old_rec     = old_vetting.get("recommendation") or "UNVETTED"
        old_score   = old_vetting.get("score") or 0

        log.info(f"Re-vetting {alias} ({address[:8]}…)")
        time.sleep(20)   # extra delay on top of get_wallet_profile's own rate limit
        profile = get_wallet_profile(address)
        if not profile:
            results["errors"].append({
                "alias":  alias,
                "wallet": address,
                "error":  "GMGN profile fetch failed",
            })
            continue

        # Build candidate dict in the same shape score_wallet() expects
        winrate  = profile.get("winrate_7d") or profile.get("winrate_30d") or 0
        realized = float(profile.get("pnl_7d") or profile.get("realized_profit") or 0)

        # Sanity check — if GMGN returns 0% WR AND low PnL the API gave us empty
        # data (common when rate-limited or profile not indexed). Skip rather than
        # scoring a legitimate wallet as REJECT on bad data.
        # Threshold $100: a real active wallet will always show >$100 realized in 7D.
        if winrate == 0 and abs(realized) < 100:
            log.warning(f"Re-vetting {alias}: skipping — API returned empty data (WR=0%, PnL≈$0)")
            results["errors"].append({
                "alias":  alias,
                "wallet": address,
                "error":  "Empty API data (WR=0%, PnL≈$0) — skipped to avoid false REJECT",
            })
            continue
        candidate = {
            "wallet":             address,
            "win_rate":           winrate,
            "winrate":            winrate,
            "realized_pnl_7d":   realized,
            "realized_profit":   realized,
            "fast_tx_ratio":     float(profile.get("fast_tx_ratio") or 0),
            "honeypot_ratio":    float(profile.get("honeypot_ratio") or 0),
            "twitter":           profile.get("twitter") or "",
            "has_twitter":       bool(profile.get("twitter")),
            "tags":              profile.get("tags") or [],
            "avg_hold_minutes":  profile.get("avg_hold_minutes"),
            "trades_7d":         0,   # not available from profile endpoint
            "avg_trades_per_day": 0,
        }

        verdict = score_wallet(candidate)
        new_rec   = verdict["recommendation"]
        new_score = verdict["score"]
        flags     = verdict.get("flags", [])

        change = {
            "alias":     alias,
            "wallet":    address,
            "old_rec":   old_rec,
            "new_rec":   new_rec,
            "old_score": old_score,
            "new_score": new_score,
            "flags":     flags[:2],   # top 2 flags for telegram
            "winrate":   winrate,
            "realized":  realized,
        }

        # Update the wallet's vetting in-place so caller can save it
        w["vetting"] = verdict
        w["copy_trade"] = verdict["copy_trade"]

        if new_rec == "REJECT":
            results["rejected"].append(change)
        elif REC_RANK.get(new_rec, 0) > REC_RANK.get(old_rec, 0):
            results["upgraded"].append(change)
        elif REC_RANK.get(new_rec, 0) < REC_RANK.get(old_rec, 0):
            results["degraded"].append(change)
        else:
            results["unchanged"].append(change)

    log.info(
        f"Re-vetting complete: "
        f"{len(results['upgraded'])} upgraded, "
        f"{len(results['degraded'])} degraded, "
        f"{len(results['rejected'])} rejected, "
        f"{len(results['unchanged'])} unchanged, "
        f"{len(results['errors'])} errors"
    )
    return results


def format_revett_telegram(results: dict) -> str:
    """Format re-vetting results as a Telegram message."""
    upgraded  = results.get("upgraded", [])
    degraded  = results.get("degraded", [])
    rejected  = results.get("rejected", [])
    errors    = results.get("errors", [])

    if not any([upgraded, degraded, rejected]):
        return "🔄 <b>Weekly re-vetting complete</b>\nAll watchlisted wallets holding steady — no changes."

    lines = ["🔄 <b>Weekly Watchlist Re-Vetting</b>\n"]

    if rejected:
        lines.append(f"🗑️ <b>AUTO-REMOVED ({len(rejected)} wallets scored REJECT):</b>")
        for c in rejected:
            lines.append(
                f"  ❌ {c['alias']} | was {c['old_rec']} {c['old_score']}/100 → REJECT {c['new_score']}/100\n"
                f"     WR: {c['winrate']*100:.0f}% | 7D: ${c['realized']:,.0f}"
            )

    if degraded:
        lines.append(f"\n⚠️ <b>DEGRADED ({len(degraded)} wallets dropped):</b>")
        for c in degraded:
            flag_str = f"\n     ⚠️ {c['flags'][0]}" if c['flags'] else ""
            lines.append(
                f"  📉 {c['alias']} | {c['old_rec']} {c['old_score']}/100 → {c['new_rec']} {c['new_score']}/100"
                f"{flag_str}"
            )

    if upgraded:
        lines.append(f"\n✅ <b>UPGRADED ({len(upgraded)} wallets improved):</b>")
        for c in upgraded:
            lines.append(
                f"  📈 {c['alias']} | {c['old_rec']} {c['old_score']}/100 → {c['new_rec']} {c['new_score']}/100\n"
                f"     WR: {c['winrate']*100:.0f}% | 7D: ${c['realized']:,.0f}"
            )

    if errors:
        lines.append(f"\n⚙️ {len(errors)} wallet(s) couldn't be fetched (GMGN API unavailable).")

    lines.append("\nWatchlist updated automatically.")
    return "\n".join(lines)
