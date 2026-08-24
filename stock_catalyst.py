#!/usr/bin/env python3
"""
stock_catalyst.py — Catalyst quality analysis for Pillar 3.

THE PROBLEM THIS SOLVES:
Pillar 3 was `news_count > 0`. That treats every headline as equally bullish,
which means a stock up 40% on a dilutive offering scores the same as one up 40%
on FDA approval. Those are opposite trades. The first is a fade candidate — the
offering IS the reason it moved, and the shares are about to be sold into.

Ross: "I always check the news catalyst before taking a trade so I can
understand why it's moving higher... there must be a headline that justifies
why the stock is moving."

TWO LAYERS, deliberately:

  1. RULE-BASED classification runs first. Keywords like "offering",
     "reverse split", "going concern" are near-disqualifying regardless of
     what any model thinks about them. Deterministic, free, instant, and
     cannot be talked out of it by persuasive phrasing.

  2. AI reads the actual content for nuance the keywords miss — whether an
     earnings beat was actually strong, whether a "partnership" has revenue
     attached or is a press release with no substance.

The rule layer can VETO on its own. The AI layer can only refine within what
the rules allow. Same asymmetry as stock_research.py, for the same reason.
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
USE_AI        = os.getenv("STOCK_CATALYST_AI", "true").lower() == "true"


# ─── RULE LAYER ───────────────────────────────────────────────────────────────
# Weighted keyword patterns. Negative weights are dilution/distress events that
# move price without supporting continuation.

HARMFUL = {
    # Dilution — the single most common trap on small-cap runners
    r"\b(public|registered direct|underwritten)\s+offering\b":      -60,
    r"\bat[- ]the[- ]market\b|\batm\s+(offering|program|facility)\b": -55,
    r"\bpricing of\b.*\boffering\b":                                 -60,
    r"\bshelf registration\b|\bs-3\b|\bf-3\b":                       -35,
    r"\bwarrant\s+(exercise|inducement)\b":                          -40,
    r"\bconvertible\s+(note|debenture|preferred)\b":                 -45,
    r"\bprivate placement\b|\bpipe\b":                               -45,
    r"\bdilut(ion|ive)\b":                                           -50,
    # Distress
    r"\breverse\s+(stock\s+)?split\b":                               -50,
    r"\bgoing concern\b":                                            -60,
    r"\bdelisting\b|\bnon[- ]compliance\b|\bdeficiency letter\b":     -55,
    r"\bbankrupt|chapter 11\b":                                      -70,
    r"\bsec (investigation|subpoena|charges)\b":                     -60,
    r"\brestat(e|ement) of\b.*financ":                               -50,
    r"\bclass action\b|\bsecurities fraud\b":                        -40,
}

STRONG = {
    # Regulatory wins — the classic biotech runner.
    # NOTE: allow words between "FDA" and the action — real headlines read
    # "Receives FDA 510(k) Clearance", not "FDA clearance". Requiring adjacency
    # silently missed the single most bullish small-cap catalyst there is.
    r"\bfda\b.{0,30}?(approv|clearance|clears|grants|accepts|authoriz)":  55,
    r"(approv|clearance|clears).{0,20}?\bfda\b":                         55,
    # No trailing \b after "510(k)" — ")" is a non-word char, so \b never
    # matches before a following space.
    r"510\s*\(\s*k\s*\)":                                                45,
    r"\b(pma|breakthrough (device|therapy)|fast track|orphan drug)\b":   45,
    r"\bce mark\b|\bema approv":                                         40,
    # Trial results — the qualifier can appear on either side of the phase.
    # "Positive Topline Phase 3 Results" and "Phase 3 Meets Primary Endpoint"
    # are the same event written two ways.
    r"\bphase\s*(2|3|ii|iii)\b.{0,60}?\b(success|positive|met|topline|endpoint)\b": 50,
    r"\b(success|positive|topline)\b.{0,60}?\bphase\s*(2|3|ii|iii)\b":              50,
    r"\bmeets? (its )?primary endpoint\b":                                          50,
    # Commercial substance
    r"\b(awarded|wins|secures|receives)\b.*\b(contract|order|award)\b": 45,
    r"\bacquisition of\b|\bto be acquired\b|\bmerger agreement\b":     50,
    r"\b(partnership|collaboration|agreement)\b.*\b(revenue|million|billion)\b": 40,
    r"\bdefinitive agreement\b":                                      40,
    # Financial
    r"\b(beats|exceeds|tops)\b.*\b(estimates|expectations|consensus)\b": 40,
    r"\braises?\s+(guidance|outlook|forecast)\b":                     45,
    r"\brecord\s+(revenue|quarter|earnings|sales)\b":                 35,
    r"\bprofitab(le|ility)\b.*\bfirst time\b":                        40,
    # Structural
    r"\buplist(ing|ed)?\b.*\b(nasdaq|nyse)\b":                        35,
    r"\b(added|inclusion) to\b.*\b(russell|s&p|index)\b":             35,
    r"\bshare (buyback|repurchase)\b":                                30,
    r"\bpatent\s+(granted|issued|allowance)\b":                       30,
}

WEAK = {
    r"\banalyst\b|\binitiat(es|ed) coverage\b|\bprice target\b":      12,
    r"\bpresent(s|ing|ation)\b.*\bconference\b":                       8,
    r"\bappoints?\b|\bnames?\b.*\b(ceo|cfo|director)\b":              10,
    r"\blaunch(es|ed|ing)?\b.*\bproduct\b":                           18,
    r"\bletter of intent\b|\bmou\b|\bnon[- ]binding\b":               10,
    r"\bupdate\b|\bprovides? update\b":                                5,
}


def classify_rules(headlines: list) -> dict:
    """
    Deterministic keyword scoring across all recent headlines.

    Returns the worst harmful hit alongside the best positive one — a stock can
    have an FDA approval AND an offering the same morning, and the offering is
    what matters for whether the move holds.
    """
    if not headlines:
        return {"score": 0, "category": "none", "matches": [],
                "harmful": [], "note": "no headlines found"}

    text = " | ".join(headlines).lower()
    matches, harmful_hits, score = [], [], 0

    for pat, w in HARMFUL.items():
        if re.search(pat, text, re.I):
            harmful_hits.append({"pattern": pat, "weight": w})
            matches.append({"type": "harmful", "weight": w})
            score += w

    best_positive = 0
    for pat, w in STRONG.items():
        if re.search(pat, text, re.I):
            matches.append({"type": "strong", "weight": w})
            best_positive = max(best_positive, w)
    score += best_positive

    weak_total = 0
    for pat, w in WEAK.items():
        if re.search(pat, text, re.I):
            matches.append({"type": "weak", "weight": w})
            weak_total = max(weak_total, w)
    if best_positive == 0:
        score += weak_total

    if harmful_hits:
        category = "harmful"
    elif best_positive >= 40:
        category = "strong"
    elif best_positive > 0 or weak_total >= 15:
        category = "moderate"
    elif weak_total > 0:
        category = "weak"
    else:
        category = "unclear"

    return {
        "score":    max(-100, min(100, score)),
        "category": category,
        "matches":  matches,
        "harmful":  harmful_hits,
        "note":     _rule_note(category, harmful_hits, best_positive),
    }


def _rule_note(category: str, harmful: list, best: int) -> str:
    if category == "harmful":
        return ("Dilution or distress language detected — the move may be the "
                "news itself, with shares about to be sold into it.")
    if category == "strong":
        return f"Clear fundamental catalyst (strength {best})."
    if category == "moderate":
        return "Some catalyst present but not a headline event."
    if category == "weak":
        return "Only routine corporate news — weak justification for the move."
    return "No recognisable catalyst — higher risk of a sudden reversal."


# ─── AI LAYER ─────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You assess whether a news catalyst justifies a small-cap stock's intraday move, for a momentum day-trading system.

The question is narrow: does this news support the stock CONTINUING higher over the next 30-120 minutes, or is the move likely to fade?

WHAT ACTUALLY SUPPORTS CONTINUATION:
- Regulatory approvals (FDA clearance, CE mark, positive trial data) — creates genuine revaluation
- Contract or order wins with named counterparties and dollar figures
- Acquisitions, especially the target being bought at a premium
- Earnings beats WITH raised guidance — a beat alone often fades
- Index inclusion or uplisting — forced buying from funds

WHAT LOOKS BULLISH BUT ISN'T:
- ANY capital raise: offerings, ATM programs, private placements, convertibles,
  warrant inducements. The stock may spike on "financing secured" but new shares
  are about to hit the market. This is the single most common trap on small-cap
  runners.
- Reverse splits — mechanical price change, usually distress
- Letters of intent, MOUs, non-binding agreements — no revenue attached
- "Partnership" announcements with no dollar figure
- Analyst price targets — not a fundamental change
- Conference presentations, corporate updates — filler PR

BE SKEPTICAL OF VAGUENESS. Small-cap promoters write headlines designed to move
a stock without saying anything. "Company announces strategic initiative in AI"
is not a catalyst. If you can't identify what concretely changed about the
business, it isn't one.

NO NEWS is a valid finding. Ross prefers a catalyst but will trade without one —
it simply carries more risk of a sudden drop.

Output MUST be valid JSON, no markdown fences:
{
  "quality": "strong" | "moderate" | "weak" | "harmful" | "none",
  "score": <integer -100 to 100, negative = actively bearish>,
  "supports_continuation": true | false,
  "catalyst_type": "<short label, e.g. 'FDA clearance', 'dilutive offering', 'analyst note'>",
  "reasoning": "<1-2 sentences. What concretely changed, or why it's noise.>",
  "dilution_risk": true | false
}"""


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
    if "quality" not in v:
        return None
    v["score"] = int(v.get("score", 0))
    return v


