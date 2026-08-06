#!/usr/bin/env python3
"""
fomo_multiposition_patch_tracker.py

Updates fomo_tracker.py to work with fomo_portfolio.py's new multi-position
schema (state["holdings"] list, cap 5, instead of a single state["holding"]).

Run fomo_multiposition_patch_portfolio.py FIRST (or in either order, but both
must be applied before committing -- fomo_tracker.py calls execute_fomo_sell()
which needs the new contract_address argument fomo_portfolio.py now requires).

WHAT CHANGES:
  - Adds a _find_holding(holdings, contract_address) helper.
  - /webhook/helius and /webhook/alchemy: BUY branches now check the position
    cap and duplicate-token instead of a single "already holding" flag; SELL
    branches look up the matching position by contract address instead of
    assuming there's only one to check.
  - The sell_exec: Telegram button handler and the relayed-text-message
    handler get the same treatment.
  - Every execute_fomo_sell(...) call now passes contract_address as the
    first argument, matching fomo_portfolio.py's new signature.

Run from the repo root:
    python fomo_multiposition_patch_tracker.py

Safe to re-run -- each edit checks whether it's already applied first.
"""

import subprocess
import sys

TARGET = "fomo_tracker.py"

EDITS = []

def add_edit(name, old, new):
    EDITS.append((name, old, new))


# ── 1. import FOMO_MAX_CONCURRENT_POSITIONS ───────────────────────────────────
add_edit(
    "import max positions constant",
    '''from fomo_portfolio import (
    execute_fomo_buy,
    execute_fomo_sell,
    load_fomo_portfolio,
    check_fomo_auto_exits,
    get_wallet_lessons,
    get_fomo_stats,
    sync_fomo_state_from_github,
)''',
    '''from fomo_portfolio import (
    execute_fomo_buy,
    execute_fomo_sell,
    load_fomo_portfolio,
    check_fomo_auto_exits,
    get_wallet_lessons,
    get_fomo_stats,
    sync_fomo_state_from_github,
    FOMO_MAX_CONCURRENT_POSITIONS,
)''',
)

# ── 2. _find_holding helper ────────────────────────────────────────────────────
add_edit(
    "_find_holding helper",
    '''_SOLANA_ADDR_RE = re.compile(r"\\b[1-9A-HJ-NP-Za-km-z]{32,44}\\b")''',
    '''_SOLANA_ADDR_RE = re.compile(r"\\b[1-9A-HJ-NP-Za-km-z]{32,44}\\b")


def _find_holding(holdings: list, contract_address: str) -> Optional[dict]:
    """Find an open position by contract address (case-insensitive). Multiple
    positions can be open at once, so lookups always go by contract address
    rather than assuming there's only one."""
    if not contract_address:
        return None
    target = contract_address.lower()
    for h in holdings:
        if (h.get("contract_address") or "").lower() == target:
            return h
    return None''',
)

# ── 3. relayed-text-message handler: BUY branch ────────────────────────────────
add_edit(
    "relay handler BUY branch",
    '''    sync_fomo_state_from_github()
    portfolio = load_fomo_portfolio()
    holding   = portfolio.get("holding")

    if action == "BUY":
        if holding:
            send_telegram(
                f"\\u26a0\\ufe0f Already holding {holding['token_ticker']} -- "
                f"skipping relayed buy signal for {token_data['symbol']}."
            )
            return''',
    '''    sync_fomo_state_from_github()
    portfolio = load_fomo_portfolio()
    holdings  = portfolio.get("holdings", [])

    if action == "BUY":
        if len(holdings) >= FOMO_MAX_CONCURRENT_POSITIONS:
            send_telegram(
                f"\\u26a0\\ufe0f At max concurrent positions -- "
                f"skipping relayed buy signal for {token_data['symbol']}."
            )
            return
        if _find_holding(holdings, contract):
            send_telegram(
                f"\\u26a0\\ufe0f Already holding {token_data['symbol']} -- "
                f"skipping duplicate relayed buy signal."
            )
            return''',
)

# ── 4. relayed-text-message handler: SELL branch ───────────────────────────────
add_edit(
    "relay handler SELL branch",
    '''    elif action == "SELL":
        if not holding or (holding.get("contract_address") or "") != contract:
            send_telegram(
                f"\\u2139\\ufe0f Not currently holding {token_data['symbol']} -- "
                f"nothing to exit on this relayed sell signal."
            )
            return''',
    '''    elif action == "SELL":
        holding = _find_holding(holdings, contract)
        if not holding:
            send_telegram(
                f"\\u2139\\ufe0f Not currently holding {token_data['symbol']} -- "
                f"nothing to exit on this relayed sell signal."
            )
            return''',
)

