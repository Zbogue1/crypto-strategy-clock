#!/usr/bin/env python3
"""
fomo_tracker.py — Always-on Flask webhook server for FOMO copy trading.

Deploy as a Railway web service alongside the 4-hour cron agent.
Receives Alchemy address-activity webhooks when trusted wallets transact.
Validates tokens, scans for catalyst, executes in fomo_portfolio, notifies via Telegram.

Railway env vars required:
  TELEGRAM_BOT_TOKEN     — from @BotFather
  TELEGRAM_CHAT_ID       — your personal chat ID
  ALCHEMY_SIGNING_KEY    — from Alchemy webhook dashboard (for signature verification)
  ALCHEMY_API_KEY        — for Alchemy API calls

Optional:
  TWITTER_BEARER_TOKEN   — for catalyst scanning (Twitter API v2)
  MIN_MARKET_CAP         — override default $500K floor
"""

import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic
import requests
from flask import Flask, request, jsonify

from fomo_portfolio import (
    execute_fomo_buy,
    execute_fomo_sell,
    load_fomo_portfolio,
    check_fomo_auto_exits,
    get_wallet_lessons,
    get_fomo_stats,
    sync_fomo_state_from_github,
    ensure_fomo_bank,
    maybe_repair_fomo_cash,
    FOMO_MAX_CONCURRENT_POSITIONS,
)
from fomo_research import research_token, ResearchVerdict, _ct_sentiment
from fomo_wallet_stats import get_wallet_leaderboard
from fomo_first_buy import check_and_record as check_first_buy
from fomo_convergence import record_signal as record_convergence_signal, check_convergence
from fomo_regime import get_market_regime
from fomo_social import start_social_poller, parse_channel_message
from fomo_confidence import get_confidence, format_confidence_line, format_suppression_telegram

# ─── CONFIG ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger(__name__)

app = Flask(__name__)

TELEGRAM_BOT_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
ALCHEMY_SIGNING_KEY = os.environ.get("ALCHEMY_SIGNING_KEY", "")
ALCHEMY_API_KEY     = os.environ.get("ALCHEMY_API_KEY", "")
TWITTER_BEARER      = os.environ.get("TWITTER_BEARER_TOKEN", "")

MIN_MARKET_CAP  = float(os.environ.get("MIN_MARKET_CAP", "500000"))
MIN_LIQUIDITY   = 50_000    # $50K minimum liquidity
MIN_TOKEN_AGE   = 3         # days — filter brand-new rugs

# ─── GOLEM INDEPENDENT TRADING ────────────────────────────────────────────────
# Golem's own scanners (new-launch radar, momentum scorer, narrative scan) hunt
# tokens 0.5-6 HOURS old. The 3-DAY execution gate above made every one of those
# signals impossible to act on — Golem generated calls for months and never once
# opened a position from its own analysis, because the token was always "too new".
#
# We now let Golem-originated signals through on a separate, tighter risk
# profile: younger tokens allowed, but smaller size, because the rug risk that
# motivated the 3-day rule is real and hasn't gone away.
GOLEM_SOURCES = {"new_launch", "scanner", "narrative", "golem"}
# Minimum age for a Golem-originated trade (hours, not days)
GOLEM_MIN_AGE_HOURS   = float(os.getenv("FOMO_GOLEM_MIN_AGE_HOURS", "0.5"))
# Liquidity floor for young tokens — lower than the $50k mainline, but not zero
GOLEM_MIN_LIQUIDITY   = float(os.getenv("FOMO_GOLEM_MIN_LIQUIDITY", "15000"))
# Fraction of normal position size for these higher-risk entries
GOLEM_SIZE_MULTIPLIER = float(os.getenv("FOMO_GOLEM_SIZE_MULT", "0.5"))
# Master switch
GOLEM_INDEPENDENT_TRADING = os.getenv("FOMO_GOLEM_TRADING", "true").lower() == "true"

# Notify on filtered/rejected signals. Off by default — rejections are the
# normal outcome and alerting on each one drowns the actionable messages.
# Set FOMO_NOTIFY_FILTERED=true to see them again while debugging.
NOTIFY_FILTERED = os.getenv("FOMO_NOTIFY_FILTERED", "false").lower() == "true"

# ─── POSITION SIZING ──────────────────────────────────────────────────────────
# Sizing is a % of REMAINING cash, so it tapers rather than hitting a wall.
# This percentage — not the position count cap — is what actually determines
# how many concurrent positions the bankroll supports:
#   15% -> ~35 positions | 8% -> ~60 | 5% -> ~89
# Lower = more concurrent positions = larger calibration sample.
DEFAULT_POSITION_PCT   = float(os.getenv("FOMO_POSITION_PCT", "8.0"))
# Ceiling when multiple wallets converge on the same token. 50% meant one
# memecoin could take half the bank — too concentrated for an asset class
# where total loss is routine.
MAX_POSITION_PCT       = float(os.getenv("FOMO_MAX_POSITION_PCT_CAP", "25.0"))
MIN_POSITION_PCT       = float(os.getenv("FOMO_MIN_POSITION_PCT", "3.0"))
MAX_LAG_MINUTES = 15        # don't enter if we're >15 min behind the trader

HEADERS = {"User-Agent": "CryptoOracle/3.0 (fomo-tracker; non-commercial)"}

HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
HELIUS_AUTH_HEADER = os.environ.get("HELIUS_AUTH_HEADER", "")
WSOL_MINT = "So11111111111111111111111111111111111111112"

TRUSTED_WALLETS_FILE = "trusted_wallets.json"
FOMO_PENDING_ALERTS_FILE = "fomo_pending_alerts.json"
BUY_ALERT_EXPIRY_MINUTES = 15   # memecoins move fast -- signal goes stale if untapped

# Relayed-signal parsing (manually forwarded emails / notes via Telegram text)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL          = "claude-sonnet-4-5"
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
_SOLANA_ADDR_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")


def _find_holding(holdings: list, contract_address: str) -> Optional[dict]:
    """Find an open position by contract address (case-insensitive). Multiple
    positions can be open at once, so lookups always go by contract address
    rather than assuming there's only one."""
    if not contract_address:
        return None
    target = contract_address.lower()
    for h in holdings:
        if (h.get("contract_address") or "").lower() == target:
            return h
    return None


# ─── WALLET REGISTRY ─────────────────────────────────────────────────────────

def load_trusted_wallets() -> dict:
    try:
        with open(TRUSTED_WALLETS_FILE) as f:
            return json.load(f)
    except Exception:
        return {"tier_a": [], "tier_b": []}


