#!/usr/bin/env python3
"""
fomo_research.py — Deep research engine for FOMO trade signals.

Replaces the basic catalyst scanner. Called before every buy decision to produce
a scored verdict with full evidence chain:
  - Token fundamentals (age, supply, holder concentration)
  - DEX health (liquidity depth, price momentum)
  - CT sentiment (Twitter free-tier search)
  - Cross-wallet conviction (other tracked wallets in same token)
  - FOMO culture assessment (Claude: language, timing, cultural fit)

Usage:
    from fomo_research import research_token
    verdict = research_token(contract, chain, signal_context)
    if verdict.go:
        # execute trade
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field
from typing import Optional

import anthropic
import requests

log = logging.getLogger(__name__)

HELIUS_API_KEY       = os.environ.get("HELIUS_API_KEY", "")
TWITTER_BEARER       = os.environ.get("TWITTER_BEARER_TOKEN", "")
ANTHROPIC_KEY        = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL             = "claude-sonnet-4-5"
HEADERS              = {"User-Agent": "CryptoOracle/3.0 (fomo-research)"}
TRUSTED_WALLETS_FILE = "trusted_wallets.json"
FOMO_PORTFOLIO_FILE  = "fomo_portfolio.json"


# ─── FOMO CULTURE KNOWLEDGE BASE ──────────────────────────────────────────────

FOMO_CULTURE_SYSTEM = """You are a deep expert in Solana memecoin culture and the FOMO.family copy-trading ecosystem.

FOMO.FAMILY DYNAMICS:
- Top traders earn leaderboard rank via realized PnL, not follower count — real traders, not influencers
- Tier A wallets ($24K–$1.4M bankrolls) can 2–10x smaller-cap tokens just by entering
- When 2+ leaderboard traders enter the same token within 30 min = coordinated early alpha
- These traders typically hold 1–48 hours; multi-day hold = unusual high conviction
- Traders on fomo.family often signal socially AFTER entering on-chain — on-chain is faster

BUY SIGNAL LANGUAGE (read posts, Telegram, X):
Explicit: "aping", "loading", "sending it", "heavy bag", "full port", "adding more", "this is early", "conviction buy"
Implicit: posting contract address (CA), sharing chart, "👀", "👀🔥", "gm [TOKEN]", sharing open PnL
Context tells: "CT sleeping on this" = early contrarian entry. "Everyone's talking about X" = likely too late.

SELL SIGNAL LANGUAGE:
Partial exit: "taking profits", "half off", "trimmed", "scaling out", "sold some"
Full exit: "out", "fully out", "took my bag", "rekt", "cut", "stop loss hit", "this was a mistake"
Ambiguous: "nice trade", "that was fun" — treat as full exit if no position context

TIMING PATTERNS (Eastern Time):
- Hot windows: 9–11 am ET (US morning), 8–11 pm ET (Asia/EU overlap, often strongest)
- Weekend 2–6 pm ET: cross-timezone volume, good for smaller caps
- Monday morning: new week FOMO energy, often sets tone for the week
- Friday 3–5 pm ET: profit-taking into weekend, avoid new entries

RUG RISK INDICATORS:
- Token < 48 h old + no well-known backing = elevated rug risk
- Top 10 holders > 80% supply = whale dump risk
- No LP lock or lock < 30 days = rug risk
- Dev wallet retains > 10% supply = dump risk
- Volume spike with zero CT catalyst = wash trading / fake pump

HIGHEST CONVICTION BUY SIGNALS:
- 2+ tracked Tier A wallets buying the same token within 30 min
- Trader making their largest entry in the past week
- Token 3–14 days old with accelerating holder growth
- CT "sleeping on it" while our trackers are buying = perfect early setup
- Our entry price at or below the tracked wallet's average buy

SELL AMPLIFIERS (exit faster / harder than the signal alone):
- Original trader sells > 50% immediately after our buy → exit fast
- Token age > 21 days and we're seeing it for the first time (we're late)
- 2+ tracked wallets exiting the same token simultaneously → coordinated dump
- Sudden volume collapse on a held token with no news

