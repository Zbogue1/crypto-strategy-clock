#!/usr/bin/env python3
"""
fomo_autoadd.py — Automatic deep analysis + watchlist admission for discovered wallets.

BEFORE: discovery found candidates → Telegram list → you copy the list on your
phone → email it to yourself → open it on desktop → paste into Claude → read the
analysis → manually add the wallet. Hours of latency, and by then the wallet's
edge may already be gone.

NOW: discovery finds candidates → each one is deep-analyzed automatically →
those that clear the bar are added to the watchlist immediately → you receive a
report of what was ADDED and what was REJECTED, with reasons. No action needed.

The analysis runs BEFORE the notification is sent, so the message you get is a
decision record, not a to-do list.

Admission requires all of:
  1. A 30-day track record (not one lucky week) — enforced upstream in fomo_gmgn
  2. A rule-based vetting score >= AUTO_ADD_MIN_SCORE
  3. Known average hold time (you can't copy a trader whose timing you can't see)
  4. An AI verdict of ADD after reviewing the full profile

Anything scoring between REVIEW_MIN_SCORE and AUTO_ADD_MIN_SCORE is reported as
"needs your call" rather than being silently dropped.
"""

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Optional

import anthropic

log = logging.getLogger(__name__)

AI_MODEL      = "claude-haiku-4-5-20251001"
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Admission thresholds
AUTO_ADD_MIN_SCORE = int(os.getenv("FOMO_AUTOADD_MIN_SCORE", "70"))
REVIEW_MIN_SCORE   = int(os.getenv("FOMO_REVIEW_MIN_SCORE", "55"))
# Score at/above this goes to tier_a (higher trust), below goes to tier_b
TIER_A_MIN_SCORE   = int(os.getenv("FOMO_TIER_A_MIN_SCORE", "80"))
# Master switch — set false to go back to manual review
AUTO_ADD_ENABLED   = os.getenv("FOMO_AUTOADD", "true").lower() == "true"
# Require a known hold time before auto-adding
REQUIRE_HOLD_TIME  = os.getenv("FOMO_REQUIRE_HOLD_TIME", "true").lower() == "true"


SYSTEM_PROMPT = """You are a copy-trading due-diligence analyst. You decide whether a Solana wallet should be added to a live copy-trade watchlist, where its buys will be mirrored with real position sizing.

You are the last check before capital follows this wallet. Be skeptical.

WHAT ACTUALLY PREDICTS COPY-TRADE SUCCESS:
1. SUSTAINED RECORD — a 30-day win rate matters far more than a 7-day one. Leaderboards are survivorship-selected: they show who won recently, never who blew up. A great week is usually luck.
2. HOLD TIME vs YOUR EXECUTION LAG — this is the single most underrated factor. If the wallet averages 10-minute holds, you cannot copy it; by the time the signal reaches you the move is over and you are exit liquidity. Hold times under ~1 hour are effectively uncopyable. 4+ hours is workable.
3. CONSISTENCY, NOT SIZE — many modest wins beat one moonshot. A wallet whose profit comes from a single 100x is unrepeatable; a wallet with steady 2-3x wins has a process.
4. TRADE FREQUENCY — too few trades means no statistical confidence. Too many (hundreds a day) means it is a bot you cannot follow.
5. TAGS AND STYLE — "kol"/influencer wallets are already front-run by their own followers, so copying them means buying after the crowd. "launchpad_smart" or early-entry behavior is genuinely valuable.
6. RUG/HONEYPOT EXPOSURE — a wallet that repeatedly touches honeypots either has poor filtering or is farming its own tokens.

RED FLAGS THAT SHOULD PRODUCE REJECT:
- No 30-day history at all (unproven — cannot distinguish skill from luck)
- Hold time unknown AND unable to infer it (you would be copying blind)
- 7-day win rate dramatically above 30-day (a hot streak, not a baseline)
- Profit concentrated in one trade
- High fast-transaction ratio (bot-speed execution you cannot match)
- Influencer/KOL wallet whose entries are already public

Be honest about uncertainty. "Insufficient data to justify capital" is a perfectly good verdict and is usually correct. It is far cheaper to skip a good wallet than to copy a bad one.

Output MUST be valid JSON, no markdown fences, exactly this shape:
{
  "verdict": "ADD" | "REVIEW" | "REJECT",
  "confidence": <integer 0-100>,
  "copyability": <integer 0-100 — can we realistically mirror this wallet's timing?>,
  "reasoning": "<2-4 plain sentences. What's genuinely good, what's uncertain, and why this verdict.>",
  "key_strength": "<the single most compelling thing about this wallet>",
  "key_concern": "<the single biggest risk in copying it>",
  "suggested_tier": "A" | "B",
  "hold_time_assessment": "<is the hold time copyable given ~5-15 min signal lag? If unknown, say so plainly.>"
}"""


