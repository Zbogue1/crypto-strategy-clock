#!/usr/bin/env python3
"""
fomo_vetting.py — Wallet quality scoring and copy-trade decision engine.

Every wallet candidate goes through this before being added to the watchlist.
Produces a score (0-100), a recommendation, and a human-readable flag list
so additions are data-driven rather than vibes-driven.

───────────────────────────────────────────────────────────────────────────────
HARD DISQUALIFIERS  (recommendation = REJECT regardless of score)
───────────────────────────────────────────────────────────────────────────────
  • Tag "arbitrager"       — speed/latency edge, cannot be replicated
  • Tag "wash_trader"      — fake volume, stats are meaningless
  • Tag "bot"              — automated, not a human trader to copy
  • fast_tx_ratio > 0.50   — more than half of txs are sandwich/bot speed
  • avg_hold_minutes < 5   — in-and-out before an email arrives

───────────────────────────────────────────────────────────────────────────────
SCORING  (0–100)
───────────────────────────────────────────────────────────────────────────────
  Win rate (0–30 pts)
    ≥ 70% → 30   ≥ 65% → 25   ≥ 60% → 18   ≥ 55% → 10   < 55% → 0

  Sample size — trades_7d (0–20 pts)
    ≥ 30 → 20   ≥ 20 → 15   ≥ 10 → 8   < 10 → 0  (+ flag: unvetted)

  Hold time — avg_hold_minutes (0–20 pts)
    ≥ 60  → 20   ≥ 30 → 15   ≥ 15 → 8   < 15 → 0  (+ flag: too fast)
    None/unknown → 10 (give benefit of the doubt, flag as unverified)

  7D realized PnL (0–15 pts)
    ≥ $20K → 15   ≥ $10K → 10   ≥ $5K → 5   ≥ $1K → 2   < $1K → 0

  Email quota safety — avg_trades_per_day (0 to −20 pts)
    ≤ 5  → +5   ≤ 15 → 0   ≤ 30 → −10   > 30 → −20

  Penalties
    top_renamed tag     → −10  (wallet cycling / fresh start after blowup)
    fast_tx_ratio 0.3–0.5 → −10
    fast_tx_ratio 0.15–0.3 → −5
    honeypot_ratio > 0.05  → −15  (bought rugs before)

───────────────────────────────────────────────────────────────────────────────
RECOMMENDATIONS
───────────────────────────────────────────────────────────────────────────────
  COPY_TRADE      score ≥ 55 AND winrate ≥ 60% AND hold ≥ 15 min (or unknown)
  NARRATIVE_WATCH score ≥ 30 OR realized_7d ≥ $10K
  TWITTER_ONLY    has_twitter AND score < 30
  REJECT          score < 20 AND no twitter AND realized_7d < $1K

  Note: "top_renamed" wallets are downgraded one level until 2 weeks of
  observed track record accumulates in fomo_wallet_stats.py.

───────────────────────────────────────────────────────────────────────────────
INTEGRATION
───────────────────────────────────────────────────────────────────────────────
  Called from fomo_gmgn.py after every discover_traders() run.
  The result dict is stored in trusted_wallets.json under "vetting" key.
  fomo_tracker.py reads vetting.recommendation to decide signal handling:
    COPY_TRADE      → full pipeline (research + position sizing + buy)
    NARRATIVE_WATCH → research only, log narrative theme, no buy
    TWITTER_ONLY    → CT sentiment feed only, no email watch slot
    REJECT          → ignore all signals from this wallet
"""

import logging
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ─── CONSTANTS ────────────────────────────────────────────────────────────────

HARD_DISQUALIFIER_TAGS = {"arbitrager", "wash_trader", "bot"}

# Scoring boundaries
WR_TIERS = [
    (0.70, 30),
    (0.65, 25),
    (0.60, 18),
    (0.55, 10),
    (0.00,  0),
]

