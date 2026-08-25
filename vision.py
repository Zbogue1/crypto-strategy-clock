#!/usr/bin/env python3
"""
vision.py — Read screenshots sent from your phone, per bot.

WHY THIS EXISTS INSTEAD OF THE X API
X removed free read access in February 2026. Reading posts now costs $0.005
each under pay-per-use, and the old free tier is closed to new signups — so
fomo_social.poll_twitter() may already be returning nothing, silently, unless
its bearer token is grandfathered.

The cheaper bridge is the one already in your pocket. You read these posts on
your phone anyway; forwarding a screenshot costs nothing, needs no new
infrastructure, and carries MORE than the API would — a screenshot includes
replies, quote-tweets, engagement counts and charts that the text endpoint
throws away.

WHY THE BOT YOU SEND IT TO MATTERS
Each bot reads screenshots through its own lens and matches against its own
book. A chart of a halted small-cap means something to Stock Golem and nothing
to Kalshi. A trader shouting about a memecoin is FOMO's business. Sending
everything to one bot would either produce nonsense matches or silently drop
intel that belonged somewhere else — so the destination bot IS the routing
decision, made by you at send time.

WHAT IT NEVER DOES
Buy or sell. A screenshot is unverifiable by construction — anyone can fake
one — so extraction ends in an alert, never an order.
"""

import base64
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

try:
    import anthropic
except Exception:                                     # pragma: no cover
    anthropic = None

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
VISION_MODEL  = os.getenv("VISION_MODEL", "claude-sonnet-4-5")

_SOL_RE = re.compile(r"\b[1-9A-HJ-NP-Za-km-z]{32,44}\b")
_EVM_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")


# ─── DOMAIN PROFILES ──────────────────────────────────────────────────────────

_BASE_RULES = (
    "Report ONLY what is visibly in the image. Never infer a ticker, price, "
    "contract address or claim that is not shown. If text is cut off or "
    "unreadable, say so rather than completing it — a hallucinated identifier "
    "would route real money to the wrong instrument.\n\n"
    "Sarcasm is common in trading posts. 'great, another rug' is bearish.\n\n"
    "Respond with JSON only."
)