def analyze_ai(symbol: str, headlines: list, pct_change: float) -> Optional[dict]:
    if not (USE_AI and ANTHROPIC_KEY and headlines):
        return None
    try:
        heads = "\n".join(f"- {h}" for h in headlines[:6])
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp = client.messages.create(
            model=AI_MODEL, max_tokens=500, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content":
                       f"Symbol: {symbol}\nMove today: {pct_change:+.1f}%\n\n"
                       f"Headlines (last 48h):\n{heads}\n\n"
                       f"Does this justify the move continuing?"}],
        )
        raw = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        return _parse(raw)
    except Exception as e:
        log.warning(f"Catalyst AI failed for {symbol}: {e}")
        return None


# ─── COMBINED ─────────────────────────────────────────────────────────────────

def analyze_catalyst(symbol: str, headlines: list, pct_change: float = 0.0) -> dict:
    """
    Full assessment. Rules run first and can veto outright; the AI refines
    within what the rules permit but cannot rescue a dilution event.
    """
    rules = classify_rules(headlines)

    # Hard veto — no AI call needed, and no AI opinion can override this
    if rules["category"] == "harmful":
        return {
            "symbol":     symbol,
            "quality":    "harmful",
            "score":      rules["score"],
            "passes":     False,
            "supports_continuation": False,
            "catalyst_type": "dilution/distress",
            "reasoning":  rules["note"],
            "dilution_risk": True,
            "headline_count": len(headlines),
            "source":     "rules",
        }

    ai = analyze_ai(symbol, headlines, pct_change)

    if ai:
        # Blend, but let the more pessimistic view win — a rule hit the model
        # missed matters more than a model's enthusiasm.
        score   = min(rules["score"], ai["score"]) if ai["score"] < rules["score"] else \
                  int(rules["score"] * 0.4 + ai["score"] * 0.6)
        quality = ai["quality"]
        if ai.get("dilution_risk"):
            quality, score = "harmful", min(score, -30)
        return {
            "symbol":     symbol,
            "quality":    quality,
            "score":      score,
            "passes":     quality in ("strong", "moderate") and score > 15,
            "supports_continuation": bool(ai.get("supports_continuation")),
            "catalyst_type": ai.get("catalyst_type", "?"),
            "reasoning":  ai.get("reasoning", rules["note"]),
            "dilution_risk": bool(ai.get("dilution_risk")),
            "headline_count": len(headlines),
            "source":     "rules+ai",
        }

    return {
        "symbol":     symbol,
        "quality":    rules["category"],
        "score":      rules["score"],
        "passes":     rules["category"] in ("strong", "moderate"),
        "supports_continuation": rules["category"] == "strong",
        "catalyst_type": rules["category"],
        "reasoning":  rules["note"],
        "dilution_risk": False,
        "headline_count": len(headlines),
        "source":     "rules",
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    tests = [
        ("BIOX", ["BioX Receives FDA 510(k) Clearance for Cardiac Monitor"]),
        ("DILU", ["Company Announces Pricing of $50 Million Public Offering"]),
        ("MIXD", ["FDA Approves Lead Candidate",
                  "Company Announces $30M Registered Direct Offering"]),
        ("VAGU", ["Company Announces Strategic Initiative in Artificial Intelligence"]),
        ("CONT", ["XYZ Awarded $120 Million Contract by U.S. Navy"]),
        ("SPLIT", ["Board Approves 1-for-20 Reverse Stock Split"]),
        ("NONE", []),
    ]
    print(f"{'SYMBOL':8s} {'CATEGORY':10s} {'SCORE':>6s}  NOTE")
    print("-" * 78)
    for sym, heads in tests:
        r = classify_rules(heads)
        print(f"{sym:8s} {r['category']:10s} {r['score']:>6d}  {r['note'][:52]}")