SAMPLE_TIERS = [
    (30, 20),
    (20, 15),
    (10,  8),
    ( 0,  0),
]

HOLD_TIERS = [
    (60, 20),
    (30, 15),
    (15,  8),
    ( 0,  0),
]

PNL_TIERS = [
    (20_000, 15),
    (10_000, 10),
    ( 5_000,  5),
    ( 1_000,  2),
    (     0,  0),
]


def _tier_score(value: float, tiers: list) -> int:
    """Return score for the first tier whose threshold value meets."""
    for threshold, pts in tiers:
        if value >= threshold:
            return pts
    return 0


# ─── MAIN SCORER ──────────────────────────────────────────────────────────────

def score_wallet(candidate: dict) -> dict:
    """
    Score a wallet candidate against the vetting framework.

    Input fields (all optional — missing = 0 / unknown):
        win_rate          float  0–1     (e.g. 0.70 for 70%)
        trades_7d         int            (total trades in 7-day window)
        avg_hold_minutes  float          (None = unknown)
        realized_pnl_7d   float  USD
        avg_trades_per_day float
        fast_tx_ratio     float  0–1
        honeypot_ratio    float  0–1
        tags              list[str]
        has_twitter       bool

    Returns:
        {
          "score":          int (0-100, may go negative before clamping),
          "recommendation": str ("COPY_TRADE" | "NARRATIVE_WATCH" | "TWITTER_ONLY" | "REJECT"),
          "copy_trade":     bool,
          "disqualifiers":  list[str],   hard stops
          "flags":          list[str],   soft warnings
          "reasoning":      str,         one-line Telegram summary
          "vetted_at":      str ISO8601
        }
    """
    win_rate          = float(candidate.get("win_rate") or candidate.get("winrate") or 0)
    trades_7d         = int(candidate.get("trades_7d") or candidate.get("buys_7d") or 0)
    avg_hold_minutes  = candidate.get("avg_hold_minutes")   # None if unknown
    realized_pnl_7d   = float(candidate.get("realized_pnl_7d") or candidate.get("realized_profit") or 0)
    avg_trades_per_day = float(candidate.get("avg_trades_per_day") or (trades_7d / 7 if trades_7d else 0))
    fast_tx_ratio     = float(candidate.get("fast_tx_ratio") or 0)
    honeypot_ratio    = float(candidate.get("honeypot_ratio") or 0)
    tags              = {t.lower() for t in (candidate.get("tags") or [])}
    has_twitter       = bool(candidate.get("has_twitter") or candidate.get("twitter"))

    disqualifiers = []
    flags         = []
    score         = 0

    # ── Hard disqualifiers ────────────────────────────────────────────────────
    bad_tags = HARD_DISQUALIFIER_TAGS & tags
    if bad_tags:
        disqualifiers.append(f"Tag(s) disqualify copy-trade: {', '.join(sorted(bad_tags))}")

    if fast_tx_ratio > 0.50:
        disqualifiers.append(f"fast_tx_ratio {fast_tx_ratio:.2f} > 0.50 — bot/sandwich speed")

    if avg_hold_minutes is not None and avg_hold_minutes < 5:
        disqualifiers.append(f"avg hold {avg_hold_minutes:.1f} min — exits before email arrives")

    # ── Win rate ──────────────────────────────────────────────────────────────
    wr_pts = _tier_score(win_rate, WR_TIERS)
    score += wr_pts
    if win_rate < 0.55:
        flags.append(f"Low win rate {win_rate*100:.0f}% — below 55% floor")

    # ── Sample size ───────────────────────────────────────────────────────────
    sample_pts = _tier_score(trades_7d, SAMPLE_TIERS)
    score += sample_pts
    if trades_7d < 10:
        flags.append(f"Only {trades_7d} trades in 7D — statistically unvetted (need 20+)")
    elif trades_7d < 20:
        flags.append(f"{trades_7d} trades in 7D — borderline sample size (want 20+)")

    # ── Hold time ─────────────────────────────────────────────────────────────
    if avg_hold_minutes is None:
        score += 10   # benefit of the doubt
        flags.append("Hold time unknown — verify before copy-trading")
    elif avg_hold_minutes < 15:
        score += 0
        flags.append(f"Avg hold {avg_hold_minutes:.0f} min — too fast for email-triggered copy")
    else:
        hold_pts = _tier_score(avg_hold_minutes, HOLD_TIERS)
        score += hold_pts

    # ── 7D PnL ────────────────────────────────────────────────────────────────
    score += _tier_score(realized_pnl_7d, PNL_TIERS)
    if realized_pnl_7d < 1000:
        flags.append(f"Low 7D PnL ${realized_pnl_7d:,.0f} — insufficient proof of edge")

    # ── Email quota safety ────────────────────────────────────────────────────
    if avg_trades_per_day <= 5:
        score += 5
    elif avg_trades_per_day <= 15:
        pass   # neutral
    elif avg_trades_per_day <= 30:
        score -= 10
        flags.append(f"{avg_trades_per_day:.0f} trades/day — moderate email quota burn")
    else:
        score -= 20
        flags.append(f"{avg_trades_per_day:.0f} trades/day — HIGH email quota burn, consider skipping Solscan slot")

    # ── Tag penalties ─────────────────────────────────────────────────────────
    if "top_renamed" in tags:
        score -= 10
        flags.append("top_renamed tag — may be fresh wallet after previous blowup. Observe 2 weeks before trusting stats.")

    if 0.30 <= fast_tx_ratio <= 0.50:
        score -= 10
        flags.append(f"fast_tx_ratio {fast_tx_ratio:.2f} — elevated bot activity")
    elif 0.15 <= fast_tx_ratio < 0.30:
        score -= 5
        flags.append(f"fast_tx_ratio {fast_tx_ratio:.2f} — mild bot activity")

    if honeypot_ratio > 0.05:
        score -= 15
        flags.append(f"honeypot_ratio {honeypot_ratio:.2f} — bought rugs before (judgment concern)")

    # ── KOL note (non-penalizing, just informational) ─────────────────────────
    if "kol" in tags:
        flags.append("KOL tag — watch on-chain buys; they buy BEFORE tweeting (we want this)")

    # Clamp score
    score = max(0, min(100, score))

    # ── Recommendation ────────────────────────────────────────────────────────
    is_disqualified = len(disqualifiers) > 0
    hold_ok = (avg_hold_minutes is None) or (avg_hold_minutes >= 15)

    if is_disqualified:
        if realized_pnl_7d >= 10_000 or has_twitter:
            recommendation = "NARRATIVE_WATCH"
        else:
            recommendation = "REJECT"
    elif score >= 55 and win_rate >= 0.60 and hold_ok:
        recommendation = "COPY_TRADE"
        # Downgrade one level for top_renamed (not enough history yet)
        if "top_renamed" in tags and trades_7d < 20:
            recommendation = "NARRATIVE_WATCH"
            flags.append("Downgraded to NARRATIVE_WATCH: top_renamed + small sample → observe 2 weeks first")
    elif score >= 30 or realized_pnl_7d >= 10_000:
        recommendation = "NARRATIVE_WATCH"
    elif has_twitter:
        recommendation = "TWITTER_ONLY"
    else:
        recommendation = "REJECT"

    copy_trade = recommendation == "COPY_TRADE"

    # ── One-line reasoning for Telegram ──────────────────────────────────────
    wr_str   = f"{win_rate*100:.0f}%WR"
    pnl_str  = f"${realized_pnl_7d:,.0f} 7D"
    hold_str = (f"{avg_hold_minutes:.0f}min hold" if avg_hold_minutes else "hold?")
    reasoning = f"Score {score}/100 | {wr_str} | {pnl_str} | {hold_str}"
    if disqualifiers:
        reasoning += f" | ⛔ {disqualifiers[0]}"
    elif flags:
        reasoning += f" | ⚠️ {flags[0]}"

    return {
        "score":          score,
        "recommendation": recommendation,
        "copy_trade":     copy_trade,
        "disqualifiers":  disqualifiers,
        "flags":          flags,
        "reasoning":      reasoning,
        "vetted_at":      datetime.now(timezone.utc).isoformat(),
    }


