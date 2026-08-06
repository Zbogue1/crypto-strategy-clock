#!/usr/bin/env python3
"""
fomo_social.py — Social signal monitoring for FOMO traders.

Two signal channels:
  1. Twitter/X (free tier) — searches for posts FROM tracked traders every 15 min
  2. Telegram channels — processes messages that arrive via the existing bot webhook
     when the bot is added to a trader's channel/group (zero extra setup)

When a buy/sell signal is detected, calls the provided callback which feeds
into the same research + execution pipeline as on-chain webhook signals.

Usage (from fomo_tracker.py at startup):
    from fomo_social import start_social_poller, parse_channel_message
    start_social_poller(callback=process_social_signal)
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone, timedelta
from typing import Callable, Optional

import anthropic
import requests

log = logging.getLogger(__name__)

TWITTER_BEARER       = os.environ.get("TWITTER_BEARER_TOKEN", "")
ANTHROPIC_KEY        = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL             = "claude-sonnet-4-5"
TRUSTED_WALLETS_FILE = "trusted_wallets.json"
SOCIAL_STATE_FILE    = "fomo_social_state.json"

# Free tier: 15 requests / 15 min at app level. One poll per 15 min is safe.
TWITTER_POLL_INTERVAL_SEC = 900

# Telegram channel names → trader aliases
# Populated automatically from trusted_wallets.json + FOMO_TELEGRAM_CHANNELS env var.
# Format in env var: "channelname1:alias1,channelname2:alias2"
_TELEGRAM_CHANNEL_MAP: dict[str, str] = {}


# ─── SIGNAL PARSE PROMPT ──────────────────────────────────────────────────────

_PARSE_SYSTEM = (
    "You parse social media posts from crypto traders for buy or sell signals. "
    "Be conservative — only mark BUY or SELL if the intent is clear or strongly implied. "
    "General bullish commentary without a specific token is NOISE. "
    "Respond with valid JSON only, no markdown."
)


def _parse_post(text: str, alias: str, handle: str) -> Optional[dict]:
    """
    Ask Claude to classify a post as BUY / SELL / NOISE.
    Returns parsed dict or None on error.
    """
    if not ANTHROPIC_KEY:
        return None
    prompt = (
        f"Trader @{handle} (alias: {alias}) posted:\n"
        f"---\n{text[:500]}\n---\n\n"
        "Does this contain a crypto trade signal? Respond JSON only:\n"
        '{'
        '"action": "BUY" or "SELL" or "NOISE", '
        '"token_symbol": "ticker or null", '
        '"contract_address": "Solana/EVM contract if pasted, else null", '
        '"confidence": "high" or "medium" or "low", '
        '"signal_text": "the specific phrase that indicates the signal", '
        '"notes": "one short note about ambiguity if any"'
        '}'
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp   = client.messages.create(
            model=AI_MODEL,
            max_tokens=250,
            system=_PARSE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        log.debug(f"Social parse error ({alias}): {e}")
        return None


# ─── WALLET REGISTRY HELPERS ──────────────────────────────────────────────────

def _load_traders() -> list[dict]:
    """Return all tracked wallets with their Twitter/fomo handles."""
    try:
        with open(TRUSTED_WALLETS_FILE) as f:
            data = json.load(f)
        wallets = data.get("tier_a", []) + data.get("tier_b", [])
        result  = []
        for w in wallets:
            profile = (w.get("fomo_profile") or "").strip()
            if not profile.startswith("@"):
                continue
            result.append({
                "handle":      profile.lstrip("@"),
                "alias":       w.get("alias", profile),
                "tier":        w.get("tier", "B"),
                "chain":       w.get("chain", "solana"),
                "bankroll_usd": w.get("bankroll_usd"),
            })
        return result
    except Exception as e:
        log.warning(f"Social: could not load traders: {e}")
        return []


def _build_telegram_channel_map() -> dict[str, str]:
    """
    Build channel-name → alias mapping from:
      1. FOMO_TELEGRAM_CHANNELS env var: "channame:alias,channame2:alias2"
      2. trusted_wallets.json "telegram_channel" field if present
    """
    mapping: dict[str, str] = {}
    env = os.environ.get("FOMO_TELEGRAM_CHANNELS", "")
    for pair in env.split(","):
        pair = pair.strip()
        if ":" in pair:
            chan, alias = pair.split(":", 1)
            mapping[chan.strip().lower().lstrip("@")] = alias.strip()
    try:
        with open(TRUSTED_WALLETS_FILE) as f:
            data = json.load(f)
        for w in data.get("tier_a", []) + data.get("tier_b", []):
            chan = (w.get("telegram_channel") or "").strip().lower().lstrip("@")
            if chan:
                mapping[chan] = w.get("alias", chan)
    except Exception:
        pass
    return mapping


# ─── SOCIAL STATE PERSISTENCE ─────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        if os.path.exists(SOCIAL_STATE_FILE):
            with open(SOCIAL_STATE_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"last_tweet_id": None, "last_poll_ts": None, "seen_tweet_ids": []}


def _save_state(state: dict):
    try:
        # Keep seen_tweet_ids bounded
        state["seen_tweet_ids"] = state.get("seen_tweet_ids", [])[-500:]
        with open(SOCIAL_STATE_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.debug(f"Social state save error: {e}")


# ─── TWITTER POLLER ───────────────────────────────────────────────────────────

def poll_twitter(callback: Callable) -> int:
    """
    Search recent tweets FROM tracked traders using Twitter free-tier API.
    Calls callback(signal_dict) for each BUY/SELL signal found.
    Returns number of signals emitted.
    """
    if not TWITTER_BEARER:
        return 0

    traders = _load_traders()
    if not traders:
        return 0

    # Build FROM query — free tier supports `from:` operator in search
    from_parts = [f"from:{t['handle']}" for t in traders]
    if not from_parts:
        return 0
    query = "(" + " OR ".join(from_parts) + ") lang:en -is:retweet"

    state    = _load_state()
    since_id = state.get("last_tweet_id")
    seen     = set(state.get("seen_tweet_ids", []))

    params: dict = {
        "query":        query,
        "max_results":  20,
        "tweet.fields": "created_at,public_metrics,text,author_id",
        "expansions":   "author_id",
        "user.fields":  "username",
        "sort_order":   "recency",
    }
    if since_id:
        params["since_id"] = since_id

    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params=params,
            headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
            timeout=15,
        )
    except Exception as e:
        log.warning(f"Social: Twitter request error: {e}")
        return 0

    if r.status_code == 429:
        log.warning("Social: Twitter rate limited — will retry next cycle")
        return 0
    if r.status_code != 200:
        log.warning(f"Social: Twitter {r.status_code}: {r.text[:120]}")
        return 0

    body   = r.json()
    tweets = body.get("data", [])
    users  = {u["id"]: u["username"].lower()
              for u in body.get("includes", {}).get("users", [])}

    if not tweets:
        log.debug("Social: Twitter — no new posts from tracked traders")
        state["last_poll_ts"] = datetime.now(timezone.utc).isoformat()
        _save_state(state)
        return 0

    # Update since_id to the newest tweet we've seen
    newest_id = str(max(int(t["id"]) for t in tweets))
    state["last_tweet_id"] = newest_id

    handle_map = {t["handle"].lower(): t for t in traders}
    signals_emitted = 0

    for tweet in tweets:
        tid = tweet["id"]
        if tid in seen:
            continue
        seen.add(tid)

        author_handle = users.get(tweet.get("author_id", ""), "")
        trader        = handle_map.get(author_handle)
        if not trader:
            continue

        text = tweet.get("text", "")
        log.info(f"Social Twitter: @{author_handle} → {text[:80]}...")

        parsed = _parse_post(text, trader["alias"], author_handle)
        if not parsed or parsed.get("action") not in ("BUY", "SELL"):
            continue

        signal = {
            "alias":            trader["alias"],
            "handle":           author_handle,
            "tier":             trader["tier"],
            "chain":            trader["chain"],
            "bankroll_usd":     trader["bankroll_usd"],
            "action":           parsed["action"],
            "token_symbol":     parsed.get("token_symbol"),
            "contract_address": parsed.get("contract_address"),
            "confidence":       parsed.get("confidence", "low"),
            "signal_text":      parsed.get("signal_text", text),
            "source":           "twitter",
            "tweet_id":         tid,
            "timestamp":        tweet.get("created_at",
                                         datetime.now(timezone.utc).isoformat()),
            "original_text":    text,
        }
        log.info(
            f"Social Twitter SIGNAL: {trader['alias']} "
            f"{parsed['action']} ${parsed.get('token_symbol', '?')} "
            f"[{parsed.get('confidence', '?')} confidence]"
        )
        try:
            callback(signal)
            signals_emitted += 1
        except Exception as e:
            log.error(f"Social: callback error for Twitter signal: {e}")

    state["seen_tweet_ids"] = list(seen)
    state["last_poll_ts"]   = datetime.now(timezone.utc).isoformat()
    _save_state(state)
    return signals_emitted


# ─── TELEGRAM CHANNEL HANDLER ─────────────────────────────────────────────────

def parse_channel_message(
    text:         str,
    chat_title:   str,
    chat_username: Optional[str],
) -> Optional[dict]:
    """
    Called from fomo_tracker.py when a Telegram message arrives from a
    group or channel. Returns a normalized signal dict or None.

    To monitor a trader's Telegram channel:
      1. Add your bot to the channel as a member (or admin for supergroups)
      2. Set FOMO_TELEGRAM_CHANNELS="channelname:traderalias" in Railway env vars
         OR add "telegram_channel": "channelname" to their entry in trusted_wallets.json

    The bot will then receive all messages from that channel and process them here.
    """
    global _TELEGRAM_CHANNEL_MAP
    if not _TELEGRAM_CHANNEL_MAP:
        _TELEGRAM_CHANNEL_MAP = _build_telegram_channel_map()

    # Try to match by @username or chat title
    key = (chat_username or "").lower().lstrip("@")
    alias = _TELEGRAM_CHANNEL_MAP.get(key)

    if not alias:
        # Try matching by title (case-insensitive partial)
        title_lower = (chat_title or "").lower()
        for chan, al in _TELEGRAM_CHANNEL_MAP.items():
            if chan in title_lower or title_lower in chan:
                alias = al
                break

    if not alias:
        return None  # message from unknown channel — ignore

    parsed = _parse_post(text, alias, f"tg:{chat_username or chat_title}")
    if not parsed or parsed.get("action") not in ("BUY", "SELL"):
        return None

    # Look up trader metadata
    trader_meta = {}
    for t in _load_traders():
        if t["alias"] == alias:
            trader_meta = t
            break

    return {
        "alias":            alias,
        "handle":           chat_username or chat_title,
        "tier":             trader_meta.get("tier", "B"),
        "chain":            trader_meta.get("chain", "solana"),
        "bankroll_usd":     trader_meta.get("bankroll_usd"),
        "action":           parsed["action"],
        "token_symbol":     parsed.get("token_symbol"),
        "contract_address": parsed.get("contract_address"),
        "confidence":       parsed.get("confidence", "low"),
        "signal_text":      parsed.get("signal_text", text),
        "source":           "telegram",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "original_text":    text,
    }


# ─── BACKGROUND THREAD ────────────────────────────────────────────────────────

def start_social_poller(callback: Callable) -> threading.Thread:
    """
    Start a background daemon thread that polls Twitter every 15 minutes.

    Telegram channel messages are handled synchronously via parse_channel_message()
    called from the /webhook/telegram Flask route — no separate thread needed.

    callback(signal_dict) is called for every confirmed BUY/SELL signal found.
    """
    def _loop():
        log.info("Social poller started (Twitter every 15 min)")
        # Stagger the first poll slightly so Flask has time to fully start
        time.sleep(30)
        while True:
            try:
                n = poll_twitter(callback)
                if n:
                    log.info(f"Social poller: emitted {n} Twitter signal(s)")
            except Exception as e:
                log.error(f"Social poller error: {e}")
            time.sleep(TWITTER_POLL_INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-social-poller")
    t.start()
    return t
