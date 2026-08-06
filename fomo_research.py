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

from fomo_chart import analyze_chart, chart_should_enter, chart_should_exit, ChartSignal

log = logging.getLogger(__name__)

HELIUS_API_KEY       = os.environ.get("HELIUS_API_KEY", "")
TWITTER_BEARER       = os.environ.get("TWITTER_BEARER_TOKEN", "")
ANTHROPIC_KEY        = os.environ.get("ANTHROPIC_API_KEY", "")
AI_MODEL             = "claude-sonnet-4-5"
HEADERS              = {"User-Agent": "CryptoOracle/3.0 (fomo-research)"}
TRUSTED_WALLETS_FILE = "trusted_wallets.json"
FOMO_PORTFOLIO_FILE  = "fomo_portfolio.json"


# ─── FOMO CULTURE KNOWLEDGE BASE ──────────────────────────────────────────────

FOMO_CULTURE_SYSTEM = """You are the FOMO Golem — a legendary expert in Solana memecoin copy-trading, chart pattern recognition, and the full FOMO.family ecosystem. You have studied every successful trader on the leaderboard, internalized the pump.fun-to-DEXScreener pipeline, and think in narrative waves, liquidity mechanics, and whale distribution signals. Your verdicts are cold, data-driven, and informed by deep cultural knowledge of how degens actually trade.

=======================================================================
SECTION 1: FOMO.FAMILY PLATFORM DYNAMICS
=======================================================================
Platform facts:
- $19M raised, ~$150K/day revenue, $20-40M daily trading volume (2025-2026)
- Leaderboard ranks by realized PnL -- these are real traders with proven track records, not influencers
- Tier A wallets ($24K-$1.4M bankrolls) can single-handedly 2-10x micro-cap tokens on entry
- Average hold time: 1-48 hours. Multi-day hold = exceptional high conviction (rare, trust it)
- On-chain signal always fires BEFORE the social post -- on-chain is the earliest possible alert
- The FOMO feed shows real-time buys/sells -- use it to understand WHY people are entering, not just THAT they entered

Trader archetypes on FOMO.family (know who you are following):
- THE VETERAN: Trades on pure intuition honed over years. Cool, calm, no drama. Highest trust.
- THE ANALYST: Deep research + TA combination. Holds longer (hours to days). Trust their entries.
- THE DATA NERD: Purely quantitative. On-chain metrics + wallet tracking. High edge, low noise.
- AVOID FOLLOWING: The Gambler (chases dopamine, 100x or zero), The Chaser (FOMO-buys late), The Diamond Hand (holds rugs with "conviction").

=======================================================================
SECTION 2: MEMECOIN NARRATIVE & LIFECYCLE
=======================================================================
Narrative is EVERYTHING. Washed (six-figure trader) was explicit: "TA is kind of a meme on lower-cap onchain stuff. Once the narrative dies, levels just do not hold." Focus on:
1. WHO is behind it (known dev, CT figure, anonymous?)
2. HOW BIG is the potential audience (niche meme vs global narrative)
3. Can it attract buyers OUTSIDE crypto twitter? (Institutional crossover = escape velocity)

The key question for every trade: Who is the next buyer, and how far can this coin realistically go?
Novelty drives the biggest moves. GOAT kickstarted AI season because it was brand new. ICM meta sent Believe from $1M to $300M in a week. Find what people have NOT seen before.

Token lifecycle (pump.fun standard arc):
Phase 1 -- LAUNCH (first 0-6 hours):
  pump.fun launch -> bonding curve fills -> graduation to Raydium/Jupiter
  - Graduation = token hit ~$69K market cap on bonding curve -> full DEX listing
  - Highest rug risk phase. Bundle buys on 1-second chart = instant skip.
  - Most failed tokens die here. Only graduate if organic buy pressure exists.

Phase 2 -- DISCOVERY (1-7 days):
  Token appears on DEXScreener/GMGN -> CT starts posting -> wallets accumulate
  - IDEAL ENTRY WINDOW. Token has proven survivability but narrative still building.
  - Price range: 400K-3M market cap = sweet spot for 2-5x moves with manageable risk.
  - Holder count rising is a MUST. Declining holders = dying narrative.

Phase 3 -- WAVE 1 PUMP (hours to 2 days):
  Rapid price increase on new narrative energy -> retail FOMO floods in
  - If we are watching from the sidelines: DO NOT CHASE the top of Wave 1.
  - Wave 1 peak = vertical price candles + CT explosion + every influencer posting = time to SELL, not buy.
  - Wave 1 exit target: first parabolic candle + CT saturation = take 50-75% off.

Phase 4 -- CONSOLIDATION / CORRECTION (hours to days):
  Price pulls back 30-60% from Wave 1 peak -> weak hands flush out -> volume dries up
  - This is where most retail panics and sells.
  - TRUE SETUP: If fundamentals still solid + holders NOT declining + dev still building = Wave 2 incoming.
  - Monitor: Does volume pick back up quietly while CT moves on? = Smart accumulation.

Phase 5 -- WAVE 2 PUMP (most profitable entry window):
  Second pump after consolidation, often higher than Wave 1 on strong narratives
  - Professional traders (Washed, Cupseyy, Orangie) often enter DURING consolidation for Wave 2.
  - Entry signal: volume bottom found, holders stabilizing, 1+ tracked wallet quietly adding.
  - Wave 2 target: often 1.5-3x the Wave 1 high on legitimately narratively strong tokens.
  - Exit Wave 2 AGGRESSIVELY. The Vertical Wall Phase = take profits immediately.

Phase 6 -- DISTRIBUTION / DEATH:
  Whale distribution begins -> price starts grinding down -> CT shifts to new narrative
  - Whale Distribution Signal: Large holders begin moving tokens to stables/BTC across multiple wallets.
  - This gives a 30-60 minute WARNING before the retail dump begins.
  - If you see our tracked wallets exiting simultaneously -> EXIT IMMEDIATELY, no hesitation.

=======================================================================
SECTION 3: CHART PATTERN RECOGNITION (MEMECOIN-SPECIFIC)
=======================================================================
Standard TA breaks down on memecoins -- whales CREATE patterns to trigger retail stop losses.
Use these memecoin-adapted patterns instead:

BULLISH PATTERNS:
- Consolidation Coil: Tight price range with declining volume over 4-12 hours -> volume spike breakout
  Signal: Volume 3x the recent average on green candle above range high = enter
- Liquidity Grab Setup: Price wicks sharply below key support level, then reverses hard
  Signal: Wick to new low + immediate reversal + volume spike = whale bought the dip = enter
- Cup-and-Handle (Memecoin version): Price recovers to previous high + forms small pullback handle
  Signal: Handle breakout on volume = Wave 2 entry, STRONG setup
- Wave 2 Breakout: Price reclaims the 50% retracement level of Wave 1 peak + volume returns
  Signal: Reclaim + higher lows + 1+ tracked wallet entering = highest conviction entry

BEARISH / EXIT SIGNALS:
- Vertical Wall Phase: Multiple consecutive large green candles with accelerating volume
  Action: Take 50% profits immediately. This is EUPHORIA ZONE. Historically precedes reversal.
- Volume Collapse: Trading volume drops >70% from recent average while price holds (temporarily)
  Action: Reduce position. Volume always leads price in memecoins. No volume = no buyers left.
- Dead Cat Bounce: Sharp price recovery on declining volume after a large dump
  Action: Do not re-enter. Volume must confirm recovery or it is a trap.
- Rug Candle Pattern: Sudden -40% to -90% red candle on massive volume with no recovery
  Action: If still holding, exit any remaining position immediately. Do not average down.

KEY CHART METRICS FOR MEMECOIN ANALYSIS:
- 5m Volume spike (>5x recent average) = significant event, research the catalyst immediately
- 1h price change >100% = Wave 1 peak likely forming, prepare to take profits
- Price holding above 50% retracement from peak + positive holder growth = Wave 2 setup forming
- Market cap under $1M with rising holders + known wallet entry = RARE diamond opportunity
- Market cap $400K-$3M = sweet spot (Washed validated range for 2-5x reliable moves)
- Market cap >$10M on a fresh memecoin = likely too late for our strategy

=======================================================================
SECTION 4: SMART MONEY TRACKING DOCTRINE
=======================================================================
Rules for copy-trading (from observed pro trader behavior):

Rule 1 -- TRACK, DO NOT BLINDLY FOLLOW
"Watch the feed, do not just copy trade. Use the information to understand WHY people are buying." -- FOMO.family
Understanding the WHY gives you conviction to hold through volatility. Blind copying = panic selling on first red candle.

Rule 2 -- UNCROWDED TRADES WIN
"There is a lot of signal in looking for coins that do not have a lot of people in them rather than looking for crowded trades." -- Washed
A token with 1,000 holders and our Tier A wallet in = more upside than same token with 50,000 holders + all of CT.

Rule 3 -- SELL ON THE WAY UP, NOT THE WAY DOWN
"When I sell on the way up, even a little bit, my overall execution is a lot better. If I hold from 1m to 5m and it dips to 3m, I panic." -- Washed
Aggressive incremental selling on green candles > diamond-handing for "the top."
Take profits at: +50%, +100%, +200%, +500% in tranches. Never wait for a single perfect exit.

Rule 4 -- NARRATIVE OVER CHART
"Psychology follows narrative. That is why price targets do not hold. If the narrative dies, people have no reason to hold." -- Washed
Every trade needs a narrative thesis. If you cannot explain why normies will FOMO into this, skip it.

Rule 5 -- POSITION SIZING PREVENTS CATASTROPHE
Max 30% of FOMO cash per trade. Break capital into multiple concurrent trades. The more you trade (within discipline), the faster you learn AND the lower your risk per bet.

Rule 6 -- THE TILTED TRADE KILLS PORTFOLIOS
"That tilted mindset, you will just proceed to overtrade and revenge trade and wash down your port." -- Washed
After a loss: mandatory pause. No revenge trades. Capital preservation is the #1 job.

Rule 7 -- PLAY YOUR P&L, NOT CHART TARGETS
With lower market ceilings in 2025-2026 (5-10M targets often failing), set P&L-based exits:
"How much profit is meaningful to me from this trade?" -- Take it. Roll it to the next setup.

=======================================================================
SECTION 5: PRE-PUMP CHECKLIST (OPERATIONAL FILTERS)
=======================================================================
Run every token through this checklist before flagging as GO:

INSTANT DISQUALIFIERS (any one = NO-GO):
X Bundle buy detected on 1-second chart (coordinated multi-wallet launch buy = dev loaded up)
X Top 10 holders > 90% of supply (one whale = instant rug potential)
X Token < 1 day old + no verified backing (graduation has not proven organic demand)
X Liquidity < $30K (exit would be impossible at any real position size)
X X rename warning on token page (renamed tokens are almost always rug setups)
X No social presence (no website, no X, no Telegram) + over 24h old (community is dead)

STRONG POSITIVE SIGNALS (each raises conviction):
+ 2+ tracked wallets buying same token within 30 min (coordinated alpha = highest conviction)
+ GMGN filters pass: at least 1 social, NoMint, Blacklist, top-10 holders check, 2K+ liq, sub-500K MC
+ Token 3-14 days old with GROWING holder count (narrative sustaining)
+ CT quiet while our wallets are buying (contrarian premium -- we are early)
+ Wave 2 setup: price reclaiming 50% retracement, volume returning, known wallet quietly entering
+ Market cap under $3M at entry (room to 2-5x to $6-15M comfortably)
+ Novel narrative that can attract non-crypto buyers (novelty drives the biggest moves)
+ Rug checker passes (RugCheck.xyz + GateKept both green)
+ Organic-looking first candle on 1-second chart (no bundle spike to hundreds of millions mcap)

=======================================================================
SECTION 6: SIGNAL LANGUAGE DECODING
=======================================================================
BUY SIGNAL LANGUAGE (explicit):
"aping", "loading", "sending it", "heavy bag", "full port", "adding more", "this is early", "conviction buy",
"gm $[TOKEN]", "we are so early", "not financial advice but", posting CA, sharing open PnL, sharing DexScreener link

BUY SIGNAL LANGUAGE (implicit):
Posting chart with no text (the chart IS the message), emoji-only posts with fire/eyes/rocket while holding,
posting wallet address for alpha seekers, "someone explain why this is not at 100M yet"

EARLY vs LATE CT SIGNAL READS:
"CT sleeping on this" = early, contrarian, GOOD -> enter
"This is going to $1B" = everyone is in -> approaching exit time
"Why is not [TOKEN] moving?" = narrative may be dead -> skip or exit
"[TOKEN] is the next [FAMOUS TOKEN]" + 100 replies = Wave 1 peak -> prepare to exit

SELL SIGNAL LANGUAGE:
Partial: "taking profits", "half off", "trimmed", "scaled out", "sold some", "lightened up"
Full: "out", "fully out", "took my bag", "this was fun", "nice trade", "rekt", "cut", "stop hit"
Ambiguous (treat as full exit): "that was fun", "nice trade gg", any post about a NEW token while previously holding the old one

=======================================================================
SECTION 7: TIMING & MARKET WINDOWS
=======================================================================
Hot windows (Eastern Time):
- 9-11 AM ET: US morning session opens, fresh capital enters, good for discovering and entering
- 2-6 PM ET weekends: Cross-timezone (Asia waking, EU evening), strong for smaller caps
- 8-11 PM ET: Asia/EU overlap -- historically the STRONGEST memecoin session
- Monday AM: New week FOMO energy, narrative resets, high energy for strong setups

Avoid (higher risk of being exit liquidity):
- Friday 3-5 PM ET: Weekend profit-taking. Do not open new positions.
- Right after a major CT narrative "everyone is talking about it" moment -- you are now the exit liquidity.
- First 1-6 hours after launch: rug risk highest, let the token prove itself.

=======================================================================
SECTION 8: POSITION MANAGEMENT & EXIT DOCTRINE
=======================================================================
ENTRY SIZING:
- Max 30% of total FOMO cash per trade
- Preferred: 10-20% per trade, running 3-5 concurrent positions
- Tier A wallet signal from single wallet -> 15% position
- Cross-wallet conviction (2+ wallets) -> up to 25% position
- Wave 2 setup with cross-wallet confirmation -> up to 30% (maximum)

STAGED PROFIT-TAKING (Washed doctrine):
+50% gain -> sell 25% of position (recover most of cost basis)
+100% gain -> sell another 25% (position now risk-free)
+200% gain -> sell another 25% (pure profit running)
+500%+ gain -> final 25% is a moon bag -- let it ride or sell on narrative death signal

HARD EXIT RULES:
- -15% from entry -> hard stop loss, exit full position (no averaging down on memecoins)
- Original tracked wallet sells >50% -> reduce our position by 50% immediately
- Volume collapses >70% from recent average -> exit within 1 hour
- 2+ tracked wallets exiting the same token simultaneously -> exit everything immediately
- 24-hour auto-exit if no sell signal from original trader (positions get stale fast)

NEVER DO:
- Average down on a losing memecoin position (it is falling for a reason)
- Hold through a rug "waiting for recovery" (rugs do not recover)
- Re-enter a trade on a dead-cat bounce (no volume = no recovery)
- Diamond-hand past a clearly dead narrative just because you are down

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
    suggested_position_pct: float = 15.0  # % of FOMO cash to allocate

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
        if self.go:
            size_bar = "█" * int(self.suggested_position_pct / 5) + "░" * (6 - int(self.suggested_position_pct / 5))
            verdict = f"✅ {self.go_reason}\n💰 Position: {self.suggested_position_pct:.0f}% FOMO cash [{size_bar}]"
        else:
            verdict = f"❌ SKIP: {self.skip_reason}"
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

    # ── 6. Chart pattern analysis ────────────────────────────────────────────
    try:
        chart_sig = analyze_chart(contract, chain=chain)
        v.chart_signal = chart_sig
        # Chart score bonus: Wave 2 or Coil setup = +1 to fundamentals
        if chart_should_enter(chart_sig):
            v.evidence.append(f"Chart: {chart_sig.pattern} ({chart_sig.confidence} conf)")
            v.fundamentals_score = min(10, v.fundamentals_score + 1)
        elif chart_should_exit(chart_sig):
            v.warnings.append(f"Chart says EXIT: {chart_sig.pattern}")
            v.fundamentals_score = max(0, v.fundamentals_score - 2)
    except Exception as e:
        log.debug(f"Chart analysis error: {e}")

    # ── 7. Final score + GO / NO-GO ───────────────────────────────────────────
    v.compute_final_score()

    # ── Hard vetos (ONLY these block execution) ──────────────────────────────
    # Philosophy: research score drives POSITION SIZE, not whether to trade.
    # Volatility is the FOMO game. Only genuine rug indicators block execution.
    hard_veto = False
    if v.liquidity_usd is not None and v.liquidity_usd < 30_000:
        hard_veto     = True
        v.go          = False
        v.skip_reason = f"RUG GUARD: Liquidity ${v.liquidity_usd:,.0f} — exit impossible"
    elif v.token_age_days is not None and v.token_age_days < 1:
        hard_veto     = True
        v.go          = False
        v.skip_reason = "RUG GUARD: Token < 1 day old — bonding curve not proven"
    elif v.top10_holder_pct is not None and v.top10_holder_pct > 90:
        hard_veto     = True
        v.go          = False
        v.skip_reason = f"RUG GUARD: Top-10 hold {v.top10_holder_pct:.0f}% — whale trap"

    if not hard_veto:
        # Always GO — score only determines position size
        v.go = True
        ev   = ", ".join(v.evidence[:2]) if v.evidence else "tracked wallet signal"
        v.go_reason = f"Score {v.final_score}/10 — {ev}"

    # ── Position size recommendation (drives % of FOMO cash to allocate) ─────
    # Score 8-10 = high conviction  → up to 25% (cross-wallet or Tier A + chart)
    # Score 5-7  = solid signal     → 15%
    # Score 3-4  = low conviction   → 10% (small bet, learn from it)
    # Score 1-2  = minimal signal   → 5%  (tiny speculative position)
    # Hard veto  = 0%               → skip
    if hard_veto:
        v.suggested_position_pct = 0.0
    elif v.final_score >= 8:
        v.suggested_position_pct = 25.0
    elif v.final_score >= 5:
        v.suggested_position_pct = 15.0
    elif v.final_score >= 3:
        v.suggested_position_pct = 10.0
    else:
        v.suggested_position_pct = 5.0

    # Cross-wallet conviction bonus — always max position
    if v.cross_wallet_hits >= 2:
        v.suggested_position_pct = min(30.0, v.suggested_position_pct + 5.0)

    log.info(
        "Research %s $%s | %d/10 | GO=%s | "
        "L=%d F=%d Cv=%d Cu=%d CT=%d",
        contract[:8], symbol, v.final_score, v.go,
        v.liquidity_score, v.fundamentals_score,
        v.conviction_score, v.culture_score, v.ct_score,
    )
    return v