def save_trusted_wallets(data: dict):
    data["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(TRUSTED_WALLETS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_wallet_info(address: str) -> Optional[dict]:
    """Find a Tier A wallet by address."""
    data = load_trusted_wallets()
    addr = address.lower()
    for w in data.get("tier_a", []):
        waddr = w.get("wallet", "").lower()
        if waddr == addr and not waddr.startswith("fill_in"):
            return w
    return None


def _get_wallet_meta(alias: str) -> dict:
    """Return trusted_wallets entry for alias, or empty dict."""
    data = load_trusted_wallets()
    for tier in ("tier_a", "tier_b"):
        for w in data.get(tier, []):
            if w.get("alias") == alias:
                return w
    return {}


def update_wallet_stats(alias: str, outcome: str, profit_pct: float):
    """Update win/loss stats for a wallet after a trade completes."""
    data = load_trusted_wallets()
    now  = datetime.now(timezone.utc).isoformat()

    for tier_key in ("tier_a", "tier_b"):
        for w in data.get(tier_key, []):
            if w.get("alias") == alias:
                s = w.setdefault("stats", {})
                s["trades_followed"]  = s.get("trades_followed", 0) + 1
                s["last_trade_at"]    = now
                s["last_outcome"]     = outcome

                if outcome == "WIN":
                    s["wins"]               = s.get("wins", 0) + 1
                    s["consecutive_losses"] = 0
                else:
                    s["losses"]             = s.get("losses", 0) + 1
                    s["consecutive_losses"] = s.get("consecutive_losses", 0) + 1

                # Recalculate win rate
                total = s.get("trades_followed", 1)
                s["win_rate_30d"] = round(s.get("wins", 0) / total * 100, 1)

                # Check demotion rules
                rules = data.get("demotion_rules", {})
                if s.get("consecutive_losses", 0) >= rules.get("consecutive_losses_for_demotion", 3):
                    log.warning(f"FOMO: {alias} — {s['consecutive_losses']} consecutive losses. "
                                f"Demotion to Tier B triggered.")
                    _demote_wallet(data, alias)
                    send_telegram(
                        f"⚠️ <b>Wallet Demoted: {alias}</b>\n"
                        f"{s['consecutive_losses']} consecutive losses\n"
                        f"Moved to Tier B — webhook paused"
                    )

    save_trusted_wallets(data)


def _demote_wallet(data: dict, alias: str):
    """Move wallet from Tier A to Tier B and clear its webhook."""
    now = datetime.now(timezone.utc).isoformat()
    for w in data.get("tier_a", []):
        if w.get("alias") == alias:
            w["tier"]              = "B"
            w["demoted_at"]        = now
            w["alchemy_webhook_id"] = None   # webhook will be deleted next cycle
            data.setdefault("tier_b", []).append(w)
            data["tier_a"].remove(w)
            break


def check_wallet_promotions() -> list:
    """
    Called during every 4-hour cycle. Checks each Tier B wallet against
    promotion_rules (trades observed, win rate, days observed, min bankroll)
    and promotes qualifying wallets to Tier A. Returns list of promoted aliases.
    Bookkeeping only — never blocks or delays live buy/sell execution.
    """
    data  = load_trusted_wallets()
    rules = data.get("promotion_rules", {})
    min_trades   = rules.get("min_tier_b_trades_observed", 10)
    min_winrate  = rules.get("min_win_rate_for_promotion", 65.0)
    min_days     = rules.get("min_days_observed", 14)
    min_bankroll = rules.get("min_bankroll_usd", 0)

    now      = datetime.now(timezone.utc)
    promoted = []
    still_b  = []

    for w in data.get("tier_b", []):
        stats    = w.get("stats", {})
        trades   = stats.get("trades_followed", 0)
        winrate  = stats.get("win_rate_30d") or 0
        bankroll = w.get("bankroll_usd") or 0

        days_observed = 0
        added_at = w.get("added_at")
        if added_at:
            try:
                added_dt = datetime.fromisoformat(added_at.replace("Z", "+00:00"))
                days_observed = (now - added_dt).days
            except Exception:
                pass

        meets_bankroll = min_bankroll <= 0 or bankroll >= min_bankroll

        if (trades >= min_trades and winrate >= min_winrate
                and days_observed >= min_days and meets_bankroll):
            w["tier"]        = "A"
            w["promoted_at"] = now.isoformat()
            promoted.append(w["alias"])
            data.setdefault("tier_a", []).append(w)
            log.info(f"FOMO: {w['alias']} promoted to Tier A "
                     f"({trades} trades, {winrate:.0f}% win rate, "
                     f"{days_observed}d observed, ${bankroll:,.0f} bankroll)")
        else:
            still_b.append(w)

    if promoted:
        data["tier_b"] = still_b
        save_trusted_wallets(data)

    return promoted


# ─── PENDING BUY ALERTS (human-confirmed execution) ──────────────────────────

def load_pending_alerts() -> dict:
    try:
        with open(FOMO_PENDING_ALERTS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_pending_alerts(data: dict):
    with open(FOMO_PENDING_ALERTS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _prune_pending_alerts(data: dict) -> dict:
    """Drop anything older than an hour so this file doesn't grow forever."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=1)
    kept = {}
    for aid, rec in data.items():
        try:
            created = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if created > cutoff:
            kept[aid] = rec
    return kept


def create_pending_buy_alert(details: dict) -> str:
    """Store a live buy signal awaiting a human tap. Returns a short alert_id."""
    data = _prune_pending_alerts(load_pending_alerts())
    alert_id = uuid.uuid4().hex[:8]
    details["created_at"] = datetime.now(timezone.utc).isoformat()
    data[alert_id] = details
    save_pending_alerts(data)
    return alert_id


def create_pending_sell_alert(details: dict) -> str:
    """Store a live sell signal (tracked wallet exited) awaiting a human tap."""
    data = _prune_pending_alerts(load_pending_alerts())
    alert_id = uuid.uuid4().hex[:8]
    details["created_at"] = datetime.now(timezone.utc).isoformat()
    data[alert_id] = details
    save_pending_alerts(data)
    return alert_id


def get_pending_alert(alert_id: str) -> Optional[dict]:
    """Return the alert if it exists and hasn't expired, else None."""
    data = load_pending_alerts()
    rec  = data.get(alert_id)
    if not rec:
        return None
    created = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
    age_min = (datetime.now(timezone.utc) - created).total_seconds() / 60
    if age_min > BUY_ALERT_EXPIRY_MINUTES:
        return None
    return rec


def consume_pending_alert(alert_id: str):
    """Remove an alert once it's been acted on (executed or expired)."""
    data = load_pending_alerts()
    if alert_id in data:
        del data[alert_id]
        save_pending_alerts(data)


def suggest_buy_amount(catalyst_score: int) -> str:
    """Map catalyst confidence to a suggested $ tier -- a suggestion only,
    the human still picks the final amount."""
    if catalyst_score >= 8:
        return "500"
    if catalyst_score >= 6:
        return "200"
    if catalyst_score >= 4:
        return "100"
    return "50"


# ─── RELAYED SIGNALS (manually forwarded email / note via Telegram text) ─────

def _match_known_alias(hint: Optional[str]) -> Optional[str]:
    """Fuzzy-match a name mentioned in a relayed message against known wallet aliases."""
    if not hint:
        return None
    norm = hint.lower().replace(" ", "").replace("_", "")
    data = load_trusted_wallets()
    for tier_key in ("tier_a", "tier_b"):
        for w in data.get(tier_key, []):
            alias = w.get("alias", "")
            if alias and alias.lower().replace(" ", "").replace("_", "") == norm:
                return alias
    return None


def _parse_relayed_signal_fallback(raw_text: str) -> dict:
    """Regex-only parse, used if ANTHROPIC_API_KEY isn't set on this service.
    Best-effort -- parse_relayed_signal() (AI path) is far more robust; this
    exists purely as a fail-safe so the feature still works in a degraded way
    instead of doing nothing."""
    addrs      = _SOLANA_ADDR_RE.findall(raw_text)
    candidates = [a for a in addrs if a not in QUOTE_MINTS]
    contract   = candidates[0] if candidates else None

    lowered = raw_text.lower()
    if "bought" in lowered or re.search(r"\bbuy\b", lowered):
        action = "BUY"
    elif "sold" in lowered or re.search(r"\bsell\b", lowered):
        action = "SELL"
    else:
        action = "UNCLEAR"

    alias = None
    data  = load_trusted_wallets()
    for w in data.get("tier_a", []) + data.get("tier_b", []):
        a = w.get("alias", "")
        if a and a.lower() in lowered:
            alias = a
            break

    return {
        "wallet_alias":     alias,
        "action":           action,
        "contract_address": contract,
        "confidence":       "low",
        "notes":            "Parsed without AI (ANTHROPIC_API_KEY not set on this service) -- best-effort only.",
    }


def _lookup_contract_by_symbol(symbol: str) -> Optional[str]:
    """Search DexScreener for a Solana token by symbol; return the highest-liquidity mint address."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/search/?q={symbol}",
            timeout=8, headers=HEADERS,
        )
        if r.status_code != 200:
            return None
        pairs = r.json().get("pairs") or []
        sol_pairs = [p for p in pairs if (p.get("chainId") or "").lower() == "solana"]
        if not sol_pairs:
            return None
        best = sorted(sol_pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0, reverse=True)[0]
        return (best.get("baseToken") or {}).get("address")
    except Exception as e:
        log.debug(f"DexScreener symbol lookup failed for {symbol}: {e}")
        return None


def parse_relayed_signal(raw_text: str) -> dict:
    """
    Interpret a manually-relayed message (pasted/forwarded email like a Solscan
    wallet alert, or a quick note in your own words) into a structured trading
    signal: which wallet, which token, buy or sell. This is the entry point for
    the "text your agent a signal" flow -- research still runs after this, and
    execution still requires a Telegram tap, exactly like every automated
    signal source in this system.
    """
    if not ANTHROPIC_API_KEY:
        return _parse_relayed_signal_fallback(raw_text)

    known_aliases = []
    data = load_trusted_wallets()
    for w in data.get("tier_a", []) + data.get("tier_b", []):
        if w.get("alias"):
            known_aliases.append(w["alias"])

    system = (
        "You extract a structured Solana trading signal from a message a user pasted into "
        "Telegram -- usually a forwarded wallet-alert email (e.g. from Solscan or Helius) "
        "showing balance changes, or sometimes just a quick note in their own words.\n\n"
        "KEY RULES:\n"
        "- In balance-change emails, a green/positive entry means tokens RECEIVED (BUY), "
        "a red/negative entry means tokens SENT (SELL).\n"
        "- SOL, USDC, and USDT are QUOTE currencies -- they are never the token being traded. "
        "The actual token is whichever balance change is NOT one of those.\n"
        "- If the ONLY balance changes are SOL/USDC/USDT (a pure transfer with no other token), "
        'set is_noise to true and action to "UNCLEAR".\n'
        "- If a token symbol is visible but the mint address is not, put the symbol in "
        "token_symbol and leave contract_address null.\n"
        "- Look for Solana mint addresses: base58 strings of 32-44 characters that are NOT "
        "in the quote mint list. Common quote mints: So111...112 (SOL), EPjFW...Dt1v (USDC), "
        "Es9vM...NYB (USDT).\n"
        "Respond ONLY with valid JSON."
    )

    prompt = (
        f"Known wallet aliases already tracked: {', '.join(known_aliases) or 'none'}\n\n"
        f"Message to parse:\n---\n{raw_text}\n---\n\n"
        "Respond with this JSON structure:\n"
        "{\n"
        '  "wallet_alias": "closest matching known alias, or name mentioned, or null",\n'
        '  "action": "BUY" or "SELL" or "UNCLEAR",\n'
        '  "contract_address": "base58 Solana mint address of the non-quote token, or null",\n'
        '  "token_symbol": "token ticker/symbol if visible but no mint address found, or null",\n'
        '  "is_noise": true if this is a pure SOL/USDC/USDT transfer with no trading token,\n'
        '  "confidence": "high" or "low",\n'
        '  "notes": "one short sentence on anything ambiguous"\n'
        "}"
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model=AI_MODEL, max_tokens=400, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        log.warning(f"FOMO: relayed-signal AI parse failed: {e}")
        return _parse_relayed_signal_fallback(raw_text)


def send_positions_update():
    """
    Called when the user sends /positions or 'my positions' via Telegram.
    Fetches live price + fresh CT sentiment for every open holding.
    """
    sync_fomo_state_from_github()
    portfolio = load_fomo_portfolio()
    holdings  = portfolio.get("holdings", [])
    cash      = portfolio.get("cash", 0)

    if not holdings:
        send_telegram(f"📭 <b>No open positions.</b>\nCash: ${cash:.2f}")
        return

    send_telegram(f"🔄 Fetching live data on {len(holdings)} position(s)...")
    lines = ["📊 <b>LIVE POSITION UPDATE</b>\n"]

    # ONE batched request for every position instead of validate_token() per
    # holding (and again in the totals). Seven positions meant ~14 rapid
    # DexScreener calls, which got rate-limited into "price unavailable" on
    # every row — making the whole report useless exactly when you need it.
    try:
        from fomo_exit import get_prices_batch
        _batch = get_prices_batch(
            [h.get("contract_address") for h in holdings if h.get("contract_address")]
        )
    except Exception as e:
        log.warning(f"Positions: batch price fetch failed: {e}")
        _batch = {}

    for h in holdings:
        contract = h.get("contract_address", "")
        ticker   = h.get("token_ticker", "???")
        entry    = h.get("entry_price", 0)
        units    = h.get("units", 0)
        spent    = h.get("spent", 0)
        stop     = h.get("stop_loss", 0)
        target   = h.get("exit_target", 0)
        wallet   = h.get("wallet_alias", "unknown")
        tranches = h.get("tranches_taken", [])

        # Batched price first; only fall back to a single lookup if this
        # token wasn't in the batch response.
        current_price = (_batch.get(contract) or (None, None))[0] or 0
        if not current_price and contract:
            current_price = (validate_token(contract) or {}).get("price", 0)

        if current_price and entry:
            pnl_pct     = (current_price - entry) / entry * 100
            current_val = units * current_price
            unrealized  = current_val - spent
            pnl_emoji   = "🟢" if pnl_pct >= 0 else "🔴"
            pnl_str     = f"{pnl_emoji} {pnl_pct:+.1f}% (${unrealized:+.2f})"
            stop_dist   = (stop - current_price) / current_price * 100 if current_price else 0
            target_dist = (target - current_price) / current_price * 100 if current_price else 0
            price_str   = f"${current_price:.8f}"
        else:
            pnl_str     = "⚠️ price unavailable"
            price_str   = "N/A"
            stop_dist   = 0
            target_dist = 0
            current_val = spent

        ct      = _ct_sentiment(ticker)
        ct_line = ct.get("summary", "N/A")

        # Read the ACTUAL flags fomo_exit sets. The old code read a
        # "tranches_taken" list that nothing ever writes, so every position
        # reported "None yet" even after both tranches had sold — which made
        # a 5x winner look like it had harvested nothing.
        t1 = bool(h.get("tranche_1_sold")) or "tranche_1_2x" in tranches
        t2 = bool(h.get("tranche_2_sold")) or "tranche_2_3x" in tranches

        if t1 and t2:
            tranche_str = "✅ 2x + 3x taken — trailing stop active"
        elif t1:
            tranche_str = "✅ 2x taken — riding to 3x"
        elif tranches:
            tranche_str = "✅ " + ", ".join(tranches)
        else:
            tranche_str = "None yet"

        # Surface the trailing stop once it's armed — otherwise "Stop: -87%
        # away" is misleading, since the real exit is now peak-based.
        if h.get("trailing_stop_active"):
            peak = h.get("peak_price") or 0
            if peak > 0 and current_price:
                drop = (current_price - peak) / peak * 100
                tranche_str += (
                    f"\nTrailing: peak ${peak:.8f}, now {drop:+.1f}% "
                    f"(exits at -30%)"
                )

        lines.append(
            f"<b>${ticker}</b> | Following {wallet}\n"
            f"Price: {price_str} | {pnl_str}\n"
            f"Invested: ${spent:.0f} → Now: ${current_val:.2f}\n"
            f"Stop: {stop_dist:+.1f}% away | Target: {target_dist:+.1f}% away\n"
            f"🐦 CT: {ct_line}\n"
            f"Tranches: {tranche_str}\n"
        )
        time.sleep(0.5)

    total_spent = sum(h.get("spent", 0) for h in holdings)
    # Reuse the batched prices — re-fetching here was the second half of the
    # rate-limit problem.
    total_val   = sum(
        h.get("units", 0) * (
            (_batch.get(h.get("contract_address")) or (None, None))[0]
            or h.get("entry_price", 0)
        )
        for h in holdings
    )
    unrealized  = total_val - total_spent

    # ── TRUE TOTAL ────────────────────────────────────────────────────────
    # The old "Total P&L" was `total_val - total_spent`, i.e. unrealized gains
    # on OPEN positions only. Every closed trade was excluded, and so was every
    # tranche harvest — and tranche sells never write a trade_history entry,
    # they only add cash. So profits from 2x/3x exits were invisible everywhere.
    #
    # Measuring against the account instead captures everything: realized,
    # unrealized, tranche harvests, and fees. cash + positions - basis is the
    # only figure that cannot silently omit a category of profit.
    state    = load_fomo_portfolio()
    basis    = float(state.get("starting_cash", 0) or 0)
    hist     = state.get("trade_history", [])
    realized_logged = sum(
        float(t.get("profit", t.get("pnl_usd", 0)) or 0) for t in hist
    )
    account_value = cash + total_val
    true_pnl      = account_value - basis if basis else 0.0

    # Tranche harvests are now recorded explicitly rather than derived as a
    # residual — a residual can't distinguish "no harvests" from "harvests
    # that cancelled out", which is why this line always read $0.00.
    tranches   = state.get("tranche_sales", [])
    tranche_pl = sum(float(t.get("profit", 0) or 0) for t in tranches)
    tranche_wins = sum(1 for t in tranches if (t.get("profit") or 0) > 0)

    tot_emoji = "🟢" if true_pnl >= 0 else "🔴"
    pct = (true_pnl / basis * 100) if basis else 0.0

    block = (
        f"───────────────\n"
        f"Cash: ${cash:.2f} | Positions: ${total_val:.2f}\n"
        f"<b>Account: ${account_value:.2f}</b> (started ${basis:,.0f})\n"
        f"{tot_emoji} <b>TOTAL P&amp;L: ${true_pnl:+.2f}</b> ({pct:+.2f}%)\n"
        f"\n"
        f"  ├ Unrealized (open): ${unrealized:+.2f}\n"
        f"  ├ Full exits ({len(hist)}): ${realized_logged:+.2f}\n"
    )
    if tranches:
        block += (f"  └ Tranche harvests ({len(tranches)}, {tranche_wins} green): "
                  f"${tranche_pl:+.2f}")
    else:
        block += ("  └ Tranche harvests: none logged yet\n"
                  "     <i>(harvests before this build weren't recorded — "
                  "the money is in cash and counted in the total)</i>")
    lines.append(block)

    send_telegram("\n".join(lines))


def _handle_screenshot(message: dict):
    """
    Read a forwarded screenshot and act on what's in it.

    Runs on its own thread: vision extraction takes several seconds and the
    Telegram webhook must return promptly or Telegram retries the update,
    which would process the same screenshot twice.

    Two routes, in priority order:
      HELD  — a trader's stance on something we own changes whether we keep
              sitting on a drawdown. Feeds straight into a position review.
      NEW   — secondary intel. Goes through the SAME research pipeline as any
              other signal and ends in a button. A screenshot is unverifiable,
              so it can raise a candidate but never authorise a buy.
    """
    token = os.getenv("FOMO_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN", "")
    try:
        import vision
        send_telegram("📷 Reading screenshot...")
        res = vision.process_screenshot(message, "fomo", token)
        if not res:
            send_telegram("⚠️ Couldn't download that image.")
            return

        send_telegram(res["text"], parse_mode=None)
        routed = res["routed"]

        # ── HELD: re-decide the position with this new information ──────────
        for item in routed.get("held", []):
            holding = item.get("position") or {}
            ticker  = item.get("symbol", "?")
            if not holding:
                continue

            # Record what the trader said ON the position, so it survives into
            # the postmortem and the next review rather than living only in a
            # Telegram message you'd have to scroll back to find.
            holding.setdefault("social_notes", []).append({
                "at":         datetime.now(timezone.utc).isoformat(),
                "source":     "screenshot",
                "poster":     res["extracted"].get("poster_handle"),
                "stance":     item.get("stance"),
                "conviction": item.get("conviction"),
                "quote":      (item.get("key_quote") or "")[:300],
            })

            try:
                from fomo_exit import get_prices_batch
                from fomo_review import review_position, format_review
                prices = get_prices_batch([holding.get("contract_address")])
                px, liq = prices.get(holding.get("contract_address"), (0, 0))
                if not px:
                    send_telegram(
                        f"⚠️ Noted the {ticker} intel, but can't price it right "
                        f"now — review skipped rather than run on a stale price."
                    )
                    continue
                r = review_position(holding, px, liq)
                if r.get("verdict") in ("EXIT", "TRIM"):
                    send_telegram_button(
                        format_review(r), "SELL NOW",
                        f"rug_sell:{holding.get('contract_address')}")
                else:
                    send_telegram(format_review(r))
            except Exception as e:
                log.error(f"FOMO: review after screenshot failed for {ticker}: {e}")

            try:
                from fomo_portfolio import load_fomo_portfolio, save_fomo_portfolio
                st = load_fomo_portfolio()
                for h in st.get("holdings", []):
                    if h.get("contract_address") == holding.get("contract_address"):
                        h["social_notes"] = holding["social_notes"]
                save_fomo_portfolio(st)
            except Exception as e:
                log.error(f"FOMO: could not persist social note: {e}")

        # ── NEW: research candidates, never auto-buy ────────────────────────
        for item in routed.get("new", []):
            if not item.get("is_new_call"):
                continue          # commentary, not a call — intel only
            ca = item.get("identifier")
            if not ca:
                send_telegram(
                    f"🔍 <b>{item.get('symbol','?')}</b> looks like a new call, "
                    f"but no contract address was readable in the image.\n\n"
                    f"Send the address and I'll research it."
                )
                continue
            send_telegram(f"🔬 Researching {item.get('symbol','?')} from screenshot...")
            try:
                process_social_signal({
                    "alias":        res["extracted"].get("poster_handle") or "screenshot",
                    "action":       "BUY",
                    "token_symbol": item.get("symbol"),
                    "contract_address": ca,
                    "confidence":   item.get("conviction", "low"),
                    "signal_text":  item.get("key_quote", ""),
                    "source":       "screenshot",
                })
            except Exception as e:
                log.error(f"FOMO: screenshot research failed: {e}")
                send_telegram(f"⚠️ Research failed: {type(e).__name__}: {e}")

    except Exception as e:
        log.error(f"FOMO: screenshot handling failed: {e}", exc_info=True)
        send_telegram(f"⚠️ Screenshot read failed: {type(e).__name__}: {e}")


def handle_relayed_text_message(message: dict):
    """A plain text message (not a button tap) arrived on the Telegram webhook --
    treat it as a manually relayed signal (forwarded email or a quick note) and
    research it. Never executes anything on its own -- always ends in an EXECUTE
    button, exactly like every other signal path in this system."""
    chat_id = message.get("chat", {}).get("id")
    text    = message.get("text", "")

    if not text or not text.strip():
        return
    # Only respond in your own chat -- don't let a random sender burn API calls
    if TELEGRAM_CHAT_ID and str(chat_id) != str(TELEGRAM_CHAT_ID):
        log.warning(f"FOMO: ignoring text message from unrecognized chat_id {chat_id}")
        return

    # -- /positions command --
    if any(kw in text.lower() for kw in ("/positions", "my positions", "/update")):
        send_positions_update()
        return

    # -- /leaderboard command --
    if any(kw in text.lower() for kw in ("/leaderboard", "leaderboard", "/stats")):
        send_telegram(get_wallet_leaderboard())
        return

    # -- /reconcile — do the books balance across all three bots? --
    if text.lower().startswith("/reconcile"):
        try:
            from reconcile import reconcile_all, format_report
            send_telegram(format_report(reconcile_all()))
        except Exception as e:
            log.error(f"Reconcile failed: {e}", exc_info=True)
            send_telegram(f"⚠️ Reconcile failed: {e}")
        return

    # -- /alpaca command — probe Alpaca free-tier data quality --
    if text.lower().startswith("/alpaca"):
        import threading
        send_telegram("🔬 Probing Alpaca data quality... (~20s)")

        def _probe():
            try:
                from alpaca_probe import run_probe
                send_telegram(run_probe())
            except Exception as e:
                log.error(f"Alpaca probe failed: {e}", exc_info=True)
                send_telegram(f"⚠️ Probe failed: {e}")

        threading.Thread(target=_probe, daemon=True, name="alpaca-probe").start()
        return

    # -- /addwallet command — manually add a wallet to the watchlist --
    if text.lower().startswith(("/addwallet", "/add_wallet", "/add ")):
        _handle_add_wallet(text)
        return

    # -- /discover command — force a wallet discovery run now --
    if text.lower().startswith(("/discover", "/discovery")):
        import threading, time as _t
        # parse.bot is rate/credit limited. Two runs in an hour exhausted it and
        # produced a full page of "couldn't evaluate". Guard manual triggers.
        global _last_manual_discovery
        _min_gap = float(os.getenv("FOMO_DISCOVER_MIN_GAP_MIN", "60")) * 60
        _since   = _t.time() - _last_manual_discovery
        if _last_manual_discovery and _since < _min_gap and "force" not in text.lower():
            mins = (_min_gap - _since) / 60
            send_telegram(
                f"⏳ <b>Discovery ran {_since/60:.0f} min ago</b>\n\n"
                f"parse.bot has tight rate/credit limits — running again too soon "
                f"returns empty data and wastes the quota.\n\n"
                f"Try again in {mins:.0f} min, or send <code>/discover force</code> "
                f"to override."
            )
            return
        _last_manual_discovery = _t.time()
        send_telegram(
            "🔍 <b>Running wallet discovery...</b>\n"
            "Scanning leaderboards, deep-analyzing each candidate, and "
            "auto-adding those that qualify. This takes a few minutes."
        )

        def _run():
            try:
                run_weekly_discovery(ignore_gate=True)
            except Exception as e:
                log.error(f"Manual discovery failed: {e}", exc_info=True)
                send_telegram(f"⚠️ Discovery failed: {e}")

        threading.Thread(target=_run, daemon=True, name="fomo-manual-discovery").start()
        return

    log.info(f"FOMO: relayed text message received ({len(text)} chars)")
    parsed     = parse_relayed_signal(text)
    alias      = parsed.get("wallet_alias")
    action     = (parsed.get("action") or "UNCLEAR").upper()
    contract   = parsed.get("contract_address")
    symbol     = parsed.get("token_symbol")
    confidence = parsed.get("confidence", "low")
    notes      = parsed.get("notes", "")
    is_noise   = parsed.get("is_noise", False)

    # Pure SOL/USDC/USDT transfer — not a trade, silently ignore
    if is_noise or (action == "UNCLEAR" and not contract and not symbol):
        log.info("FOMO: relayed message is noise (pure quote transfer), ignoring silently")
        return

    # Symbol known but no contract — try DexScreener lookup
    if not contract and symbol:
        log.info(f"FOMO: no contract in relayed message, trying DexScreener lookup for {symbol}")
        contract = _lookup_contract_by_symbol(symbol)
        if contract:
            log.info(f"FOMO: resolved {symbol} → {contract}")
        else:
            send_telegram(
                f"\U0001f914 <b>Couldn't find contract for {symbol}.</b>\n"
                + (f"Notes: {notes}\n" if notes else "")
                + f"Forward the Solscan tx or paste the mint address directly."
            )
            return

    if action == "UNCLEAR" or not contract:
        send_telegram(
            "\U0001f914 <b>Couldn't parse a clear signal from that.</b>\n"
            + (f"Notes: {notes}\n" if notes else "")
            + "Try including the token's contract address and whether it was a buy or sell."
        )
        return

    matched_alias = _match_known_alias(alias) or alias or "unknown trader"

    token_data = validate_token(contract)
    if not token_data["valid"]:
        send_telegram(
            f"\u26a0\ufe0f <b>Relayed signal skipped</b>\n"
            f"{matched_alias} {action.lower()} {token_data.get('symbol','???')}\n"
            f"Reason: {token_data.get('reject_reason')}"
        )
        return

    sync_fomo_state_from_github()
    portfolio = load_fomo_portfolio()
    holdings  = portfolio.get("holdings", [])

    if action == "BUY":
        if len(holdings) >= FOMO_MAX_CONCURRENT_POSITIONS:
            send_telegram(
                f"\u26a0\ufe0f At max concurrent positions -- "
                f"skipping relayed buy signal for {token_data['symbol']}."
            )
            return
        if _find_holding(holdings, contract):
            send_telegram(
                f"\u26a0\ufe0f Already holding {token_data['symbol']} -- "
                f"skipping duplicate relayed buy signal."
            )
            return
        # Deep research on relayed signal
        _relay_alias = matched_alias or "unknown"
        _relay_wallet_info = _get_wallet_meta(_relay_alias)
        signal_ctx = {
            "alias":        _relay_alias,
            "tier":         _relay_wallet_info.get("tier", "B"),
            "bankroll_usd": _relay_wallet_info.get("bankroll_usd"),
            "action":       "BUY",
            "symbol":       token_data["symbol"],
            "source":       "email",
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            # The relayed message body. This used to read
            #   raw_text if "raw_text" in dir() else ""
            # — but dir() inside a function lists LOCALS only, raw_text was
            # never one, so the guard was always False and research received an
            # empty string on every relayed signal. The variable is `text`.
            "original_text": text,
        }
        verdict = research_token(contract, "solana", signal_ctx)
        lessons     = get_wallet_lessons(matched_alias)
        best        = lessons.get("best_conditions", {})
        skip_reason = None
        if not verdict.go:
            skip_reason = verdict.skip_reason
        elif (best.get("min_catalyst_score_for_win") and
              verdict.final_score < best["min_catalyst_score_for_win"] - 2):
            skip_reason = (f"Research score {verdict.final_score} below "
                           f"{matched_alias}'s historical win threshold")
        if skip_reason:
            send_telegram(
                f"\u26a0\ufe0f <b>Relayed Signal Filtered</b>\n"
                f"{matched_alias} bought {token_data['symbol']}\n"
                f"Reason: {skip_reason}\n"
                + verdict.to_telegram_summary()
            )
            return

        alert_id = create_pending_buy_alert({
            "token_ticker":     token_data["symbol"],
            "token_name":       token_data["name"],
            "entry_price":      token_data["price"],
            "wallet_alias":     matched_alias,
            "wallet_address":   "",
            "contract_address": contract,
            "catalyst":         verdict.go_reason,
            "catalyst_score":   verdict.final_score,
            "market_cap":       token_data.get("market_cap"),
            "liquidity_usd":    token_data.get("liquidity_usd"),
            "token_age_days":   token_data.get("age_days"),
            "volume_spike_pct": token_data.get("volume_spike_pct"),
            "signal_source":    "relayed_email",
        })
        send_telegram_button(
            "\U0001f4e9 <b>RELAYED BUY SIGNAL: " + token_data["symbol"] + " @ $"
            + "{:.8f}".format(token_data["price"]) + "</b>\n"
            + "From: " + matched_alias + f" (confidence: {confidence})\n"
            # catalyst_data never existed in this scope — this line raised
            # NameError on EVERY relayed buy signal, after the pending alert had
            # already been created. The alert sat there unexecutable and no
            # button ever reached Telegram. The research result is `verdict`.
            + "Catalyst (" + str(verdict.final_score) + "/10): "
            + str(verdict.go_reason or "n/a") + "\n"
            + "Mcap: $" + "{:,.0f}".format(token_data.get("market_cap") or 0)
            + " | Liq: $" + "{:,.0f}".format(token_data.get("liquidity_usd") or 0) + "\n"
            + "\u23f1 Expires in " + str(BUY_ALERT_EXPIRY_MINUTES) + " min",
            "EXECUTE",
            f"buy_show:{alert_id}",
        )

    elif action == "SELL":
        holding = _find_holding(holdings, contract)
        if not holding:
            send_telegram(
                f"\u2139\ufe0f Not currently holding {token_data['symbol']} -- "
                f"nothing to exit on this relayed sell signal."
            )
            return
        alert_id = create_pending_sell_alert({
            "token_ticker":     holding["token_ticker"],
            "wallet_alias":     matched_alias,
            "contract_address": holding.get("contract_address"),
            "price_at_signal":  token_data.get("price") or holding["entry_price"],
        })
        send_telegram_button(
            f"\U0001f4e9 <b>RELAYED SELL SIGNAL: {matched_alias} sold {holding['token_ticker']}</b>\n"
            f"Tap to confirm your exit.",
            "EXECUTE",
            f"sell_exec:{alert_id}",
        )


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    """
    Send a message, with a plain-text fallback if HTML parsing is rejected.

    The previous version discarded the response entirely — no status check, no
    logging. A message rejected by Telegram (400) simply vanished and looked
    identical to a successful send. Memecoin names routinely contain `<`, `>`
    and `&`, all of which break HTML parse mode, so this was silently dropping
    alerts about real positions.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TELEGRAM] {message}")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
        if r.status_code == 200:
            return

        body = (r.text or "").lower()
        if r.status_code == 400 and ("can't parse entities" in body
                                     or "unsupported start tag" in body
                                     or "unmatched end tag" in body):
            log.warning(f"FOMO Telegram: HTML rejected, resending plain "
                        f"({r.text[:120]})")
            try:
                plain = re.sub(r"<[^>]+>", "", message)
                r2 = requests.post(url,
                                   json={"chat_id": TELEGRAM_CHAT_ID, "text": plain},
                                   timeout=10)
                if r2.status_code != 200:
                    log.error(f"FOMO Telegram: plain retry failed "
                              f"HTTP {r2.status_code}: {r2.text[:160]}")
            except Exception as e:
                log.error(f"FOMO Telegram: plain retry error: {e}")
            return

        log.warning(f"FOMO Telegram HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


def send_telegram_button(message: str, button_text: str, callback_data: str):
    """
    Send a Telegram message with a single tappable inline button.
    This is the plumbing for the execute-confirmation flow -- the button tap
    is the actual human trigger-pull, never automatic.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TELEGRAM-BUTTON] {message} | [{button_text}]")
        return None
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": button_text, "callback_data": callback_data}
                    ]]
                },
            },
            timeout=10,
        )
        return r.json()
    except Exception as e:
        log.warning(f"Telegram button send failed: {e}")
        return None


def register_telegram_webhook():
    """
    Called once at startup. Tells Telegram where to POST button-tap events.
    Requires RAILWAY_PUBLIC_DOMAIN to be set on this service.
    """
    webhook_base = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")
    if not TELEGRAM_BOT_TOKEN or not webhook_base:
        log.warning("Telegram webhook not registered -- missing token or public domain")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/setWebhook",
            json={"url": f"https://{webhook_base}/webhook/telegram"},
            timeout=10,
        )
        log.info(f"Telegram webhook registered: {r.json()}")
    except Exception as e:
        log.warning(f"Telegram webhook registration failed: {e}")


# ─── MULTI-WALLET LAUNCH CONSENSUS ───────────────────────────────────────────
# When a brand-new token (<MIN_TOKEN_AGE days) is normally filtered as rug risk,
# we watch it in a short-lived cache. If 2+ vetted wallets buy the same contract
# within CONSENSUS_WINDOW_HOURS, we fire a consensus signal — multi-wallet
# conviction overrides the age filter.

LAUNCH_CACHE_FILE      = "fomo_launch_cache.json"
CONSENSUS_WINDOW_HOURS = 4    # window to collect wallet buys
CONSENSUS_MIN_WALLETS  = 2    # number of qualified wallets to trigger
CONSENSUS_MIN_SCORE    = 60   # NARRATIVE_WATCH wallets need at least this vetting score

_launch_cache: dict = {}      # in-memory; synced with LAUNCH_CACHE_FILE


def _load_launch_cache() -> dict:
    try:
        if os.path.exists(LAUNCH_CACHE_FILE):
            with open(LAUNCH_CACHE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {}


def _save_launch_cache(cache: dict):
    try:
        with open(LAUNCH_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        log.warning(f"Could not save launch cache: {e}")


def _prune_launch_cache(cache: dict) -> dict:
    """Drop entries older than CONSENSUS_WINDOW_HOURS."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=CONSENSUS_WINDOW_HOURS)
    pruned = {}
    for contract, entry in cache.items():
        try:
            if datetime.fromisoformat(entry["first_seen"]) > cutoff:
                pruned[contract] = entry
        except Exception:
            pass
    return pruned


def _is_qualified_for_consensus(wallet_meta: dict) -> bool:
    """
    True if this wallet counts toward the consensus threshold.
      - COPY_TRADE recommendation, OR
      - NARRATIVE_WATCH with score >= CONSENSUS_MIN_SCORE, OR
      - Grandfathered (no vetting) with copy_trade=True
    """
    vetting = wallet_meta.get("vetting") or {}
    rec     = vetting.get("recommendation")
    score   = vetting.get("score") or 0
    if rec == "COPY_TRADE":
        return True
    if rec == "NARRATIVE_WATCH" and score >= CONSENSUS_MIN_SCORE:
        return True
    if rec is None and wallet_meta.get("copy_trade", False):
        return True   # grandfathered wallet
    return False


def _check_launch_consensus(contract: str, alias: str, wallet_meta: dict, token_data: dict):
    """
    Add this wallet's buy to the new-token consensus cache.
    Fires a Telegram signal with EXECUTE button when CONSENSUS_MIN_WALLETS
    qualified wallets have bought the same contract within CONSENSUS_WINDOW_HOURS.
    """
    global _launch_cache

    _launch_cache = _prune_launch_cache(_load_launch_cache())
    now_str   = datetime.now(timezone.utc).isoformat()
    qualified = _is_qualified_for_consensus(wallet_meta)

    if contract not in _launch_cache:
        _launch_cache[contract] = {
            "first_seen": now_str,
            "token_data": token_data,
            "entries":    [],
            "notified":   False,
        }

    entry = _launch_cache[contract]

    # Dedup — never count the same wallet twice
    if alias in {e["alias"] for e in entry["entries"]}:
        _save_launch_cache(_launch_cache)
        return

    entry["entries"].append({
        "alias":     alias,
        "timestamp": now_str,
        "qualified": qualified,
    })

    qualified_wallets = [e for e in entry["entries"] if e["qualified"]]
    q_count = len(qualified_wallets)

    symbol = token_data.get("symbol", "???")
    log.info(
        f"Launch consensus: {symbol} ({contract[:8]}…) — "
        f"{q_count}/{CONSENSUS_MIN_WALLETS} qualified wallets | notified={entry['notified']}"
    )

    if q_count >= CONSENSUS_MIN_WALLETS and not entry["notified"]:
        entry["notified"] = True
        _save_launch_cache(_launch_cache)

        mcap     = token_data.get("market_cap", 0) or 0
        liq      = token_data.get("liquidity_usd", 0) or 0
        price    = token_data.get("price", 0) or 0
        age_days = token_data.get("age_days", 0) or 0
        age_h    = round(age_days * 24, 1)
        amount   = 50  # default paper position size

        wallet_list = "\n".join(
            f"  {'✅' if e['qualified'] else '👁️'} {e['alias']}"
            for e in entry["entries"]
        )

        alert_id = create_pending_buy_alert({
            "type":             "BUY",
            "contract":         contract,
            "symbol":           symbol,
            "wallet_alias":     f"CONSENSUS ({q_count} wallets)",
            "amount_usd":       amount,
            "price":            price,
            "catalyst":         f"Consensus launch: {q_count} vetted wallets within {CONSENSUS_WINDOW_HOURS}h",
            "catalyst_score":   8,
            "market_cap":       mcap,
            "liquidity_usd":    liq,
            "token_age_days":   age_days,
            "signal_source":    "consensus_launch",
        })

        send_telegram_button(
            f"🚀 <b>CONSENSUS LAUNCH: ${symbol}</b>\n"
            f"⏱️ Age: {age_h}h | 💧 Liq: ${liq:,.0f} | Mcap: ${mcap:,.0f}\n"
            f"\n<b>{q_count} vetted wallets bought:</b>\n{wallet_list}\n"
            f"\n<i>Age filter overridden by multi-wallet conviction.</i>",
            "🚀 EXECUTE",
            f"buy_exec_{amount}:{alert_id}",
        )
    else:
        _save_launch_cache(_launch_cache)
        if q_count < CONSENSUS_MIN_WALLETS:
            log.info(
                f"Launch consensus: watching ${symbol} — "
                f"need {CONSENSUS_MIN_WALLETS - q_count} more qualified wallet(s)"
            )


# ─── TOKEN VALIDATION (DexScreener) ──────────────────────────────────────────

def validate_token(contract_address: str) -> dict:
    """
    Quick sanity check via DexScreener before buying.
    Returns dict with valid:bool, price, market_cap, liquidity, symbol, name, age_days.
    """
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract_address}",
            timeout=10,
            headers=HEADERS,
        )
        if r.status_code != 200:
            return {"valid": False, "reject_reason": f"DexScreener HTTP {r.status_code}"}

        pairs = r.json().get("pairs", [])
        if not pairs:
            return {"valid": False, "reject_reason": "No trading pairs found"}

        # Use the pair with highest liquidity
        pair = sorted(pairs, key=lambda p: p.get("liquidity", {}).get("usd", 0) or 0, reverse=True)[0]

        market_cap = pair.get("marketCap") or 0
        liquidity  = (pair.get("liquidity") or {}).get("usd") or 0
        price      = float(pair.get("priceUsd") or 0)
        symbol     = (pair.get("baseToken") or {}).get("symbol", "???")
        name       = (pair.get("baseToken") or {}).get("name", "???")

        # Token age from pair creation timestamp
        created_at = pair.get("pairCreatedAt")  # unix ms
        age_days   = None
        if created_at:
            age_days = (time.time() - created_at / 1000) / 86400

        # Volume spike in last 5 min vs 1h average
        vol_5m = (pair.get("volume") or {}).get("m5") or 0
        vol_1h = (pair.get("volume") or {}).get("h1") or 0
        volume_spike_pct = ((vol_5m * 12) / vol_1h * 100 - 100) if vol_1h > 0 else 0

        # Validation checks
        reasons = []
        if market_cap < MIN_MARKET_CAP:
            reasons.append(f"Market cap ${market_cap:,.0f} < ${MIN_MARKET_CAP:,.0f} minimum")
        if liquidity < MIN_LIQUIDITY:
            reasons.append(f"Liquidity ${liquidity:,.0f} < ${MIN_LIQUIDITY:,.0f} minimum")
        age_reject = age_days is not None and age_days < MIN_TOKEN_AGE
        if age_reject:
            reasons.append(f"Token only {age_days:.1f} days old — rug risk")
        if price <= 0:
            reasons.append("Price is zero")

        valid = len(reasons) == 0
        # True when age is the ONLY rejection reason — market cap, liquidity, price all pass.
        # Callers can route these to the consensus cache instead of dropping them.
        age_only_reject = age_reject and len(reasons) == 1

        return {
            "valid":            valid,
            "age_only_reject":  age_only_reject,
            "reject_reason":    " | ".join(reasons) if reasons else None,
            "price":            price,
            "market_cap":       market_cap,
            "liquidity_usd":    liquidity,
            "symbol":           symbol,
            "name":             name,
            "age_days":         age_days,
            "volume_spike_pct": volume_spike_pct,
        }

    except Exception as e:
        return {"valid": False, "reject_reason": f"Validation error: {e}"}


# ─── CATALYST SCANNER ─────────────────────────────────────────────────────────

def scan_catalyst(symbol: str, contract_address: str) -> dict:
    """
    Scan for what drove this buy. Checks Twitter mentions and DexScreener alerts.
    Returns {catalyst: str, score: int (0-10), sources: list}
    """
    catalyst_signals = []
    score            = 0

    # ── Twitter/X recent mentions ─────────────────────────────────────────────
    if TWITTER_BEARER:
        try:
            r = requests.get(
                "https://api.twitter.com/2/tweets/search/recent",
                params={
                    "query":        f"${symbol} OR #{symbol} lang:en -is:retweet",
                    "max_results":  20,
                    "tweet.fields": "created_at,public_metrics",
                    "sort_order":   "recency",
                },
                headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
                timeout=10,
            )
            if r.status_code == 200:
                tweets = r.json().get("data", [])
                if tweets:
                    # Count tweets in last 30 min
                    cutoff     = datetime.now(timezone.utc) - timedelta(minutes=30)
                    recent_tweets = [
                        t for t in tweets
                        if datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")) > cutoff
                    ]
                    total_likes = sum(t.get("public_metrics", {}).get("like_count", 0) for t in recent_tweets)
                    total_rt    = sum(t.get("public_metrics", {}).get("retweet_count", 0) for t in recent_tweets)

                    if len(recent_tweets) >= 5:
                        catalyst_signals.append(f"{len(recent_tweets)} tweets in last 30min")
                        score += 2
                    if total_likes > 500:
                        catalyst_signals.append(f"{total_likes} likes on recent posts")
                        score += 3
                    if total_rt > 100:
                        catalyst_signals.append(f"{total_rt} retweets")
                        score += 2
        except Exception as e:
            log.debug(f"Twitter scan failed: {e}")

    # ── DexScreener volume spike ───────────────────────────────────────────────
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract_address}",
            timeout=10, headers=HEADERS,
        )
        if r.status_code == 200:
            pairs = r.json().get("pairs", [])
            if pairs:
                pair   = pairs[0]
                vol_5m = (pair.get("volume") or {}).get("m5") or 0
                vol_1h = (pair.get("volume") or {}).get("h1") or 0
                if vol_1h > 0:
                    spike = (vol_5m * 12) / vol_1h
                    if spike > 3:
                        catalyst_signals.append(f"Volume {spike:.1f}x above hourly average")
                        score += 3
                    elif spike > 1.5:
                        catalyst_signals.append(f"Volume {spike:.1f}x above hourly average")
                        score += 1

                # Price momentum
                price_change_5m = float((pair.get("priceChange") or {}).get("m5") or 0)
                if price_change_5m > 10:
                    catalyst_signals.append(f"Price +{price_change_5m:.1f}% in 5 min")
                    score += 2
    except Exception as e:
        log.debug(f"DexScreener catalyst scan failed: {e}")

    catalyst_str = " | ".join(catalyst_signals) if catalyst_signals else "No clear catalyst identified"
    score        = min(score, 10)

    return {
        "catalyst": catalyst_str,
        "score":    score,
        "sources":  catalyst_signals,
    }


