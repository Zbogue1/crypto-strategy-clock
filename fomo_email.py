#!/usr/bin/env python3
"""
fomo_email.py -- Gmail IMAP poller for Solscan transaction alerts.

Polls Gmail every 5 minutes for unread emails from noreply@mailer.solscan.io.
Parses the wallet name, contract address, and direction (buy/sell) from each
alert, filters out stablecoins/SOL, and feeds real memecoin signals into the
same research pipeline used by on-chain webhooks and social signals.

Setup (Railway env vars required):
    GMAIL_ADDRESS       -- your Gmail address
    GMAIL_APP_PASSWORD  -- 16-char App Password (no spaces)

Solscan email format parsed:
    Subject: [Alert] Curb has a transaction:
    Body:
        Timestamp: Wed, 05 Aug 2026 18:02:52 GMT
        Balance changes:
        + 0.034997 EPJFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
        Instructions (3): transferChecked
        View on Solscan

Signal logic:
    + amount  non-stable contract  = BUY  (wallet received a memecoin)
    - amount  non-stable contract  = SELL (wallet sent a memecoin away)
    +/- USDC/USDT/SOL              = STABLECOIN_FLOW (logged, no trade signal)
    USDC inflow after recent trade = potential SELL confirmation (logged)
"""

import email
import imaplib
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from email.header import decode_header
from typing import Callable, Optional

log = logging.getLogger(__name__)

GMAIL_ADDRESS      = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
GMAIL_ADDRESS_2      = os.environ.get("GMAIL_ADDRESS_2", "").strip()
GMAIL_APP_PASSWORD_2 = os.environ.get("GMAIL_APP_PASSWORD_2", "").strip()
TRUSTED_WALLETS_FILE = "trusted_wallets.json"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL_SEC = 300   # 5 minutes

SOLSCAN_SENDER = "noreply@mailer.solscan.io"

# Known stablecoins and native tokens on Solana -- skip these as trade signals
SKIP_CONTRACTS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",    # USDT
    "So11111111111111111111111111111111111111112",        # Wrapped SOL
    "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",    # mSOL
    "7dHbWXmci3dT8UFYWYZweBLXgycu7Y3iL6trKn1Y7ARj",   # stSOL
    "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1",    # bSOL
    "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",   # BONK (not stable but commonly noise)
}
SKIP_SYMBOLS = {"SOL", "USDC", "USDT", "WSOL", "MSOL", "STSOL", "BSOL"}


# ─── WALLET NAME → ALIAS MAPPING ─────────────────────────────────────────────

def _build_name_map() -> dict[str, dict]:
    """
    Build a case-insensitive map of Solscan display name -> wallet entry.
    Solscan uses whatever name you gave when adding the watchlist address.

    We register multiple key variants per alias so minor mismatches between
    what the user typed on Solscan and our alias field don't silently break
    signal detection. Variants added per alias:
        - exact lowercase            e.g. "poorgoat_"
        - trailing underscores stripped  e.g. "poorgoat"
        - all underscores stripped   e.g. "poorgoat"
        - all hyphens stripped       e.g. "dopaminefeen"
        - fomo_profile handle (without @) e.g. "poorgoat_"
    """
    mapping: dict[str, dict] = {}
    try:
        with open(TRUSTED_WALLETS_FILE) as f:
            data = json.load(f)
        for tier in ("tier_a", "tier_b"):
            for w in data.get(tier, []):
                alias = w.get("alias", "")
                if not alias:
                    continue
                entry = {
                    "alias":        alias,
                    "tier":         w.get("tier", "B"),
                    "chain":        w.get("chain", "solana"),
                    "bankroll_usd": w.get("bankroll_usd"),
                    "wallet":       w.get("wallet", ""),
                    "copy_trade":   w.get("copy_trade", True),
                    "copy_trade_reason": w.get("copy_trade_reason", ""),
                }
                # Build a set of lowercase key variants to register
                keys = set()
                base = alias.lower()
                keys.add(base)                            # exact
                keys.add(base.strip("_"))                 # trailing/leading underscores
                keys.add(base.replace("_", ""))           # all underscores removed
                keys.add(base.replace("-", ""))           # all hyphens removed
                keys.add(base.replace("_", "").replace("-", ""))  # both removed
                # Handle common 0/O confusion in crypto handles
                keys.add(base.replace("0", "o"))
                keys.add(base.replace("o", "0"))

                # Also register the fomo_profile handle (strip leading @)
                profile = w.get("fomo_profile", "")
                if profile:
                    handle = profile.lstrip("@").lower()
                    keys.add(handle)
                    keys.add(handle.strip("_"))
                    keys.add(handle.replace("_", ""))
                    keys.add(handle.replace("0", "o"))
                    keys.add(handle.replace("o", "0"))

                for key in keys:
                    if key and key not in mapping:
                        mapping[key] = entry

    except Exception as e:
        log.warning(f"Email: could not load wallet map: {e}")
    return mapping


