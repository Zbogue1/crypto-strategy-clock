#!/usr/bin/env python3
"""
fomo_relay_patch.py

Adds a "text your agent a signal" flow to fomo_tracker.py.

WHAT THIS DOES: right now /webhook/telegram only reacts to button taps
(callback_query). This patch adds handling for plain text messages too --
so you can forward a wallet-alert email (e.g. from Solscan) or just type a
quick note to the same Telegram bot, and the agent will:

  1. Parse the message (via Claude, if ANTHROPIC_API_KEY is set on this
     service -- falls back to a regex-only best-effort parse otherwise)
     into {wallet_alias, action (BUY/SELL), contract_address, confidence}.
  2. Run it through the exact same research pipeline as an automated
     webhook signal -- validate_token() (mcap/liquidity/age floors),
     scan_catalyst(), and the wallet-lesson filter.
  3. Send a signal card with a single EXECUTE button.

Execution still ALWAYS requires your Telegram tap -- this only adds a new
way to get a signal INTO the pipeline, it does not change how trades get
executed. Reuses the existing buy_show:/buy_amt:/sell_exec: button handlers
untouched.

Run from the repo root:
    python fomo_relay_patch.py

Safe to re-run -- each edit checks whether it's already applied first.
"""

import subprocess
import sys

TARGET = "fomo_tracker.py"

EDITS = []

def add_edit(name, old, new):
    EDITS.append((name, old, new))


# ── 1. imports: add re + anthropic ───────────────────────────────────────────
add_edit(
    "imports",
    '''import hashlib
import hmac
import json
import logging
import os
import time
import uuid
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
)''',
    '''import hashlib
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
)''',
)

# ── 2. config: ANTHROPIC_API_KEY + known quote mints ─────────────────────────
add_edit(
    "config",
    '''HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
HELIUS_AUTH_HEADER = os.environ.get("HELIUS_AUTH_HEADER", "")
WSOL_MINT = "So11111111111111111111111111111111111111112"

TRUSTED_WALLETS_FILE = "trusted_wallets.json"
FOMO_PENDING_ALERTS_FILE = "fomo_pending_alerts.json"
BUY_ALERT_EXPIRY_MINUTES = 15   # memecoins move fast -- signal goes stale if untapped''',
    '''HELIUS_API_KEY     = os.environ.get("HELIUS_API_KEY", "")
HELIUS_AUTH_HEADER = os.environ.get("HELIUS_AUTH_HEADER", "")
WSOL_MINT = "So11111111111111111111111111111111111111112"

TRUSTED_WALLETS_FILE = "trusted_wallets.json"
FOMO_PENDING_ALERTS_FILE = "fomo_pending_alerts.json"
BUY_ALERT_EXPIRY_MINUTES = 15   # memecoins move fast -- signal goes stale if untapped

# Relayed-signal parsing (manually forwarded emails / notes via Telegram text)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL          = "claude-opus-4-6"
QUOTE_MINTS = {
    "So11111111111111111111111111111111111111112": "SOL",
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
_SOLANA_ADDR_RE = re.compile(r"\\b[1-9A-HJ-NP-Za-km-z]{32,44}\\b")''',
)