def _build_context(c: dict) -> str:
    """Assemble everything known about a candidate wallet."""
    def fmt_pct(v):
        return f"{v*100:.0f}%" if isinstance(v, (int, float)) and v <= 1 else (str(v) if v else "unknown")

    hold_min = c.get("avg_hold_minutes")
    if hold_min:
        hold_str = (f"{hold_min:.0f} minutes ({hold_min/60:.1f} hours)"
                    if hold_min < 1440 else f"{hold_min/1440:.1f} days")
    else:
        hold_str = "UNKNOWN — not reported by GMGN"

    tags = ", ".join(c.get("tags", [])) or "none"
    vet  = c.get("vetting") or {}

    return f"""=== COPY-TRADE CANDIDATE REVIEW ===
Reviewed: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}

WALLET: {c.get('wallet','?')}
Alias/handle: {c.get('twitter') or c.get('alias') or 'none'}

--- PERFORMANCE ---
Win rate (30d):      {fmt_pct(c.get('winrate_30d'))}   <- the number that matters
Win rate (7d):       {fmt_pct(c.get('win_rate') or c.get('winrate_7d'))}
Realized P&L 7d:     ${float(c.get('realized_pnl_7d') or 0):,.0f}
Realized P&L 30d:    ${float(c.get('realized_pnl_30d') or 0):,.0f}
Total P&L 7d:        ${float(c.get('pnl_7d') or 0):,.0f}

--- BEHAVIOR ---
Average hold time:   {hold_str}
Buys in last 7d:     {c.get('buys_7d', 'unknown')}
Avg trades/day:      {c.get('avg_trades_per_day', 'unknown')}
Open positions:      {c.get('open_positions', 'unknown')}
Fast-tx ratio:       {c.get('fast_tx_ratio', 'unknown')}  (high = bot speed we can't match)
Honeypot ratio:      {c.get('honeypot_ratio', 'unknown')}
Tags:                {tags}

--- RULE-BASED VETTING (already run) ---
Score: {vet.get('score', 'n/a')}/100
Recommendation: {vet.get('recommendation', 'n/a')}
Flags: {', '.join(vet.get('flags', [])) or 'none'}

--- OUR EXECUTION REALITY ---
Signal reaches us via Solscan email alerts with roughly 5-15 minutes of lag.
We then paper-buy at market. Any wallet whose edge decays faster than that
window is not copyable regardless of how good its numbers look.

=== END ===

Give your JSON verdict."""


def _parse(raw: str) -> Optional[dict]:
    txt = raw.strip()
    if "```" in txt:
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", txt)
        if m:
            txt = m.group(1).strip()
    else:
        s, e = txt.find("{"), txt.rfind("}")
        if s != -1 and e > s:
            txt = txt[s:e + 1]
    try:
        v = json.loads(txt)
    except json.JSONDecodeError:
        return None
    if "verdict" not in v or "reasoning" not in v:
        return None
    v["confidence"]  = int(v.get("confidence", 50))
    v["copyability"] = int(v.get("copyability", 50))
    if v["verdict"] not in ("ADD", "REVIEW", "REJECT"):
        v["verdict"] = "REVIEW"
    return v