# ─── TRANSACTION PARSING ──────────────────────────────────────────────────────

def parse_alchemy_activity(activity: dict, wallet_address: str) -> Optional[dict]:
    """
    Parse an Alchemy address-activity event.
    Returns {"type": "BUY"|"SELL", "contract": str, "symbol": str, "value": float} or None.
    """
    from_addr = (activity.get("fromAddress") or "").lower()
    to_addr   = (activity.get("toAddress")   or "").lower()
    category  = activity.get("category", "")
    contract  = (activity.get("rawContract") or {}).get("address", "")

    # Only care about ERC-20 token transfers
    if category != "token" or not contract:
        return None

    wallet = wallet_address.lower()

    if to_addr == wallet and from_addr != wallet:
        # Tokens arriving at wallet = BUY
        return {"type": "BUY", "contract": contract, "value": activity.get("value", 0)}

    if from_addr == wallet and to_addr != wallet:
        # Tokens leaving wallet = SELL
        return {"type": "SELL", "contract": contract, "value": activity.get("value", 0)}

    return None



# ─── HELIUS (SOLANA) WEBHOOK SUPPORT ─────────────────────────────────────────

def parse_helius_activity(tx, wallet_address):
    if tx.get("type") != "SWAP":
        return None
    fee_payer = tx.get("feePayer", "")
    if fee_payer.lower() != wallet_address.lower():
        return None
    token_transfers = tx.get("tokenTransfers", [])
    bought, sold = [], []
    for xfer in token_transfers:
        mint = xfer.get("mint", "")
        if mint == WSOL_MINT:
            continue
        to_user   = xfer.get("toUserAccount", "")
        from_user = xfer.get("fromUserAccount", "")
        if to_user.lower() == wallet_address.lower():
            bought.append(mint)
        elif from_user.lower() == wallet_address.lower():
            sold.append(mint)
    if bought:
        return {"type": "BUY", "contract": bought[0]}
    if sold:
        return {"type": "SELL", "contract": sold[0]}
    return None