# ─── EMAIL PARSING ────────────────────────────────────────────────────────────

def _decode_subject(msg) -> str:
    """Decode email subject handling UTF-8 and encoded headers."""
    raw = msg.get("Subject", "")
    parts = decode_header(raw)
    decoded = []
    for part, enc in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return "".join(decoded)


def _extract_body(msg) -> str:
    """Extract body from email — prefers plain text, falls back to HTML."""
    text_body = ""
    html_body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and not text_body:
                try:
                    text_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
            elif ct == "text/html" and not html_body:
                try:
                    html_body = part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
    else:
        try:
            raw = msg.get_payload(decode=True).decode("utf-8", errors="replace")
            if "<html" in raw.lower():
                html_body = raw
            else:
                text_body = raw
        except Exception:
            pass
    return text_body if text_body.strip() else html_body


def _parse_solscan_email(subject: str, body: str, name_map: dict) -> Optional[dict]:
    """
    Parse a Solscan alert email into a normalized signal dict.
    Returns None if:
      - Wallet not in our tracking list
      - Only stablecoin/SOL movements (no memecoin signal)
      - Can't extract a contract address
    """
    # ── Extract wallet name from subject ─────────────────────────────────────
    # Format: "[Alert] 🚨 Curb has a transaction:" or "[Alert] Curb has a transaction:"
    subject_clean = subject.strip()

    # Remove emoji and alert prefix to get the name
    # Pattern: anything before "has a transaction"
    name_match = re.search(r'([A-Za-z0-9_\-]+)\s+has a transaction', subject_clean, re.IGNORECASE)
    if not name_match:
        log.warning(f"Email: no wallet name found in subject: {subject_clean[:80]}")
        return None

    raw_name = name_match.group(1).strip()
    wallet_info = name_map.get(raw_name.lower())
    if not wallet_info:
        log.warning(f"Email: wallet '{raw_name}' not in tracked list — known aliases: {list(name_map.keys())[:10]}")
        return None

    # ── Extract balance change lines ──────────────────────────────────────────
    # Solscan emails can be plain text or HTML. Two formats:
    #
    # Plain text (old):
    #   + 0.034997 EPJFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v
    #
    # HTML (current):
    #   🟢 + 0.002249429 <a href="https://solscan.io/token/PreweJYE...">
    #   🔴 - 1.5 <a href="https://solscan.io/token/So11111...">

    changes = []

    # Try HTML format first: extract contract from solscan.io/token/ href
    #
    # NOTE: the sign is OPTIONAL. Solscan renders outgoing amounts with the red
    # emoji and NO minus sign:
    #     🔴  24533153.6309 <a href="https://solscan.io/token/HLv8...">
    # The previous pattern required [+\-], so it matched zero real token
    # transfers — every wallet looked inactive and the logs filled with
    # "no balance changes found". The emoji already encodes direction, so we
    # take the sign only when present and fall back to the emoji.
    #
    # Also: don't require a closing quote after the address (href formats vary)
    # and cap the address at 44 chars — Solana base58 addresses are 32-44, and
    # {30,50} could swallow trailing markup.
    html_pattern = re.compile(
        r'(🟢|🔴)\s*([+\-]?)\s*([\d,]+\.?\d*)\s*<a[^>]*?solscan\.io/token/([A-Za-z0-9]{30,44})',
        re.MULTILINE
    )
    for emoji, sign, amount, contract in html_pattern.findall(body):
        # 🟢 = received (BUY), 🔴 = sent (SELL)
        direction = "+" if emoji == "🟢" else "-"
        changes.append((direction, amount, contract))

    # Fallback: plain text format
    if not changes:
        plain_pattern = re.compile(
            r'([+\-])\s*([\d,]+\.?\d*)\s+([A-Za-z0-9]{30,50})',
            re.MULTILINE
        )
        changes = plain_pattern.findall(body)

    if not changes:
        # Distinguish "nothing to parse" from "parser couldn't read it" — the
        # difference between a wallet being quiet and the pipeline being broken.
        has_token_link = "solscan.io/token/" in body
        has_emoji      = ("🟢" in body) or ("🔴" in body)
        if has_token_link and has_emoji:
            log.error(
                f"Email PARSER FAILURE for {wallet_info['alias']}: body contains a "
                f"token link and balance emoji but nothing matched. Regex needs "
                f"updating. Snippet: {repr(body[:400])}"
            )
        else:
            log.info(
                f"Email: no token balance changes for {wallet_info['alias']} "
                f"(SOL/stablecoin transfer or non-swap tx)"
            )
        return None

    # ── Extract timestamp ─────────────────────────────────────────────────────
    ts_match = re.search(r'Timestamp:\s*(.+?)(?:\n|$)', body)
    timestamp_str = ts_match.group(1).strip() if ts_match else datetime.now(timezone.utc).isoformat()

    # ── Find the most interesting non-stable balance change ───────────────────
    signals = []
    stable_flows = []

    for direction_sign, amount_str, contract in changes:
        amount = float(amount_str.replace(",", ""))
        direction = "BUY" if direction_sign == "+" else "SELL"

        if contract in SKIP_CONTRACTS:
            stable_flows.append({
                "direction": direction,
                "amount":    amount,
                "contract":  contract,
            })
            continue

        signals.append({
            "direction": direction,
            "amount":    amount,
            "contract":  contract,
        })

    # If only stablecoins moved, log context and return None (no trade signal)
    if not signals:
        log.warning(f"Email: {wallet_info['alias']} — all balance changes were stablecoins/SOL, no memecoin signal")
        if stable_flows:
            # Stablecoin inflow after a sell = contextually useful (profit-taking)
            flow_desc = ", ".join(
                f"{'received' if f['direction']=='BUY' else 'sent'} {f['amount']:.4f} USDC/stable"
                for f in stable_flows
            )
            log.info(
                f"Email [{wallet_info['alias']}]: stablecoin flow only ({flow_desc}) — "
                f"no memecoin signal, but may indicate profit-taking or buy prep"
            )
        return None

    # Pick the largest non-stable movement as the primary signal
    primary = max(signals, key=lambda x: x["amount"])

    # If multiple contracts moved, note them all
    all_contracts = [s["contract"] for s in signals]

    return {
        "alias":            wallet_info["alias"],
        "tier":             wallet_info["tier"],
        "chain":            wallet_info["chain"],
        "bankroll_usd":     wallet_info["bankroll_usd"],
        "copy_trade":       wallet_info.get("copy_trade", True),
        "copy_trade_reason": wallet_info.get("copy_trade_reason", ""),
        "action":           primary["direction"],
        "token_symbol":     None,   # contract only — research_token will resolve symbol
        "contract_address": primary["contract"],
        "confidence":       "high",  # direct on-chain = highest possible confidence
        "signal_text":      f"Solscan alert: {direction_sign}{primary['amount']:.6f} of {primary['contract'][:8]}...",
        "source":           "email",
        "timestamp":        timestamp_str,
        "original_text":    body[:300],
        "all_contracts":    all_contracts,
        "stable_flows":     stable_flows,
    }


