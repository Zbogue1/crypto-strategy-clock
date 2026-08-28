#!/usr/bin/env python3
"""
fomo_exit.py -- Automated exit monitor for FOMO Golem positions.

Runs as a background thread, checking all open positions every 5 minutes.
Executes exits based on a tiered tranche system:

  TRANCHE 1  — Position hits 2x entry price  → sell 33%, lock profit
  TRANCHE 2  — Position hits 3x entry price  → sell another 33%
  TRANCHE 3  — Final 33% rides until:
                 (a) Tracked wallet sells     → follow them out
                 (b) Trailing stop -30% from peak → protect gains
                 (c) Chart shows distribution  → technical exit
  STOP-LOSS  — Position drops -35% from entry → full exit, no exceptions

Telegram messages sent for every action so nothing is silent.
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

import requests
from fomo_wallet_stats import record_trade_outcome
from fomo_portfolio import run_fomo_postmortem, run_fomo_ai_postmortem

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# Exit thresholds
STOP_LOSS_PCT        = -0.35   # -35% from entry → full exit
TRANCHE_1_MULT       = 2.0     # 2x → sell 33%
TRANCHE_2_MULT       = 3.0     # 3x → sell 33%
TRAILING_STOP_PCT    = 0.30    # -30% from peak → exit final 33%
TRANCHE_SIZE         = 1 / 3   # each tranche is 33% of original units

POLL_INTERVAL_SEC    = 300     # check every 5 minutes
STARTUP_DELAY_SEC    = 120     # wait 2 min after Flask starts

# Proactive rug-pull detection (fires before the -35% stop-loss)
RUG_LIQUIDITY_DROP_PCT = 0.65   # liquidity fell ≥65% from peak → rug signal
RUG_PRICE_CRASH_PCT    = 0.50   # price fell ≥50% since last 5-min check → rug signal

_rug_warned_positions: set = set()
# contract -> consecutive failed exit attempts. A stop-loss that cannot
# execute leaves the position unprotected, which must not be forgotten
# between cycles.
_unprotected: dict = {}


# ─── TELEGRAM ─────────────────────────────────────────────────────────────────

def _send_telegram(text: str):
    """Delegates to the shared sender — see fomo_telegram.py.

    The previous inline version discarded the HTTP response, so any message
    Telegram rejected (commonly an HTML parse error from `<`, `>` or `&` in a
    token name) vanished with no log and no retry."""
    from fomo_telegram import send
    return send(text)


def _send_telegram_button_local(message: str, button_text: str, callback_data: str):
    """Send a Telegram message with a single inline button (local copy — no circular import)."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id":    TELEGRAM_CHAT_ID,
                "text":       message,
                "parse_mode": "HTML",
                "reply_markup": {
                    "inline_keyboard": [[
                        {"text": button_text, "callback_data": callback_data}
                    ]]
                },
            },
            timeout=10,
        )
    except Exception as e:
        log.error(f"Exit monitor Telegram button error: {e}")


# ─── STOP-LOSS ALARM ──────────────────────────────────────────────────────────
# Repeats every 90 seconds until the user taps "GOT IT" in Telegram, or 30 min
# has elapsed. Auto-execution has already happened; this alarm just ensures the
# user knows ASAP so they can mentally update their P&L.

_stop_alarms: dict = {}   # ack_id → threading.Event (set() to silence)

ALARM_INTERVAL_SEC = 90
ALARM_MAX_REPEATS  = 20   # 20 × 90s ≈ 30 minutes max


def _fire_stop_alarm(ticker: str, net_usd: float, gain_pct: float):
    """
    Immediately send an urgent Telegram alarm for a stop-loss and begin repeating
    it every 90 seconds until the user taps the silence button.
    The trade has already been auto-executed before this is called.
    """
    ack_id     = uuid.uuid4().hex[:8]
    stop_event = threading.Event()
    _stop_alarms[ack_id] = stop_event

    net_str  = f"${net_usd:.2f}"
    pct_str  = f"{gain_pct:.0f}%"
    msg = (
        f"🚨🚨🚨 <b>STOP-LOSS FIRED: {ticker}</b> 🚨🚨🚨\n"
        f"Down <b>{pct_str}</b> from entry — <b>AUTO-EXITED</b>\n"
        f"Recovered: <b>{net_str}</b>\n"
        f"⚠️ Tap to silence this alarm"
    )
    button = "🔕 GOT IT — SILENCE ALARM"

    def _alarm_loop():
        repeat = 0
        while not stop_event.is_set() and repeat < ALARM_MAX_REPEATS:
            _send_telegram_button_local(msg, button, f"ack_stop:{ack_id}")
            repeat += 1
            # Sleep 90 s, checking for stop every second so it cancels quickly
            for _ in range(ALARM_INTERVAL_SEC):
                if stop_event.is_set():
                    break
                time.sleep(1)
        if repeat >= ALARM_MAX_REPEATS and not stop_event.is_set():
            _send_telegram(
                f"🔕 Stop-loss alarm for {ticker} auto-silenced after "
                f"{ALARM_MAX_REPEATS * ALARM_INTERVAL_SEC // 60} minutes."
            )
        _stop_alarms.pop(ack_id, None)
        log.info(f"Stop alarm {ack_id} ({ticker}) finished after {repeat} ping(s)")

    threading.Thread(
        target=_alarm_loop, daemon=True, name=f"stop-alarm-{ack_id}"
    ).start()
    log.info(f"Stop-loss alarm started for {ticker} (ack_id={ack_id})")