def register_helius_webhook(wallet_address, webhook_url):
    if not HELIUS_API_KEY:
        log.warning("HELIUS_API_KEY not set")
        return None
    try:
        payload = {
            "webhookURL":       webhook_url + "/webhook/helius",
            "transactionTypes": ["SWAP"],
            "accountAddresses": [wallet_address],
            "webhookType":      "enhanced",
        }
        if HELIUS_AUTH_HEADER:
            payload["authHeader"] = HELIUS_AUTH_HEADER
        r = requests.post(
            "https://api.helius.xyz/v0/webhooks",
            params={"api-key": HELIUS_API_KEY},
            json=payload,
            timeout=15,
        )
        if r.status_code in (200, 201):
            wid = r.json().get("webhookID")
            log.info("Helius webhook: %s -> %s", wallet_address[:8], wid)
            return wid
        log.warning("Helius registration failed: %s %s", r.status_code, r.text[:120])
        return None
    except Exception as e:
        log.warning("Helius registration error: %s", e)
        return None


def delete_helius_webhook(webhook_id):
    if not HELIUS_API_KEY:
        return False
    try:
        r = requests.delete(
            "https://api.helius.xyz/v0/webhooks/" + webhook_id,
            params={"api-key": HELIUS_API_KEY},
            timeout=10,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


def sync_helius_webhooks(webhook_base_url):
    """Register Helius webhooks for ALL confirmed Solana wallets (tier A and B).
    Tier B wallets are monitored for context/learning even if we don't auto-trade them.
    Webhooks are only deleted if a wallet is fully removed from the config."""
    data    = load_trusted_wallets()
    changed = False
    for w in data.get("tier_a", []) + data.get("tier_b", []):
        addr = w.get("wallet", "")
        if addr.startswith("FILL_IN") or w.get("chain", "base") != "solana":
            continue
        if not w.get("alchemy_webhook_id"):
            wid = register_helius_webhook(addr, webhook_base_url)
            if wid:
                w["alchemy_webhook_id"] = wid
                changed = True
    if changed:
        save_trusted_wallets(data)

# ─── WEBHOOK ENDPOINT ─────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    stats = get_fomo_stats()
    return jsonify({
        "status":      "ok",
        "timestamp":   datetime.now(timezone.utc).isoformat(),
        "fomo_value":  stats["total_value"],
        "fomo_trades": stats["total_trades"],
    })


@app.route("/test/telegram-button", methods=["GET"])
def test_telegram_button():
    """Plumbing test only -- sends a fake button, no trade logic attached."""
    send_telegram_button(
        "\U0001f514 TEST NOTIFICATION -- this is a plumbing test, not a real trade.",
        "EXECUTE (test)",
        "test_execute_1",
    )
    return jsonify({"ok": True, "message": "Test button sent"})


@app.route("/test/buy-alert", methods=["GET"])
def test_buy_alert():
    """Visual test only -- shows what a real buy alert would look like, with fake data."""
    send_telegram_button(
        "\U0001f6a8 TEST BUY: TESTCOIN @ $0.00043",
        "EXECUTE",
        "test_buy_show_amounts",
    )
    return jsonify({"ok": True, "message": "Test buy alert sent"})


@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    """Receives button taps and relayed text messages from Telegram -- both are
    human-initiated; execution always still needs a tap."""
    update = request.json or {}
    callback = update.get("callback_query")
    if not callback:
        message = update.get("message") or update.get("channel_post")
        if message:
            chat      = message.get("chat", {})
            chat_type = chat.get("type", "private")  # private / group / supergroup / channel
            text      = message.get("text", "")
            if chat_type in ("channel", "group", "supergroup") and text:
                # Message from a monitored channel/group — parse for social signal
                chan_username = chat.get("username")
                chan_title    = chat.get("title", "")
                signal = parse_channel_message(text, chan_title, chan_username)
                if signal:
                    log.info(f"Telegram channel signal: {signal['alias']} {signal['action']} ${signal.get('token_symbol','?')} from {chan_title}")
                    process_social_signal(signal)
                else:
                    log.debug(f"Telegram channel message from {chan_title}: no signal detected")
            else:
                # Screenshot from the phone — a trader's post about a coin.
                # Checked before the text relay because an image message has
                # no "text" field and would otherwise be dropped in silence.
                handled = False
                try:
                    import vision
                    if vision.has_image(message):
                        handled = True
                        import threading as _th
                        _th.Thread(target=_handle_screenshot, args=(message,),
                                   daemon=True, name="fomo-vision").start()
                except Exception as e:
                    log.error(f"FOMO: screenshot dispatch failed: {e}")
                if not handled:
                    # Direct message from user — existing relay handling
                    handle_relayed_text_message(message)
        return jsonify({"ok": True})

    callback_id = callback["id"]
    data        = callback.get("data", "")
    message     = callback.get("message", {})
    chat_id     = message.get("chat", {}).get("id")
    message_id  = message.get("message_id")

    log.info(f"Telegram button tapped: {data}")

    def edit(text, reply_markup=None):
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/editMessageText",
                json=payload, timeout=10,
            )
        except Exception as e:
            log.warning(f"Telegram edit failed: {e}")

    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": "Received!"},
            timeout=10,
        )

        # ── Real buy flow: EXECUTE tapped -> reveal $ amount options ──────────
        if data.startswith("buy_show:"):
            alert_id = data.split(":", 1)[1]
            alert    = get_pending_alert(alert_id)
            if not alert:
                edit("\u23f1 Signal expired -- no longer valid.")
            else:
                suggested = suggest_buy_amount(alert.get("catalyst_score", 0))
                def label(amt):
                    return f"\u2b50 ${amt}" if amt == suggested else f"${amt}"
                edit(
                    f"\U0001f6a8 <b>{alert['token_ticker']} @ ${alert['entry_price']:.8f}</b>\n"
                    f"How much to invest?",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": label("50"),  "callback_data": f"buy_amt:{alert_id}:50"},
                             {"text": label("100"), "callback_data": f"buy_amt:{alert_id}:100"}],
                            [{"text": label("200"), "callback_data": f"buy_amt:{alert_id}:200"},
                             {"text": label("500"), "callback_data": f"buy_amt:{alert_id}:500"}],
                        ]
                    },
                )

        # ── Real buy flow: $ amount tapped -> actually execute the paper buy ──
        elif data.startswith("buy_amt:"):
            _, alert_id, amount_str = data.split(":", 2)
            alert = get_pending_alert(alert_id)
            if not alert:
                edit("\u23f1 Window expired -- trade not executed.")
            else:
                result = execute_fomo_buy(
                    token_ticker=alert["token_ticker"],
                    token_name=alert["token_name"],
                    entry_price=alert["entry_price"],
                    wallet_alias=alert["wallet_alias"],
                    wallet_address=alert["wallet_address"],
                    contract_address=alert.get("contract_address"),
                    catalyst=alert.get("catalyst"),
                    catalyst_score=alert.get("catalyst_score", 0),
                    market_cap=alert.get("market_cap"),
                    liquidity_usd=alert.get("liquidity_usd"),
                    token_age_days=alert.get("token_age_days"),
                    volume_spike_pct=alert.get("volume_spike_pct"),
                    amount_usd=float(amount_str),
                    signal_source=alert.get("signal_source", "fomo_copy"),
                    regime=alert.get("regime"),
                )
                consume_pending_alert(alert_id)
                if result:
                    edit(
                        f"\u2705 <b>Bought {result['token_ticker']}</b>\n"
                        f"${result['spent']:.2f} @ ${result['entry_price']:.8f}\n"
                        f"Stop: ${result['stop_loss']:.8f} (-15%) | "
                        f"Target: ${result['exit_target']:.8f} (+30%)\n"
                        f"Following {alert['wallet_alias']}"
                    )
                else:
                    edit("\u26a0\ufe0f Buy did not execute (already holding a position, or insufficient cash).")

        # ── Real sell flow: EXECUTE tapped -> re-check price, execute the sell,
        #    reveal $ profit as the actual confirm ─────────────────────────────
        elif data.startswith("sell_exec:"):
            alert_id = data.split(":", 1)[1]
            alert    = get_pending_alert(alert_id)
            if not alert:
                edit("\u23f1 Signal expired -- no longer valid.")
            else:
                portfolio = load_fomo_portfolio()
                holdings  = portfolio.get("holdings", [])
                holding   = _find_holding(holdings, alert.get("contract_address"))
                if not holding:
                    consume_pending_alert(alert_id)
                    edit("\u26a0\ufe0f Position already closed (an auto-exit likely already fired) -- nothing to execute.")
                else:
                    token_data      = validate_token(alert["contract_address"])
                    current_price   = token_data.get("price") or alert["price_at_signal"]
                    price_at_signal = alert.get("price_at_signal") or current_price
                    drift_pct = ((current_price - price_at_signal) / price_at_signal * 100) if price_at_signal else 0

                    entered_at   = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
                    held_hrs     = (datetime.now(timezone.utc) - entered_at).total_seconds() / 3600
                    created_at   = datetime.fromisoformat(alert["created_at"].replace("Z", "+00:00"))
                    exit_lag_min = (datetime.now(timezone.utc) - created_at).total_seconds() / 60

                    result = execute_fomo_sell(
                        holding["contract_address"],
                        current_price,
                        reason="tracker_sell_" + alert["wallet_alias"],
                        trader_held_hours=held_hrs,
                        exit_lag_minutes=exit_lag_min,
                    )
                    consume_pending_alert(alert_id)

                    if result:
                        pct     = result["profit_pct"]
                        profit  = result["profit"]
                        outcome = "WIN" if pct > 0 else "LOSS"
                        update_wallet_stats(alert["wallet_alias"], outcome, pct)
                        icon = "\u2705" if pct > 0 else "\U0001f534"
                        drift_note = ""
                        if abs(drift_pct) >= 10:
                            drift_note = f"\n(Price moved {drift_pct:+.1f}% since the signal was sent)"
                        edit(
                            f"{icon} <b>SOLD {result['token_ticker']}</b>\n"
                            f"{'+' if profit >= 0 else ''}${profit:,.2f} ({pct:+.1f}%)\n"
                            f"Following {alert['wallet_alias']}'s exit{drift_note}"
                        )
                    else:
                        edit("\u26a0\ufe0f Sell did not execute (no matching position found).")

        # ── Visual-test flows (unchanged) ──────────────────────────────────────
        elif data == "test_buy_show_amounts":
            suggested = "200"
            def label(amt):
                return f"\u2b50 ${amt}" if amt == suggested else f"${amt}"
            edit(
                "\U0001f6a8 TEST BUY: TESTCOIN @ $0.00043\nHow much to invest?",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": label("50"),  "callback_data": "test_buy_amt_50"},
                         {"text": label("100"), "callback_data": "test_buy_amt_100"}],
                        [{"text": label("200"), "callback_data": "test_buy_amt_200"},
                         {"text": label("500"), "callback_data": "test_buy_amt_500"}],
                    ]
                },
            )

        elif data.startswith("test_buy_amt_"):
            amount = data.replace("test_buy_amt_", "")
            edit(
                f"\u2705 TEST: Would execute ${amount} buy of TESTCOIN @ $0.00043\n"
                f"(This was a test -- no trade occurred.)"
            )

        elif data.startswith("rug_sell:"):
            # Fired by the proactive rug detector in fomo_exit.py
            from fomo_exit import handle_tracker_sell
            contract_addr = data.split(":", 1)[1]
            portfolio     = load_fomo_portfolio()
            held          = _find_holding(portfolio.get("holdings", []), contract_addr)
            if not held:
                edit("⚠️ Position not found — may already be closed.")
            else:
                ticker        = held.get("token_ticker", "?")
                current_token = validate_token(contract_addr)
                cur_price     = current_token.get("price") or held["entry_price"]
                net           = handle_tracker_sell(contract_addr, cur_price, ticker)
                if net is not None:
                    entry   = held.get("entry_price", cur_price)
                    pnl_pct = ((cur_price / entry) - 1) * 100 if entry else 0
                    edit(
                        f"✅ <b>Manual exit executed: {ticker}</b>\n"
                        f"P&amp;L: {pnl_pct:+.0f}% | Recovered: ${net:.2f}"
                    )
                else:
                    edit("⚠️ Position not found or already closed.")

        elif data.startswith("ack_stop:"):
            from fomo_exit import silence_stop_alarm
            ack_id   = data.split(":", 1)[1]
            silenced = silence_stop_alarm(ack_id)
            if silenced:
                edit(
                    "\ud83d\udd15 <b>Stop-loss alarm silenced.</b>\n"
                    "Trade already auto-executed. Check your portfolio for updated P&amp;L."
                )
            else:
                edit("\u2705 Alarm already silenced (or expired).")

        else:
            edit(f"\u2705 Button tap received: {data}")

    except Exception as e:
        log.warning(f"Telegram callback handling failed: {e}")

    return jsonify({"ok": True})


