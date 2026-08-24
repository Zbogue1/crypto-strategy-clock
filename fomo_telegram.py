#!/usr/bin/env python3
"""
fomo_telegram.py — Single Telegram sender for all FOMO modules.

WHY THIS EXISTS:
Five separate files each had their own copy of the send logic
(fomo_tracker, fomo_exit, fomo_portfolio, fomo_drift, fomo_wallet_stats), and
every one of them threw away the HTTP response:

    requests.post(url, json={...}, timeout=10)      # no status check

So a message Telegram rejected simply vanished. No log, no error, nothing
distinguishable from success. That matters because HTML parse mode breaks on
`<`, `>` and `&` — characters that appear routinely in memecoin names and in
AI-generated reasoning text. Exit alerts about real positions were being
dropped silently.

This module checks the response, logs failures with the actual status code, and
falls back to plain text when HTML parsing is rejected. Losing the formatting
beats losing the alert.
"""

import logging
import os
import re
from typing import Optional

import requests

log = logging.getLogger(__name__)

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

_API = "https://api.telegram.org/bot{token}/{method}"


def _url(method: str) -> str:
    return _API.format(token=TOKEN, method=method)


def _strip_html(text: str) -> str:
    """Remove tags but keep the content, and unescape the basic entities."""
    out = re.sub(r"<[^>]+>", "", text)
    return (out.replace("&lt;", "<").replace("&gt;", ">")
               .replace("&amp;", "&").replace("&quot;", '"'))


def send(text: str, parse_mode: Optional[str] = "HTML") -> bool:
    """
    Send a message. Returns True on delivery.

    On an HTML parse rejection, retries once as plain text rather than losing
    the message entirely.
    """
    if not TOKEN or not CHAT_ID:
        log.info(f"[TELEGRAM] {text[:200]}")
        return False

    payload = {"chat_id": CHAT_ID, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode
    else:
        payload["text"] = _strip_html(text)

    try:
        r = requests.post(_url("sendMessage"), json=payload, timeout=10)
        if r.status_code == 200:
            return True

        body = (r.text or "").lower()

        if "chat not found" in body:
            log.error(f"FOMO Telegram: chat {CHAT_ID} unreachable — has the "
                      f"bot been messaged from this account?")
            return False

        if r.status_code == 400 and (
            "can't parse entities" in body
            or "unsupported start tag" in body
            or "unmatched end tag" in body
            or "can't find end of the entity" in body
        ):
            log.warning(f"FOMO Telegram: HTML rejected, retrying plain "
                        f"({r.text[:120]})")
            try:
                r2 = requests.post(
                    _url("sendMessage"),
                    json={"chat_id": CHAT_ID, "text": _strip_html(text)},
                    timeout=10)
                if r2.status_code == 200:
                    return True
                log.error(f"FOMO Telegram: plain retry failed "
                          f"HTTP {r2.status_code}: {r2.text[:160]}")
            except Exception as e:
                log.error(f"FOMO Telegram: plain retry error: {e}")
            return False

        log.warning(f"FOMO Telegram HTTP {r.status_code}: {r.text[:200]}")
        return False

    except Exception as e:
        log.warning(f"FOMO Telegram send failed: {e}")
        return False


def send_button(text: str, button_text: str, callback_data: str) -> Optional[int]:
    """Message with a single inline button. Returns message_id on success."""
    if not TOKEN or not CHAT_ID:
        log.info(f"[TELEGRAM-BUTTON] {text[:160]} | [{button_text}]")
        return None
    try:
        r = requests.post(_url("sendMessage"), json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": [[
                {"text": button_text, "callback_data": callback_data}]]},
        }, timeout=10)
        if r.status_code == 200:
            return (r.json().get("result") or {}).get("message_id")

        body = (r.text or "").lower()
        if r.status_code == 400 and "parse" in body:
            log.warning("FOMO Telegram button: HTML rejected, retrying plain")
            r2 = requests.post(_url("sendMessage"), json={
                "chat_id": CHAT_ID,
                "text": _strip_html(text),
                "reply_markup": {"inline_keyboard": [[
                    {"text": button_text, "callback_data": callback_data}]]},
            }, timeout=10)
            if r2.status_code == 200:
                return (r2.json().get("result") or {}).get("message_id")

        log.warning(f"FOMO Telegram button HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"FOMO Telegram button failed: {e}")
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    # Characters that break HTML mode and previously caused silent drops
    print(_strip_html("<b>$A&B</b> up 40% — 5 < 10 > 3"))