Respond ONLY with valid JSON, no markdown fences."""


# ─── DATA CLASSES ─────────────────────────────────────────────────────────────

@dataclass
class ResearchVerdict:
    """Structured output from the research engine."""
    token_symbol:        str
    contract:            str
    chain:               str

    # Sub-scores 0–10
    fundamentals_score:  int  = 0   # token age, supply health
    liquidity_score:     int  = 0   # DEX depth
    ct_score:            int  = 0   # Twitter sentiment quality
    conviction_score:    int  = 0   # source tier + cross-wallet
    culture_score:       int  = 0   # FOMO culture fit
    final_score:         int  = 0   # weighted composite

    # Evidence
    evidence:            list = field(default_factory=list)
    warnings:            list = field(default_factory=list)

    # Metadata
    token_age_days:      Optional[float] = None
    liquidity_usd:       Optional[float] = None
    market_cap_usd:      Optional[float] = None
    top10_holder_pct:    Optional[float] = None
    cross_wallet_hits:   int  = 0
    cross_wallet_names:  list = field(default_factory=list)
    ct_summary:          str  = ""
    culture_assessment:  str  = ""
    culture_insight:     str  = ""

    go:                  bool = False
    go_reason:           str  = ""
    skip_reason:         str  = ""

    def compute_final_score(self):
        """Weighted composite: conviction 30%, fundamentals 25%, liquidity 20%, culture 15%, CT 10%."""
        self.final_score = min(10, round(
            self.conviction_score   * 0.30 +
            self.fundamentals_score * 0.25 +
            self.liquidity_score    * 0.20 +
            self.culture_score      * 0.15 +
            self.ct_score           * 0.10
        ))
        return self.final_score

    def to_telegram_summary(self) -> str:
        icon  = "🟢" if self.go else "🔴"
        lines = [f"{icon} <b>Research: ${self.token_symbol}</b>  Score {self.final_score}/10"]
        if self.token_age_days is not None:
            lines.append(f"📅 Age: {self.token_age_days:.1f}d")
        if self.liquidity_usd:
            lines.append(f"💧 Liquidity: ${self.liquidity_usd:,.0f}")
        if self.market_cap_usd:
            lines.append(f"📊 MCap: ${self.market_cap_usd:,.0f}")
        if self.top10_holder_pct is not None:
            tag = " ⚠️ WHALE RISK" if self.top10_holder_pct > 75 else ""
            lines.append(f"🐋 Top-10 holders: {self.top10_holder_pct:.0f}%{tag}")
        if self.cross_wallet_hits > 0:
            names = ", ".join(self.cross_wallet_names)
            lines.append(f"🔥 <b>Cross-wallet: {names} also in this token</b>")
        if self.ct_summary:
            lines.append(f"🐦 CT: {self.ct_summary}")
        if self.culture_insight:
            lines.append(f"🧠 {self.culture_insight}")
        if self.warnings:
            lines.append("⚠️ " + " | ".join(self.warnings[:3]))
        if self.evidence:
            lines.append("✔ " + " | ".join(self.evidence[:2]))
        verdict = f"✅ {self.go_reason}" if self.go else f"❌ SKIP: {self.skip_reason}"
        lines.append(f"\n{verdict}")
        return "\n".join(lines)


# ─── INTERNAL HELPERS ─────────────────────────────────────────────────────────

def _dex_data(contract: str, chain: str) -> dict:
    """Pull the best trading pair for this token from DexScreener."""
    try:
        r = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{contract}",
            timeout=10,
            headers=HEADERS,
        )
        if r.status_code != 200:
            return {}
        pairs = r.json().get("pairs", [])
        if not pairs:
            return {}
        # Sort by liquidity — first entry = deepest market
        pairs.sort(key=lambda p: ((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
        p = pairs[0]
        return {
            "name":        p.get("baseToken", {}).get("name", ""),
            "symbol":      p.get("baseToken", {}).get("symbol", ""),
            "price_usd":   float(p.get("priceUsd") or 0),
            "market_cap":  float(p.get("fdv") or 0),
            "liquidity":   float((p.get("liquidity") or {}).get("usd") or 0),
            "volume_5m":   float((p.get("volume") or {}).get("m5") or 0),
            "volume_1h":   float((p.get("volume") or {}).get("h1") or 0),
            "volume_24h":  float((p.get("volume") or {}).get("h24") or 0),
            "price_5m":    float((p.get("priceChange") or {}).get("m5") or 0),
            "price_1h":    float((p.get("priceChange") or {}).get("h1") or 0),
            "price_24h":   float((p.get("priceChange") or {}).get("h24") or 0),
            "created_at":  p.get("pairCreatedAt"),   # epoch ms, may be None
            "dex":         p.get("dexId", ""),
        }
    except Exception as e:
        log.debug(f"DexScreener error for {contract[:8]}: {e}")
        return {}


def _token_age_days(created_at_ms) -> Optional[float]:
    if not created_at_ms:
        return None
    try:
        created = datetime.fromtimestamp(int(created_at_ms) / 1000, tz=timezone.utc)
        return (datetime.now(timezone.utc) - created).total_seconds() / 86400
    except Exception:
        return None


def _solana_holders(contract: str) -> dict:
    """Get Solana token holder concentration via Helius JSON-RPC."""
    if not HELIUS_API_KEY:
        return {}
    url = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
    try:
        # Top 20 largest accounts
        r = requests.post(url, json={
            "jsonrpc": "2.0", "id": 1,
            "method": "getTokenLargestAccounts",
            "params": [contract],
        }, timeout=10)
        if r.status_code != 200:
            return {}
        accounts = r.json().get("result", {}).get("value", [])
        if not accounts:
            return {}

        # Total supply
        r2 = requests.post(url, json={
            "jsonrpc": "2.0", "id": 2,
            "method": "getTokenSupply",
            "params": [contract],
        }, timeout=10)
        total_supply = 0.0
        if r2.status_code == 200:
            total_supply = float(
                r2.json().get("result", {}).get("value", {}).get("uiAmount") or 0
            )

        if total_supply <= 0:
            return {}

        top10_amount = sum(float(a.get("uiAmount") or 0) for a in accounts[:10])
        top10_pct    = (top10_amount / total_supply) * 100

        return {
            "total_supply": total_supply,
            "top10_pct":    top10_pct,
        }
    except Exception as e:
        log.debug(f"Helius holder data error: {e}")
        return {}


def _ct_sentiment(symbol: str) -> dict:
    """Search Twitter (free tier) for CT buzz around this token ticker."""
    if not TWITTER_BEARER:
        return {"summary": "Twitter not configured", "score": 0}
    try:
        r = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={
                "query":        f"${symbol} lang:en -is:retweet",
                "max_results":  20,
                "tweet.fields": "created_at,public_metrics",
                "sort_order":   "recency",
            },
            headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
            timeout=10,
        )
        if r.status_code == 429:
            return {"summary": "Twitter rate limited", "score": 3}  # neutral, don't penalise
        if r.status_code != 200:
            return {"summary": f"Twitter {r.status_code}", "score": 0}

        tweets = r.json().get("data", [])
        if not tweets:
            return {"summary": "CT quiet — contrarian setup possible", "score": 2}

        cutoff_1h = datetime.now(timezone.utc) - timedelta(hours=1)
        cutoff_4h = datetime.now(timezone.utc) - timedelta(hours=4)

        recent_1h   = [t for t in tweets
                       if datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")) > cutoff_1h]
        recent_4h   = [t for t in tweets
                       if datetime.fromisoformat(t["created_at"].replace("Z", "+00:00")) > cutoff_4h]
        total_likes = sum(t.get("public_metrics", {}).get("like_count", 0) for t in tweets)
        total_rt    = sum(t.get("public_metrics", {}).get("retweet_count", 0) for t in tweets)

        score = 0
        notes = []

        if len(recent_1h) >= 5:
            score += 3
            notes.append(f"{len(recent_1h)} tweets/1h")
        elif len(recent_4h) >= 5:
            score += 1
            notes.append(f"{len(recent_4h)} tweets/4h")
        else:
            # Low CT noise while a trusted wallet is buying = contrarian premium
            notes.append("CT quiet")

        if total_likes > 1000:
            score += 3
            notes.append(f"{total_likes:,} likes")
        elif total_likes > 200:
            score += 1
            notes.append(f"{total_likes} likes")

        if total_rt > 200:
            score += 2
            notes.append(f"{total_rt} RTs")

        return {
            "summary": " | ".join(notes) if notes else "No CT activity",
            "score":   min(score, 10),
            "count":   len(tweets),
        }
    except Exception as e:
        log.debug(f"CT sentiment error: {e}")
        return {"summary": "CT scan failed", "score": 0}


def _cross_wallet_conviction(contract: str, symbol: str, source_alias: str) -> dict:
    """
    Check if OTHER tracked wallets recently bought the same token.
    Looks in fomo_portfolio.json trade history (last 4h) and current holdings.
    """
    hits   = []
    try:
        if not os.path.exists(FOMO_PORTFOLIO_FILE):
            return {"hits": 0, "names": []}
        with open(FOMO_PORTFOLIO_FILE) as f:
            portfolio = json.load(f)

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        sym_upper = symbol.upper()

        # Check current holdings
        for h in portfolio.get("holdings", []):
            if h.get("wallet_alias") and h.get("wallet_alias") != source_alias:
                if (h.get("token_contract") == contract or
                        h.get("token_ticker", "").upper() == sym_upper):
                    hits.append(h["wallet_alias"])

        # Check recent trade history
        for t in portfolio.get("trade_history", []):
            if t.get("action") != "BUY":
                continue
            if t.get("wallet_alias") == source_alias:
                continue
            if t.get("timestamp", "") < cutoff:
                continue
            if (t.get("contract") == contract or
                    t.get("token_ticker", "").upper() == sym_upper):
                hits.append(t.get("wallet_alias", "unknown"))

    except Exception as e:
        log.debug(f"Cross-wallet conviction error: {e}")

    unique = list(set(hits))
    return {"hits": len(unique), "names": unique}


def _culture_assessment(signal_ctx: dict, dex: dict, holders: dict, ct: dict, cross: dict) -> dict:
    """
    Ask Claude Sonnet to rate this signal through the FOMO culture lens.
    Returns: {assessment, score (0–10), insight}
    """
    if not ANTHROPIC_KEY:
        return {"assessment": "", "score": 5, "insight": "No AI key"}

    age_str = f"{dex.get('age_days', '?'):.1f}d" if isinstance(dex.get("age_days"), (int, float)) else "?"
    prompt = (
        f"Assess this FOMO trade signal from a culture/pattern perspective.\n\n"
        f"SIGNAL:\n"
        f"  Trader: {signal_ctx.get('alias')} (Tier {signal_ctx.get('tier', 'B')}, "
        f"  bankroll: ${signal_ctx.get('bankroll_usd') or 'unknown'})\n"
        f"  Action: {signal_ctx.get('action')} ${signal_ctx.get('symbol')}\n"
        f"  Source: {signal_ctx.get('source', 'on-chain')}\n"
        f"  Time: {signal_ctx.get('timestamp', 'now')}\n"
        f"  Post text: {signal_ctx.get('original_text', 'N/A')}\n\n"
        f"TOKEN:\n"
        f"  Age: {age_str}\n"
        f"  Market cap: ${dex.get('market_cap', 0):,.0f}\n"
        f"  Liquidity: ${dex.get('liquidity', 0):,.0f}\n"
        f"  Price 1h: {dex.get('price_1h', 0):+.1f}%\n"
        f"  Top-10 holders: {holders.get('top10_pct', 'unknown')}%\n\n"
        f"SOCIAL:\n"
        f"  CT: {ct.get('summary', '?')}\n"
        f"  Other tracked wallets also in: {cross.get('hits', 0)}\n\n"
        f"Respond as JSON only:\n"
        f'{{"assessment": "2-3 sentence FOMO culture evaluation", '
        f'"score": 0-10, '
        f'"insight": "single most important cultural observation"}}'
    )

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
        resp   = client.messages.create(
            model=AI_MODEL,
            max_tokens=300,
            system=FOMO_CULTURE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return {
            "assessment": str(data.get("assessment", "")),
            "score":      int(data.get("score", 5)),
            "insight":    str(data.get("insight", "")),
        }
    except Exception as e:
        log.warning(f"Culture assessment error: {e}")
        return {"assessment": "", "score": 5, "insight": ""}


# ─── PUBLIC API ───────────────────────────────────────────────────────────────

def research_token(
    contract:       str,
    chain:          str,
    signal_context: dict,
) -> ResearchVerdict:
    """
    Master entry point. Call before every FOMO buy decision.

    signal_context keys:
        alias       str   — wallet alias (e.g. "continuity")
        tier        str   — "A" or "B"
        bankroll_usd float — known bankroll, or None
        action      str   — "BUY" or "SELL"
        symbol      str   — token ticker
        source      str   — "on-chain" | "twitter" | "telegram" | "email"
        timestamp   str   — ISO8601
        original_text str — raw post text if social signal, else None
    """
    symbol = signal_context.get("symbol", "UNKNOWN")
    alias  = signal_context.get("alias", "unknown")
    chain  = chain.lower()

    v = ResearchVerdict(token_symbol=symbol, contract=contract, chain=chain)

    # ── 1. DEX data ───────────────────────────────────────────────────────────
    dex = _dex_data(contract, chain)
    if dex:
        v.liquidity_usd  = dex.get("liquidity")
        v.market_cap_usd = dex.get("market_cap")

        age = _token_age_days(dex.get("created_at"))
        v.token_age_days = age
        dex["age_days"]  = age  # pass through to culture assessment

        # Fundamentals score from token age
        if age is None:
            v.fundamentals_score = 4
        elif age < 1:
            v.warnings.append("Token < 1 day old — rug risk elevated")
            v.fundamentals_score = 1
        elif age < 3:
            v.fundamentals_score = 5
            v.evidence.append(f"Early: {age:.1f}d old")
        elif age < 14:
            v.fundamentals_score = 8
            v.evidence.append(f"Prime window: {age:.1f}d old")
        else:
            v.fundamentals_score = 5
            v.evidence.append(f"Established: {age:.1f}d old")

        # Liquidity score
        liq = dex.get("liquidity", 0)
        if liq >= 500_000:
            v.liquidity_score = 9
            v.evidence.append(f"Deep liq ${liq:,.0f}")
        elif liq >= 200_000:
            v.liquidity_score = 7
            v.evidence.append(f"Healthy liq ${liq:,.0f}")
        elif liq >= 75_000:
            v.liquidity_score = 5
        elif liq >= 30_000:
            v.liquidity_score = 3
            v.warnings.append(f"Thin liq ${liq:,.0f}")
        else:
            v.liquidity_score = 1
            v.warnings.append(f"Very thin liq ${liq:,.0f}")

        # Price momentum bonus signal
        p5m = dex.get("price_5m", 0)
        if p5m > 20:
            v.evidence.append(f"+{p5m:.0f}% in 5m — momentum")
        elif p5m < -15:
            v.warnings.append(f"{p5m:.0f}% dump in 5m")

        # Volume spike
        vol_5m = dex.get("volume_5m", 0)
        vol_1h = dex.get("volume_1h", 0)
        if vol_1h > 0 and vol_5m > 0:
            spike = (vol_5m * 12) / vol_1h
            if spike > 5:
                v.evidence.append(f"Vol {spike:.1f}x spike")
    else:
        v.warnings.append("No DEX pair found — unverifiable liquidity")
        v.fundamentals_score = 2
        v.liquidity_score    = 2

    # ── 2. Holder distribution (Solana only) ──────────────────────────────────
    holders = {}
    if chain == "solana" and HELIUS_API_KEY:
        holders = _solana_holders(contract)
        if holders:
            v.top10_holder_pct = holders.get("top10_pct")
            top10 = holders.get("top10_pct", 0)
            if top10 > 85:
                v.warnings.append(f"Top-10 hold {top10:.0f}% — whale trap risk")
                v.fundamentals_score = max(0, v.fundamentals_score - 3)
            elif top10 > 70:
                v.warnings.append(f"Top-10 hold {top10:.0f}% — concentrated")
                v.fundamentals_score = max(0, v.fundamentals_score - 1)
            else:
                v.evidence.append(f"Distribution OK ({top10:.0f}% top-10)")

    # ── 3. CT sentiment ───────────────────────────────────────────────────────
    ct           = _ct_sentiment(symbol)
    v.ct_score   = ct.get("score", 0)
    v.ct_summary = ct.get("summary", "")

    # ── 4. Cross-wallet conviction ────────────────────────────────────────────
    cross                = _cross_wallet_conviction(contract, symbol, alias)
    v.cross_wallet_hits  = cross["hits"]
    v.cross_wallet_names = cross["names"]

    # Conviction score
    tier     = (signal_context.get("tier") or "B").upper()
    bankroll = float(signal_context.get("bankroll_usd") or 0)
    source   = signal_context.get("source", "on-chain")

    base_conviction = 7 if tier == "A" else 4

    # Cross-wallet multiplier — most powerful signal in the system
    if v.cross_wallet_hits >= 2:
        base_conviction = 10
        v.evidence.append(f"🔥 {v.cross_wallet_hits} other tracked wallets also buying — max conviction")
    elif v.cross_wallet_hits == 1:
        base_conviction = min(10, base_conviction + 3)
        v.evidence.append(f"{v.cross_wallet_names[0]} also in this token")

    # Social source bonus: trader ANNOUNCED it publicly = higher personal conviction
    if source in ("twitter", "telegram"):
        base_conviction = min(10, base_conviction + 1)
        v.evidence.append(f"Trader announced on {source}")

    # Bankroll premium: larger wallet = more market-moving impact
    if bankroll > 500_000:
        base_conviction = min(10, base_conviction + 1)

    v.conviction_score = base_conviction

    # ── 5. FOMO culture assessment ────────────────────────────────────────────
    culture              = _culture_assessment(signal_context, dex, holders, ct, cross)
    v.culture_score      = culture.get("score", 5)
    v.culture_assessment = culture.get("assessment", "")
    v.culture_insight    = culture.get("insight", "")

    # ── 6. Final score + GO / NO-GO ───────────────────────────────────────────
    v.compute_final_score()

    # Hard vetos — override score
    if v.liquidity_usd is not None and v.liquidity_usd < 30_000:
        v.go          = False
        v.skip_reason = f"Liquidity too thin (${v.liquidity_usd:,.0f})"
    elif v.token_age_days is not None and v.token_age_days < 1:
        v.go          = False
        v.skip_reason = "Token < 1 day old — rug risk too high"
    elif v.top10_holder_pct is not None and v.top10_holder_pct > 90:
        v.go          = False
        v.skip_reason = f"Whale trap: top-10 hold {v.top10_holder_pct:.0f}%"
    elif v.final_score >= 5:
        v.go       = True
        ev         = ", ".join(v.evidence[:2]) if v.evidence else "research passes"
        v.go_reason = f"Score {v.final_score}/10 — {ev}"
    else:
        v.go          = False
        wn            = ", ".join(v.warnings[:2]) if v.warnings else "below threshold"
        v.skip_reason = f"Score {v.final_score}/10 — {wn}"

    log.info(
        "Research %s $%s | %d/10 | GO=%s | "
        "L=%d F=%d Cv=%d Cu=%d CT=%d",
        contract[:8], symbol, v.final_score, v.go,
        v.liquidity_score, v.fundamentals_score,
        v.conviction_score, v.culture_score, v.ct_score,
    )
    return v