# ── 3. new functions: relayed-signal parsing + handling ──────────────────────
add_edit(
    "relay functions",
    '''def suggest_buy_amount(catalyst_score: int) -> str:
    """Map catalyst confidence to a suggested $ tier -- a suggestion only,
    the human still picks the final amount."""
    if catalyst_score >= 8:
        return "500"
    if catalyst_score >= 6:
        return "200"
    if catalyst_score >= 4:
        return "100"
    return "50"


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────''',
    '''def suggest_buy_amount(catalyst_score: int) -> str:
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
    if "bought" in lowered or re.search(r"\\bbuy\\b", lowered):
        action = "BUY"
    elif "sold" in lowered or re.search(r"\\bsell\\b", lowered):
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

    system = ("You extract a structured Solana trading signal from a message a user "
              "pasted into Telegram -- usually a forwarded wallet-alert email (e.g. from "
              "Solscan) showing balance changes, or sometimes just a quick note in their "
              "own words. Identify which wallet/trader is involved, which token was bought "
              "or sold, and the direction. In balance-change style emails, a green/positive "
              "entry is a token received and a red/negative entry is a token sent; SOL, "
              "USDC, and USDT are quote currencies, not the token being traded -- the "
              "actual token is whichever side is NOT one of those. Respond ONLY with valid JSON.")

    prompt = (
        f"Known wallet aliases already tracked: {', '.join(known_aliases) or 'none'}\\n\\n"
        f"Message to parse:\\n---\\n{raw_text}\\n---\\n\\n"
        "Respond with this JSON structure:\\n"
        "{\\n"
        '  "wallet_alias": "closest matching known alias, or the name mentioned if not in the known list, or null if no wallet/trader is identifiable",\\n'
        '  "action": "BUY" or "SELL" or "UNCLEAR",\\n'
        '  "contract_address": "the base58 Solana mint address of the token being bought/sold (not SOL/USDC/USDT), or null if not identifiable",\\n'
        '  "confidence": "high" or "low",\\n'
        '  "notes": "one short sentence on anything ambiguous or worth flagging"\\n'
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

    log.info(f"FOMO: relayed text message received ({len(text)} chars)")
    parsed     = parse_relayed_signal(text)
    alias      = parsed.get("wallet_alias")
    action     = (parsed.get("action") or "UNCLEAR").upper()
    contract   = parsed.get("contract_address")
    confidence = parsed.get("confidence", "low")
    notes      = parsed.get("notes", "")

    if action == "UNCLEAR" or not contract:
        send_telegram(
            "\\U0001f914 <b>Couldn't parse a clear signal from that.</b>\\n"
            + (f"Notes: {notes}\\n" if notes else "")
            + "Try including the token's contract address and whether it was a buy or sell."
        )
        return

    matched_alias = _match_known_alias(alias) or alias or "unknown trader"

    token_data = validate_token(contract)
    if not token_data["valid"]:
        send_telegram(
            f"\\u26a0\\ufe0f <b>Relayed signal skipped</b>\\n"
            f"{matched_alias} {action.lower()} {token_data.get('symbol','???')}\\n"
            f"Reason: {token_data.get('reject_reason')}"
        )
        return

    sync_fomo_state_from_github()
    portfolio = load_fomo_portfolio()
    holding   = portfolio.get("holding")

    if action == "BUY":
        if holding:
            send_telegram(
                f"\\u26a0\\ufe0f Already holding {holding['token_ticker']} -- "
                f"skipping relayed buy signal for {token_data['symbol']}."
            )
            return
        catalyst_data = scan_catalyst(token_data["symbol"], contract)
        lessons = get_wallet_lessons(matched_alias)
        best    = lessons.get("best_conditions", {})
        skip_reason = None
        if best.get("min_catalyst_score_for_win") and catalyst_data["score"] < best["min_catalyst_score_for_win"] - 2:
            skip_reason = (f"Catalyst score {catalyst_data['score']} below "
                           f"{matched_alias}'s historical win threshold")
        if skip_reason:
            send_telegram(
                f"\\u26a0\\ufe0f <b>Relayed Signal Filtered by Lessons</b>\\n"
                f"{matched_alias} bought {token_data['symbol']}\\n"
                f"Reason: {skip_reason}"
            )
            return

        alert_id = create_pending_buy_alert({
            "token_ticker":     token_data["symbol"],
            "token_name":       token_data["name"],
            "entry_price":      token_data["price"],
            "wallet_alias":     matched_alias,
            "wallet_address":   "",
            "contract_address": contract,
            "catalyst":         catalyst_data["catalyst"],
            "catalyst_score":   catalyst_data["score"],
            "market_cap":       token_data.get("market_cap"),
            "liquidity_usd":    token_data.get("liquidity_usd"),
            "token_age_days":   token_data.get("age_days"),
            "volume_spike_pct": token_data.get("volume_spike_pct"),
        })
        send_telegram_button(
            "\\U0001f4e9 <b>RELAYED BUY SIGNAL: " + token_data["symbol"] + " @ $"
            + "{:.8f}".format(token_data["price"]) + "</b>\\n"
            + "From: " + matched_alias + f" (confidence: {confidence})\\n"
            + "Catalyst (" + str(catalyst_data["score"]) + "/10): "
            + catalyst_data["catalyst"] + "\\n"
            + "Mcap: $" + "{:,.0f}".format(token_data.get("market_cap") or 0)
            + " | Liq: $" + "{:,.0f}".format(token_data.get("liquidity_usd") or 0) + "\\n"
            + "\\u23f1 Expires in " + str(BUY_ALERT_EXPIRY_MINUTES) + " min",
            "EXECUTE",
            f"buy_show:{alert_id}",
        )

    elif action == "SELL":
        if not holding or (holding.get("contract_address") or "") != contract:
            send_telegram(
                f"\\u2139\\ufe0f Not currently holding {token_data['symbol']} -- "
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
            f"\\U0001f4e9 <b>RELAYED SELL SIGNAL: {matched_alias} sold {holding['token_ticker']}</b>\\n"
            f"Tap to confirm your exit.",
            "EXECUTE",
            f"sell_exec:{alert_id}",
        )


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────''',
)

