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


# ─── ON-DEMAND CONTEXT LOOKUP ─────────────────────────────────────────────────
#
# poll_twitter() is a PUSH firehose: it watches for new BUY/SELL signals and
# discards everything else as NOISE. That throws away exactly the material that
# matters when a position has gone quiet and underwater — "still holding",
# "hold out boys", "this one's dead, I'm out". Conviction and capitulation are
# not trade signals, so the parser drops them.
#
# This is the PULL side: at decision time, ask what a specific trader has said
# about a specific token recently.

CONTEXT_LOOKBACK_DAYS = int(os.getenv("FOMO_SOCIAL_LOOKBACK_DAYS", "7"))


def fetch_trader_context(handle: str, token_symbol: str,
                         days: int = None) -> dict:
    """
    What has this trader said about this token lately?

    Returns:
      {"available": bool, "posts": [...], "reason": str}

    `available` False means we could not look — no API token, rate limited, or
    the handle is unknown. That is NOT the same as "the trader said nothing",
    and the caller must not read silence as a bearish signal. Conflating
    "no data" with "no confidence" is how a missing integration turns into a
    fabricated reason to sell.
    """
    days = days or CONTEXT_LOOKBACK_DAYS

    if not TWITTER_BEARER:
        return {"available": False, "posts": [],
                "reason": "TWITTER_BEARER_TOKEN not configured"}
    if not handle:
        return {"available": False, "posts": [],
                "reason": "no Twitter handle known for this trader"}

    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")
    # Free-tier search only reaches back 7 days; asking for more returns an error
    # rather than a truncated result, so clamp instead of failing.
    query = f"from:{handle.lstrip('@')} -is:retweet"

    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={"query": query, "max_results": 50,
                    "tweet.fields": "created_at,public_metrics,text",
                    "start_time": start},
            headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
            timeout=15,
        )
    except Exception as e:
        return {"available": False, "posts": [],
                "reason": f"Twitter request failed: {e}"}

    if r.status_code == 429:
        return {"available": False, "posts": [],
                "reason": "Twitter rate limit (free tier: 15 req/15min)"}
    if r.status_code != 200:
        return {"available": False, "posts": [],
                "reason": f"Twitter HTTP {r.status_code}: {r.text[:100]}"}

    tweets = (r.json() or {}).get("data") or []
    if not tweets:
        return {"available": True, "posts": [],
                "reason": f"no posts from @{handle} in {days}d"}

    # Keep posts that plausibly reference this token. Deliberately loose —
    # traders write "$WIF", "wif", "the dog one". Precision matters less than
    # not silently dropping the one post that says "still holding".
    sym  = (token_symbol or "").lstrip("$").lower()
    hits = []
    for t in tweets:
        txt = t.get("text", "")
        if sym and sym in txt.lower():
            hits.append({"text": txt[:400],
                         "created_at": t.get("created_at", ""),
                         "metrics": t.get("public_metrics", {})})

    return {
        "available": True,
        "posts": hits,
        "reason": (f"{len(hits)} post(s) mentioning {token_symbol} "
                   f"out of {len(tweets)} in {days}d"),
    }


def summarize_trader_sentiment(posts: list, token_symbol: str) -> dict:
    """
    Turn raw posts into a stance: HOLDING / EXITING / UNCLEAR.

    Separate from _parse_post() on purpose. That one answers "is this a trade
    signal?" and returns NOISE for "hold out boys" — which is precisely the
    sentiment we need here.
    """
    if not posts:
        return {"stance": "NO_DATA", "confidence": "none",
                "summary": "no posts found mentioning this token"}
    if not ANTHROPIC_KEY:
        return {"stance": "UNCLEAR", "confidence": "none",
                "summary": "no ANTHROPIC_API_KEY to interpret posts"}

    joined = "\n---\n".join(
        f"[{p.get('created_at','')[:10]}] {p['text']}" for p in posts[:12])
    prompt = (
        f"These are recent posts from a crypto trader we copy-trade. We hold a "
        f"position in {token_symbol} that is currently underwater, and we are "
        f"deciding whether to keep holding or cut it.\n\n"
        f"POSTS:\n{joined}\n\n"
        "What is this trader's current stance on this token? Respond JSON only:\n"
        '{"stance": "HOLDING" or "EXITING" or "UNCLEAR", '
        '"confidence": "high" or "medium" or "low", '
        '"summary": "one sentence on what they are signalling", '
        '"key_quote": "the most telling phrase, verbatim, or null"}\n\n'
        "Be conservative. Hype about a DIFFERENT token is not a stance on this "
        "one. If the posts do not clearly address this position, say UNCLEAR."
    )
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=AI_MODEL, max_tokens=300,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        log.warning(f"Social: sentiment summary failed: {e}")
        return {"stance": "UNCLEAR", "confidence": "none",
                "summary": f"could not interpret posts: {e}"}


def get_trader_handle(alias: str) -> str:
    """Twitter handle for a wallet alias, from trusted_wallets.json."""
    for t in _load_traders():
        if (t.get("alias") or "").lower() == (alias or "").lower():
            return t.get("handle", "")
    return ""


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