# ─── IMAP POLLER ─────────────────────────────────────────────────────────────

def _poll_one_account(address: str, app_password: str, name_map: dict, callback: Callable) -> int:
    """Poll a single Gmail account for unread Solscan alerts. Returns signals emitted."""
    signals_emitted = 0
    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(address, app_password)
        mail.select("inbox")

        _, search_data = mail.search(None, f'(UNSEEN FROM "{SOLSCAN_SENDER}")')
        email_ids = search_data[0].split()

        if not email_ids or email_ids == [b""]:
            log.debug(f"Email [{address}]: no new Solscan alerts")
            mail.logout()
            return 0

        log.info(f"Email [{address}]: {len(email_ids)} new Solscan alert(s)")

        for eid in email_ids:
            try:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _decode_subject(msg)
                body    = _extract_body(msg)
                signal  = _parse_solscan_email(subject, body, name_map)

                mail.store(eid, "+FLAGS", "\\Seen")

                if signal:
                    log.info(
                        f"Email SIGNAL [{address}]: {signal['alias']} "
                        f"{signal['action']} {signal['contract_address'][:8]}..."
                    )
                    try:
                        callback(signal)
                        signals_emitted += 1
                    except Exception as e:
                        log.error(f"Email: callback error: {e}")

            except Exception as e:
                log.error(f"Email: error processing message {eid} ({address}): {e}")
                continue

        mail.logout()

    except imaplib.IMAP4.error as e:
        log.error(f"Email: IMAP auth/connection error ({address}): {e}")
    except Exception as e:
        log.error(f"Email: unexpected error ({address}): {e}")

    return signals_emitted