PROFILES = {
    "fomo": {
        "label": "FOMO Golem",
        "what": "crypto traders' social posts about memecoins",
        "system": (
            "You read screenshots of crypto traders' social posts (X/Twitter, "
            "Telegram, Discord) for a memecoin copy-trading system.\n\n"
            "Distinguish carefully between the poster's OWN position and "
            "conviction ('still holding', 'I'm out'), general market "
            "commentary, and promotion of a token they may not hold. "
            "Conviction and capitulation matter as much as explicit calls.\n\n"
            + _BASE_RULES),
        "schema": """{
  "readable": true/false, "unreadable_reason": "if not readable, why",
  "poster_handle": "@handle or null", "poster_name": "display name or null",
  "platform": "twitter"/"telegram"/"discord"/"unknown",
  "posted_when": "time shown or null",
  "items": [{
    "symbol": "ticker without $",
    "identifier": "contract address ONLY if literally visible, else null",
    "stance": "BULLISH"/"BEARISH"/"HOLDING"/"EXITING"/"NEUTRAL",
    "conviction": "high"/"medium"/"low",
    "key_quote": "most telling phrase, verbatim",
    "is_new_call": true/false
  }],
  "market_commentary": "broader view expressed, or null",
  "urgency": "high"/"medium"/"low",
  "summary": "one sentence on what this means for someone copying this trader",
  "caution": "cropped text, unclear date, possible parody account, or null"
}""",
    },
    "stock": {
        "label": "Stock Golem",
        "what": "small-cap stock news, charts, halts and filings",
        "system": (
            "You read screenshots relevant to a momentum day-trading system "
            "that trades small-cap stocks on news catalysts (Ross Cameron "
            "methodology: relative volume, gap, catalyst, low float).\n\n"
            "These may be news headlines, Level 2 windows, charts, halt "
            "notices, SEC filings or scanner output. Extract the CATALYST and "
            "whether it is dilutive — offerings, warrants, ATMs and shelf "
            "registrations are harmful catalysts that disqualify a long.\n\n"
            "If a chart is shown, describe the pattern factually (gap, "
            "pullback depth, whether it holds VWAP) without predicting.\n\n"
            + _BASE_RULES),
        "schema": """{
  "readable": true/false, "unreadable_reason": "if not readable, why",
  "source": "news outlet, platform or app shown, else null",
  "posted_when": "time shown or null",
  "items": [{
    "symbol": "stock ticker",
    "identifier": null,
    "catalyst": "what the news actually is",
    "catalyst_type": "earnings"/"fda"/"contract"/"offering"/"merger"/"halt"/"technical"/"other",
    "is_dilutive": true/false/null,
    "stance": "BULLISH"/"BEARISH"/"NEUTRAL",
    "conviction": "high"/"medium"/"low",
    "key_quote": "the headline or key line, verbatim",
    "price_info": "any price/volume/float figures visible, else null"
  }],
  "market_commentary": "broader market view, or null",
  "urgency": "high"/"medium"/"low",
  "summary": "one sentence on the trading relevance",
  "caution": "stale date, unclear source, cropped text, or null"
}""",
    },
    "kalshi": {
        "label": "Kalshi Golem",
        "what": "prediction-market odds, news bearing on event outcomes",
        "system": (
            "You read screenshots for a prediction-market betting system that "
            "trades Kalshi event contracts (sports, weather, economics, "
            "company events) and crypto perpetuals.\n\n"
            "These may be Kalshi market pages, odds from sportsbooks, injury "
            "reports, weather forecasts, economic releases or news bearing on "
            "a pending event. Extract the specific CLAIM and any probability "
            "or odds shown.\n\n"
            "Convert American odds to implied probability when shown, and say "
            "you did. Distinguish a resolved outcome from a forecast — that "
            "difference decides whether a position settles or is still live.\n\n"
            + _BASE_RULES),
        "schema": """{
  "readable": true/false, "unreadable_reason": "if not readable, why",
  "source": "platform or outlet shown, else null",
  "posted_when": "time shown or null",
  "items": [{
    "symbol": "the subject - team, ticker, indicator or event name",
    "identifier": "Kalshi ticker if visible, else null",
    "claim": "the specific factual claim or forecast",
    "implied_prob": "percentage 0-100 if odds or probability shown, else null",
    "is_resolved": true if this reports a FINAL outcome, false if a forecast,
    "stance": "YES"/"NO"/"NEUTRAL",
    "conviction": "high"/"medium"/"low",
    "key_quote": "the key line, verbatim"
  }],
  "market_commentary": "broader context, or null",
  "urgency": "high"/"medium"/"low",
  "summary": "one sentence on the betting relevance",
  "caution": "stale date, unclear source, cropped text, or null"
}""",
    },
}


# ─── TELEGRAM PHOTO DOWNLOAD ──────────────────────────────────────────────────

def download_photo(message: dict, bot_token: str) -> Optional[tuple]:
    """
    Pull the largest version of a photo out of a Telegram message.

    Telegram sends several resolutions and the last is biggest. Resolution
    matters here — a downscaled screenshot of dense text is the difference
    between reading a ticker and guessing at it.

    bot_token is passed in rather than read from env: each bot has its own
    token, and a file_id issued to one bot cannot be fetched by another.
    """
    if not bot_token:
        log.error("vision: no bot token — cannot download photo")
        return None

    photos = message.get("photo") or []
    file_id, mime = None, "image/jpeg"

    if photos:
        file_id = photos[-1].get("file_id")
    else:
        # "Send as file" preserves quality — people often do that for text-heavy
        # screenshots, and those arrive as documents, not photos.
        doc = message.get("document") or {}
        if (doc.get("mime_type") or "").startswith("image/"):
            file_id, mime = doc.get("file_id"), doc["mime_type"]
    if not file_id:
        return None

    try:
        r = requests.get(f"https://api.telegram.org/bot{bot_token}/getFile",
                         params={"file_id": file_id}, timeout=15)
        if r.status_code != 200:
            log.error(f"vision: getFile HTTP {r.status_code}: {r.text[:120]}")
            return None
        path = (r.json().get("result") or {}).get("file_path")
        if not path:
            log.error("vision: getFile returned no file_path")
            return None

        f = requests.get(
            f"https://api.telegram.org/file/bot{bot_token}/{path}", timeout=30)
        if f.status_code != 200:
            log.error(f"vision: file download HTTP {f.status_code}")
            return None
        if path.lower().endswith(".png"):
            mime = "image/png"
        return f.content, mime
    except Exception as e:
        log.error(f"vision: photo download failed: {e}")
        return None