def silence_stop_alarm(ack_id: str) -> bool:
    """
    Called by the Telegram webhook when the user taps the silence button.
    Returns True if the alarm was found and silenced, False if already gone.
    """
    event = _stop_alarms.get(ack_id)
    if event:
        event.set()
        log.info(f"Stop alarm {ack_id} silenced by user")
        return True
    return False


# ─── PRICE FETCHING ───────────────────────────────────────────────────────────

def _pairs_to_price_liq(pairs: list) -> tuple:
    """Pick the deepest pair and return (price, liquidity)."""
    if not pairs:
        return None, None
    best  = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd", 0) or 0))
    price = float(best.get("priceUsd", 0) or 0)
    liq   = float((best.get("liquidity") or {}).get("usd", 0) or 0)
    return (price if price > 0 else None), (liq if liq > 0 else None)


def get_prices_batch(contracts: list) -> dict:
    """
    Fetch prices for MANY tokens in one request.

    DexScreener's token endpoint accepts comma-separated addresses (30 max per
    call). Fetching one position at a time meant N requests per monitoring
    cycle, against an API this codebase already hits from the scanner, the
    narrative scan, token validation and discovery. At 6 positions we saw an
    83% failure rate — not load from position count, but cumulative pressure on
    a shared free endpoint.

    One request for all positions removes the exit monitor as a contributor.

    Returns {contract: (price, liquidity)} — missing keys mean no data.
    """
    out: dict = {}
    if not contracts:
        return out

    for i in range(0, len(contracts), 30):
        chunk = [c for c in contracts[i:i + 30] if c]
        if not chunk:
            continue
        url = "https://api.dexscreener.com/latest/dex/tokens/" + ",".join(chunk)
        for attempt in range(3):
            try:
                resp = requests.get(url, timeout=15)
                if resp.status_code == 200:
                    pairs = resp.json().get("pairs") or []
                    # Group returned pairs back to their token
                    by_token: dict = {}
                    for p in pairs:
                        for side in ("baseToken", "quoteToken"):
                            addr = (p.get(side) or {}).get("address")
                            if addr in chunk:
                                by_token.setdefault(addr, []).append(p)
                    for c in chunk:
                        out[c] = _pairs_to_price_liq(by_token.get(c, []))
                    break

                if resp.status_code == 429:
                    wait = 2 ** attempt
                    log.warning(
                        f"DexScreener rate limited (429) — backing off {wait}s "
                        f"[attempt {attempt+1}/3]"
                    )
                    time.sleep(wait)
                    continue

                # Log the actual status. Silently returning None on any non-200
                # made rate limiting indistinguishable from a dead token.
                log.warning(
                    f"DexScreener batch HTTP {resp.status_code} for "
                    f"{len(chunk)} token(s): {resp.text[:120]}"
                )
                break
            except Exception as e:
                log.warning(f"DexScreener batch attempt {attempt+1} failed: {e}")
                time.sleep(1)

    return out


def _get_price_and_liquidity(contract: str, chain: str = "solana") -> tuple:
    """
    Single-token fetch. Prefer get_prices_batch() when checking several.
    Kept for callers that genuinely need one token.
    """
    got = get_prices_batch([contract])
    return got.get(contract, (None, None))


def _get_current_price(contract: str, chain: str = "solana") -> Optional[float]:
    """Compatibility wrapper — returns price only."""
    price, _ = _get_price_and_liquidity(contract, chain)
    return price


# ─── CHART EXIT CHECK ─────────────────────────────────────────────────────────

def _chart_says_exit(contract: str, chain: str = "solana") -> bool:
    """Quick chart exit check — returns True if distribution/wall detected."""
    try:
        from fomo_chart import analyze_chart, chart_should_exit
        sig = analyze_chart(contract, chain=chain)
        return chart_should_exit(sig)
    except Exception:
        return False


# ─── PARTIAL SELL ─────────────────────────────────────────────────────────────

# ─── MONITOR LOAD HEALTH ──────────────────────────────────────────────────────
# Every open position costs one DexScreener price fetch per 5-minute cycle. At
# 20+ positions that's 240+ calls/hour from the exit monitor alone, on top of
# the scanners. DexScreener rate-limits (we already see 429s elsewhere), and a
# throttled price fetch means a stop-loss silently doesn't fire — the position
# just isn't checked that cycle. This warns before that becomes invisible.
import time as _time

MONITOR_WARN_THRESHOLD = int(os.getenv("FOMO_MONITOR_WARN_AT", "20"))
MONITOR_FAIL_PCT_WARN  = float(os.getenv("FOMO_MONITOR_FAIL_PCT", "0.20"))
MONITOR_SLOW_CYCLE_SEC = float(os.getenv("FOMO_MONITOR_SLOW_SEC", "240"))

_load_stats = {"checked": 0, "price_fail": 0}

