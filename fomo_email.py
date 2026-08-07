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
TRUSTED_WALLETS_FILE = "trusted_wallets.json"

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL_SEC = 300   # 5 minutes

SOLSCAN_SENDER = "noreply@mailer.solscan.io"

# Known stablecoins and native tokens on Solana -- skip these as trade signals
SKIP_CONTRACTS = {
    "EPJFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
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
                }
                # Build a set of lowercase key variants to register
                keys = set()
                base = alias.lower()
                keys.add(base)                            # exact
                keys.add(base.strip("_"))                 # trailing/leading underscores
                keys.add(base.replace("_", ""))           # all underscores removed
                keys.add(base.replace("-", ""))           # all hyphens removed
                keys.add(base.replace("_", "").replace("-", ""))  # both removed

                # Also register the fomo_profile handle (strip leading @)
                profile = w.get("fomo_profile", "")
                if profile:
                    handle = profile.lstrip("@").lower()
                    keys.add(handle)
                    keys.add(handle.strip("_"))
                    keys.add(handle.replace("_", ""))

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
    """Extract plain text body from email (handles multipart)."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain":
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="replace")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="replace")
        except Exception:
            pass
    return body


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
        log.debug(f"Email: no wallet name found in subject: {subject_clean[:80]}")
        return None

    raw_name = name_match.group(1).strip()
    wallet_info = name_map.get(raw_name.lower())
    if not wallet_info:
        log.debug(f"Email: wallet '{raw_name}' not in tracked list — skipping")
        return None

    # ── Extract balance change lines ──────────────────────────────────────────
    # Format: "+ 0.034997 EPJFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    # or:     "- 0.5 So11111111111111111111111111111111111111112"
    balance_pattern = re.compile(
        r'([+\-])\s*([\d,]+\.?\d*)\s+([A-Za-z0-9]{30,50})',
        re.MULTILINE
    )
    changes = balance_pattern.findall(body)

    if not changes:
        log.debug(f"Email: no balance changes found for {wallet_info['alias']}")
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

def poll_gmail(callback: Callable) -> int:
    """
    Connect to Gmail via IMAP, fetch unread Solscan alerts, parse each one,
    and call callback(signal) for every real memecoin signal found.
    Returns count of signals emitted.
    """
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


# ─── BACKGROUND THREAD ────────────────────────────────────────────────────────

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