def deep_analyze_candidate(candidate: dict) -> dict:
    """Run AI due diligence on one wallet. Always returns a dict."""
    if not ANTHROPIC_KEY:
        return {"verdict": "REVIEW", "confidence": 0, "copyability": 0,
                "reasoning": "No API key — cannot analyze.", "key_strength": "",
                "key_concern": "Analysis unavailable", "suggested_tier": "B",
                "hold_time_assessment": ""}
    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=AI_MODEL,
            max_tokens=900,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_context(candidate)}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        parsed = _parse(raw)
        if parsed:
            return parsed
        log.warning(f"Auto-add: unparseable analysis for {candidate.get('wallet','?')[:8]}")
    except Exception as e:
        log.error(f"Auto-add: analysis failed for {candidate.get('wallet','?')[:8]}: {e}")

    return {"verdict": "REVIEW", "confidence": 0, "copyability": 0,
            "reasoning": "Analysis error — flagged for manual review.",
            "key_strength": "", "key_concern": "Could not complete analysis",
            "suggested_tier": "B", "hold_time_assessment": ""}


def _admission_check(c: dict, analysis: dict) -> tuple[bool, str]:
    """Final gate. Returns (should_add, reason_if_not)."""
    vet   = c.get("vetting") or {}
    score = vet.get("score") or 0

    if not AUTO_ADD_ENABLED:
        return False, "auto-add disabled"
    if analysis["verdict"] != "ADD":
        return False, f"AI verdict {analysis['verdict']}"
    if score < AUTO_ADD_MIN_SCORE:
        return False, f"score {score} < {AUTO_ADD_MIN_SCORE}"
    if REQUIRE_HOLD_TIME and not c.get("avg_hold_minutes"):
        return False, "hold time unknown — can't verify copyability"
    if analysis.get("copyability", 0) < 50:
        return False, f"copyability {analysis.get('copyability')}/100 too low"
    return True, ""


def _data_is_missing(c: dict) -> bool:
    """
    True when the API returned nothing usable for this wallet.

    Critical distinction: "we evaluated this and it's bad" vs "we couldn't
    evaluate it". Treating an outage as a rejection permanently shelves good
    wallets, because rejected candidates go into the 30-day seen-cache and
    never resurface.
    """
    wr30 = c.get("winrate_30d") or 0
    wr7  = c.get("win_rate") or c.get("winrate_7d") or 0
    pnl  = abs(float(c.get("realized_pnl_7d") or 0)) + abs(float(c.get("pnl_7d") or 0))
    hold = c.get("avg_hold_minutes")

    # No win rate at all, from either window, and no hold time = empty response
    if not wr30 and not wr7 and hold is None:
        return True
    # Win rates zero but real money moved — partial response, win rate missing
    if not wr30 and not wr7 and pnl > 100:
        return True
    return False


def process_candidates(candidates: list, existing_wallets: set,
                       wallet_data: dict) -> dict:
    """
    Analyze every candidate and add the qualifying ones to wallet_data in place.

    wallet_data: the trusted_wallets dict ({"tier_a": [...], "tier_b": [...]})
    Returns {"added": [...], "review": [...], "rejected": [...]}
    """
    results = {"added": [], "review": [], "rejected": [], "unavailable": []}

    for c in candidates:
        wallet = c.get("wallet", "")
        if not wallet or wallet in existing_wallets:
            continue

        vet   = c.get("vetting") or {}
        score = vet.get("score") or 0

        # Couldn't evaluate ≠ evaluated and rejected. Don't spend an AI call,
        # and don't let the caller shelve it in the 30-day seen-cache.
        if _data_is_missing(c):
            log.warning(
                f"Auto-add: {wallet[:8]}... data unavailable "
                f"(API degraded) — deferring, not rejecting"
            )
            results["unavailable"].append(c)
            continue

        # Skip the clearly-bad without spending an AI call
        if score < REVIEW_MIN_SCORE:
            results["rejected"].append({
                **c, "analysis": {"verdict": "REJECT",
                                  "reasoning": f"Vetting score {score} below review floor "
                                               f"{REVIEW_MIN_SCORE} — not worth deeper analysis."}
            })
            continue

        analysis = deep_analyze_candidate(c)
        c = {**c, "analysis": analysis}

        should_add, why_not = _admission_check(c, analysis)
        if should_add:
            tier = "tier_a" if score >= TIER_A_MIN_SCORE else "tier_b"
            entry = {
                "wallet":     wallet,
                "alias":      c.get("twitter") or c.get("alias") or wallet[:8],
                "added_at":   datetime.now(timezone.utc).isoformat(),
                "added_by":   "auto_discovery",
                "vetting":    vet,
                "analysis":   analysis,
                "source":     c.get("source", "gmgn_discovery"),
            }
            wallet_data.setdefault(tier, []).append(entry)
            existing_wallets.add(wallet)
            c["_tier"] = tier
            results["added"].append(c)
            log.warning(
                f"AUTO-ADDED {entry['alias']} ({wallet[:8]}...) to {tier} — "
                f"score {score}, copyability {analysis.get('copyability')}"
            )
        elif analysis["verdict"] == "REJECT" or score < AUTO_ADD_MIN_SCORE:
            c["_why"] = why_not
            results["rejected"].append(c)
        else:
            c["_why"] = why_not
            results["review"].append(c)

    return results


