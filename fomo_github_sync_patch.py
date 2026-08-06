#!/usr/bin/env python3
"""
fomo_github_sync_patch.py

Adds GitHub `data`-branch persistence to fomo_portfolio.py, mirroring the
pull_state_from_github()/push_results_to_github() pattern already used by
crypto_oracle_v3.py.

WHY: crypto-strategy-clock (cron) and desirable-insight (webhook) are two
separate Railway services with separate, ephemeral filesystems. Buys/sells
executed via the Telegram EXECUTE flow only ever wrote to desirable-insight's
local disk, which (a) gets wiped on every redeploy of that service, and
(b) is never seen by crypto-strategy-clock's cron loop -- which is what
actually checks the -15% stop / +30% target / 24h auto-exits. This patch
closes that gap: fomo_portfolio.json is pulled from the `data` branch before
every read/mutate and pushed back right after every write.

Run from the repo root:
    python fomo_github_sync_patch.py

Safe to re-run -- each edit checks whether it's already applied before
touching anything.
"""

import subprocess
import sys

TARGET = "fomo_portfolio.py"

EDITS = []

def add_edit(name, old, new):
    EDITS.append((name, old, new))


# ── 1. GITHUB_* constants, right after the existing TELEGRAM_CHAT_ID line ────
add_edit(
    "constants",
    '''ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL           = "claude-opus-4-6"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")''',
    '''ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL           = "claude-opus-4-6"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# GitHub state sync -- crypto-strategy-clock (cron) and desirable-insight (webhook)
# are separate Railway services with separate ephemeral filesystems. This is the
# only channel they share: fomo_portfolio.json is pulled before every read/mutate
# and pushed right after every write, so a buy/sell made on either service is
# durable across redeploys and visible to the other service within seconds.
GITHUB_TOKEN       = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_REPO        = "Zbogue1/crypto-strategy-clock"
GITHUB_DATA_BRANCH = "data"''',
)

# ── 2. Sync helper functions, inserted after save_fomo_lessons() ────────────
add_edit(
    "sync functions",
    '''def save_fomo_lessons(lessons: dict):
    lessons["last_rebuilt"] = datetime.now(timezone.utc).isoformat()
    with open(FOMO_LESSONS_FILE, "w") as f:
        json.dump(lessons, f, indent=2, default=str)


# ─── PORTFOLIO VALUE ──────────────────────────────────────────────────────────''',
    '''def save_fomo_lessons(lessons: dict):
    lessons["last_rebuilt"] = datetime.now(timezone.utc).isoformat()
    with open(FOMO_LESSONS_FILE, "w") as f:
        json.dump(lessons, f, indent=2, default=str)


# ─── GITHUB STATE SYNC ────────────────────────────────────────────────────────

def _github_pull_file(filename: str) -> bool:
    """Pull one file from the data branch, overwriting the local copy if found."""
    if not GITHUB_TOKEN:
        return False
    import base64
    gh_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    base_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
    try:
        r = requests.get(f"{base_url}/{filename}?ref={GITHUB_DATA_BRANCH}", headers=gh_headers, timeout=10)
        if r.status_code == 200:
            content = base64.b64decode(r.json()["content"]).decode()
            with open(filename, "w") as f:
                f.write(content)
            log.info(f"FOMO GitHub: restored {filename} from data branch")
            return True
        elif r.status_code == 404:
            log.info(f"FOMO GitHub: {filename} not on GitHub yet")
        else:
            log.warning(f"FOMO GitHub pull {filename}: {r.status_code}")
    except Exception as e:
        log.warning(f"FOMO GitHub pull error ({filename}): {e}")
    return False


def _github_push_file(filename: str):
    """Push one local file to the data branch."""
    if not GITHUB_TOKEN or not os.path.exists(filename):
        return
    import base64
    gh_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    base_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents"
    try:
        with open(filename) as f:
            content = f.read()
        r = requests.get(f"{base_url}/{filename}?ref={GITHUB_DATA_BRANCH}", headers=gh_headers, timeout=10)
        sha = r.json().get("sha") if r.status_code == 200 else None
        payload = {
            "message": f"[fomo-bot] {filename} update",
            "branch":  GITHUB_DATA_BRANCH,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        }
        if sha:
            payload["sha"] = sha
        pr = requests.put(f"{base_url}/{filename}", headers=gh_headers, json=payload, timeout=20)
        if pr.status_code in (200, 201):
            log.info(f"FOMO GitHub: pushed {filename}")
        else:
            log.warning(f"FOMO GitHub push {filename}: {pr.status_code} {pr.text[:120]}")
    except Exception as e:
        log.warning(f"FOMO GitHub push error ({filename}): {e}")


def sync_fomo_state_from_github():
    """Pull the latest state before reading/mutating -- this is what lets a buy
    made via the webhook service become visible to the cron service's auto-exit
    checks, and what survives a redeploy wiping the local filesystem."""
    _github_pull_file(FOMO_PORTFOLIO_FILE)
    _github_pull_file(FOMO_LESSONS_FILE)


def sync_fomo_state_to_github():
    """Push current local state back right after a mutation."""
    _github_push_file(FOMO_PORTFOLIO_FILE)


# ─── PORTFOLIO VALUE ──────────────────────────────────────────────────────────''',
)

