#!/usr/bin/env python3
"""
fomo_first_buy.py — First-buy detection for tracked wallets.

A wallet's FIRST entry into a token is a much stronger signal than an add
to an existing position. This module tracks which wallet has previously
bought which contract, and flags new entries so the signal pipeline can
boost conviction and surface the flag in Telegram messages.

Data stored in fomo_first_buy_log.json on the GitHub data branch.
Structure: { "Binkieee": ["contractA", "contractB", ...], ... }

A wallet buying the same contract a second time = "adding to position" —
still useful context but not flagged as a fresh initiation.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO        = "Zbogue1/crypto-strategy-clock"
GITHUB_DATA_BRANCH = "data"
FIRST_BUY_FILE     = "fomo_first_buy_log.json"


# ─── GITHUB HELPERS ───────────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept":        "application/vnd.github.v3+json",
    }


def _load_log() -> dict:
    """Pull first-buy log from data branch. Falls back to local file."""
    # Try local first (fast path when file was already pulled this session)
    if os.path.exists(FIRST_BUY_FILE):
        try:
            with open(FIRST_BUY_FILE) as f:
                return json.load(f)
        except Exception:
            pass

    if not GITHUB_TOKEN:
        return {}

    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FIRST_BUY_FILE}"
            f"?ref={GITHUB_DATA_BRANCH}",
            headers=_gh_headers(), timeout=10,
        )
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"]).decode("utf-8")
            data = json.loads(content)
            # Cache locally
            with open(FIRST_BUY_FILE, "w") as f:
                json.dump(data, f)
            return data
    except Exception as e:
        log.warning(f"FirstBuy: could not pull log from GitHub: {e}")
    return {}


def _save_log(data: dict):
    """Push first-buy log to data branch and update local cache."""
    try:
        with open(FIRST_BUY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.warning(f"FirstBuy: local write failed: {e}")

    if not GITHUB_TOKEN:
        return

    try:
        content_b64 = base64.b64encode(
            json.dumps(data, indent=2).encode("utf-8")
        ).decode("utf-8")

        # Get existing SHA if file exists
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FIRST_BUY_FILE}"
            f"?ref={GITHUB_DATA_BRANCH}",
            headers=_gh_headers(), timeout=10,
        )
        sha = r.json().get("sha") if r.status_code == 200 else None

        payload = {
            "message": "auto: update first-buy log",
            "content": content_b64,
            "branch":  GITHUB_DATA_BRANCH,
        }
        if sha:
            payload["sha"] = sha

        requests.put(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{FIRST_BUY_FILE}",
            headers=_gh_headers(), json=payload, timeout=15,
        )
    except Exception as e:
        log.warning(f"FirstBuy: GitHub push failed: {e}")


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def check_and_record(alias: str, contract: str) -> bool:
    """
    Check if this is the first time `alias` has bought `contract`.

    Returns True  if this is a FIRST BUY (wallet has never bought this token).
    Returns False if wallet has bought this contract before (adding to position).

    Always records the buy regardless of outcome so future calls are accurate.
    """
    if not alias or not contract:
        return False

    try:
        log_data = _load_log()

        wallet_contracts = log_data.get(alias, [])
        is_first = contract not in wallet_contracts

        if is_first:
            wallet_contracts.append(contract)
            log_data[alias] = wallet_contracts
            _save_log(log_data)
            log.info(f"FirstBuy: {alias} → {contract[:8]}... FIRST BUY ✅")
        else:
            log.info(f"FirstBuy: {alias} → {contract[:8]}... adding to existing position")

        return is_first

    except Exception as e:
        log.warning(f"FirstBuy: check_and_record failed: {e}")
        return False  # safe default — don't block signal on error


def get_wallet_history(alias: str) -> list:
    """Return list of contracts a wallet has previously bought. For debugging."""
    try:
        return _load_log().get(alias, [])
    except Exception:
        return []