# ─── EXTRACTION ───────────────────────────────────────────────────────────────

def extract(image_bytes: bytes, bot: str, mime: str = "image/jpeg",
            caption: str = "") -> dict:
    """Read a screenshot through one bot's lens."""
    profile = PROFILES.get(bot)
    if not profile:
        return {"readable": False, "items": [],
                "unreadable_reason": f"unknown bot profile {bot!r}"}
    if not ANTHROPIC_KEY or anthropic is None:
        return {"readable": False, "items": [],
                "unreadable_reason": "no ANTHROPIC_API_KEY configured"}

    note = f"\n\nThe user added this note: {caption}" if caption else ""
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=VISION_MODEL, max_tokens=1500, system=profile["system"],
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": mime,
                    "data": base64.b64encode(image_bytes).decode()}},
                {"type": "text", "text": (
                    f"Extract the trading intel from this screenshot.{note}\n\n"
                    f"Respond with JSON matching exactly this shape:\n"
                    f"{profile['schema']}")},
            ]}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        out = json.loads(raw.strip())
    except Exception as e:
        log.error(f"vision: extraction failed ({bot}): {e}")
        return {"readable": False, "items": [], "unreadable_reason": str(e)}

    # Never trust an identifier the model produced unless it also appears in
    # the caption the user typed. Vision models complete plausible-looking
    # base58 strings, and a wrong contract address sends money to the wrong
    # token — an error no downstream check would catch.
    typed = set(_SOL_RE.findall(caption or "")) | set(_EVM_RE.findall(caption or ""))
    for it in out.get("items") or []:
        ident = it.get("identifier")
        if ident and len(ident) > 25 and ident not in typed:
            it["identifier_unverified"] = ident
            it["identifier"] = None
    out["_bot"] = bot
    out["_received_at"] = datetime.now(timezone.utc).isoformat()
    return out


# ─── PORTFOLIO MATCHING ───────────────────────────────────────────────────────

def _open_symbols(bot: str) -> dict:
    """symbol -> position, for whichever book this bot owns."""
    out = {}
    try:
        if bot == "fomo":
            from fomo_portfolio import load_fomo_portfolio
            for h in load_fomo_portfolio().get("holdings", []):
                s = (h.get("token_ticker") or "").lstrip("$").upper()
                if s:
                    out[s] = h
        elif bot == "stock":
            from stock_portfolio import get_summary
            for p in get_summary().get("positions", []):
                s = (p.get("symbol") or "").upper()
                if s:
                    out[s] = p
        elif bot == "kalshi":
            # Both Kalshi books — perps and event bets.
            from kalshi_portfolio import get_portfolio_summary
            for p in get_portfolio_summary().get("positions", []):
                t = (p.get("ticker") or "").upper()
                if t:
                    out[t] = p
            try:
                from kalshi_event_portfolio import get_summary as ev
                for p in ev().get("positions", []):
                    t = (p.get("ticker") or "").upper()
                    if t:
                        out[t] = p
            except Exception:
                pass
    except Exception as e:
        log.error(f"vision: could not load {bot} book: {e}")
    return out


