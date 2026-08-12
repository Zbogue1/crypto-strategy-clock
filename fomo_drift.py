#!/usr/bin/env python3
"""
fomo_drift.py — Wallet drift detection for the FOMO Golem.

Traders change their style over time. A wallet that used to scalp sub-$500K
mcap tokens might start buying $5M mcap tokens after they grow their bankroll.
If we don't detect this, we're copying a different strategy than the one that
earned the wallet its reputation.

Drift dimensions tracked per wallet (comparing recent 5 trades vs prior 10):
  - avg_entry_mcap      : are they buying bigger or smaller tokens?
  - avg_hold_minutes    : are they holding longer or cutting faster?
  - avg_catalyst_score  : are their entries getting lazier or more selective?
  - win_rate_trend      : is performance improving or degrading?

Drift is flagged when any dimension shifts >30% from the historical baseline.
A Telegram alert fires once per wallet per drift event (not every trade).

Called from fomo_portfolio.py after postmortems accumulate enough data.
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import requests

log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
FOMO_LESSONS_FILE  = "fomo_lessons.json"

DRIFT_THRESHOLD    = 0.30   # 30% shift = drift flag
MIN_TRADES_RECENT  = 3      # need at least 3 recent trades to compare
MIN_TRADES_HISTORY = 5      # need at least 5 historical trades to have a baseline


def _send_telegram(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception as e:
        log.warning(f"Drift: Telegram notify failed: {e}")


def _load_lessons() -> dict:
    try:
        if os.path.exists(FOMO_LESSONS_FILE):
            with open(FOMO_LESSONS_FILE) as f:
                return json.load(f)
    except Exception:
        pass
    return {"wallets": {}, "global": []}


def _save_lessons(data: dict):
    try:
        with open(FOMO_LESSONS_FILE, "w") as f:
            json.dump(data, f, indent=2, default=str)
    except Exception as e:
        log.warning(f"Drift: lessons save failed: {e}")


def _avg(values: list) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _pct_change(old: float, new: float) -> float:
    if not old or old == 0:
        return 0.0
    return (new - old) / abs(old)


def check_wallet_drift(alias: str) -> Optional[dict]:
    """
    Analyse a wallet's recent trades vs historical baseline for style drift.

    Returns a drift report dict if drift is detected, None if no drift or
    insufficient data.
    """
    db     = _load_lessons()
    wallet = db.get("wallets", {}).get(alias, {})
    history = wallet.get("trade_history", [])

    if len(history) < MIN_TRADES_HISTORY + MIN_TRADES_RECENT:
        return None  # not enough data yet

    # Split: most recent vs prior baseline
    recent   = history[-MIN_TRADES_RECENT:]
    baseline = history[-(MIN_TRADES_HISTORY + MIN_TRADES_RECENT):-MIN_TRADES_RECENT]

    def extract(trades):
        return {
            "mcap":      _avg([t.get("market_cap") for t in trades]),
            "hold_min":  _avg([t.get("held_minutes") for t in trades]),
            "cat_score": _avg([t.get("catalyst_score") for t in trades]),
            "win_rate":  sum(1 for t in trades if t.get("outcome") == "WIN") / len(trades),
        }

    rec = extract(recent)
    bas = extract(baseline)

    drift_flags = []

    mcap_chg = _pct_change(bas["mcap"], rec["mcap"])
    if bas["mcap"] and abs(mcap_chg) >= DRIFT_THRESHOLD:
        direction = "larger" if mcap_chg > 0 else "smaller"
        drift_flags.append(
            f"Entry size: buying {direction} tokens "
            f"(${bas['mcap']/1e6:.1f}M → ${rec['mcap']/1e6:.1f}M avg mcap)"
        )

    hold_chg = _pct_change(bas["hold_min"], rec["hold_min"])
    if bas["hold_min"] and abs(hold_chg) >= DRIFT_THRESHOLD:
        direction = "longer" if hold_chg > 0 else "shorter"
        drift_flags.append(
            f"Hold time: holding {direction} "
            f"({bas['hold_min']:.0f}min → {rec['hold_min']:.0f}min avg)"
        )

    cat_chg = _pct_change(bas["cat_score"], rec["cat_score"])
    if bas["cat_score"] and abs(cat_chg) >= DRIFT_THRESHOLD:
        direction = "more selective" if cat_chg > 0 else "lazier"
        drift_flags.append(
            f"Entry quality: getting {direction} "
            f"(catalyst {bas['cat_score']:.1f} → {rec['cat_score']:.1f} avg)"
        )

    wr_chg = _pct_change(bas["win_rate"], rec["win_rate"])
    if abs(wr_chg) >= DRIFT_THRESHOLD:
        direction = "improving" if wr_chg > 0 else "degrading"
        drift_flags.append(
            f"Win rate {direction}: "
            f"{bas['win_rate']*100:.0f}% → {rec['win_rate']*100:.0f}% (last {MIN_TRADES_RECENT} trades)"
        )

    if not drift_flags:
        return None

    # Check if we already alerted for this drift recently (avoid spam)
    last_drift = wallet.get("last_drift_alert")
    already_alerted = False
    if last_drift:
        from datetime import timedelta
        last_ts = datetime.fromisoformat(last_drift)
        if (datetime.now(timezone.utc) - last_ts).days < 3:
            already_alerted = True

    report = {
        "alias":       alias,
        "drift_flags": drift_flags,
        "recent":      rec,
        "baseline":    bas,
        "detected_at": datetime.now(timezone.utc).isoformat(),
    }

    if not already_alerted:
        flag_lines = "\n".join(f"• {f}" for f in drift_flags)
        _send_telegram(
            f"\U0001f6a8 <b>WALLET DRIFT DETECTED: {alias}</b>\n"
            f"Trading style has shifted significantly:\n"
            f"{flag_lines}\n\n"
            f"<i>Review copy-trade settings for this wallet.</i>"
        )
        # Record alert timestamp
        db["wallets"][alias]["last_drift_alert"] = datetime.now(timezone.utc).isoformat()
        _save_lessons(db)
        log.info(f"Drift alert sent for {alias}: {len(drift_flags)} flag(s)")

    return report


def check_all_wallets_for_drift() -> list:
    """
    Run drift check across all wallets with enough trade history.
    Called periodically by the cron agent.
    Returns list of drift reports.
    """
    db      = _load_lessons()
    reports = []
    for alias in db.get("wallets", {}):
        report = check_wallet_drift(alias)
        if report:
            reports.append(report)
            log.info(f"Drift detected: {alias} — {report['drift_flags']}")
    return reports