@app.route("/webhook/helius", methods=["POST"])
def helius_webhook():
    if HELIUS_AUTH_HEADER:
        incoming = request.headers.get("Authorization", "")
        if incoming != HELIUS_AUTH_HEADER:
            log.warning("Helius webhook: invalid auth header")
            return jsonify({"error": "Unauthorized"}), 401

    events = request.json or []
    if isinstance(events, dict):
        events = [events]

    for tx in events:
        fee_payer   = tx.get("feePayer", "").lower()
        wallet_info = get_wallet_info(fee_payer)
        if not wallet_info:
            continue

        alias       = wallet_info["alias"]
        wallet_addr = wallet_info["wallet"]
        portfolio   = load_fomo_portfolio()
        holdings    = portfolio.get("holdings", [])
        parsed      = parse_helius_activity(tx, wallet_addr)
        if not parsed:
            continue

        held_match = _find_holding(holdings, parsed.get("contract")) if parsed["type"] == "SELL" else None
        if parsed["type"] == "SELL" and held_match:
            holding = held_match
            log.info("FOMO Solana: %s sold %s - awaiting human confirm", alias, holding["token_ticker"])
            token_data = validate_token(parsed["contract"])
            price_at_signal = token_data.get("price") or holding["entry_price"]
            alert_id = create_pending_sell_alert({
                "token_ticker":     holding["token_ticker"],
                "wallet_alias":     alias,
                "contract_address": holding.get("contract_address"),
                "price_at_signal":  price_at_signal,
            })
            send_telegram_button(
                "\U0001f514 <b>" + alias + " sold " + holding["token_ticker"] + "</b>\n"
                + "Tap to confirm your exit.",
                "EXECUTE",
                f"sell_exec:{alert_id}",
            )

        elif (parsed["type"] == "BUY" and len(holdings) < FOMO_MAX_CONCURRENT_POSITIONS
              and not _find_holding(holdings, parsed.get("contract"))):
            contract   = parsed["contract"]
            token_data = validate_token(contract)
            if not token_data["valid"]:
                if token_data.get("age_only_reject"):
                    _check_launch_consensus(contract, alias, wallet_info, token_data)
                else:
                    log.info("FOMO Solana: skipping %s buy - %s",
                             alias, token_data.get("reject_reason"))
                continue
            # Deep research engine — replaces basic catalyst scanner
            signal_ctx = {
                "alias":        alias,
                "tier":         wallet_info.get("tier", "A"),
                "bankroll_usd": wallet_info.get("bankroll_usd"),
                "action":       "BUY",
                "symbol":       token_data["symbol"],
                "source":       "on-chain",
                "timestamp":    datetime.now(timezone.utc).isoformat(),
            }
            verdict = research_token(contract, "solana", signal_ctx)

            lessons     = get_wallet_lessons(alias)
            best        = lessons.get("best_conditions", {})
            skip_reason = None
            if not verdict.go:
                skip_reason = verdict.skip_reason
            elif (best.get("min_catalyst_score_for_win") and
                  verdict.final_score < best["min_catalyst_score_for_win"] - 2):
                skip_reason = (f"Research score {verdict.final_score} below "
                               f"{alias}'s historical win threshold")
            if skip_reason:
                log.info("FOMO Solana: skipping %s buy - %s", alias, skip_reason)
                send_telegram(
                    f"\u26a0\ufe0f <b>FOMO Signal Filtered</b>\n"
                    f"{alias} bought {token_data['symbol']}\n"
                    f"Reason: {skip_reason}\n"
                    + verdict.to_telegram_summary()
                )
                continue

            # Don't auto-buy -- store the signal and let the human tap EXECUTE.
            alert_id = create_pending_buy_alert({
                "token_ticker":     token_data["symbol"],
                "token_name":       token_data["name"],
                "entry_price":      token_data["price"],
                "wallet_alias":     alias,
                "wallet_address":   wallet_addr,
                "contract_address": contract,
                "catalyst":         verdict.go_reason,
                "catalyst_score":   verdict.final_score,
                "market_cap":       token_data.get("market_cap"),
                "liquidity_usd":    token_data.get("liquidity_usd"),
                "token_age_days":   token_data.get("age_days"),
                "volume_spike_pct": token_data.get("volume_spike_pct"),
                "signal_source":    "onchain_solana",
            })
            send_telegram_button(
                "\U0001f6a8 <b>ON-CHAIN SIGNAL: " + token_data["symbol"] + " @ $"
                + "{:.8f}".format(token_data["price"]) + "</b>\n"
                + "Following: " + alias + " (on-chain)\n"
                + verdict.to_telegram_summary() + "\n"
                + "\u23f1 Expires in " + str(BUY_ALERT_EXPIRY_MINUTES) + " min",
                "EXECUTE",
                f"buy_show:{alert_id}",
            )

    return jsonify({"ok": True})