# ─── TELEGRAM FORMATTER ───────────────────────────────────────────────────────

RECOMMENDATION_ICONS = {
    "COPY_TRADE":      "✅",
    "NARRATIVE_WATCH": "👁️",
    "TWITTER_ONLY":    "🐦",
    "REJECT":          "❌",
}


def format_vetting_telegram(alias: str, wallet: str, result: dict) -> str:
    """Format a vetting result as a Telegram message block."""
    icon  = RECOMMENDATION_ICONS.get(result["recommendation"], "❓")
    lines = [
        f"{icon} <b>{alias}</b> — {result['recommendation']}",
        f"Score: {result['score']}/100 | {result['reasoning'].split('|')[0].strip()}",
    ]

    if result["disqualifiers"]:
        for d in result["disqualifiers"]:
            lines.append(f"  ⛔ {d}")

    if result["flags"]:
        for f in result["flags"][:3]:   # cap at 3 flags in message
            lines.append(f"  ⚠️ {f}")

    lines.append(f"  <code>{wallet}</code>")
    return "\n".join(lines)


def format_vetting_batch_telegram(results: list[dict]) -> str:
    """
    Format a batch of vetting results (from discovery scan) as one Telegram message.
    results: list of {"alias": str, "wallet": str, "vetting": dict}
    """
    if not results:
        return ""

    lines = [f"🔬 <b>Wallet Vetting Results ({len(results)} candidates)</b>\n"]

    # Group by recommendation
    groups = {"COPY_TRADE": [], "NARRATIVE_WATCH": [], "TWITTER_ONLY": [], "REJECT": []}
    for r in results:
        rec = r["vetting"]["recommendation"]
        groups.setdefault(rec, []).append(r)

    for rec in ["COPY_TRADE", "NARRATIVE_WATCH", "TWITTER_ONLY", "REJECT"]:
        bucket = groups.get(rec, [])
        if not bucket:
            continue
        icon = RECOMMENDATION_ICONS[rec]
        lines.append(f"\n{icon} <b>{rec}</b>")
        for r in bucket:
            v      = r["vetting"]
            alias  = r.get("alias") or r.get("wallet", "?")[:8]
            wallet = r.get("wallet", "")
            wr     = v.get("score", 0)
            tw     = f"@{r['twitter']}" if r.get("twitter") else "no twitter"
            lines.append(f"  • {tw} | Score {wr}/100")
            if v["flags"]:
                lines.append(f"    ⚠️ {v['flags'][0]}")
            if v["disqualifiers"]:
                lines.append(f"    ⛔ {v['disqualifiers'][0]}")
            lines.append(f"    <code>{wallet}</code>")

    return "\n".join(lines)


# ─── CONVENIENCE WRAPPER ─────────────────────────────────────────────────────

def vet_and_annotate(candidate: dict) -> dict:
    """
    Run score_wallet() on a candidate dict and return the candidate
    with a 'vetting' key added. Mutates in-place and returns the dict.

    candidate should include: win_rate/winrate, buys_7d, realized_profit,
    fast_tx_ratio, tags, twitter (from fomo_gmgn.py discover_traders output).
    """
    result = score_wallet(candidate)
    candidate["vetting"] = result
    # Propagate copy_trade recommendation (can be overridden manually)
    if "copy_trade" not in candidate:
        candidate["copy_trade"] = result["copy_trade"]
    log.info(
        f"Vetting: {candidate.get('twitter') or candidate.get('wallet','?')[:8]} "
        f"→ {result['recommendation']} (score {result['score']}/100)"
    )
    return candidate
