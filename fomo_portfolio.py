#!/usr/bin/env python3
"""
fomo_portfolio.py — FOMO copy-trade portfolio tracker.

Manages the $500 FOMO allocation separately from the main swing trade portfolio.
Tracks quick trades (typically <24h), runs deep post-mortems on every trade,
builds per-wallet lesson profiles, and maintains a 20-trade graduation framework.

Architecture:
  - fomo_portfolio.json  : live state (cash, holding, history)
  - fomo_lessons.json    : per-wallet lesson profiles built from post-mortems
  - trusted_wallets.json : wallet registry (managed by fomo_wallet_manager.py)
"""

import json
import logging
import os
from datetime import datetime, timezone, timedelta
from typing import Optional

import anthropic
import requests

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY  = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL           = "claude-opus-4-6"
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

FOMO_PORTFOLIO_FILE  = "fomo_portfolio.json"
FOMO_LESSONS_FILE    = "fomo_lessons.json"
FOMO_STARTING_CASH   = 500.0
FOMO_MAX_POSITION_PCT = 0.30   # max 30% of FOMO cash per trade
FOMO_TAKER_FEE       = 0.001   # 0.1% per side
FOMO_AUTO_EXIT_HOURS = 24      # auto-exit if original trader hasn't sold
FOMO_HARD_STOP_PCT   = -0.15   # -15% hard stop

# Graduation thresholds
GRAD_MIN_TRADES      = 20
GRAD_MIN_WIN_RATE    = 55.0
GRAD_MIN_AVG_RETURN  = 3.0     # avg % return per trade
GRAD_MIN_DAYS        = 20      # days running (shorter than main — quicker trades)
GRAD_MAX_DRAWDOWN    = 20.0    # peak-to-trough % drawdown allowed


# ─── STATE MANAGEMENT ─────────────────────────────────────────────────────────

def _default_state() -> dict:
    return {
        "cash":           FOMO_STARTING_CASH,
        "starting_cash":  FOMO_STARTING_CASH,
        "holding":        None,
        "trade_history":  [],
        "total_trades":   0,
        "winning_trades": 0,
        "peak_value":     FOMO_STARTING_CASH,
        "max_drawdown":   0.0,
        "started_at":     datetime.now(timezone.utc).isoformat(),
        "last_updated":   datetime.now(timezone.utc).isoformat(),
    }