def poll_gmail(callback: Callable) -> int:
    """
    Poll all configured Gmail accounts for unread Solscan alerts.
    Supports up to two accounts via GMAIL_ADDRESS/GMAIL_ADDRESS_2 env vars.
    Returns total signals emitted across all accounts.
    """
    name_map = _build_name_map()
    if not name_map:
        log.warning("Email: no wallets in name map — check trusted_wallets.json")
        return 0

    accounts = []
    if GMAIL_ADDRESS and GMAIL_APP_PASSWORD:
        accounts.append((GMAIL_ADDRESS, GMAIL_APP_PASSWORD))
    if GMAIL_ADDRESS_2 and GMAIL_APP_PASSWORD_2:
        accounts.append((GMAIL_ADDRESS_2, GMAIL_APP_PASSWORD_2))

    if not accounts:
        log.debug("Email: no Gmail credentials configured — skipping")
        return 0

    total = 0
    for address, password in accounts:
        total += _poll_one_account(address, password, name_map, callback)
    return total


def _poll_gmail_legacy(callback: Callable) -> int:
    """Legacy single-account poller — kept for reference, not called."""
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        log.debug("Email: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not set — skipping")
        return 0

    name_map = _build_name_map()
    if not name_map:
        log.warning("Email: no wallets in name map — check trusted_wallets.json")
        return 0

    signals_emitted = 0

    try:
        mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        # Search for unread emails from Solscan
        _, search_data = mail.search(
            None,
            f'(UNSEEN FROM "{SOLSCAN_SENDER}")'
        )
        email_ids = search_data[0].split()

        if not email_ids or email_ids == [b""]:
            log.debug(f"Email: no new Solscan alerts")
            mail.logout()
            return 0

        log.info(f"Email: {len(email_ids)} new Solscan alert(s) to process")

        for eid in email_ids:
            try:
                _, msg_data = mail.fetch(eid, "(RFC822)")
                raw = msg_data[0][1]
                msg = email.message_from_bytes(raw)

                subject = _decode_subject(msg)
                body    = _extract_body(msg)

                log.debug(f"Email: processing '{subject[:60]}'")

                signal = _parse_solscan_email(subject, body, name_map)

                # Mark as read regardless of whether we emit a signal
                mail.store(eid, "+FLAGS", "\\Seen")

                if signal:
                    log.info(
                        f"Email SIGNAL: {signal['alias']} "
                        f"{signal['action']} {signal['contract_address'][:8]}... "
                        f"[{signal['chain']}]"
                    )
                    try:
                        callback(signal)
                        signals_emitted += 1
                    except Exception as e:
                        log.error(f"Email: callback error: {e}")

            except Exception as e:
                log.error(f"Email: error processing message {eid}: {e}")
                continue

        mail.logout()

    except imaplib.IMAP4.error as e:
        log.error(f"Email: IMAP auth/connection error: {e}")
    except Exception as e:
        log.error(f"Email: unexpected error: {e}")

    return signals_emitted


# ─── BACKGROUND THREAD ───────────────────────────────────────────────────────

def start_email_poller(callback: Callable) -> threading.Thread:
    """
    Start a background daemon thread that polls Gmail every 5 minutes.
    callback(signal_dict) is called for each confirmed memecoin signal.
    """
    def _loop():
        log.info(f"Email poller started (Gmail IMAP every {POLL_INTERVAL_SEC//60} min)")
        # Small startup delay so Flask has time to initialize
        time.sleep(60)
        while True:
            try:
                n = poll_gmail(callback)
                if n:
                    log.info(f"Email poller: emitted {n} signal(s)")
            except Exception as e:
                log.error(f"Email poller loop error: {e}")
            time.sleep(POLL_INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-email-poller")
    t.start()
    return t
