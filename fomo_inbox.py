#!/usr/bin/env python3
"""
fomo_inbox.py — Relay context from your phone to Claude on your desktop.

THE ROUTING PROBLEM THIS SOLVES
Telegram is the phone-to-bot bridge, and it works. But the bots run on Railway
and Claude runs on the desktop — they share no filesystem. A screenshot sent to
FOMO gets read by the bot and vanishes as far as the desktop is concerned.

The one thing both sides already touch is the GitHub `data` branch: FOMO pushes
portfolio state there, and the desktop can pull it. So that is the mailbox.

    phone -> Telegram -> Railway bot -> GitHub data branch -> desktop Claude

WHY THIS IS SEPARATE FROM fomo_intel
fomo_intel stores TRADING intel — a token, a stance, a conviction — and feeds
research decisions. It has a rigid schema because it drives automated logic.

Most of what you want to relay isn't that. A Railway error screenshot, an
article, a chart, a thought about strategy, a note to look at something later.
Forced through the trading schema, all of it comes back empty and gets dropped.

This inbox takes anything. No schema, no scoring, no trade logic. It exists so
that "here, look at this" has somewhere to land.

WHAT READS IT
The daily 6am self-check pulls the inbox and includes it in the report, so
anything relayed while you were away is waiting when you get back. You can also
just ask Claude to read it.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

INBOX_FILE   = "fomo_inbox.json"
GITHUB_TOKEN = (os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN", ""))
GITHUB_REPO  = os.getenv("GITHUB_REPO", "Zbogue1/crypto-strategy-clock")
DATA_BRANCH  = os.getenv("GITHUB_DATA_BRANCH", "data")

MAX_ENTRIES = int(os.getenv("FOMO_INBOX_MAX", "200"))


def _headers() -> dict:
    return {"Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github+json"}


def _pull() -> dict:
    """Read the inbox from the data branch."""
    if not GITHUB_TOKEN:
        if os.path.exists(INBOX_FILE):
            try:
                with open(INBOX_FILE) as f:
                    return json.load(f)
            except Exception:
                pass
        return {"entries": []}

    try:
        r = requests.get(
            f"https://api.github.com/repos/{GITHUB_REPO}/contents/{INBOX_FILE}",
            params={"ref": DATA_BRANCH}, headers=_headers(), timeout=15)
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"]).decode()
            return json.loads(content)
        if r.status_code != 404:
            log.warning(f"Inbox: pull HTTP {r.status_code}: {r.text[:120]}")
    except Exception as e:
        log.error(f"Inbox: pull failed: {e}")
    return {"entries": []}


def _push(state: dict) -> bool:
    """
    Write the inbox back to the data branch.

    Returns success. The caller MUST surface a False — a relay that silently
    fails to save is worse than no relay, because you'd believe the note got
    through and stop thinking about it.
    """
    try:
        with open(INBOX_FILE, "w") as f:
            json.dump(state, f, indent=2, default=str)
    except Exception as e:
        log.error(f"Inbox: local write failed: {e}")

    if not GITHUB_TOKEN:
        log.error("Inbox: no GITHUB_TOKEN — note saved locally only, and "
                  "Railway containers are ephemeral, so it will be LOST")
        return False

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{INBOX_FILE}"
    try:
        sha = None
        r = requests.get(url, params={"ref": DATA_BRANCH},
                         headers=_headers(), timeout=15)
        if r.status_code == 200:
            sha = r.json().get("sha")

        body = {
            "message": f"inbox: relay {datetime.now(timezone.utc).isoformat()[:16]}",
            "content": base64.b64encode(
                json.dumps(state, indent=2, default=str).encode()).decode(),
            "branch": DATA_BRANCH,
        }
        if sha:
            body["sha"] = sha

        p = requests.put(url, json=body, headers=_headers(), timeout=20)
        if p.status_code in (200, 201):
            return True
        log.error(f"Inbox: push HTTP {p.status_code}: {p.text[:160]}")
    except Exception as e:
        log.error(f"Inbox: push failed: {e}")
    return False


# ─── WRITE ────────────────────────────────────────────────────────────────────

def relay(kind: str, summary: str, detail: str = "", note: str = "",
          source_bot: str = "fomo", raw: dict = None) -> bool:
    """
    Drop something in the inbox for Claude to read later.

    kind is free-form ("screenshot", "note", "error", "article"). Nothing
    downstream branches on it — it's there to help a human skim.
    """
    state = _pull()
    state.setdefault("entries", []).append({
        "at":       datetime.now(timezone.utc).isoformat(),
        "kind":     kind,
        "bot":      source_bot,
        "summary":  summary[:600],
        "detail":   detail[:4000],
        "note":     note[:600],
        "raw":      raw or {},
        "read":     False,
    })
    if len(state["entries"]) > MAX_ENTRIES:
        state["entries"] = state["entries"][-MAX_ENTRIES:]

    ok = _push(state)
    log.info(f"Inbox: relayed {kind} — {summary[:60]!r} "
             f"({'saved' if ok else 'SAVE FAILED'})")
    return ok


# ─── READ ─────────────────────────────────────────────────────────────────────

def unread(limit: int = 50) -> list:
    return [e for e in _pull().get("entries", []) if not e.get("read")][-limit:]


def mark_all_read() -> bool:
    state = _pull()
    for e in state.get("entries", []):
        e["read"] = True
    return _push(state)


def build_report(include_read: bool = False, limit: int = 25) -> str:
    """Plain text — relayed content is arbitrary and breaks Markdown."""
    entries = _pull().get("entries", [])
    if not include_read:
        entries = [e for e in entries if not e.get("read")]
    if not entries:
        return "INBOX — empty. Nothing relayed since the last check."

    L = [f"INBOX — {len(entries)} item(s)", ""]
    for e in entries[-limit:]:
        L.append(f"[{str(e.get('at'))[:16]}] {e.get('kind','?')} "
                 f"via {e.get('bot','?')}")
        L.append(f"  {e.get('summary','')}")
        if e.get("note"):
            L.append(f"  your note: {e['note']}")
        if e.get("detail"):
            d = e["detail"]
            L.append(f"  detail: {d[:300]}{'...' if len(d) > 300 else ''}")
        L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(build_report(include_read=True))