def load_fomo_portfolio() -> dict:
    if os.path.exists(FOMO_PORTFOLIO_FILE):
        try:
            with open(FOMO_PORTFOLIO_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return _default_state()


def save_fomo_portfolio(state: dict):
    state["last_updated"] = datetime.now(timezone.utc).isoformat()
    with open(FOMO_PORTFOLIO_FILE, "w") as f:
        json.dump(state, f, indent=2, default=str)


def load_fomo_lessons() -> dict:
    if os.path.exists(FOMO_LESSONS_FILE):
        try:
            with open(FOMO_LESSONS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"wallets": {}, "global": [], "last_rebuilt": None}


def save_fomo_lessons(lessons: dict):
    lessons["last_rebuilt"] = datetime.now(timezone.utc).isoformat()
    with open(FOMO_LESSONS_FILE, "w") as f:
        json.dump(lessons, f, indent=2, default=str)


# ─── PORTFOLIO VALUE ──────────────────────────────────────────────────────────

def get_fomo_value(current_price: float = None) -> dict:
    state   = load_fomo_portfolio()
    cash    = state["cash"]
    holding = state.get("holding")

    position_value = 0.0
    unrealized_pct = 0.0
    if holding:
        price          = current_price or holding["entry_price"]
        position_value = holding["units"] * price
        unrealized_pct = (price - holding["entry_price"]) / holding["entry_price"] * 100

    total            = cash + position_value
    total_return_pct = (total - FOMO_STARTING_CASH) / FOMO_STARTING_CASH * 100
    n                = state["total_trades"]
    wins             = state["winning_trades"]

    return {
        "cash":             round(cash, 2),
        "position_value":   round(position_value, 2),
        "total_value":      round(total, 2),
        "total_return_pct": round(total_return_pct, 2),
        "unrealized_pct":   round(unrealized_pct, 2),
        "holding":          holding,
        "total_trades":     n,
        "win_rate":         round(wins / n * 100, 1) if n > 0 else 0.0,
        "winning_trades":   wins,
        "max_drawdown":     state.get("max_drawdown", 0.0),
    }


# ─── TRADE EXECUTION ──────────────────────────────────────────────────────────

def execute_fomo_buy(
    token_ticker:     str,
    token_name:       str,
    entry_price:      float,
    wallet_alias:     str,
    wallet_address:   str,
    contract_address: str  = None,
    catalyst:         str  = None,   # e.g. "influencer post 50K views", "new DEX listing"
    catalyst_score:   int  = 0,      # 0-10 quality score
    market_cap:       float = None,
    liquidity_usd:    float = None,
    token_age_days:   float = None,
    holder_count:     int   = None,
    volume_spike_pct: float = None,  # % volume increase in last 10 min
    amount_usd:       float = None,  # human-selected $ from Telegram button; overrides auto-sizing
) -> Optional[dict]:
    """Execute a FOMO copy trade buy. Returns holding dict or None if skipped."""
    state = load_fomo_portfolio()

    if state.get("holding"):
        log.warning(f"FOMO: Already holding {state['holding']['token_ticker']} — skip buy")
        return None

    cash = state["cash"]
    if amount_usd is not None:
        spend = min(amount_usd, cash * 0.95)
    else:
        spend = min(cash * FOMO_MAX_POSITION_PCT, cash * 0.90)
    if spend < 5:
        log.warning("FOMO: Insufficient cash for buy")
        return None

    fee      = spend * FOMO_TAKER_FEE
    net_spend = spend - fee
    units    = net_spend / entry_price

    stop_loss   = round(entry_price * (1 + FOMO_HARD_STOP_PCT), 8)
    exit_target = round(entry_price * 1.30, 8)   # 30% target

    holding = {
        "token_ticker":     token_ticker,
        "token_name":       token_name,
        "entry_price":      entry_price,
        "units":            units,
        "spent":            spend,
        "stop_loss":        stop_loss,
        "exit_target":      exit_target,
        "wallet_alias":     wallet_alias,
        "wallet_address":   wallet_address,
        "contract_address": contract_address,
        # Context captured at entry — used for post-mortem later
        "catalyst":         catalyst,
        "catalyst_score":   catalyst_score,
        "market_cap":       market_cap,
        "liquidity_usd":    liquidity_usd,
        "token_age_days":   token_age_days,
        "holder_count":     holder_count,
        "volume_spike_pct": volume_spike_pct,
        "entered_at":       datetime.now(timezone.utc).isoformat(),
        "auto_exit_at":     (datetime.now(timezone.utc) + timedelta(hours=FOMO_AUTO_EXIT_HOURS)).isoformat(),
        "source":           "fomo_copy",
        "partial_taken":    False,
    }

    state["cash"]    -= spend
    state["holding"] = holding
    save_fomo_portfolio(state)

    log.info(f"FOMO BUY: {token_ticker} @ ${entry_price:.8f} | "
             f"${spend:.2f} | following {wallet_alias} | catalyst: {catalyst or 'none'}")
    return holding


def execute_fomo_sell(
    current_price: float,
    reason:        str = "tracker_exit",
    trader_held_hours: float = None,   # how long the original trader held
    exit_lag_minutes:  float = None,   # how many minutes after trader sold did we sell
) -> Optional[dict]:
    """Exit the active FOMO quick trade."""
    state   = load_fomo_portfolio()
    holding = state.get("holding")
    if not holding:
        return None

    proceeds   = holding["units"] * current_price
    fee        = proceeds * FOMO_TAKER_FEE
    net        = proceeds - fee
    profit     = net - holding["spent"]
    profit_pct = profit / holding["spent"] * 100

    # Calculate hold duration
    entered_at  = datetime.fromisoformat(holding["entered_at"].replace("Z", "+00:00"))
    held_minutes = (datetime.now(timezone.utc) - entered_at).total_seconds() / 60

    state["cash"] += net

    # Update peak / drawdown
    total_val = state["cash"]
    if total_val > state.get("peak_value", FOMO_STARTING_CASH):
        state["peak_value"] = total_val
    else:
        drawdown = (state["peak_value"] - total_val) / state["peak_value"] * 100
        if drawdown > state.get("max_drawdown", 0):
            state["max_drawdown"] = round(drawdown, 2)

    trade_record = {
        **holding,
        "exit_price":         current_price,
        "exit_reason":        reason,
        "profit":             round(profit, 6),
        "profit_pct":         round(profit_pct, 2),
        "held_minutes":       round(held_minutes, 1),
        "trader_held_hours":  trader_held_hours,
        "exit_lag_minutes":   exit_lag_minutes,
        "exited_at":          datetime.now(timezone.utc).isoformat(),
        "postmortem_done":    False,
    }

    state["trade_history"].append(trade_record)
    state["total_trades"]  += 1
    if profit > 0:
        state["winning_trades"] += 1

    state["holding"] = None
    save_fomo_portfolio(state)

    outcome = "WIN" if profit > 0 else "LOSS"
    log.info(f"FOMO SELL: {holding['token_ticker']} @ ${current_price:.8f} | "
             f"{profit_pct:+.1f}% | {outcome} | {reason}")
    return trade_record


# ─── AUTO EXIT CHECKS ─────────────────────────────────────────────────────────

def _notify_auto_exit(result: dict):
    """
    Informational-only Telegram message for auto-exits (stop/target/24h) --
    the trade has already executed by the time this fires, so there's no
    button, nothing to tap. Just makes sure you always get a text either way.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.info(f"[TELEGRAM] auto-exit notify skipped (no token/chat id): {result}")
        return
    reason = result.get("exit_reason", "")
    labels = {
        "take_profit":   "\u2705 Target hit",
        "hard_stop":     "\U0001f534 Stop hit",
        "time_exit_24h": "\u23f1 24h time exit",
    }
    label  = labels.get(reason, "Auto-exit")
    ticker = result.get("token_ticker", "???")
    price  = result.get("exit_price", 0)
    pct    = result.get("profit_pct", 0)
    profit = result.get("profit", 0)
    sign   = "+" if profit >= 0 else ""
    message = (
        f"{label}: SOLD {ticker} @ ${price:.8f}\n"
        f"{sign}${profit:,.2f} ({pct:+.1f}%)"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"FOMO: auto-exit Telegram notify failed: {e}")


def check_fomo_auto_exits(price_map: dict = None) -> Optional[dict]:
    """
    Called during every 4-hour cycle and by the webhook server.
    Checks hard stop, take-profit target, and 24h time limit.
    Returns trade record if exited, None otherwise.
    """
    state   = load_fomo_portfolio()
    holding = state.get("holding")
    if not holding:
        return None

    ticker        = holding["token_ticker"]
    current_price = (price_map or {}).get(ticker, holding["entry_price"])
    now           = datetime.now(timezone.utc)

    # Time exit — 24h limit
    auto_exit_at = datetime.fromisoformat(holding["auto_exit_at"].replace("Z", "+00:00"))
    if now >= auto_exit_at:
        log.warning(f"FOMO: Auto-exit {ticker} — 24h time limit reached")
        result = execute_fomo_sell(current_price, reason="time_exit_24h")
        if result:
            _notify_auto_exit(result)
        return result

    if current_price > 0:
        # Take profit — hit exit_target
        if current_price >= holding["exit_target"]:
            log.warning(f"FOMO: Take-profit {ticker} — target ${holding['exit_target']:.8f} reached")
            result = execute_fomo_sell(current_price, reason="take_profit")
            if result:
                _notify_auto_exit(result)
            return result

        # Hard stop — -15%
        pct = (current_price - holding["entry_price"]) / holding["entry_price"] * 100
        if pct <= FOMO_HARD_STOP_PCT * 100:
            log.warning(f"FOMO: Hard stop {ticker} — {pct:.1f}%")
            result = execute_fomo_sell(current_price, reason="hard_stop")
            if result:
                _notify_auto_exit(result)
            return result

    return None


# ─── POST-MORTEM & LESSONS ────────────────────────────────────────────────────

def run_fomo_postmortem(trade: dict) -> dict:
    """
    Deep analysis of a completed FOMO trade.
    Builds structured lessons for both the specific wallet and global patterns.
    Called by the 4-hour agent using Claude.
    Returns a postmortem dict to be stored alongside the trade record.
    """
    profit_pct   = trade.get("profit_pct", 0)
    outcome      = "WIN" if profit_pct > 0 else "LOSS"
    ticker       = trade.get("token_ticker", "???")
    alias        = trade.get("wallet_alias", "unknown")
    catalyst     = trade.get("catalyst", "none identified")
    cat_score    = trade.get("catalyst_score", 0)
    market_cap   = trade.get("market_cap")
    liquidity    = trade.get("liquidity_usd")
    token_age    = trade.get("token_age_days")
    holder_count = trade.get("holder_count")
    vol_spike    = trade.get("volume_spike_pct")
    held_min     = trade.get("held_minutes", 0)
    trader_hours = trade.get("trader_held_hours")
    lag_min      = trade.get("exit_lag_minutes")
    exit_reason  = trade.get("exit_reason", "unknown")

    # Build structured questions the post-mortem answers
    questions = {
        "was_catalyst_valid":   cat_score >= 6 if cat_score else None,
        "was_liquidity_ok":     (liquidity or 0) >= 100_000,
        "was_market_cap_ok":    (market_cap or 0) >= 500_000,
        "was_token_mature":     (token_age or 0) >= 7,
        "was_volume_confirmed": (vol_spike or 0) >= 100,
        "was_time_exit":        exit_reason in ("time_exit_24h",),
        "was_stop_hit":         exit_reason == "hard_stop",
        "lag_was_acceptable":   (lag_min or 0) <= 10,  # within 10 min of trader
    }

    # Derive lessons
    lessons = []

    if outcome == "LOSS":
        if not questions["was_liquidity_ok"]:
            lessons.append(f"LOW LIQUIDITY LOSS: {ticker} had <$100K liquidity — slippage hurt entry/exit")
        if not questions["was_catalyst_valid"]:
            lessons.append(f"WEAK CATALYST LOSS: {ticker} entered without strong organic catalyst")
        if not questions["was_token_mature"]:
            lessons.append(f"NEW TOKEN LOSS: {ticker} was <7 days old — higher rug risk")
        if not questions["was_volume_confirmed"]:
            lessons.append(f"NO VOLUME CONFIRMATION: {ticker} lacked volume spike before entry")
        if questions["was_stop_hit"]:
            lessons.append(f"STOP HIT: {ticker} dropped 15%+ — {alias} may have poor stop discipline")
        if questions["was_time_exit"]:
            lessons.append(f"TIME EXIT: Held {ticker} 24h without {alias} selling — timing mismatch")
    else:
        if questions["was_catalyst_valid"]:
            lessons.append(f"CATALYST CONFIRMED: {ticker} win backed by strong catalyst (score {cat_score}/10)")
        if questions["was_volume_confirmed"]:
            lessons.append(f"VOLUME EDGE: {ticker} volume spike {vol_spike:.0f}% preceded move")
        if trader_hours and held_min / 60 < trader_hours * 0.8:
            lessons.append(f"QUICK EXIT WIN: Exited {ticker} before trader — captured most of move")

    # Wallet-specific pattern
    wallet_lesson = {
        "trade":         ticker,
        "outcome":       outcome,
        "profit_pct":    profit_pct,
        "market_cap":    market_cap,
        "catalyst_score": cat_score,
        "held_minutes":  held_min,
        "lessons":       lessons,
        "timestamp":     datetime.now(timezone.utc).isoformat(),
    }

    postmortem = {
        "trade_id":       trade.get("entered_at", ""),
        "ticker":         ticker,
        "wallet_alias":   alias,
        "outcome":        outcome,
        "profit_pct":     profit_pct,
        "questions":      questions,
        "lessons":        lessons,
        "wallet_lesson":  wallet_lesson,
        "completed_at":   datetime.now(timezone.utc).isoformat(),
    }

    # Update lessons database
    _update_fomo_lessons(alias, wallet_lesson, lessons, outcome)

    # Mark trade as post-mortemed
    state = load_fomo_portfolio()
    for t in state["trade_history"]:
        if t.get("entered_at") == trade.get("entered_at"):
            t["postmortem_done"] = True
            t["postmortem"]      = postmortem
            break
    save_fomo_portfolio(state)

    log.info(f"FOMO post-mortem complete: {ticker} ({outcome}) — {len(lessons)} lessons")
    return postmortem


# --- AI post-mortem & social commentary (side-channel, non-blocking) ---------

def run_fomo_ai_postmortem(trade: dict, commentary: str = None) -> Optional[dict]:
    """
    Optional deep-dive analysis of a completed trade, mirroring the main portfolio's
    Claude-powered post-mortem (crystal_ball_memory.py). Runs on the side after the
    rule-based post-mortem -- never blocks a buy, sell, or auto-exit.

    If `commentary` (something the trader posted publicly about this trade) is
    supplied, it's folded in as extra context. If not supplied, the analysis still
    runs -- commentary is a bonus input, never a requirement.
    """
    if not ANTHROPIC_API_KEY:
        log.warning("FOMO: no ANTHROPIC_API_KEY -- skipping AI post-mortem")
        return None

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    alias  = trade.get("wallet_alias", "unknown")
    ticker = trade.get("token_ticker", "???")

    commentary_block = (
        f'\nTRADER\'S OWN COMMENTARY ABOUT THIS TRADE:\n"{commentary}"\n'
        if commentary else
        "\n(No trader commentary captured for this trade yet.)\n"
    )

    system = """You are a forensic analyst reviewing a copy-trade in a Solana memecoin
FOMO copy-trading system. Judge whether following this trader on this specific trade
was a genuinely sound decision, independent of whether it happened to make money --
a win on a reckless setup and a loss on a sound setup both deserve honest scrutiny.
If trader commentary is provided, assess whether it shows real understanding of
market structure (liquidity, catalyst quality, exchange/KOL dynamics, rug risk) or
is just hype-repeating with no substance. Respond ONLY with valid JSON."""

    prompt = f"""Review this completed FOMO copy-trade.

WALLET: {alias}
TOKEN: {ticker}
CATALYST AT ENTRY: {trade.get('catalyst', 'none')} (score {trade.get('catalyst_score', 0)}/10)
MARKET CAP AT ENTRY: ${(trade.get('market_cap') or 0):,.0f}
LIQUIDITY AT ENTRY: ${(trade.get('liquidity_usd') or 0):,.0f}
TOKEN AGE AT ENTRY: {trade.get('token_age_days')} days
ENTRY PRICE: ${trade.get('entry_price', 0):.8f}
EXIT PRICE: ${trade.get('exit_price', 0):.8f}
RESULT: {trade.get('profit_pct', 0):+.1f}% ({trade.get('exit_reason', 'unknown')})
HELD: {trade.get('held_minutes', 0):.0f} minutes
{commentary_block}
Respond with this JSON structure:
{{
  "was_good_decision": true or false,
  "reasoning": "Was following this trade sound, independent of the outcome? Why.",
  "commentary_shows_understanding": true, false, or null if no commentary given,
  "commentary_assessment": "What the trader's own words reveal about their process, or null",
  "lessons_for_this_wallet": ["specific, actionable lesson", "..."],
  "trust_adjustment": "increase | decrease | no_change"
}}"""

    try:
        resp = client.messages.create(
            model=AI_MODEL, max_tokens=1000, system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        result["analyzed_at"]    = datetime.now(timezone.utc).isoformat()
        result["had_commentary"] = commentary is not None
        result["ticker"]         = ticker

        _record_ai_insight(alias, result)

        log.info(f"FOMO AI post-mortem: {ticker} -- "
                 f"good_decision={result.get('was_good_decision')} "
                 f"commentary={'yes' if commentary else 'no'}")
        return result

    except Exception as e:
        log.error(f"FOMO AI post-mortem failed for {ticker}: {e}")
        return None


def _record_ai_insight(alias: str, insight: dict):
    """
    Append an AI-generated qualitative insight to a wallet's profile. Kept in its
    own list, separate from trade_history, so it never skews the numeric
    best_conditions/avoid_when averages already computed there.
    """
    db = load_fomo_lessons()
    if alias not in db["wallets"]:
        db["wallets"][alias] = {
            "trade_history":   [],
            "patterns":        [],
            "best_conditions": {},
            "avoid_when":      [],
            "ai_insights":     [],
            "last_updated":    None,
        }
    db["wallets"][alias].setdefault("ai_insights", []).append(insight)
    db["wallets"][alias]["last_updated"] = datetime.now(timezone.utc).isoformat()
    save_fomo_lessons(db)


def enrich_trade_with_commentary(trade_id: str, commentary: str) -> Optional[dict]:
    """
    Manual, side-channel entry point. Call any time after a trade closes -- an hour
    later, next week, whenever you've captured something the trader posted about it
    on fomo.family's feed. trade_id is the trade's "entered_at" timestamp (visible
    in fomo_portfolio.json's trade_history). Never required for a trade to execute,
    exit, or for its base post-mortem to complete -- pure bonus context.
    """
    state = load_fomo_portfolio()
    trade = next((t for t in state.get("trade_history", [])
                  if t.get("entered_at") == trade_id), None)
    if not trade:
        log.warning(f"FOMO: no trade found with id {trade_id}")
        return None

    return run_fomo_ai_postmortem(trade, commentary=commentary)


def _update_fomo_lessons(alias: str, wallet_lesson: dict, new_lessons: list, outcome: str):
    """Update the per-wallet lessons database."""
    db = load_fomo_lessons()

    if alias not in db["wallets"]:
        db["wallets"][alias] = {
            "trade_history":   [],
            "patterns":        [],
            "best_conditions": {},
            "avoid_when":      [],
            "last_updated":    None,
        }

    wallet = db["wallets"][alias]
    wallet["trade_history"].append(wallet_lesson)

    # Rebuild patterns from history
    history = wallet["trade_history"]
    wins    = [t for t in history if t["outcome"] == "WIN"]
    losses  = [t for t in history if t["outcome"] == "LOSS"]

    if len(history) >= 3:
        # What market caps do they win at?
        win_caps  = [t["market_cap"] for t in wins  if t.get("market_cap")]
        loss_caps = [t["market_cap"] for t in losses if t.get("market_cap")]
        if win_caps:
            wallet["best_conditions"]["avg_win_market_cap"] = round(sum(win_caps) / len(win_caps))
        if loss_caps:
            wallet["best_conditions"]["avg_loss_market_cap"] = round(sum(loss_caps) / len(loss_caps))

        # Catalyst quality patterns
        win_cat  = [t["catalyst_score"] for t in wins  if t.get("catalyst_score")]
        loss_cat = [t["catalyst_score"] for t in losses if t.get("catalyst_score")]
        if win_cat:
            wallet["best_conditions"]["min_catalyst_score_for_win"] = round(sum(win_cat) / len(win_cat), 1)

        # Hold time patterns
        win_hold  = [t["held_minutes"] for t in wins  if t.get("held_minutes")]
        if win_hold:
            wallet["best_conditions"]["avg_win_hold_minutes"] = round(sum(win_hold) / len(win_hold))

    # Add new lessons to global avoid_when if they're loss patterns
    for lesson in new_lessons:
        if "LOSS" in lesson and lesson not in wallet.get("avoid_when", []):
            wallet["avoid_when"].append(lesson)

    wallet["last_updated"] = datetime.now(timezone.utc).isoformat()
    db["wallets"][alias]   = wallet

    # Global lessons
    for lesson in new_lessons:
        if lesson not in db.get("global", []):
            db["global"].append(lesson)

    save_fomo_lessons(db)


def get_wallet_lessons(alias: str) -> dict:
    """Return the lesson profile for a specific wallet — used at entry decision time."""
    db = load_fomo_lessons()
    return db["wallets"].get(alias, {})


def get_pending_postmortems() -> list:
    """Return trades that need post-mortems."""
    state = load_fomo_portfolio()
    return [t for t in state.get("trade_history", [])
            if not t.get("postmortem_done", False)]


# ─── GRADUATION CHECK ─────────────────────────────────────────────────────────

def get_fomo_graduation_status() -> dict:
    """
    Check if the FOMO system is ready for real money.
    Requires 20 trades, 55% win rate, 3% avg return, 20 days, <20% drawdown,
    and meaningful lessons (not just blind copying).
    """
    state   = load_fomo_portfolio()
    history = state.get("trade_history", [])
    n       = state.get("total_trades", 0)
    wins    = state.get("winning_trades", 0)

    criteria = {}
    criteria["min_trades"] = n >= GRAD_MIN_TRADES

    win_rate = (wins / n * 100) if n > 0 else 0.0
    criteria["win_rate"] = win_rate >= GRAD_MIN_WIN_RATE

    avg_pct = sum(t.get("profit_pct", 0) for t in history) / len(history) if history else 0.0
    criteria["avg_return"] = avg_pct >= GRAD_MIN_AVG_RETURN

    started     = state.get("started_at", datetime.now(timezone.utc).isoformat())
    days_running = (datetime.now(timezone.utc) -
                    datetime.fromisoformat(started.replace("Z", "+00:00"))).days
    criteria["days_running"] = days_running >= GRAD_MIN_DAYS

    max_dd = state.get("max_drawdown", 0.0)
    criteria["max_drawdown"] = max_dd <= GRAD_MAX_DRAWDOWN

    # Lessons quality check — must have post-mortems on at least 80% of trades
    pm_done = sum(1 for t in history if t.get("postmortem_done", False))
    criteria["lessons_built"] = n > 0 and (pm_done / n) >= 0.80

    # No 3-loss streak in last 10 trades
    recent = history[-10:] if len(history) >= 10 else history
    streak = max_streak = 0
    for t in recent:
        if t.get("profit_pct", 0) < 0:
            streak += 1
            max_streak = max(max_streak, streak)
        else:
            streak = 0
    criteria["no_bad_streak"] = max_streak < 3

    score     = sum(criteria.values())
    score_str = f"{score}/7"
    ready     = score == 7

    return {
        "score":       score_str,
        "ready":       ready,
        "criteria":    criteria,
        "days_running": days_running,
        "win_rate":    round(win_rate, 1),
        "avg_return":  round(avg_pct, 2),
        "n_trades":    n,
        "max_drawdown": max_dd,
    }


# ─── PORTFOLIO SUMMARY ────────────────────────────────────────────────────────

def get_fomo_stats() -> dict:
    """Full stats including per-wallet breakdown — used in 4-hour agent output."""
    state   = load_fomo_portfolio()
    history = state.get("trade_history", [])
    n       = state.get("total_trades", 0)
    wins    = state.get("winning_trades", 0)
    cash    = state["cash"]

    # Per-wallet stats
    wallet_stats = {}
    for t in history:
        alias = t.get("wallet_alias", "unknown")
        if alias not in wallet_stats:
            wallet_stats[alias] = {"trades": 0, "wins": 0, "total_pct": 0.0,
                                   "consecutive_losses": 0, "last_outcome": None}
        ws = wallet_stats[alias]
        ws["trades"]     += 1
        ws["total_pct"]  += t.get("profit_pct", 0)
        if t.get("profit_pct", 0) > 0:
            ws["wins"]              += 1
            ws["consecutive_losses"] = 0
            ws["last_outcome"]       = "WIN"
        else:
            ws["consecutive_losses"] += 1
            ws["last_outcome"]        = "LOSS"

    for alias, ws in wallet_stats.items():
        ws["win_rate"]   = round(ws["wins"] / ws["trades"] * 100, 1) if ws["trades"] > 0 else 0.0
        ws["avg_return"] = round(ws["total_pct"] / ws["trades"], 2) if ws["trades"] > 0 else 0.0

    holding    = state.get("holding")
    pos_value  = 0.0
    if holding:
        pos_value = holding["units"] * holding["entry_price"]
    total_val  = cash + pos_value

    return {
        "total_value":      round(total_val, 2),
        "total_return_pct": round((total_val - FOMO_STARTING_CASH) / FOMO_STARTING_CASH * 100, 2),
        "cash":             round(cash, 2),
        "total_trades":     n,
        "win_rate":         round(wins / n * 100, 1) if n > 0 else 0.0,
        "avg_return":       round(sum(t.get("profit_pct", 0) for t in history) / len(history), 2) if history else 0.0,
        "max_drawdown":     state.get("max_drawdown", 0.0),
        "wallet_stats":     wallet_stats,
        "holding":          holding,
    }


def reset_fomo_portfolio():
    """Hard reset — wipe state back to $500."""
    state = _default_state()
    save_fomo_portfolio(state)
    log.info("FOMO portfolio reset to $500")