# ── 4. route plain text messages into the new handler ───────────────────────
add_edit(
    "telegram_webhook routing",
    '''@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    """Receives button taps from Telegram -- this is the human trigger-pull."""
    update = request.json or {}
    callback = update.get("callback_query")
    if not callback:
        return jsonify({"ok": True})''',
    '''@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    """Receives button taps and relayed text messages from Telegram -- both are
    human-initiated; execution always still needs a tap."""
    update = request.json or {}
    callback = update.get("callback_query")
    if not callback:
        message = update.get("message")
        if message:
            handle_relayed_text_message(message)
        return jsonify({"ok": True})''',
)


def patch_file(path, edits):
    with open(path, encoding="utf-8") as f:
        content = f.read()

    applied, skipped = [], []
    for name, old, new in edits:
        if new in content:
            skipped.append(name)
            continue
        if old not in content:
            print(f"MISSING ANCHOR [{path} :: {name}]")
            print("---- expected to find ----")
            print(old)
            sys.exit(1)
        content = content.replace(old, new, 1)
        applied.append(name)

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    for name in applied:
        print(f"APPLIED: {name}")
    for name in skipped:
        print(f"ALREADY APPLIED (skipped): {name}")


def main():
    patch_file(TARGET, EDITS)

    print("\nChecking for anthropic package...")
    check = subprocess.run([sys.executable, "-c", "import anthropic"], capture_output=True)
    if check.returncode != 0:
        print("NOTE: 'anthropic' package isn't importable in this environment.")
        print("      It needs to be in requirements.txt / installed on Railway for")
        print("      desirable-insight (it's likely already there since crypto_oracle_v3.py")
        print("      and fomo_portfolio.py both use it -- but if requirements.txt scopes")
        print("      differently per service, double check).")
    else:
        print("OK -- anthropic package importable here.")

    print("\nCompiling...")
    subprocess.run([sys.executable, "-m", "py_compile", TARGET], check=True)
    print("COMPILE OK")

    print("\nGit status:")
    subprocess.run(["git", "status", "--short"])

    print(
        "\nNext steps:\n"
        "  git add fomo_tracker.py\n"
        "  git commit -m \"Add manually-relayed signal parsing via Telegram text messages\"\n"
        "  git push origin master\n"
        "  git push origin main\n"
        "\n"
        "IMPORTANT:\n"
        "  1. Check the desirable-insight service in Railway (Variables tab) has an\n"
        "     ANTHROPIC_API_KEY env var set. If it's missing, relayed messages will\n"
        "     still get a best-effort regex-only parse (never crashes) but won't be\n"
        "     nearly as reliable -- copy the value over from crypto-strategy-clock's\n"
        "     Variables if it's not there.\n"
        "  2. To use it: just send a text message (paste/forward an email, or type\n"
        "     a quick note) to the same Telegram bot you already use for buy/sell\n"
        "     alerts. You'll get back either a signal card with an EXECUTE button,\n"
        "     or a message asking for more detail if it couldn't parse a clear signal.\n"
        "  3. Nothing executes without your tap -- this only adds a new way to get\n"
        "     a signal into the pipeline.\n"
    )


if __name__ == "__main__":
    main()