# ── 3. execute_fomo_buy: pull before read ────────────────────────────────────
add_edit(
    "execute_fomo_buy pull",
    '''    """Execute a FOMO copy trade buy. Returns holding dict or None if skipped."""
    state = load_fomo_portfolio()''',
    '''    """Execute a FOMO copy trade buy. Returns holding dict or None if skipped."""
    sync_fomo_state_from_github()
    state = load_fomo_portfolio()''',
)

# ── 4. execute_fomo_buy: push after write ────────────────────────────────────
add_edit(
    "execute_fomo_buy push",
    '''    state["cash"]    -= spend
    state["holding"] = holding
    save_fomo_portfolio(state)

    log.info(f"FOMO BUY: {token_ticker} @ ${entry_price:.8f} | "
             f"${spend:.2f} | following {wallet_alias} | catalyst: {catalyst or 'none'}")
    return holding''',
    '''    state["cash"]    -= spend
    state["holding"] = holding
    save_fomo_portfolio(state)
    sync_fomo_state_to_github()

    log.info(f"FOMO BUY: {token_ticker} @ ${entry_price:.8f} | "
             f"${spend:.2f} | following {wallet_alias} | catalyst: {catalyst or 'none'}")
    return holding''',
)

# ── 5. execute_fomo_sell: pull before read ───────────────────────────────────
add_edit(
    "execute_fomo_sell pull",
    '''    """Exit the active FOMO quick trade."""
    state   = load_fomo_portfolio()''',
    '''    """Exit the active FOMO quick trade."""
    sync_fomo_state_from_github()
    state   = load_fomo_portfolio()''',
)

# ── 6. execute_fomo_sell: push after write ───────────────────────────────────
add_edit(
    "execute_fomo_sell push",
    '''    state["holding"] = None
    save_fomo_portfolio(state)

    outcome = "WIN" if profit > 0 else "LOSS"''',
    '''    state["holding"] = None
    save_fomo_portfolio(state)
    sync_fomo_state_to_github()

    outcome = "WIN" if profit > 0 else "LOSS"''',
)

# ── 7. check_fomo_auto_exits: pull before read ───────────────────────────────
add_edit(
    "check_fomo_auto_exits pull",
    '''    state   = load_fomo_portfolio()
    holding = state.get("holding")
    if not holding:
        return None

    ticker        = holding["token_ticker"]''',
    '''    sync_fomo_state_from_github()
    state   = load_fomo_portfolio()
    holding = state.get("holding")
    if not holding:
        return None

    ticker        = holding["token_ticker"]''',
)

# ── 8. get_fomo_stats: pull before read (this is what backs /health) ────────
add_edit(
    "get_fomo_stats pull",
    '''def get_fomo_stats() -> dict:
    """Full stats including per-wallet breakdown — used in 4-hour agent output."""
    state   = load_fomo_portfolio()''',
    '''def get_fomo_stats() -> dict:
    """Full stats including per-wallet breakdown — used in 4-hour agent output."""
    sync_fomo_state_from_github()
    state   = load_fomo_portfolio()''',
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

    print("\nCompiling...")
    subprocess.run([sys.executable, "-m", "py_compile", TARGET], check=True)
    print("COMPILE OK")

    print("\nGit status:")
    subprocess.run(["git", "status", "--short"])

    print(
        "\nNext steps:\n"
        "  git add fomo_portfolio.py\n"
        "  git commit -m \"Add GitHub data-branch sync to fomo_portfolio.py (fixes cross-service state loss)\"\n"
        "  git push origin master\n"
        "  git push origin main\n"
        "\n"
        "IMPORTANT -- before this actually works in production:\n"
        "  1. Check the desirable-insight service in Railway (Variables tab) has a\n"
        "     GITHUB_TOKEN env var set. crypto-strategy-clock already has one (that's\n"
        "     how paper_portfolio.json syncs today) but desirable-insight has never\n"
        "     needed one until now -- if it's missing, this patch will silently no-op\n"
        "     (same fail-safe pattern as crypto_oracle_v3.py: no token = skip sync,\n"
        "     never crash). Copy the same token value over if it's not there.\n"
        "  2. Railway will redeploy desirable-insight when you push -- that's fine,\n"
        "     there's no live position to lose right now.\n"
        "  3. The earlier test position is gone for good (wiped before this fix\n"
        "     existed) -- you'll want to re-trigger a test buy afterward to confirm\n"
        "     the auto-exits actually fire and text you this time.\n"
    )


if __name__ == "__main__":
    main()