def format_autoadd_telegram(results: dict) -> str:
    """A decision record, not a to-do list."""
    added       = results.get("added", [])
    review      = results.get("review", [])
    rejected    = results.get("rejected", [])
    unavailable = results.get("unavailable", [])

    if not (added or review or rejected or unavailable):
        return "🔍 <b>Wallet discovery</b>\nNo new candidates this week."

    lines = ["🤖 <b>WALLET DISCOVERY — auto-analyzed</b>\n"]

    if added:
        lines.append(f"✅ <b>ADDED TO WATCHLIST ({len(added)})</b>")
        for c in added:
            a     = c["analysis"]
            alias = c.get("twitter") or c.get("alias") or c["wallet"][:8]
            tier  = "A" if c.get("_tier") == "tier_a" else "B"
            wr30  = c.get("winrate_30d")
            wr_s  = f"{wr30*100:.0f}%" if wr30 else "?"
            lines.append(
                f"\n<b>{alias}</b> → Tier {tier}\n"
                f"  30d WR: {wr_s} | copyability {a.get('copyability')}/100\n"
                f"  💬 {a.get('reasoning','')}\n"
                f"  ⏱ {a.get('hold_time_assessment','')}\n"
                f"  ⚠️ {a.get('key_concern','')}\n"
                f"  <code>{c['wallet']}</code>"
            )
        lines.append("\n<i>Already live — these are being tracked now.</i>\n")

    if review:
        lines.append(f"\n🟡 <b>YOUR CALL ({len(review)})</b>")
        for c in review:
            a     = c.get("analysis", {})
            alias = c.get("twitter") or c.get("alias") or c["wallet"][:8]
            lines.append(
                f"\n<b>{alias}</b> — {c.get('_why','')}\n"
                f"  💬 {a.get('reasoning','')}\n"
                f"  <code>{c['wallet']}</code>"
            )
        lines.append("\n<i>Reply with an address to add manually.</i>\n")

    if unavailable:
        lines.append(f"\n⚠️ <b>COULDN'T EVALUATE ({len(unavailable)})</b>")
        lines.append("<i>GMGN/parse.bot returned no data — these were NOT rejected "
                     "and will resurface next run.</i>")
        for c in unavailable[:8]:
            alias = c.get("twitter") or c.get("alias") or c["wallet"][:8]
            lines.append(f"  • {alias}")
        if len(unavailable) > 8:
            lines.append(f"  • …and {len(unavailable)-8} more")
        lines.append("")

    if rejected:
        lines.append(f"\n❌ <b>Rejected ({len(rejected)})</b>")
        for c in rejected[:6]:
            a     = c.get("analysis", {})
            alias = c.get("twitter") or c.get("alias") or c["wallet"][:8]
            why   = c.get("_why") or a.get("key_concern") or "failed vetting"
            lines.append(f"  • {alias} — {why}")
        if len(rejected) > 6:
            lines.append(f"  • …and {len(rejected)-6} more")

    return "\n".join(lines)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = {
        "wallet": "FVZRwUp6E4m9jV4VumF8q7m8q3mF9fpikRrJSCCfFAdP",
        "twitter": "@000xy_0", "win_rate": 0.68, "winrate_30d": 0.62,
        "realized_pnl_7d": 9432, "realized_pnl_30d": 24000,
        "avg_hold_minutes": 320, "buys_7d": 84, "avg_trades_per_day": 12,
        "fast_tx_ratio": 0.08, "honeypot_ratio": 0.0,
        "tags": ["launchpad_smart", "gmgn"],
        "vetting": {"score": 74, "recommendation": "COPY_TRADE", "flags": []},
    }
    print(json.dumps(deep_analyze_candidate(sample), indent=2))