@app.route("/webhook/alchemy", methods=["POST"])
def alchemy_webhook():
    # ── Verify Alchemy signature ──────────────────────────────────────────────
    if ALCHEMY_SIGNING_KEY:
        sig      = request.headers.get("X-Alchemy-Signature", "")
        body     = request.get_data()
        expected = hmac.new(
            ALCHEMY_SIGNING_KEY.encode("utf-8"), body, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(sig, expected):
            log.warning("Alchemy webhook: invalid signature")
            return jsonify({"error": "Invalid signature"}), 401

    payload    = request.json or {}
    activities = payload.get("event", {}).get("activity", [])

    for activity in activities:
        from_addr = (activity.get("fromAddress") or "").lower()
        to_addr   = (activity.get("toAddress")   or "").lower()

        # Identify which trusted wallet this involves
        wallet_info = get_wallet_info(from_addr) or get_wallet_info(to_addr)
        if not wallet_info:
            continue

        wallet_addr = wallet_info["wallet"].lower()
        alias       = wallet_info["alias"]

        parsed = parse_alchemy_activity(activity, wallet_addr)
        if not parsed:
            continue

        portfolio = load_fomo_portfolio()
        holdings  = portfolio.get("holdings", [])

        # ── SELL: tracked wallet selling a token we're holding ────────────────
        held_match = _find_holding(holdings, parsed.get("contract")) if parsed["type"] == "SELL" else None
        if parsed["type"] == "SELL" and held_match:
            holding = held_match
            log.info(f"FOMO: {alias} sold {holding['token_ticker']} — awaiting human confirm")

            # Get a reference price now; re-checked again at the moment of execution
            token_data = validate_token(parsed["contract"])
            price_at_signal = token_data.get("price") or holding["entry_price"]

            alert_id = create_pending_sell_alert({
                "token_ticker":     holding["token_ticker"],
                "wallet_alias":     alias,
                "contract_address": holding.get("contract_address"),
                "price_at_signal":  price_at_signal,
            })
            send_telegram_button(
                f"🔔 <b>{alias} sold {holding['token_ticker']}</b>\n"
                f"Tap to confirm your exit.",
                "EXECUTE",
                f"sell_exec:{alert_id}",
            )

        # ── BUY: tracked wallet buying something new ──────────────────────────
        elif (parsed["type"] == "BUY" and len(holdings) < FOMO_MAX_CONCURRENT_POSITIONS
              and not _find_holding(holdings, parsed.get("contract"))):
            contract = parsed["contract"]

            # Validate the token
            token_data = validate_token(contract)
            if not token_data["valid"]:
                if token_data.get("age_only_reject"):
                    _check_launch_consensus(contract, alias, wallet_info, token_data)
                else:
                    reason = token_data.get("reject_reason", "failed validation")
                    log.info(f"FOMO: Skipping {alias} buy — {reason}")
                    if not any(s in reason for s in ("429", "No trading pairs", "Validation error")):
                        send_telegram(
                            f"⚠️ <b>FOMO Signal Filtered</b>\n"
                            f"{alias} bought {token_data.get('symbol','???')}\n"
                            f"Skipped: {reason}"
                        )
                continue

            # Deep research engine — replaces basic catalyst scanner
            # Fetch lessons BEFORE the decision, not after.
            # Previously get_wallet_lessons() ran on the line *after*
            # research_token(), so the past performance of this wallet was
            # only ever used to decorate the Telegram message — the agent
            # deciding whether to buy never saw it. The system recorded
            # lessons diligently and then ignored them.
            lessons = get_wallet_lessons(alias)
            best    = lessons.get("best_conditions", {})

            signal_ctx = {
                "alias":        alias,
                "tier":         wallet_info.get("tier", "A"),
                "bankroll_usd": wallet_info.get("bankroll_usd"),
                "action":       "BUY",
                "symbol":       token_data["symbol"],
                "source":       "on-chain",
                "timestamp":    datetime.now(timezone.utc).isoformat(),
                "wallet_lessons": lessons,
            }
            verdict = research_token(contract, "base", signal_ctx)
            skip_reason = None
            if not verdict.go:
                skip_reason = verdict.skip_reason
            elif (best.get("min_catalyst_score_for_win") and
                  verdict.final_score < best["min_catalyst_score_for_win"] - 2):
                skip_reason = (f"Research score {verdict.final_score} below "
                               f"{alias}'s historical win threshold")

            if skip_reason:
                log.info(f"FOMO: Skipping {alias} buy — {skip_reason}")
                send_telegram(
                    f"⚠️ <b>FOMO Signal Filtered</b>\n"
                    f"{alias} bought {token_data['symbol']}\n"
                    f"Reason: {skip_reason}\n"
                    + verdict.to_telegram_summary()
                )
                continue

            # Don't auto-buy -- store the signal and let the human tap EXECUTE.
            alert_id = create_pending_buy_alert({
                "token_ticker":     token_data["symbol"],
                "token_name":       token_data["name"],
                "entry_price":      token_data["price"],
                "wallet_alias":     alias,
                "wallet_address":   wallet_addr,
                "contract_address": contract,
                "catalyst":         verdict.go_reason,
                "catalyst_score":   verdict.final_score,
                "market_cap":       token_data.get("market_cap"),
                "liquidity_usd":    token_data.get("liquidity_usd"),
                "token_age_days":   token_data.get("age_days"),
                "volume_spike_pct": token_data.get("volume_spike_pct"),
                "signal_source":    "onchain_base",
            })
            send_telegram_button(
                "🚨 <b>ON-CHAIN SIGNAL: " + token_data["symbol"] + " @ $"
                + "{:.8f}".format(token_data["price"]) + "</b>\n"
                + "Following: " + alias + " (Base chain)\n"
                + verdict.to_telegram_summary() + "\n"
                + "⏱ Expires in " + str(BUY_ALERT_EXPIRY_MINUTES) + " min",
                "EXECUTE",
                f"buy_show:{alert_id}",
            )

    return jsonify({"ok": True})


# ─── ALCHEMY WEBHOOK MANAGEMENT ───────────────────────────────────────────────

def register_alchemy_webhook(wallet_address: str, webhook_url: str) -> Optional[str]:
    """
    Register an Alchemy address-activity webhook for a wallet.
    Returns webhook_id or None on failure.
    Requires ALCHEMY_AUTH_TOKEN env var (different from API key).
    """
    auth_token = os.environ.get("ALCHEMY_AUTH_TOKEN", "")
    if not auth_token:
        log.warning("ALCHEMY_AUTH_TOKEN not set — cannot register webhook")
        return None
    try:
        r = requests.post(
            "https://dashboard.alchemy.com/api/create-webhook",
            headers={"X-Alchemy-Token": auth_token, "Content-Type": "application/json"},
            json={
                "network":         "BASE_MAINNET",
                "webhook_type":    "ADDRESS_ACTIVITY",
                "webhook_url":     webhook_url,
                "addresses":       [wallet_address],
            },
            timeout=15,
        )
        if r.status_code in (200, 201):
            webhook_id = r.json().get("data", {}).get("id")
            log.info("Alchemy webhook registered: %s... -> %s", wallet_address[:10], webhook_id)
            return webhook_id
        else:
            log.warning(f"Alchemy webhook registration failed: {r.status_code} {r.text[:120]}")
            return None
    except Exception as e:
        log.warning(f"Alchemy webhook registration error: {e}")
        return None


def delete_alchemy_webhook(webhook_id: str) -> bool:
    """Delete an Alchemy webhook by ID."""
    auth_token = os.environ.get("ALCHEMY_AUTH_TOKEN", "")
    if not auth_token:
        return False
    try:
        r = requests.delete(
            "https://dashboard.alchemy.com/api/delete-webhook",
            headers={"X-Alchemy-Token": auth_token},
            params={"webhook_id": webhook_id},
            timeout=10,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


def sync_alchemy_webhooks(webhook_base_url: str):
    """
    Called by the 4-hour agent. Ensures all Tier A wallets have active webhooks
    and demoted wallets have their webhooks deleted.
    """
    data    = load_trusted_wallets()
    changed = False

    for w in data.get("tier_a", []):
        addr = w.get("wallet", "")
        if addr.startswith("FILL_IN"):
            continue   # not yet populated
        if w.get("chain", "solana") != "base":
            continue   # Alchemy webhooks are Base/EVM only; Solana uses Helius
        if not w.get("alchemy_webhook_id"):
            webhook_id = register_alchemy_webhook(
                addr, f"{webhook_base_url}/webhook/alchemy"
            )
            if webhook_id:
                w["alchemy_webhook_id"] = webhook_id
                changed = True

    # Clean up demoted wallets (Base chain only)
    for w in data.get("tier_b", []):
        if w.get("chain", "solana") != "base":
            continue
        if w.get("alchemy_webhook_id"):
            deleted = delete_alchemy_webhook(w["alchemy_webhook_id"])
            if deleted:
                w["alchemy_webhook_id"] = None
                changed = True
                log.info(f"Deleted webhook for demoted wallet {w['alias']}")

    if changed:
        save_trusted_wallets(data)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

# ─── SOCIAL SIGNAL PROCESSOR ──────────────────────────────────────────────────────────────────────────────

def process_social_signal(signal: dict):
    """
    Process a social media signal (Twitter or Telegram channel) through the
    same research + execution pipeline as on-chain webhook signals.

    Called by the background social poller and by the Telegram webhook handler
    when a message arrives from a monitored channel.

    signal keys: alias, tier, chain, bankroll_usd, action, token_symbol,
                 contract_address, confidence, source, timestamp, original_text
    """
    alias    = signal.get("alias", "unknown")
    action   = (signal.get("action") or "").upper()
    symbol   = signal.get("token_symbol")
    contract = signal.get("contract_address")
    source   = signal.get("source", "social")
    chain    = signal.get("chain", "solana")

    log.info(f"Social signal: {alias} {action} ${symbol or '?'} via {source}")

    # Need at least a symbol or contract to proceed
    if not symbol and not contract:
        log.info(f"Social signal from {alias}: no token identified, skipping")
        return

    # If no contract, try DexScreener lookup
    if not contract and symbol:
        contract = _lookup_contract_by_symbol(symbol)
        if contract:
            log.info(f"Social: resolved ${symbol} → {contract}")
        else:
            send_telegram(
                f"🤔 <b>Social signal: ${symbol}</b> from {alias} ({source})\n"
                f"Couldn't find a contract address for ${symbol}. "
                f"Forward the CA directly to research it."
            )
            return

    # Validate token basics
    token_data   = validate_token(contract)
    display_name = f"${symbol}" if symbol else f"{contract[:8]}…"
    # ── Golem independent path ────────────────────────────────────────────────
    # A young token that fails ONLY on age is exactly what Golem's radar is built
    # to find. If the signal came from Golem's own analysis, judge it on the
    # young-token risk profile rather than the copy-trade one.
    golem_trade = False
    if (GOLEM_INDEPENDENT_TRADING
            and token_data.get("age_only_reject")
            and source in GOLEM_SOURCES):
        age_h = (token_data.get("age_days") or 0) * 24
        liq   = token_data.get("liquidity") or 0
        if age_h >= GOLEM_MIN_AGE_HOURS and liq >= GOLEM_MIN_LIQUIDITY:
            token_data["valid"] = True
            golem_trade = True
            log.info(
                f"Golem independent trade allowed: {display_name} "
                f"({age_h:.1f}h old, ${liq:,.0f} liq) — young-token profile, "
                f"{GOLEM_SIZE_MULTIPLIER:.0%} size"
            )
        else:
            log.info(
                f"Golem signal {display_name} rejected: {age_h:.1f}h old "
                f"(need {GOLEM_MIN_AGE_HOURS}h), ${liq:,.0f} liq "
                f"(need ${GOLEM_MIN_LIQUIDITY:,.0f})"
            )

    if not token_data["valid"]:
        reject = token_data.get("reject_reason", "failed validation")
        if token_data.get("age_only_reject"):
            # New token, otherwise healthy — route to consensus tracker instead of dropping
            _check_launch_consensus(contract, alias, _get_wallet_meta(alias), token_data)
        elif "No trading pairs" in reject or "429" in reject or "Validation error" in reject:
            # Transient API errors — DexScreener rate limit or network blip. Silent skip.
            log.info(f"Social signal filtered ({alias}): {display_name} — {reject}")
        else:
            # Rejections are the normal case — most signals should be filtered.
            # Alerting on every one buries the alerts that actually matter
            # (entries, exits, stop-losses) under routine noise. Logged always,
            # notified only if you opt in.
            log.info(f"Signal filtered ({alias} via {source}): "
                     f"{display_name} — {reject}")
            if NOTIFY_FILTERED:
                send_telegram(
                    f"⚠️ <b>Signal filtered</b> ({alias} via {source})\n"
                    f"{display_name}: {reject}"
                )
        return

    sync_fomo_state_from_github()
    portfolio = load_fomo_portfolio()
    holdings  = portfolio.get("holdings", [])

    # ── Vetting gate ──────────────────────────────────────────────────────────
    # Wallets added before vetting (vetting=null) are grandfathered — existing
    # copy_trade flag controls behavior unchanged.
    wallet_meta   = _get_wallet_meta(alias)
    vetting       = wallet_meta.get("vetting") or {}
    vetting_rec   = vetting.get("recommendation")   # None = grandfathered
    vetting_score = vetting.get("score")

    if vetting_rec == "REJECT":
        log.info(f"Signal from {alias} suppressed — vetting: REJECT")
        return

    if vetting_rec == "TWITTER_ONLY":
        send_telegram(
            f"🐦 <b>CT signal: {alias} on ${token_data['symbol']}</b>\n"
            f"<i>Wallet scored {vetting_score}/100 — Twitter-only, no on-chain slot.</i>\n"
            f"Sentiment context logged. No buy action."
        )
        return

    if action == "BUY":
        if len(holdings) >= FOMO_MAX_CONCURRENT_POSITIONS:
            send_telegram(
                f"⚠️ Social signal from {alias}: at max positions, "
                f"skipping ${token_data['symbol']} buy."
            )
            return
        if _find_holding(holdings, contract):
            log.info(f"Social: already holding {token_data['symbol']}, ignoring duplicate")
            return

        # Deep research
        # First-buy detection — flag if wallet has never bought this contract before
        is_first_buy = check_first_buy(alias, contract)

        # Convergence: record this buy, then check if other wallets bought same token recently
        record_convergence_signal(alias, contract)
        convergence = check_convergence(alias, contract)

        # Market regime: scale position size based on BTC/SOL trend
        regime = get_market_regime()

        signal_ctx = {
            "alias":        alias,
            "tier":         signal.get("tier", "B"),
            "bankroll_usd": signal.get("bankroll_usd"),
            "action":       "BUY",
            "symbol":       token_data["symbol"],
            "source":       source,
            "timestamp":    signal.get("timestamp", datetime.now(timezone.utc).isoformat()),
            "original_text": signal.get("original_text", ""),
            "is_first_buy": is_first_buy,
        }
        verdict = research_token(contract, chain, signal_ctx)

        # Hard veto only — research score drives position size, not execution gate
        source_icon  = "🐦" if source == "twitter" else "📢" if source == "telegram" else "📧"
        first_buy_badge = " 🆕 FIRST BUY" if is_first_buy else " 📈 adding to position"

        # Narrative-watch wallets: inform but never execute
        if not signal.get("copy_trade", True):
            ct_reason = signal.get("copy_trade_reason", "narrative watch only")
            send_telegram(
                f"👁️ <b>{alias} {action} {token_data['symbol']}</b> {source_icon}\n"
                f"<i>Narrative watch — not copy-trading ({ct_reason[:100]})</i>\n"
                + verdict.to_telegram_summary()
            )
            return

        if not verdict.go:
            # Genuine rug guard triggered — do not execute
            # If we're ALREADY HOLDING this token, escalate to an urgent button alert
            held = _find_holding(holdings, contract)
            if held:
                sell_alert_id = create_pending_sell_alert({
                    "token_ticker":     held["token_ticker"],
                    "wallet_alias":     "rug_guard",
                    "contract_address": contract,
                    "price_at_signal":  token_data.get("price") or held["entry_price"],
                })
                send_telegram_button(
                    f"🚨 <b>RUG GUARD — ACTIVE POSITION AT RISK: {token_data['symbol']}</b>\n"
                    f"You are currently holding this token and it just flagged as a rug risk.\n"
                    f"Reason: {verdict.skip_reason}\n"
                    + verdict.to_telegram_summary(),
                    "🚨 SELL NOW",
                    f"sell_exec:{sell_alert_id}",
                )
            else:
                send_telegram(
                    f"🛡️ <b>RUG GUARD: {token_data['symbol']} blocked</b> {source_icon}\n"
                    f"Trader: {alias} | Source: {source}\n"
                    f"Reason: {verdict.skip_reason}\n"
                    + verdict.to_telegram_summary()
                )
            return

        # ── Confidence calibration ──────────────────────────────────────────
        conf = get_confidence({
            "alias":            alias,
            "catalyst_score":   verdict.final_score,
            "market_cap":       token_data.get("market_cap"),
            "liquidity_usd":    token_data.get("liquidity_usd"),
            "token_age_days":   token_data.get("age_days"),
            "volume_spike_pct": token_data.get("volume_spike_pct"),
            "regime":           regime.get("regime", "UNKNOWN"),
        })
        if conf["suppress"]:
            log.info(f"Signal from {alias} suppressed — confidence {conf['score']}/100")
            send_telegram(
                format_suppression_telegram(
                    alias, token_data["symbol"], conf,
                    verdict_summary=verdict.to_telegram_summary()
                )
            )
            return

        pos_pct = getattr(verdict, "suggested_position_pct", None) or DEFAULT_POSITION_PCT
        # Apply regime modifier (reduce in bear, unchanged in bull)
        pos_pct = max(MIN_POSITION_PCT, pos_pct + regime["modifier_pct"])
        # Apply convergence boost (more wallets = bigger position)
        if convergence["is_convergence"]:
            pos_pct = min(MAX_POSITION_PCT, pos_pct + convergence["boost_pct"])
        # Apply confidence position multiplier (reduces size for lower-confidence setups)
        pos_pct = max(MIN_POSITION_PCT, pos_pct * conf["position_multiplier"])
        # Golem independent trades on very young tokens carry real rug risk —
        # take the setup, but at reduced size.
        if golem_trade:
            pos_pct = max(2.0, pos_pct * GOLEM_SIZE_MULTIPLIER)
            log.info(f"Golem young-token sizing: {pos_pct:.1f}% of bankroll")
        alert_id = create_pending_buy_alert({
            "token_ticker":          token_data["symbol"],
            "token_name":            token_data["name"],
            "entry_price":           token_data["price"],
            "wallet_alias":          alias,
            "wallet_address":        "",
            "contract_address":      contract,
            "catalyst":              verdict.go_reason,
            "catalyst_score":        verdict.final_score,
            "market_cap":            token_data.get("market_cap"),
            "liquidity_usd":         token_data.get("liquidity_usd"),
            "token_age_days":        token_data.get("age_days"),
            "volume_spike_pct":      token_data.get("volume_spike_pct"),
            "suggested_position_pct": pos_pct,
            "signal_source":         source,            # "scanner"=Path A, "golem_momentum"/"new_launch"/"narrative"=Path B
            "regime":                regime["regime"],  # market regime at signal time
        })
        # Conviction label for the Telegram message
        if pos_pct >= 25:
            conv_label = "🔥 HIGH CONVICTION"
        elif pos_pct >= 15:
            conv_label = "⚡ SOLID SIGNAL"
        else:
            conv_label = "🔎 SPECULATIVE"
        convergence_line = ("\n" + convergence["label"]) if convergence["is_convergence"] else ""
        regime_line = "\n" + regime["summary"] if regime["regime"] != "BULL" else ""
        vetting_line = (
            f"\n🔬 Vetting: {vetting_score}/100 {vetting_rec}"
            if vetting_rec and vetting_score is not None else ""
        )
        confidence_line = format_confidence_line(conf)
        send_telegram_button(
            source_icon + " <b>FOMO SIGNAL: " + token_data["symbol"] + "</b>  " + conv_label + first_buy_badge + "\n"
            + "Trader: " + alias + " (" + source + ")  |  "
            + "Suggested: <b>" + str(int(pos_pct)) + "% FOMO cash</b>\n"
            + (f"Signal: \"{signal.get('signal_text', '')[:80]}\"\n" if signal.get("signal_text") else "")
            + verdict.to_telegram_summary() + "\n"
            + convergence_line + regime_line + vetting_line + confidence_line + "\n"
            + "⏱ Expires in " + str(BUY_ALERT_EXPIRY_MINUTES) + " min",
            "EXECUTE",
            f"buy_show:{alert_id}",
        )

    elif action == "SELL":
        holding = _find_holding(holdings, contract)
        if not holding:
            send_telegram(
                source_icon + f" <b>{alias} sold ${token_data['symbol']}</b> ({source})\n"
                f"You're not holding this token."
            )
            return
        # Tranche-aware exit — respects already-sold tranches
        current_price = token_data.get("price") or holding["entry_price"]
        from fomo_exit import handle_tracker_sell
        handle_tracker_sell(contract, current_price, ticker=token_data["symbol"])


_SOLANA_ADDR_RE = re.compile(r"\b([1-9A-HJ-NP-Za-km-z]{32,44})\b")


def _handle_add_wallet(text: str):
    """
    Manually add a wallet to the watchlist.

        /addwallet <address> <alias> [A|B]

    Goes to Tier B by default — unproven wallets get observed before being
    trusted with webhooks. Promotion to Tier A happens automatically once the
    wallet meets the promotion rules.
    """
    parts = text.split()
    if len(parts) < 2:
        send_telegram(
            "➕ <b>Add a wallet</b>\n\n"
            "<code>/addwallet &lt;address&gt; &lt;name&gt;</code>\n\n"
            "Example:\n"
            "<code>/addwallet FVZRwUp6E4m9jV4VumF8q7m8q3mF9fpikRrJSCCfFAdP 000xy_0</code>\n\n"
            "Adds to Tier B (observation). Auto-promotes to Tier A once it "
            "proves out."
        )
        return

    m = _SOLANA_ADDR_RE.search(text)
    if not m:
        send_telegram(
            "⚠️ No valid Solana address found.\n"
            "Addresses are 32-44 base58 characters."
        )
        return
    address = m.group(1)

    # Alias = first token that isn't the command or the address
    alias = ""
    for p in parts[1:]:
        if p != address and not p.startswith("/"):
            alias = p.lstrip("@")
            break
    alias = alias or address[:8]

    tier = "tier_b"
    if parts[-1].upper() == "A":
        tier = "tier_a"

    try:
        data = load_trusted_wallets()
        for t in ("tier_a", "tier_b"):
            for w in data.get(t, []):
                if w.get("wallet") == address:
                    send_telegram(
                        f"ℹ️ Already watching <b>{w.get('alias', alias)}</b> "
                        f"in {t.replace('tier_', 'Tier ').upper()}."
                    )
                    return

        data.setdefault(tier, []).append({
            "wallet":     address,
            "alias":      alias,
            "tier":       "A" if tier == "tier_a" else "B",
            "added_at":   datetime.now(timezone.utc).isoformat(),
            "added_by":   "manual_telegram",
            "notes":      "Added manually — no automated vetting was possible.",
        })
        save_trusted_wallets(data)

        total = len(data.get("tier_a", [])) + len(data.get("tier_b", []))
        send_telegram(
            f"✅ <b>Added {alias}</b>\n\n"
            f"Tier: {'A (trusted)' if tier == 'tier_a' else 'B (observation)'}\n"
            f"<code>{address}</code>\n\n"
            f"Watchlist now has {total} wallet(s).\n\n"
            "<i>⚠️ Added without vetting — its hold time and 30d record are "
            "unverified. It'll be scored on the next re-vetting run.</i>"
        )
        log.warning(f"Manually added wallet {alias} ({address[:8]}...) to {tier}")
    except Exception as e:
        log.error(f"Manual wallet add failed: {e}", exc_info=True)
        send_telegram(f"⚠️ Couldn't add wallet: {e}")


_last_manual_discovery: float = 0.0


def run_weekly_discovery(ignore_gate: bool = False):
    """
    Full weekly discovery cycle — runs once per week (gated via GitHub-persisted timestamp).

    ignore_gate=True bypasses the 7-day lock (used by the /discover command).

    Run 0: Re-vet existing watchlist — auto-remove REJECT wallets, update scores.
    Scan 1: GMGN leaderboard — copy-trade candidates by win rate.
    Scan 2: Narrative whales — high-profit spray-and-pray for theme awareness.
    Scan 3: Reverse discovery — wallets found inside tokens that actually pumped
            (portfolio winners + DexScreener trending). Higher signal quality than
            leaderboard: presence in real winners, not just rankings.
    All results sent to Telegram for user review — nothing added automatically.
    """
    from fomo_gmgn import (
        discover_traders, format_discovery_telegram,
        discover_narrative_whales, format_whale_telegram,
        revett_watchlist, format_revett_telegram,
    )
    from fomo_portfolio import load_discovery_seen, save_discovery_seen

    # ── 7-day gate — gate EVERYTHING (re-vetting + all scans) ────────────────
    # Must come first: the poller calls this every hour, so the gate is what
    # prevents hourly re-vetting spam and GMGN credit burn.
    seen_data = load_discovery_seen()
    last_run  = seen_data.get("last_run")
    if last_run and not ignore_gate:
        days_ago = (datetime.now(timezone.utc) - datetime.fromisoformat(last_run)).days
        if days_ago < 7:
            log.info(f"GMGN discovery: skipping — last run was {days_ago}d ago (need 7)")
            return
    if ignore_gate:
        log.warning("GMGN discovery: MANUAL RUN — 7-day gate bypassed")
    else:
        log.info("GMGN weekly discovery: 7-day gate passed — starting re-vetting + all scans...")

    # ── Run 0: Re-vet existing watchlist ──────────────────────────────────────
    try:
        wallet_data = load_trusted_wallets()
        all_wallets = wallet_data.get("tier_a", []) + wallet_data.get("tier_b", [])
        log.info(f"Re-vetting {len(all_wallets)} watchlisted wallets...")
        revett_results = revett_watchlist(all_wallets)   # mutates wallet dicts in-place

        # Safety gate — if >50% of wallets errored (API down/rate-limited),
        # the data is too unreliable to act on. Skip removal entirely this run.
        total_wallets  = len([w for w in all_wallets
                               if w.get("wallet") and not w["wallet"].lower().startswith("fill_in")])
        total_errors   = len(revett_results.get("errors", []))
        # If a meaningful share of wallets errored, the whole run is suspect —
        # removing wallets based on a partially-down API loses good traders
        # permanently. 30% is deliberately conservative.
        _flaky_limit   = float(os.getenv("FOMO_REVETT_ERROR_LIMIT", "0.30"))
        api_too_flaky  = total_wallets > 0 and (total_errors / total_wallets) > _flaky_limit

        # Auto-remove wallets that now score REJECT — but ONLY if the data is
        # credible (WR > 0 or |PnL| > $100) AND the API wasn't mostly down.
        rejected_addrs = set()
        if not api_too_flaky:
            rejected_addrs = {
                c["wallet"] for c in revett_results.get("rejected", [])
                if c.get("winrate", 0) > 0 or abs(c.get("realized", 0)) > 100
            }
        else:
            log.warning(
                f"Re-vetting: {total_errors}/{total_wallets} wallets errored "
                f"({total_errors/max(total_wallets,1)*100:.0f}% > {_flaky_limit*100:.0f}% limit) — "
                f"API too unreliable, skipping auto-removal this run"
            )
            send_telegram(
                f"⚠️ <b>Re-vetting skipped removals</b>\n"
                f"{total_errors} of {total_wallets} wallets couldn't be fetched — "
                f"GMGN API is degraded. No wallets were removed, since acting on "
                f"missing data would drop good traders permanently."
            )
        if rejected_addrs:
            wallet_data["tier_a"] = [w for w in wallet_data.get("tier_a", [])
                                     if w.get("wallet") not in rejected_addrs]
            wallet_data["tier_b"] = [w for w in wallet_data.get("tier_b", [])
                                     if w.get("wallet") not in rejected_addrs]
            log.info(f"Auto-removed {len(rejected_addrs)} REJECT wallet(s) from watchlist")

        save_trusted_wallets(wallet_data)
        revett_msg = format_revett_telegram(revett_results)
        send_telegram(revett_msg)
    except Exception as e:
        log.error(f"Watchlist re-vetting error: {e}")

    # Load existing wallet addresses (case-sensitive — Solana base58)
    existing = set()
    try:
        with open(TRUSTED_WALLETS_FILE) as f:
            data = json.load(f)
        for tier in ("tier_a", "tier_b"):
            for w in data.get(tier, []):
                existing.add(w.get("wallet", ""))
    except Exception:
        pass

    seen_wallets = set(seen_data.get("seen", {}).keys())
    now_str      = datetime.now(timezone.utc).isoformat()

    # Scan 1: COPY_TRADE candidates — analyzed and admitted automatically
    newly_shown = []
    try:
        from fomo_autoadd import (process_candidates, format_autoadd_telegram,
                                  AUTO_ADD_ENABLED)
        candidates = discover_traders(period="7d", limit=50)

        if AUTO_ADD_ENABLED:
            # Deep-analyze BEFORE notifying, so the message is a decision record
            log.info(f"Auto-analyzing {len(candidates)} copy-trade candidate(s)...")
            results = process_candidates(candidates, existing, wallet_data)

            if results["added"]:
                save_trusted_wallets(wallet_data)
                log.warning(f"Auto-add: {len(results['added'])} wallet(s) added to watchlist")

            send_telegram(format_autoadd_telegram(results))
            # NOTE: "unavailable" is deliberately excluded — those wallets were
            # never actually evaluated (API outage). Adding them to the seen
            # cache would shelve them for 30 days over a transient failure.
            for group in ("added", "review", "rejected"):
                newly_shown.extend(c["wallet"] for c in results[group] if c.get("wallet"))
            log.info(
                f"GMGN discovery: {len(results['added'])} added, "
                f"{len(results['review'])} for review, {len(results['rejected'])} rejected"
            )
        else:
            msg, shown = format_discovery_telegram(candidates, existing, seen_wallets)
            send_telegram(msg)
            newly_shown.extend(shown)
            log.info(f"GMGN copy-trade discovery: {len(shown)} new candidate(s) shown")
    except Exception as e:
        log.error(f"GMGN copy-trade discovery error: {e}", exc_info=True)

    # Scan 2: narrative whales (high profit, spray-and-pray style)
    try:
        whales = discover_narrative_whales(period="30d", limit=50)
        whale_msg = format_whale_telegram(whales, existing | seen_wallets)
        if whale_msg:
            send_telegram(whale_msg)
            log.info(f"GMGN whale discovery: new whales found")
        else:
            log.info("GMGN whale discovery: no new whales this week")
    except Exception as e:
        log.error(f"GMGN whale discovery error: {e}")

    # Scan 3: Reverse discovery — wallets found inside tokens that actually pumped
    try:
        from fomo_gmgn import reverse_discover_from_winners, format_reverse_discovery_telegram
        from fomo_autoadd import (process_candidates, format_autoadd_telegram,
                                  AUTO_ADD_ENABLED)
        rev_candidates = reverse_discover_from_winners()

        if AUTO_ADD_ENABLED:
            log.info(f"Auto-analyzing {len(rev_candidates)} reverse-discovery candidate(s)...")
            rev_results = process_candidates(rev_candidates, existing, wallet_data)
            if rev_results["added"]:
                save_trusted_wallets(wallet_data)
                log.warning(f"Auto-add (reverse): {len(rev_results['added'])} wallet(s) added")
            msg = format_autoadd_telegram(rev_results)
            send_telegram("🔄 <b>From tokens that actually pumped:</b>\n\n" + msg)
            # "unavailable" excluded — see note above.
            for group in ("added", "review", "rejected"):
                newly_shown.extend(c["wallet"] for c in rev_results[group] if c.get("wallet"))
            log.info(f"Reverse discovery: {len(rev_results['added'])} added")
        else:
            rev_msg, rev_shown = format_reverse_discovery_telegram(
                rev_candidates, existing, seen_wallets
            )
            send_telegram(rev_msg)
            newly_shown.extend(rev_shown)
            log.info(f"Reverse discovery: {len(rev_shown)} new candidate(s) shown")
    except Exception as e:
        log.error(f"Reverse discovery error: {e}")

    # Persist last_run + all shown addresses so they're skipped next week
    seen_data["last_run"] = now_str
    for addr in newly_shown:
        seen_data.setdefault("seen", {})[addr] = now_str
    save_discovery_seen(seen_data)
    log.info(f"GMGN discovery: saved seen cache ({len(seen_data['seen'])} total addresses)")


def start_discovery_poller():
    """
    Background thread — checks every hour whether 7 days have passed since
    the last discovery run. The actual weekly gate is enforced inside
    run_weekly_discovery() via the persistent seen cache, so restarts and
    redeployments don't trigger extra runs.
    """
    import threading, time as _time
    CHECK_INTERVAL = 60 * 60   # check every hour; gate inside run_weekly_discovery

    def _loop():
        log.info("GMGN discovery poller started (checks hourly, runs weekly)")
        _time.sleep(10 * 60)   # 10-min startup delay
        while True:
            run_weekly_discovery()
            _time.sleep(CHECK_INTERVAL)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-gmgn-discovery")
    t.start()
    return t


def start_reconcile_loop():
    """
    Periodic books check for FOMO's own portfolio.

    FOMO is the bot where the silent-money bugs actually happened — tranche
    harvests crediting cash with no trade record, a bad price inventing $7.8M.
    Both were found by eye, weeks late. This makes the next one self-announcing.

    Only checks FOMO: this service has no credentials for the other two stores,
    and a blank read from them is indistinguishable from an empty one.
    """
    import threading, time as _time

    def _loop():
        _time.sleep(5 * 60)   # let startup settle before the first check
        while True:
            try:
                from reconcile import check_self
                check_self("fomo", send_telegram, html=True)
            except Exception as e:
                log.error(f"FOMO reconcile self-check error: {e}")
            _time.sleep(30 * 60)   # check_self rate-limits itself internally

    t = threading.Thread(target=_loop, daemon=True, name="fomo-reconcile")
    t.start()
    log.info("FOMO reconcile self-check loop started (silent unless books disagree)")
    return t


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"FOMO Tracker starting on port {port}")

    # Top up the paper bank if FOMO_DEPOSIT_TO is set (idempotent — no-op once funded)
    try:
        sync_fomo_state_from_github()
        maybe_repair_fomo_cash()   # fix phantom cash before any top-up
        ensure_fomo_bank()
    except Exception as e:
        log.warning(f"FOMO: bank maintenance failed: {e}")

    register_telegram_webhook()
    # Start background social poller (Twitter every 15 min)
    start_social_poller(callback=process_social_signal)
    from fomo_email import start_email_poller
    start_email_poller(callback=process_social_signal)
    start_discovery_poller()
    start_reconcile_loop()
    from fomo_exit import start_exit_monitor
    start_exit_monitor()
    import threading
    from fomo_drift import check_all_wallets_for_drift

    # Drift detection: run every 12 hours
    def _drift_loop():
        import time as _time
        _time.sleep(3600)   # 1-hour startup delay
        while True:
            try:
                reports = check_all_wallets_for_drift()
                if reports:
                    log.info(f"Drift check: {len(reports)} wallet(s) flagged")
                else:
                    log.info("Drift check: no drift detected")
            except Exception as e:
                log.error(f"Drift check error: {e}")
            _time.sleep(12 * 3600)
    threading.Thread(target=_drift_loop, daemon=True, name="fomo-drift-check").start()

    from fomo_scanner import start_scanner
    start_scanner(callback=process_social_signal)

    # ── Health-check endpoint ──────────────────────────────────────────────
    @app.route("/gmgn-debug")
    def gmgn_debug():
        """Dump raw GMGN leaderboard response so we can see actual field names."""
        import json as _json
        from fomo_gmgn import _get, CHAIN
        data = _get("get_wallet_rankings", {
            "chain":   CHAIN,
            "period":  "7d",
            "orderby": "winrate",
            "limit":   5,
        })
        if not data:
            return _json.dumps({"error": "no data returned"}), 200, {"Content-Type": "application/json"}
        rankings = data.get("rank") or []
        # Return first 3 entries raw so we can see all field names
        sample = rankings[:3] if rankings else []
        result = {"total_returned": len(rankings), "sample": sample, "top_level_keys": list(data.keys())}
        return _json.dumps(result, indent=2, default=str), 200, {"Content-Type": "application/json"}

    @app.route("/discover")
    def trigger_discovery():
        """Manually trigger GMGN trader discovery — runs both copy-trade and whale scans."""
        import threading as _threading
        import json as _json
        def _run():
            try:
                # ignore_gate: a manual trigger should always run, otherwise it
                # silently no-ops inside the 7-day window.
                run_weekly_discovery(ignore_gate=True)
            except Exception as e:
                log.error(f"Manual discovery error: {e}")
        _threading.Thread(target=_run, daemon=True, name="manual-discovery").start()
        send_telegram("🔍 <b>Manual discovery triggered</b>\nScanning GMGN leaderboard for copy-trade candidates and narrative whales...\nResults will appear here in a few minutes.")
        return _json.dumps({"status": "discovery started — watch Telegram"}), 200, {"Content-Type": "application/json"}

    @app.route("/healthcheck")
    def healthcheck():
        import json as _json
        results = {}

        # 1. Telegram
        try:
            send_telegram("🧪 <b>Golem health check started</b>\nTesting all systems...")
            results["telegram"] = "ok"
        except Exception as e:
            results["telegram"] = f"FAIL: {e}"

        # 2. DexScreener
        try:
            import requests as _req
            r = _req.get("https://api.dexscreener.com/token-boosts/top/v1", timeout=10)
            results["dexscreener"] = "ok" if r.status_code in (200, 429) else f"HTTP {r.status_code}"  # 429 = rate-limited but alive
        except Exception as e:
            results["dexscreener"] = f"FAIL: {e}"

        # 3. GMGN / parse.bot
        try:
            from fomo_gmgn import get_token_security, PARSE_API_KEY
            if not PARSE_API_KEY:
                results["gmgn"] = "FAIL: PARSE_BOT_API_KEY not set"
            else:
                # BONK is a stable known token -- good connectivity test
                sec = get_token_security("DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")
                results["gmgn"] = "ok" if sec else "FAIL: empty response"
        except Exception as e:
            results["gmgn"] = f"FAIL: {e}"

        # 4. Portfolio
        try:
            from fomo_portfolio import load_fomo_portfolio
            state = load_fomo_portfolio()
            cash = state.get("cash", 0)
            holdings = len(state.get("holdings", []))
            results["portfolio"] = f"ok — cash=${cash:.2f}, positions={holdings}"
        except Exception as e:
            results["portfolio"] = f"FAIL: {e}"

        # 5. Gmail env vars present
        import os as _os
        results["gmail"] = (
            "ok" if _os.environ.get("GMAIL_ADDRESS") and _os.environ.get("GMAIL_APP_PASSWORD")
            else "WARN: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set"
        )

        # Send summary to Telegram
        lines = ["🧪 <b>Golem Health Check Results</b>"]
        icons = {"ok": "✅", "FAIL": "❌", "WARN": "⚠️"}
        for k, v in results.items():
            icon = next((ic for tag, ic in icons.items() if v.startswith(tag)), "ℹ️")
            lines.append(f"{icon} <b>{k}</b>: {v}")
        send_telegram("\n".join(lines))

        return _json.dumps(results, indent=2), 200, {"Content-Type": "application/json"}

    app.run(host="0.0.0.0", port=port, debug=False)