# contract -> consecutive cycles with no price. Survives across cycles on
# purpose: the aggregate counter is reset every cycle, so a token failing
# every single time looks the same as five different tokens failing once each.
_consec_price_fail: dict = {}
PRICE_FAIL_ALARM = int(os.getenv("FOMO_PRICE_FAIL_ALARM", "3"))

# Stale-position reviews that come back HOLD are recorded but not pushed by
# default. A notification every 48h per quiet position is how alerts get
# ignored — and an ignored alert is worse than none, because it looks like
# coverage. Set FOMO_NOTIFY_HOLD_REVIEWS=true to see every review.
NOTIFY_HOLD_REVIEWS = os.getenv("FOMO_NOTIFY_HOLD_REVIEWS", "false").lower() == "true"
_load_warned_at = 0          # highest position count already warned about
_last_load_warning = 0.0     # timestamp, for cooldown
LOAD_WARN_COOLDOWN = 6 * 3600


def _check_monitor_load(n_positions: int):
    """Warn once per 10-position tier when the monitoring workload grows."""
    global _load_warned_at, _last_load_warning
    if n_positions < MONITOR_WARN_THRESHOLD:
        return

    tier = (n_positions // 10) * 10
    now  = _time.time()
    if tier <= _load_warned_at and (now - _last_load_warning) < LOAD_WARN_COOLDOWN:
        return

    _load_warned_at    = tier
    _last_load_warning = now

    calls_hr = n_positions * 12   # one fetch per position per 5-min cycle
    _send_telegram(
        f"📊 <b>Monitoring load check — {n_positions} open positions</b>\n\n"
        f"The exit monitor now makes ~<b>{calls_hr:,} DexScreener calls/hour</b> "
        f"just to price your positions, plus scanner traffic.\n\n"
        f"<b>What to watch for:</b>\n"
        f"• 429 rate-limit warnings in the logs\n"
        f"• Cycles taking longer than the 5-minute interval\n"
        f"• Positions whose price hasn't updated in several cycles\n\n"
        f"<i>A throttled price fetch means that position isn't checked that "
        f"cycle — a stop-loss can be late. I'll alert if failures climb.</i>"
    )
    log.warning(f"Monitor load: {n_positions} positions, ~{calls_hr}/hr price calls")


def _report_cycle_health(elapsed: float):
    """After a cycle, warn if price fetches are failing or the cycle is slow."""
    global _last_load_warning
    checked = _load_stats.get("checked", 0)
    failed  = _load_stats.get("price_fail", 0)
    if checked < 5:
        return

    fail_pct = failed / checked
    now = _time.time()
    if (now - _last_load_warning) < LOAD_WARN_COOLDOWN:
        return

    problems = []
    if fail_pct >= MONITOR_FAIL_PCT_WARN:
        problems.append(
            f"• <b>{failed}/{checked} price fetches failed</b> ({fail_pct:.0%}) — "
            f"those positions were NOT checked for stops this cycle"
        )
    if elapsed > MONITOR_SLOW_CYCLE_SEC:
        problems.append(
            f"• <b>Cycle took {elapsed/60:.1f} min</b> — approaching or exceeding "
            f"the 5-minute interval, so cycles may be overlapping"
        )
    if not problems:
        return

    _last_load_warning = now
    _send_telegram(
        f"⚠️ <b>Exit monitor degraded</b>\n\n" + "\n".join(problems) +
        f"\n\n<i>Stop-losses and tranche exits may be delayed. Consider reducing "
        f"position count or raising the monitor interval.</i>"
    )
    log.error(f"Monitor health: {failed}/{checked} failed, cycle {elapsed:.0f}s")


def _execute_partial_sell(
    holding: dict,
    fraction: float,
    current_price: float,
    reason: str,
    state: dict,
    flags: list = None,
) -> float:
    """
    Sell `fraction` of the holding's remaining units.
    Updates holding in-place. Returns USD proceeds.
    state is the live portfolio dict (mutated directly).
    """
    from fomo_portfolio import FOMO_TAKER_FEE, save_fomo_portfolio, sync_fomo_state_to_github

    # FLAGS ARE SET HERE, ONLY ON A COMPLETED SALE — never by the caller.
    #
    # The old design had the caller set tranche_1_sold BEFORE calling, so that
    # the flag and the sale saved together and the tranche couldn't fire twice
    # (GTA6 exited four times before that). It fixed double-firing and created
    # something worse: every early return below left the position MARKED as
    # harvested while holding 100% of its units, with no record and no way for
    # the tranche to ever fire again.
    #
    # $MADE ran to 2x, was flagged, sold nothing, and round-tripped the entire
    # move. The Telegram alert still said "TRANCHE 1 EXIT — Locked: $0.00".
    #
    # Setting the flag inside, immediately before the same save that writes the
    # sale and the record, keeps the double-fire protection AND makes it
    # impossible to be flagged without having sold. That is the invariant:
    # flag set <=> units sold <=> record written, all in one save.
    def _abort(why: str) -> float:
        log.error(f"Tranche sale ABORTED for {holding.get('token_ticker','?')} "
                  f"({reason}): {why} — nothing flagged, will retry next cycle")
        try:
            _send_telegram(
                f"⚠️ <b>{holding.get('token_ticker','?')}: tranche sale failed</b>\n\n"
                f"{why}\n\n"
                f"<i>Nothing was sold and nothing was flagged, so it retries "
                f"next cycle. The position still holds all its units.</i>")
        except Exception:
            pass
        return 0.0

    units_to_sell = holding["units"] * fraction
    if units_to_sell <= 0:
        return _abort(f"nothing to sell (units={holding.get('units')})")

    # Reject impossible prices before they corrupt the portfolio
    from fomo_portfolio import is_price_sane
    if not is_price_sane(holding.get("entry_price"), current_price,
                         holding.get("token_ticker", "?")):
        return _abort(f"price ${current_price:.8f} failed the sanity check "
                      f"against entry ${holding.get('entry_price', 0):.8f}")

    proceeds = units_to_sell * current_price
    fee      = proceeds * FOMO_TAKER_FEE
    net      = proceeds - fee

    # Update holding — reduce units in place
    holding["units"]        -= units_to_sell
    holding["spent"]        *= (1 - fraction)   # proportional cost basis reduction
    holding["peak_price"]    = max(holding.get("peak_price", current_price), current_price)

    state["cash"] += net

    # ── LOG THE HARVEST ───────────────────────────────────────────────────
    # Tranche sells previously wrote no record anywhere. The money reached
    # cash, so totals stayed correct — but the trade log showed "7 closed,
    # all losses" while a winner was being banked in the background, and
    # there was no way to measure whether the tranche system works at all.
    #
    # Kept in a separate list rather than trade_history so win/loss counts
    # for FULL exits stay comparable, while the harvests remain visible.
    cost_of_slice = holding.get("spent", 0) * fraction / max(1 - fraction, 1e-9) \
        if fraction < 1 else holding.get("spent", 0)
    state.setdefault("tranche_sales", []).append({
        "token_ticker":  holding.get("token_ticker", "?"),
        "contract":      holding.get("contract_address", ""),
        "reason":        reason,
        "fraction":      round(fraction, 4),
        "units_sold":    units_to_sell,
        "price":         current_price,
        "entry_price":   holding.get("entry_price", 0),
        "proceeds":      round(net, 2),
        "cost_basis":    round(cost_of_slice, 2),
        "profit":        round(net - cost_of_slice, 2),
        "gain_x":        round(current_price / holding["entry_price"], 2)
                         if holding.get("entry_price") else 0,
        "wallet_alias":  holding.get("wallet_alias", ""),
        "sold_at":       datetime.now(timezone.utc).isoformat(),
    })

    entry   = holding["entry_price"]
    gain_x  = current_price / entry if entry > 0 else 1
    gain_pct = (gain_x - 1) * 100

    log.info(
        f"EXIT [{reason}] {holding['token_ticker']}: "
        f"sold {fraction*100:.0f}% @ ${current_price:.8f} "
        f"({gain_pct:+.0f}%) → +${net:.2f}"
    )

    # Flag ONLY now — the sale is done, the record is written, and all of it
    # persists in the single save below.
    for f in (flags or []):
        holding[f] = True

    save_fomo_portfolio(state)
    sync_fomo_state_to_github()
    return net


def _execute_full_sell(
    holding: dict,
    current_price: float,
    reason: str,
    state: dict,
) -> float:
    """Full exit — removes holding from portfolio entirely."""
    from fomo_portfolio import (FOMO_TAKER_FEE, save_fomo_portfolio,
                                sync_fomo_state_to_github, is_price_sane)

    # THIS IS THE STOP-LOSS PATH. A silent abort here is the worst failure in
    # the system: the position is NOT sold, keeps falling, and the caller
    # previously fired the "AUTO-EXITED" alarm anyway with net=$0.00 — telling
    # you a losing position had been closed when it was still open and bleeding.
    #
    # Refusing to sell on a bad price is correct: selling at a garbage price
    # realises a fictional loss. But it must SCREAM, and it must keep screaming
    # while the position sits unprotected.
    ticker = holding.get("token_ticker", "?")
    if not is_price_sane(holding.get("entry_price"), current_price, ticker):
        n = _unprotected.get(holding.get("contract_address"), 0) + 1
        _unprotected[holding.get("contract_address")] = n
        log.error(f"{reason.upper()} COULD NOT EXECUTE for {ticker}: price "
                  f"${current_price:.8f} vs entry "
                  f"${holding.get('entry_price', 0):.8f} failed sanity check. "
                  f"POSITION REMAINS OPEN AND UNPROTECTED (attempt {n}).")
        try:
            _send_telegram(
                f"🚨 <b>{ticker}: {reason.upper()} DID NOT EXECUTE</b>\n\n"
                f"Price ${current_price:.8f} failed the sanity check against "
                f"entry ${holding.get('entry_price', 0):.8f}.\n\n"
                f"<b>The position is still open and unprotected.</b> "
                f"Failed attempts: {n}\n\n"
                f"<i>Selling at a bad price would realise a fictional loss, so "
                f"nothing was sold. If this repeats, check the token on "
                f"DexScreener and consider exiting manually.</i>")
        except Exception:
            pass
        return 0.0

    _unprotected.pop(holding.get("contract_address"), None)

    proceeds = holding["units"] * current_price
    fee      = proceeds * FOMO_TAKER_FEE
    net      = proceeds - fee

    entry     = holding["entry_price"]
    gain_x    = current_price / entry if entry > 0 else 1
    gain_pct  = (gain_x - 1) * 100

    state["cash"] += net
    state["total_trades"] = state.get("total_trades", 0) + 1
    if gain_pct > 0:
        state["winning_trades"] = state.get("winning_trades", 0) + 1

    # Record in trade history
    state.setdefault("trade_history", []).append({
        **holding,
        "exit_price":   current_price,
        "exit_reason":  reason,
        "pnl_usd":      round(net - holding.get("spent", 0), 2),
        "pnl_pct":      round(gain_pct, 2),
        "exited_at":    datetime.now(timezone.utc).isoformat(),
    })

    state["holdings"] = [h for h in state["holdings"] if h.get("position_id") != holding.get("position_id")]

    log.info(
        f"FULL EXIT [{reason}] {holding['token_ticker']}: "
        f"@ ${current_price:.8f} ({gain_pct:+.0f}%) → +${net:.2f}"
    )

    save_fomo_portfolio(state)
    sync_fomo_state_to_github()

    # Start watching what this token does AFTER we sold. Without this, an exit
    # at 3x scores identically whether the token then died or ran to 20x — so
    # the tranche levels can never be shown to be too tight or too loose.
    try:
        from fomo_aftermath import record_exit
        record_exit(holding, current_price, reason, entry)
    except Exception as e:
        log.warning(f"Aftermath: could not record exit for "
                    f"{holding.get('token_ticker')}: {e}")

    # Record outcome against the wallet that triggered this trade
    try:
        record_trade_outcome(
            alias       = holding.get("wallet_alias", "unknown"),
            token       = holding.get("token_ticker", "?"),
            win         = gain_pct > 0,
            pnl_pct     = round(gain_pct, 2),
            pnl_usd     = round(net - holding.get("spent", 0), 2),
            exit_reason = reason,
        )
    except Exception as e:
        log.warning(f"WalletStats: record_trade_outcome failed: {e}")

    # Run postmortem — rule-based immediately, AI analysis in background thread
    try:
        # Build trade record for postmortem
        trade_record = {
            **holding,
            "exit_price":  current_price,
            "exit_reason": reason,
            "profit_pct":  round(gain_pct, 2),
            "pnl_usd":     round(net - holding.get("spent", 0), 2),
            "exited_at":   datetime.now(timezone.utc).isoformat(),
        }
        run_fomo_postmortem(trade_record)

        # AI postmortem runs in background so it never delays the exit
        def _ai_pm():
            try:
                run_fomo_ai_postmortem(trade_record)
            except Exception as e:
                log.warning(f"AI postmortem background error: {e}")
        threading.Thread(target=_ai_pm, daemon=True, name="fomo-ai-postmortem").start()

    except Exception as e:
        log.warning(f"Postmortem failed (non-fatal): {e}")

    return net


# ─── CONDITION CHECKS ─────────────────────────────────────────────────────────

def _check_holding(holding: dict, state: dict, prefetched: tuple = None):
    """
    Evaluate all exit conditions for one holding.
    Mutates holding and state in-place when action taken.
    """
    contract = holding.get("contract_address")
    ticker   = holding.get("token_ticker", "?")
    chain    = holding.get("chain", "solana")

    if not contract:
        return

    _load_stats["checked"] = _load_stats.get("checked", 0) + 1
    # Use the batched price when available — avoids a second request per position
    if prefetched and prefetched[0]:
        current_price, current_liq = prefetched
    else:
        current_price, current_liq = _get_price_and_liquidity(contract, chain)
    if not current_price:
        # Not a harmless skip: this position went unchecked for stops this cycle
        _load_stats["price_fail"] = _load_stats.get("price_fail", 0) + 1
        log.debug(f"Exit monitor: couldn't fetch price for {ticker}")

        # One miss is API noise. Repeated misses on the SAME token are not —
        # DexScreener stops returning pairs when liquidity is pulled, so the
        # position most likely to be going to zero is exactly the one we can
        # no longer price. The aggregate "1/5 failed" warning cannot see this
        # because it forgets which token failed the moment the cycle ends.
        n = _consec_price_fail.get(contract, 0) + 1
        _consec_price_fail[contract] = n
        if n == PRICE_FAIL_ALARM:
            _send_telegram(
                f"🚨 <b>{ticker}: no price data for {n} straight cycles</b>\n\n"
                f"Its stop-loss has not been evaluated in ~{n * 5} minutes.\n\n"
                f"<i>DexScreener usually stops returning pairs when liquidity is "
                f"pulled. Check this one manually — a token that can't be priced "
                f"is the one most likely to be going to zero.</i>"
            )
            log.error(f"Exit monitor: {ticker} unpriceable for {n} cycles — "
                      f"stop-loss not enforced")
        return

    # Priced successfully — clear any failure streak.
    _consec_price_fail.pop(contract, None)

    entry_price = holding.get("entry_price", 0)
    if not entry_price:
        return

    # Update peak price
    peak = holding.get("peak_price", entry_price)
    if current_price > peak:
        peak = current_price
        holding["peak_price"] = peak

    # Update peak liquidity (for rug detection)
    if current_liq:
        peak_liq = holding.get("peak_liquidity", current_liq)
        if current_liq > peak_liq:
            peak_liq = current_liq
        holding["peak_liquidity"] = peak_liq
    else:
        peak_liq = None

    gain_x   = current_price / entry_price
    gain_pct = (gain_x - 1) * 100

    # ── PRICE SANITY CHECK ────────────────────────────────────────────────
    # If DexScreener returns a price implying an impossible gain, the data is
    # corrupt or from the wrong pair. Skip this cycle entirely rather than
    # triggering phantom tranche exits with million-percent gains.
    from fomo_portfolio import MAX_REALISTIC_GAIN_X
    if gain_x > MAX_REALISTIC_GAIN_X:
        log.warning(
            f"Exit monitor {ticker}: suspicious price ${current_price:.8f} "
            f"implies {gain_x:.0f}x gain — likely bad DexScreener data, skipping cycle"
        )
        return

    # ── PROACTIVE RUG DETECTION (advisory — fires before stop-loss) ────────
    # Detects rug-pull signatures: liquidity collapse or sudden single-check
    # price crash. Sends a warning with a manual SELL button so the user can
    # exit before waiting for the -35% stop-loss to trigger automatically.
    position_id = holding.get("position_id", contract[:12])
    if position_id not in _rug_warned_positions:
        rug_reason = None
        if (peak_liq and current_liq
                and current_liq < peak_liq * (1 - RUG_LIQUIDITY_DROP_PCT)):
            rug_reason = (
                f"Liquidity collapsed: "
                f"${current_liq:,.0f} (was ${peak_liq:,.0f}, "
                f"-{(1 - current_liq/peak_liq)*100:.0f}%)"
            )
        last_price = holding.get("last_price_check")
        if (last_price and current_price < last_price * (1 - RUG_PRICE_CRASH_PCT)):
            rug_reason = rug_reason or (
                f"Price crashed {(1 - current_price/last_price)*100:.0f}% "
                f"in one check (${last_price:.8f} → ${current_price:.8f})"
            )
        if rug_reason:
            log.warning(f"Rug risk detected for {ticker}: {rug_reason}")
            _rug_warned_positions.add(position_id)

            # Log this event into the holding — postmortem reads it later to learn
            # whether acting on this warning would have saved money
            holding.setdefault("warning_events", []).append({
                "type":           "rug_risk_detector",
                "timestamp":      datetime.now(timezone.utc).isoformat(),
                "trigger":        rug_reason,
                "gain_pct":       round(gain_pct, 2),
                "liquidity_usd":  current_liq,
                "peak_liquidity": peak_liq,
            })

            # Golem's short recommendation — context-aware based on trigger + P&L
            is_liq_collapse = "Liquidity" in rug_reason
            if gain_pct > 5:
                advice = "You're still in profit. Sell now and keep the gain — this rarely recovers."
            elif gain_pct > 0:
                advice = "Barely green. Take it. Waiting for the stop-loss will cost you more."
            elif is_liq_collapse:
                advice = "Liquidity is gone. Price will follow. Exit now before it goes to zero."
            else:
                advice = "Price is in freefall. Cut the loss here — stop-loss is your floor, not your target."

            _send_telegram_button_local(
                f"🚩🚩 <b>RUG RISK DETECTED: {ticker}</b> 🚩🚩\n"
                f"{rug_reason}\n"
                f"📊 P&amp;L: <b>{gain_pct:+.0f}%</b> from entry\n"
                f"🧠 <i>{advice}</i>",
                "🚨 SELL NOW",
                f"rug_sell:{contract}",
            )

    # Track last seen price (for rug crash detection next cycle)
    holding["last_price_check"] = current_price

    # ── STOP-LOSS: -35% from entry ─────────────────────────────────────────
    if gain_pct <= STOP_LOSS_PCT * 100:
        net = _execute_full_sell(holding, current_price, "stop_loss", state)
        if net > 0:
            _fire_stop_alarm(ticker, net, gain_pct)   # repeats until user ACKs
        # If net == 0 the sale was refused and _execute_full_sell has already
        # alarmed. Firing the "AUTO-EXITED / Recovered $0.00" alarm here would
        # tell the user a still-open position had been closed.
        return

    # ── TRANCHE 1: 2x → sell 33% ──────────────────────────────────────────
    if gain_x >= TRANCHE_1_MULT and not holding.get("tranche_1_sold"):
        net = _execute_partial_sell(holding, TRANCHE_SIZE, current_price,
                                    "tranche_1_2x", state,
                                    flags=["tranche_1_sold"])
        # Only claim an exit that actually happened. This message used to fire
        # unconditionally and reported "Locked: $0.00" on a sale that aborted.
        if net > 0:
            _send_telegram(
                f"💸 <b>TRANCHE 1 EXIT: {ticker}</b> hit 2x\n"
                f"Sold 33% @ ${current_price:.8f} (+{gain_pct:.0f}%)\n"
                f"Locked: ${net:.2f} | Remaining 67% riding 🚀"
            )
        return

    # ── TRANCHE 2: 3x → sell another 33% ─────────────────────────────────
    if gain_x >= TRANCHE_2_MULT and holding.get("tranche_1_sold") and not holding.get("tranche_2_sold"):
        net = _execute_partial_sell(holding, 0.5, current_price,
                                    "tranche_2_3x", state,
                                    flags=["tranche_2_sold", "trailing_stop_active"])
        if net > 0:
            _send_telegram(
                f"💸 <b>TRANCHE 2 EXIT: {ticker}</b> hit 3x\n"
                f"Sold another 33% @ ${current_price:.8f} (+{gain_pct:.0f}%)\n"
                f"Locked: ${net:.2f} | Final 33% riding — trailing stop armed at -30% from peak 🎯"
            )
        return

    # ── TRAILING STOP on final 33% ────────────────────────────────────────
    # The final third is the piece meant to catch a real run, and a flat 30%
    # stop shakes it out on ordinary mid-run noise — that is what closed CATE.
    # Before firing, ask whether the evidence says this is still running:
    # on-chain momentum plus any screenshot the user forwarded. Strictly
    # guarded — see fomo_runner for why each guard exists.
    if holding.get("trailing_stop_active") and peak > 0:
        drop_from_peak = (current_price - peak) / peak
        trailing = TRAILING_STOP_PCT
        decision = None

        try:
            from fomo_runner import (evaluate_runner, apply_extension,
                                     format_extension)
            decision = evaluate_runner(holding, current_price, peak,
                                       TRAILING_STOP_PCT, current_liq or 0)
            if decision.get("hard_exit"):
                net = _execute_full_sell(holding, current_price,
                                         "runner_hard_floor", state)
                _send_telegram(
                    f"🛑 <b>{ticker} — hard exit</b>\n"
                    f"{decision['reason']}\n"
                    f"Recovered: ${net:.2f}"
                )
                return
            trailing = decision["trailing_pct"]
            if decision.get("extended") and not holding.get("runner_extension_until"):
                apply_extension(holding, decision)
                _send_telegram(format_extension(ticker, decision, gain_x))
            elif decision.get("extended"):
                apply_extension(holding, decision)
        except Exception as e:
            # Any failure here falls back to the standard stop. A broken
            # momentum check must never leave a position unprotected.
            log.error(f"Runner evaluation failed for {ticker}: {e} — "
                      f"using standard {TRAILING_STOP_PCT*100:.0f}% stop")
            trailing = TRAILING_STOP_PCT

        if drop_from_peak <= -trailing:
            net = _execute_full_sell(holding, current_price, "trailing_stop", state)
            extra = ""
            if decision and decision.get("extended"):
                extra = (f"\n<i>Stop had been widened to {trailing*100:.0f}% "
                         f"on momentum — it still broke.</i>")
            _send_telegram(
                f"📉 <b>TRAILING STOP: {ticker}</b>\n"
                f"Dropped {drop_from_peak*100:.0f}% from peak "
                f"(stop {trailing*100:.0f}%) — final 33% auto-exited.\n"
                f"Recovered: ${net:.2f}{extra}"
            )
            return

    # ── CHART EXIT SIGNAL on final 33% ───────────────────────────────────
    if holding.get("tranche_2_sold") and _chart_says_exit(contract, chain):
        # Log chart warning event before executing — captured in postmortem
        holding.setdefault("warning_events", []).append({
            "type":      "chart_distribution",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "trigger":   "Distribution/wall pattern detected by chart analysis",
            "gain_pct":  round(gain_pct, 2),
        })
        net = _execute_full_sell(holding, current_price, "chart_distribution", state)
        _send_telegram(
            f"📊 <b>CHART EXIT: {ticker}</b>\n"
            f"Distribution/wall pattern detected — final 33% auto-exited.\n"
            f"Proceeds: ${net:.2f} | Gain: {gain_pct:+.0f}%"
        )
        return

    # ── STALE POSITION REVIEW ─────────────────────────────────────────────
    # Nothing above fired, which means this position is neither winning enough
    # to tranche nor losing fast enough to stop out. That is exactly the state
    # a position can sit in indefinitely, quietly holding capital.
    #
    # A timer would sell it on the calendar. Instead re-decide it: is the
    # reason we bought still true? Ends in a recommendation, never a sale.
    try:
        from fomo_review import needs_review, review_position, format_review
        due, why = needs_review(holding, current_price)
        if due:
            log.info(f"Exit monitor: {ticker} due for review — {why}")
            r = review_position(holding, current_price, current_liq)
            holding["last_reviewed_at"] = datetime.now(timezone.utc).isoformat()
            holding.setdefault("reviews", []).append({
                "at":       holding["last_reviewed_at"],
                "verdict":  r.get("verdict"),
                "thesis":   r.get("thesis_status"),
                "pct":      r.get("pct"),
                "reason":   (r.get("reasoning") or "")[:300],
            })
            # Only interrupt for an actionable call. A HOLD verdict is logged
            # for the record but not pushed — otherwise every quiet position
            # generates a notification every two days and he stops reading them.
            if r.get("verdict") in ("EXIT", "TRIM"):
                _send_telegram_button_local(
                    format_review(r), "SELL NOW", f"rug_sell:{contract}")
            elif NOTIFY_HOLD_REVIEWS:
                _send_telegram(format_review(r))
            else:
                log.info(f"Exit monitor: {ticker} review -> "
                         f"{r.get('verdict')} ({r.get('thesis_status')}), "
                         f"logged not sent")
    except Exception as e:
        log.error(f"Exit monitor: review failed for {ticker}: {e}")

    log.debug(
        f"Exit monitor {ticker}: {gain_pct:+.1f}% | "
        f"T1={'✅' if holding.get('tranche_1_sold') else '⏳'} "
        f"T2={'✅' if holding.get('tranche_2_sold') else '⏳'} "
        f"peak={peak:.8f}"
    )


# ─── MAIN MONITOR LOOP ────────────────────────────────────────────────────────

def run_exit_checks():
    """Single pass — check all open holdings for exit conditions."""
    from fomo_portfolio import load_fomo_portfolio, save_fomo_portfolio, sync_fomo_state_from_github

    sync_fomo_state_from_github()
    state    = load_fomo_portfolio()
    holdings = state.get("holdings", [])

    if not holdings:
        log.debug("Exit monitor: no open positions")
        return

    log.info(f"Exit monitor: checking {len(holdings)} position(s)")

    # Load-health check — see _check_monitor_load() for why this matters
    _check_monitor_load(len(holdings))
    _load_stats["checked"] = 0
    _load_stats["price_fail"] = 0
    _cycle_start = _time.time()

    # ONE request for every position's price instead of one per position.
    contracts = [h.get("contract_address") for h in holdings if h.get("contract_address")]
    price_map = get_prices_batch(contracts)
    got = sum(1 for v in price_map.values() if v[0])
    log.info(f"Exit monitor: batch price fetch — {got}/{len(contracts)} resolved")

    # Iterate over a copy — holdings may be removed during iteration
    for holding in list(holdings):
        # Re-check that position still exists in state (may have been closed)
        if not any(h.get("position_id") == holding.get("position_id") for h in state["holdings"]):
            continue
        try:
            _check_holding(holding, state,
                           prefetched=price_map.get(holding.get("contract_address")))
            # Prices came from one batched call, so no per-token pacing needed.
            # A short pause still keeps chart/rug lookups from bunching up.
            time.sleep(0.5)
        except Exception as e:
            log.error(f"Exit monitor error on {holding.get('token_ticker','?')}: {e}")

    # Cycle finished — check whether the workload is degrading monitoring
    _report_cycle_health(_time.time() - _cycle_start)


def start_exit_monitor() -> threading.Thread:
    """
    Start the background exit monitor thread.
    Polls all open positions every 5 minutes.
    """
    def _loop():
        log.info(f"Exit monitor started (checking every {POLL_INTERVAL_SEC//60} min)")
        time.sleep(STARTUP_DELAY_SEC)
        while True:
            try:
                run_exit_checks()
            except Exception as e:
                log.error(f"Exit monitor loop error: {e}")
            time.sleep(POLL_INTERVAL_SEC)

    t = threading.Thread(target=_loop, daemon=True, name="fomo-exit-monitor")
    t.start()
    return t


# ─── TRANCHE-AWARE SELL (called when tracked wallet sells) ────────────────────

def handle_tracker_sell(contract: str, current_price: float, ticker: str = "?") -> Optional[float]:
    """
    Called when a tracked wallet sells a token we're holding.
    Exits only the remaining position (respects already-sold tranches).
    Returns USD net proceeds or None if not holding.
    """
    from fomo_portfolio import load_fomo_portfolio, sync_fomo_state_from_github

    sync_fomo_state_from_github()
    state    = load_fomo_portfolio()
    holdings = state.get("holdings", [])
    holding  = next((h for h in holdings if (h.get("contract_address") or "") == contract), None)

    if not holding:
        return None

    entry    = holding.get("entry_price", 0)
    gain_pct = ((current_price / entry) - 1) * 100 if entry > 0 else 0

    net = _execute_full_sell(holding, current_price, "tracker_sold", state)

    t1 = holding.get("tranche_1_sold", False)
    t2 = holding.get("tranche_2_sold", False)

    if t1 and t2:
        msg = (
            f"🏁 <b>FINAL EXIT: {ticker}</b> — tracker sold\n"
            f"Final 33% closed @ {gain_pct:+.0f}%\n"
            f"Proceeds: ${net:.2f} | All tranches complete ✅"
        )
    elif t1:
        msg = (
            f"🏁 <b>EXIT: {ticker}</b> — tracker sold\n"
            f"Remaining 67% closed @ {gain_pct:+.0f}%\n"
            f"Proceeds: ${net:.2f}"
        )
    else:
        msg = (
            f"🏁 <b>FULL EXIT: {ticker}</b> — tracker sold before 2x\n"
            f"Closed full position @ {gain_pct:+.0f}%\n"
            f"Proceeds: ${net:.2f} | Following trader's early exit"
        )
    _send_telegram(msg)
    return net