# ── 5. sell_exec: Telegram button handler ──────────────────────────────────────
add_edit(
    "sell_exec handler",
    '''            else:
                portfolio = load_fomo_portfolio()
                holding   = portfolio.get("holding")
                if not holding or (holding.get("contract_address") or "") != alert.get("contract_address"):
                    consume_pending_alert(alert_id)
                    edit("\\u26a0\\ufe0f Position already closed (an auto-exit likely already fired) -- nothing to execute.")
                else:
                    token_data      = validate_token(alert["contract_address"])
                    current_price   = token_data.get("price") or alert["price_at_signal"]
                    price_at_signal = alert.get("price_at_signal") or current_price
                    drift_pct = ((current_price - price_at_signal) / price_at_signal * 100) if price_at_signal else 0

                    entered_at   = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
                    held_hrs     = (datetime.now(timezone.utc) - entered_at).total_seconds() / 3600
                    created_at   = datetime.fromisoformat(alert["created_at"].replace("Z", "+00:00"))
                    exit_lag_min = (datetime.now(timezone.utc) - created_at).total_seconds() / 60

                    result = execute_fomo_sell(
                        current_price,
                        reason="tracker_sell_" + alert["wallet_alias"],
                        trader_held_hours=held_hrs,
                        exit_lag_minutes=exit_lag_min,
                    )
                    consume_pending_alert(alert_id)''',
    '''            else:
                portfolio = load_fomo_portfolio()
                holdings  = portfolio.get("holdings", [])
                holding   = _find_holding(holdings, alert.get("contract_address"))
                if not holding:
                    consume_pending_alert(alert_id)
                    edit("\\u26a0\\ufe0f Position already closed (an auto-exit likely already fired) -- nothing to execute.")
                else:
                    token_data      = validate_token(alert["contract_address"])
                    current_price   = token_data.get("price") or alert["price_at_signal"]
                    price_at_signal = alert.get("price_at_signal") or current_price
                    drift_pct = ((current_price - price_at_signal) / price_at_signal * 100) if price_at_signal else 0

                    entered_at   = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
                    held_hrs     = (datetime.now(timezone.utc) - entered_at).total_seconds() / 3600
                    created_at   = datetime.fromisoformat(alert["created_at"].replace("Z", "+00:00"))
                    exit_lag_min = (datetime.now(timezone.utc) - created_at).total_seconds() / 60

                    result = execute_fomo_sell(
                        holding["contract_address"],
                        current_price,
                        reason="tracker_sell_" + alert["wallet_alias"],
                        trader_held_hours=held_hrs,
                        exit_lag_minutes=exit_lag_min,
                    )
                    consume_pending_alert(alert_id)''',
)

# ── 6. /webhook/helius handler ─────────────────────────────────────────────────
add_edit(
    "helius webhook",
    '''        alias       = wallet_info["alias"]
        wallet_addr = wallet_info["wallet"]
        portfolio   = load_fomo_portfolio()
        holding     = portfolio.get("holding")
        parsed      = parse_helius_activity(tx, wallet_addr)
        if not parsed:
            continue

        if parsed["type"] == "SELL" and holding:
            held_contract = (holding.get("contract_address") or "").lower()
            if held_contract == parsed["contract"].lower():
                log.info("FOMO Solana: %s sold %s - awaiting human confirm", alias, holding["token_ticker"])
                token_data = validate_token(parsed["contract"])
                price_at_signal = token_data.get("price") or holding["entry_price"]
                alert_id = create_pending_sell_alert({
                    "token_ticker":     holding["token_ticker"],
                    "wallet_alias":     alias,
                    "contract_address": holding.get("contract_address"),
                    "price_at_signal":  price_at_signal,
                })
                send_telegram_button(
                    "\\U0001f514 <b>" + alias + " sold " + holding["token_ticker"] + "</b>\\n"
                    + "Tap to confirm your exit.",
                    "EXECUTE",
                    f"sell_exec:{alert_id}",
                )

        elif parsed["type"] == "BUY" and not holding:
            contract   = parsed["contract"]''',
    '''        alias       = wallet_info["alias"]
        wallet_addr = wallet_info["wallet"]
        portfolio   = load_fomo_portfolio()
        holdings    = portfolio.get("holdings", [])
        parsed      = parse_helius_activity(tx, wallet_addr)
        if not parsed:
            continue

        held_match = _find_holding(holdings, parsed.get("contract")) if parsed["type"] == "SELL" else None
        if parsed["type"] == "SELL" and held_match:
            holding = held_match
            log.info("FOMO Solana: %s sold %s - awaiting human confirm", alias, holding["token_ticker"])
            token_data = validate_token(parsed["contract"])
            price_at_signal = token_data.get("price") or holding["entry_price"]
            alert_id = create_pending_sell_alert({
                "token_ticker":     holding["token_ticker"],
                "wallet_alias":     alias,
                "contract_address": holding.get("contract_address"),
                "price_at_signal":  price_at_signal,
            })
            send_telegram_button(
                "\\U0001f514 <b>" + alias + " sold " + holding["token_ticker"] + "</b>\\n"
                + "Tap to confirm your exit.",
                "EXECUTE",
                f"sell_exec:{alert_id}",
            )

        elif (parsed["type"] == "BUY" and len(holdings) < FOMO_MAX_CONCURRENT_POSITIONS
              and not _find_holding(holdings, parsed.get("contract"))):
            contract   = parsed["contract"]''',
)

