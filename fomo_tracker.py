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
import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests
from flask import Flask, request, jsonify

from fomo_portfolio import (
    execute_fomo_buy,
    execute_fomo_sell,
    load_fomo_portfolio,
    check_fomo_auto_exits,
    get_wallet_lessons,
    get_fomo_stats,
)

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
MAX_LAG_MINUTES = 15        # don't enter if we're >15 min behind the trader

HEADERS = {"User-Agent": "CryptoOracle/3.0 (fomo-tracker; non-commercial)"}

HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
HELIUS_AUTH_HEADER = os.environ.get("HELIUS_AUTH_HEADER", "")
WSOL_MINT = "So11111111111111111111111111111111111111112"

TRUSTED_WALLETS_FILE = "trusted_wallets.json"


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


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def send_telegram(message: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TELEGRAM] {message}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Telegram send failed: {e}")


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
        if age_days is not None and age_days < MIN_TOKEN_AGE:
            reasons.append(f"Token only {age_days:.1f} days old — rug risk")
        if price <= 0:
            reasons.append("Price is zero")

        valid = len(reasons) == 0

        return {
            "valid":            valid,
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
    data    = load_trusted_wallets()
    changed = False
    for w in data.get("tier_a", []):
        addr = w.get("wallet", "")
        if addr.startswith("FILL_IN") or w.get("chain", "base") != "solana":
            continue
        if not w.get("alchemy_webhook_id"):
            wid = register_helius_webhook(addr, webhook_base_url)
            if wid:
                w["alchemy_webhook_id"] = wid
                changed = True
    for w in data.get("tier_b", []):
        if w.get("chain", "base") == "solana" and w.get("alchemy_webhook_id"):
            if delete_helius_webhook(w["alchemy_webhook_id"]):
                w["alchemy_webhook_id"] = None
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
        holding     = portfolio.get("holding")
        parsed      = parse_helius_activity(tx, wallet_addr)
        if not parsed:
            continue

        if parsed["type"] == "SELL" and holding:
            held_contract = (holding.get("contract_address") or "").lower()
            if held_contract == parsed["contract"].lower():
                log.info("FOMO Solana: %s sold %s - following", alias, holding["token_ticker"])
                token_data = validate_token(parsed["contract"])
                exit_price = token_data.get("price") or holding["entry_price"]
                entered_at = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
                held_hrs   = (datetime.now(timezone.utc) - entered_at).total_seconds() / 3600
                result     = execute_fomo_sell(
                    exit_price,
                    reason="tracker_sell_" + alias,
                    trader_held_hours=held_hrs,
                    exit_lag_minutes=0,
                )
                if result:
                    pct     = result["profit_pct"]
                    outcome = "WIN" if pct > 0 else "LOSS"
                    update_wallet_stats(alias, outcome, pct)
                    icon = "\U0001f7e2" if pct > 0 else "\U0001f534"
                    msg  = (icon + " <b>FOMO Exit (Solana): " + holding["token_ticker"] + "</b>\n"
                            + "Following " + alias + " sell\n"
                            + "Return: <b>" + "{:+.1f}".format(pct) + "%</b>")
                    send_telegram(msg)

        elif parsed["type"] == "BUY" and not holding:
            contract   = parsed["contract"]
            token_data = validate_token(contract)
            if not token_data["valid"]:
                log.info("FOMO Solana: skipping %s buy - %s",
                         alias, token_data.get("reject_reason"))
                continue
            catalyst_data = scan_catalyst(token_data["symbol"], contract)
            result = execute_fomo_buy(
                token_ticker=token_data["symbol"],
                token_name=token_data["name"],
                entry_price=token_data["price"],
                wallet_alias=alias,
                wallet_address=wallet_addr,
                contract_address=contract,
                catalyst=catalyst_data["catalyst"],
                catalyst_score=catalyst_data["score"],
                market_cap=token_data.get("market_cap"),
                liquidity_usd=token_data.get("liquidity_usd"),
                token_age_days=token_data.get("age_days"),
                volume_spike_pct=token_data.get("volume_spike_pct"),
            )
            if result:
                msg = ("\U0001f6a8 <b>FOMO Entry (Solana): " + token_data["symbol"] + "</b>\n"
                       + "Following: " + alias + "\n"
                       + "Price: $" + "{:.8f}".format(token_data["price"]) + "\n"
                       + "Market cap: $" + "{:,.0f}".format(token_data.get("market_cap") or 0) + "\n"
                       + "Catalyst (" + str(catalyst_data["score"]) + "/10): "
                       + catalyst_data["catalyst"] + "\n"
                       + "Stop: -15% | Auto-exit: 24h")
                send_telegram(msg)

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
        holding   = portfolio.get("holding")

        # ── SELL: tracked wallet selling a token we're holding ────────────────
        if parsed["type"] == "SELL" and holding:
            if (holding.get("contract_address") or "").lower() == parsed["contract"].lower():
                log.info(f"FOMO: {alias} sold {holding['token_ticker']} — following exit")

                # Get current price
                token_data = validate_token(parsed["contract"])
                exit_price = token_data.get("price") or holding["entry_price"]

                # Calculate how long trader held vs us
                entered_at       = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
                trader_held_hrs  = (datetime.now(timezone.utc) - entered_at).total_seconds() / 3600
                exit_lag_min     = 0   # we're following within seconds

                result = execute_fomo_sell(
                    exit_price,
                    reason=f"tracker_sell_{alias}",
                    trader_held_hours=trader_held_hrs,
                    exit_lag_minutes=exit_lag_min,
                )

                if result:
                    pct   = result["profit_pct"]
                    emoji = "🟢" if pct > 0 else "🔴"
                    outcome = "WIN" if pct > 0 else "LOSS"
                    update_wallet_stats(alias, outcome, pct)
                    send_telegram(
                        f"{emoji} <b>FOMO Auto-Exit: {holding['token_ticker']}</b>\n"
                        f"Following {alias}'s sell\n"
                        f"Entry: ${holding['entry_price']:.6f} → Exit: ${exit_price:.6f}\n"
                        f"Return: <b>{pct:+.1f}%</b>\n"
                        f"Held: {trader_held_hrs:.1f}h\n"
                        f"Post-mortem will run next 4h cycle"
                    )

        # ── BUY: tracked wallet buying something new ──────────────────────────
        elif parsed["type"] == "BUY" and not holding:
            contract = parsed["contract"]

            # Validate the token
            token_data = validate_token(contract)
            if not token_data["valid"]:
                reason = token_data.get("reject_reason", "failed validation")
                log.info(f"FOMO: Skipping {alias} buy — {reason}")
                send_telegram(
                    f"⚠️ <b>FOMO Signal Filtered</b>\n"
                    f"{alias} bought {token_data.get('symbol','???')}\n"
                    f"Skipped: {reason}"
                )
                continue

            # Scan for catalyst
            catalyst_data = scan_catalyst(token_data["symbol"], contract)

            # Check wallet lessons — avoid known bad patterns for this trader
            lessons = get_wallet_lessons(alias)
            avoid   = lessons.get("avoid_when", [])
            best    = lessons.get("best_conditions", {})

            # Apply learned filters
            skip_reason = None
            if best.get("min_catalyst_score_for_win") and catalyst_data["score"] < best["min_catalyst_score_for_win"] - 2:
                skip_reason = (f"Catalyst score {catalyst_data['score']} below "
                               f"{alias}'s historical win threshold")

            if skip_reason:
                log.info(f"FOMO: Skipping {alias} buy — learned filter: {skip_reason}")
                send_telegram(
                    f"⚠️ <b>FOMO Signal Filtered by Lessons</b>\n"
                    f"{alias} bought {token_data['symbol']}\n"
                    f"Reason: {skip_reason}"
                )
                continue

            # Execute the buy
            result = execute_fomo_buy(
                token_ticker=token_data["symbol"],
                token_name=token_data["name"],
                entry_price=token_data["price"],
                wallet_alias=alias,
                wallet_address=wallet_addr,
                contract_address=contract,
                catalyst=catalyst_data["catalyst"],
                catalyst_score=catalyst_data["score"],
                market_cap=token_data.get("market_cap"),
                liquidity_usd=token_data.get("liquidity_usd"),
                token_age_days=token_data.get("age_days"),
                volume_spike_pct=token_data.get("volume_spike_pct"),
            )

            if result:
                stats    = wallet_info.get("stats", {})
                win_rate = stats.get("win_rate_30d", "N/A")
                cat_str  = catalyst_data["catalyst"]
                score    = catalyst_data["score"]
                send_telegram(
                    f"🚨 <b>FOMO Entry: {token_data['symbol']}</b>\n"
                    f"Following: {alias} (30d win rate: {win_rate}%)\n"
                    f"Price: ${token_data['price']:.8f}\n"
                    f"Market cap: ${token_data['market_cap']:,.0f}\n"
                    f"Liquidity: ${token_data['liquidity_usd']:,.0f}\n"
                    f"Catalyst ({score}/10): {cat_str}\n"
                    f"Stop: -15% | Target: +30%\n"
                    f"Auto-exit in 24h if {alias} hasn't sold"
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
            log.info(f"Alchemy webhook registered: {wallet_address[:10]}... → {webhook_id}")
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
        if not w.get("alchemy_webhook_id"):
            webhook_id = register_alchemy_webhook(
                addr, f"{webhook_base_url}/webhook/alchemy"
            )
            if webhook_id:
                w["alchemy_webhook_id"] = webhook_id
                changed = True

    # Clean up demoted wallets
    for w in data.get("tier_b", []):
        if w.get("alchemy_webhook_id"):
            deleted = delete_alchemy_webhook(w["alchemy_webhook_id"])
            if deleted:
                w["alchemy_webhook_id"] = None
                changed = True
                log.info(f"Deleted webhook for demoted wallet {w['alias']}")

    if changed:
        save_trusted_wallets(data)


# ─── ENTRY POINT ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    log.info(f"FOMO Tracker starting on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False)