def classify(extracted: dict, bot: str) -> dict:
    """
    Split findings into things this bot holds and things it doesn't.

    Held positions are the priority — that's the case where someone's
    conviction changes whether we keep sitting on a drawdown. Unheld names are
    secondary intel and go through normal research, never straight to a buy.
    """
    held, new = [], []
    book = _open_symbols(bot)

    for it in extracted.get("items") or []:
        sym = (it.get("symbol") or "").lstrip("$").upper()
        ident = (it.get("identifier") or "").upper()
        match = book.get(sym) or book.get(ident)
        # Kalshi tickers are long (KXNBAGAME-25AUG24-LAL) and a screenshot
        # rarely shows the full string, so also match on prefix.
        if not match and bot == "kalshi" and sym:
            for k, v in book.items():
                if sym in k or k.startswith(sym):
                    match = v
                    break
        (held if match else new).append(
            {**it, "position": match} if match else it)
    return {"held": held, "new": new}


def format_result(extracted: dict, routed: dict, bot: str) -> str:
    """Plain-text summary. Plain because tickers and addresses break Markdown."""
    profile = PROFILES.get(bot, {})
    if not extracted.get("readable", True):
        return ("Couldn't read that screenshot.\n\n"
                f"{extracted.get('unreadable_reason','unknown reason')}\n\n"
                "Try a full-resolution shot, or send it as a file rather than "
                "a photo so Telegram doesn't compress it.")

    who = (extracted.get("poster_handle") or extracted.get("poster_name")
           or extracted.get("source") or "unknown source")
    L = [f"SCREENSHOT READ — {profile.get('label', bot)}",
         f"{who} · {extracted.get('posted_when') or 'time not shown'}", ""]

    if extracted.get("summary"):
        L += [extracted["summary"], ""]

    if routed["held"]:
        L.append("ABOUT POSITIONS YOU HOLD")
        for it in routed["held"]:
            L.append(f"\n  {it.get('symbol','?')} — {it.get('stance','?')} "
                     f"({it.get('conviction','?')} conviction)")
            for k in ("catalyst", "claim"):
                if it.get(k):
                    L.append(f"    {k}: {it[k][:120]}")
            if it.get("is_dilutive"):
                L.append("    WARNING: dilutive catalyst — disqualifies a long")
            if it.get("key_quote"):
                L.append(f"    \"{it['key_quote'][:150]}\"")
        L.append("")

    if routed["new"]:
        L.append("NOT IN YOUR BOOK")
        for it in routed["new"]:
            tag = " [NEW CALL]" if it.get("is_new_call") else ""
            L.append(f"\n  {it.get('symbol','?')} — {it.get('stance','?')} "
                     f"({it.get('conviction','?')}){tag}")
            for k in ("catalyst", "claim"):
                if it.get(k):
                    L.append(f"    {k}: {it[k][:120]}")
            if it.get("implied_prob") is not None:
                L.append(f"    implied probability: {it['implied_prob']}%")
            if it.get("is_dilutive"):
                L.append("    WARNING: dilutive catalyst")
            if it.get("key_quote"):
                L.append(f"    \"{it['key_quote'][:150]}\"")
            if it.get("identifier_unverified"):
                L.append(f"    address in image: {it['identifier_unverified'][:24]}...")
                L.append("    UNVERIFIED — confirm before any trade")
        L.append("")

    if extracted.get("market_commentary"):
        L += [f"Broader: {extracted['market_commentary'][:220]}", ""]
    if extracted.get("caution"):
        L += [f"CAUTION: {extracted['caution'][:200]}", ""]

    L.append("Nothing was bought or sold. Screenshots can't be verified, so "
             "this is intel only.")
    return "\n".join(L)


def process_screenshot(message: dict, bot: str, bot_token: str) -> Optional[dict]:
    """
    Full pipeline. Returns {"extracted", "routed", "text"} or None if the
    message contained no image.
    """
    got = download_photo(message, bot_token)
    if not got:
        return None
    image_bytes, mime = got
    caption = message.get("caption", "") or ""

    log.info(f"vision[{bot}]: reading {len(image_bytes)}B {mime}, "
             f"caption={caption[:60]!r}")

    extracted = extract(image_bytes, bot, mime, caption)
    routed    = classify(extracted, bot)
    return {"extracted": extracted, "routed": routed,
            "text": format_result(extracted, routed, bot)}


def has_image(message: dict) -> bool:
    if message.get("photo"):
        return True
    doc = message.get("document") or {}
    return (doc.get("mime_type") or "").startswith("image/")