# ── 7. /webhook/alchemy handler ────────────────────────────────────────────────
add_edit(
    "alchemy webhook",
    '''        portfolio = load_fomo_portfolio()
        holding   = portfolio.get("holding")

        # ── SELL: tracked wallet selling a token we're holding ────────────────
        if parsed["type"] == "SELL" and holding:
            if (holding.get("contract_address") or "").lower() == parsed["contract"].lower():
                log.info(f"FOMO: {alias} sold {holding['token_ticker']} — awaiting human confirm")

                # Get a reference price now; re-checked again at the moment of execution
                token_data = validate_token(parsed["contract"])
                price_at_signal = token_data.get("price") or holding["entry_price"]

                alert_id = create_pending_sell_alert({
                    "token_ticker":     holding["token_ticker"],
                    "wallet_alias":     alias,
                    "contract_address": holding.get("contract_address"),
                    "price_at_signal":  price_at_signal,
                })
                send_telegram_button(
                    f"🔔 <b>{alias} sold {holding['token_ticker']}</b>\\n"
                    f"Tap to confirm your exit.",
                    "EXECUTE",
                    f"sell_exec:{alert_id}",
                )

        # ── BUY: tracked wallet buying something new ──────────────────────────
        elif parsed["type"] == "BUY" and not holding:
            contract = parsed["contract"]''',
    '''        portfolio = load_fomo_portfolio()
        holdings  = portfolio.get("holdings", [])

        # ── SELL: tracked wallet selling a token we're holding ────────────────
        held_match = _find_holding(holdings, parsed.get("contract")) if parsed["type"] == "SELL" else None
        if parsed["type"] == "SELL" and held_match:
            holding = held_match
            log.info(f"FOMO: {alias} sold {holding['token_ticker']} — awaiting human confirm")

            # Get a reference price now; re-checked again at the moment of execution
            token_data = validate_token(parsed["contract"])
            price_at_signal = token_data.get("price") or holding["entry_price"]

            alert_id = create_pending_sell_alert({
                "token_ticker":     holding["token_ticker"],
                "wallet_alias":     alias,
                "contract_address": holding.get("contract_address"),
                "price_at_signal":  price_at_signal,
            })
            send_telegram_button(
                f"🔔 <b>{alias} sold {holding['token_ticker']}</b>\\n"
                f"Tap to confirm your exit.",
                "EXECUTE",
                f"sell_exec:{alert_id}",
            )

        # ── BUY: tracked wallet buying something new ──────────────────────────
        elif (parsed["type"] == "BUY" and len(holdings) < FOMO_MAX_CONCURRENT_POSITIONS
              and not _find_holding(holdings, parsed.get("contract"))):
            contract = parsed["contract"]''',
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
        "\nMake sure fomo_multiposition_patch_portfolio.py has ALSO been run\n"
        "(same repo root) before committing -- these two files depend on each\n"
        "other's changes.\n"
        "\n"
        "  git add fomo_portfolio.py fomo_tracker.py\n"
        "  git commit -m \"Support multiple concurrent FOMO positions (cap 5)\"\n"
        "  git push origin master\n"
        "  git push origin main\n"
    )


if __name__ == "__main__":
    main()
