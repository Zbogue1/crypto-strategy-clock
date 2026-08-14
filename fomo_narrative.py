#!/usr/bin/env python3
"""
fomo_narrative.py -- Cross-chain narrative detection and Solana import.

An elite trader doesn't wait for Solana tokens to trend — they watch ETH and
BASE as leading indicators. When a narrative category catches fire on ETH
(AI agents, gaming, new meme format), Solana versions typically follow within
12-48 hours. This module runs that workflow automatically.

Pipeline (runs every 2 hours):
  1. Pull trending + boosted tokens from ETH and BASE via DexScreener (free)
  2. Extract narrative categories from token names and symbols
  3. Identify hot narratives — categories with ≥3 trending ETH/BASE tokens
  4. Search DexScreener Solana for tokens matching those narrative keywords
  5. Filter: not already pumped (h24 < 400%), enough liquidity
  6. Score candidates on momentum + narrative fit
  7. Fire signals for top candidates above threshold
"""

import logging
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# ─── NARRATIVE KEYWORD MAP ────────────────────────────────────────────────────
# Each category maps to a list of keywords checked against token name + symbol.
# Keywords are lowercase — matching is case-insensitive substring.
NARRATIVE_KEYWORDS: dict[str, list[str]] = {
    "AI_AGENTS":    ["ai", "agent", "gpt", "llm", "neural", "virtuals", "artificial",
                     "intelligence", "chatbot", "agi", "deepseek", "gemini", "claude",
                     "openai", "mistral", "robot", "sentient"],
    "GAMING":       ["game", "gaming", "play", "pixel", "quest", "realm", "rpg",
                     "arena", "battle", "guild", "loot", "nft", "metaverse", "world"],
    "DESCI":        ["science", "desci", "research", "bio", "medical", "health",
                     "gene", "lab", "molecule", "longevity", "pharma"],
    "RWA":          ["rwa", "estate", "property", "gold", "silver", "commodity",
                     "asset", "treasury", "tokenized", "real world"],
    "MEME_DOG":     ["dog", "doge", "shib", "shiba", "pup", "puppy", "woof",
                     "bark", "inu", "akita", "husky", "corgi"],
    "MEME_CAT":     ["cat", "kitty", "meow", "nyan", "feline", "kitten"],
    "MEME_FROG":    ["pepe", "frog", "toad", "ribbit", "kek"],
    "DEPIN":        ["depin", "network", "node", "physical", "infrastructure",
                     "iot", "sensor", "mesh", "wireless", "hotspot"],
    "SOCIAL_FI":    ["social", "friend", "chat", "message", "community",
                     "creator", "influencer", "fan", "token"],
    "SPACE":        ["space", "moon", "mars", "rocket", "cosmos", "galaxy",
                     "astro", "alien", "nasa", "orbit", "stellar"],
    "MEME_MISC":    ["chad", "wojak", "based", "cope", "sigma", "alpha",
                     "gigachad", "meme", "ape", "degen", "giga"],
    "POLITICAL":    ["trump", "maga", "elon", "grok", "doge", "america",
                     "freedom", "liberty", "patriot"],
    "ANIME":        ["anime", "waifu", "manga", "otaku", "kawaii", "naruto",
                     "dragonball", "pokemon", "pikachu"],
    "YIELD_DEFI":   ["yield", "stake", "lending", "liquid", "restake",
                     "eigenlayer", "points", "vault", "apy"],
}

# A narrative needs this many trending ETH/BASE tokens to be confirmed "hot"
NARRATIVE_MIN_TOKENS    = 2

# Don't signal Solana tokens that already ran hard
NARRATIVE_MAX_H24_GAIN  = 400.0

# Minimum liquidity on Solana target
NARRATIVE_MIN_LIQUIDITY = 15_000

# Minimum score to fire a narrative import signal
NARRATIVE_SCORE_THRESHOLD = 50

# Max signals per narrative category per run (avoid spam)
MAX_SIGNALS_PER_NARRATIVE = 2


# ─── ETH/BASE TRENDING PULL ──────────────────────────────────────────────────

def get_eth_base_trending() -> list[dict]:
    """
    Pull trending/boosted tokens from ETH and BASE via DexScreener.
    Returns list of {contract, symbol, name, chain, narratives}.
    """
    results = []
    TARGET_CHAINS = {"ethereum", "base", "eth"}

    endpoints = [
        "https://api.dexscreener.com/token-boosts/top/v1",
        "https://api.dexscreener.com/token-profiles/latest/v1",
    ]
    for url in endpoints:
        try:
            resp = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code != 200:
                continue
            items = resp.json()
            if isinstance(items, dict):
                items = items.get("pairs") or items.get("tokens") or []
            for t in (items or []):
                chain = (t.get("chainId") or t.get("chain") or "").lower()
                if chain not in TARGET_CHAINS:
                    continue
                name   = t.get("name") or t.get("description") or ""
                symbol = t.get("symbol") or ""
                addr   = t.get("tokenAddress") or t.get("address") or ""
                if not addr:
                    continue
                narratives = _extract_narratives(name, symbol)
                results.append({
                    "contract":   addr,
                    "symbol":     symbol,
                    "name":       name,
                    "chain":      chain,
                    "narratives": narratives,
                })
        except Exception as e:
            log.warning(f"Narrative: ETH/BASE fetch error ({url}): {e}")

    log.info(f"Narrative: {len(results)} ETH/BASE tokens pulled")
    return results


def _extract_narratives(name: str, symbol: str) -> list[str]:
    """Return matching narrative category names for a token name+symbol."""
    text = f"{name} {symbol}".lower()
    return [
        cat for cat, keywords in NARRATIVE_KEYWORDS.items()
        if any(kw in text for kw in keywords)
    ]


def detect_hot_narratives(tokens: list[dict]) -> dict[str, int]:
    """
    Count ETH/BASE trending tokens per narrative category.
    Returns only narratives that clear the NARRATIVE_MIN_TOKENS threshold.
    """
    counts: dict[str, int] = {}
    for t in tokens:
        for narrative in t.get("narratives", []):
            counts[narrative] = counts.get(narrative, 0) + 1

    hot = {n: c for n, c in counts.items() if c >= NARRATIVE_MIN_TOKENS}
    if hot:
        log.info(
            "Narrative: hot on ETH/BASE → "
            + ", ".join(f"{n}({c})" for n, c in sorted(hot.items(), key=lambda x: -x[1]))
        )
    else:
        log.info("Narrative: no hot narratives this cycle")
    return hot


# ─── SOLANA NARRATIVE SEARCH ──────────────────────────────────────────────────

def search_solana_by_narrative(
    narrative: str,
    seen_contracts: set,
) -> list[dict]:
    """
    Search DexScreener for Solana tokens matching a hot narrative.
    Tries multiple keywords from the category, deduplicates by contract.
    Returns list of scored candidate dicts.
    """
    keywords  = NARRATIVE_KEYWORDS.get(narrative, [])
    tried     = set()
    candidates = []

    for keyword in keywords[:5]:   # top 5 keywords per narrative
        try:
            time.sleep(0.5)
            resp = requests.get(
                "https://api.dexscreener.com/latest/dex/search",
                params={"q": keyword},
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            if resp.status_code != 200:
                continue
            pairs = resp.json().get("pairs") or []
            for pair in pairs:
                if (pair.get("chainId") or "").lower() != "solana":
                    continue
                contract = (pair.get("baseToken") or {}).get("address", "")
                if not contract or contract in tried or contract in seen_contracts:
                    continue
                tried.add(contract)

                # Extract fields from search result (same shape as pair data)
                liq       = float((pair.get("liquidity") or {}).get("usd", 0) or 0)
                pc        = pair.get("priceChange") or {}
                h1_gain   = float(pc.get("h1") or 0)
                h24_gain  = float(pc.get("h24") or 0)
                txns      = pair.get("txns") or {}
                txns_h1   = txns.get("h1") or {}
                vol       = pair.get("volume") or {}
                created   = pair.get("pairCreatedAt")

                age_days = None
                if created:
                    try:
                        age_ms   = datetime.now(timezone.utc).timestamp() * 1000 - float(created)
                        age_days = age_ms / (1000 * 86400)
                    except Exception:
                        pass

                # Pre-filter: already exploded, or too thin
                if h24_gain > NARRATIVE_MAX_H24_GAIN:
                    continue
                if liq < NARRATIVE_MIN_LIQUIDITY:
                    continue

                candidates.append({
                    "contract":          contract,
                    "symbol":            (pair.get("baseToken") or {}).get("symbol", "?"),
                    "name":              (pair.get("baseToken") or {}).get("name", "?"),
                    "chain":             "solana",
                    "liquidity_usd":     liq,
                    "price_change_h1":   h1_gain,
                    "price_change_h24":  h24_gain,
                    "buys_h1":           int(txns_h1.get("buys") or 0),
                    "sells_h1":          int(txns_h1.get("sells") or 0),
                    "volume_h1":         float(vol.get("h1") or 0),
                    "volume_h6":         float(vol.get("h6") or 0),
                    "token_age_days":    age_days,
                    "narrative":         narrative,
                    "matched_keyword":   keyword,
                })
        except Exception as e:
            log.warning(f"Narrative search error (keyword={keyword}): {e}")

    return candidates


# ─── SCORING ──────────────────────────────────────────────────────────────────

def _score_narrative_candidate(token: dict, narrative_heat: int) -> tuple[int, list[str]]:
    """
    Score a Solana narrative import candidate.
    Starts with a narrative fit bonus proportional to ETH/BASE heat.
    """
    # Narrative fit bonus — scales with how many ETH/BASE tokens share this narrative
    fit_bonus = min(15 + (narrative_heat - 2) * 3, 25)
    score     = fit_bonus
    factors   = [f"Narrative fit: {token.get('narrative','?')} trending on ETH/BASE ({narrative_heat} tokens)"]

    # Buy pressure
    buys_h1  = token.get("buys_h1", 0) or 0
    sells_h1 = token.get("sells_h1", 0) or 0
    total_h1 = buys_h1 + sells_h1
    if total_h1 >= 10:
        buy_ratio = buys_h1 / total_h1
        if buy_ratio >= 0.68:
            score += 20
            factors.append(f"Buy pressure: {buy_ratio*100:.0f}% buys h1")
        elif buy_ratio >= 0.58:
            score += 12

    # Volume acceleration
    vol_h1 = token.get("volume_h1", 0) or 0
    vol_h6 = token.get("volume_h6", 0) or 0
    avg_h6 = vol_h6 / 6 if vol_h6 > 0 else 0
    if avg_h6 > 0:
        accel = vol_h1 / avg_h6
        if accel >= 2.5:
            score += 20
            factors.append(f"Volume surge: {accel:.1f}x recent avg")
        elif accel >= 1.5:
            score += 12
    elif vol_h1 >= 10_000:
        score += 8

    # Price — early in the move
    pc_h1  = token.get("price_change_h1", 0) or 0
    pc_h24 = token.get("price_change_h24", 0) or 0
    if pc_h1 >= 5 and pc_h24 < 150:
        score += 15
        factors.append(f"Early mover: +{pc_h1:.0f}% h1, {pc_h24:.0f}% h24")
    elif pc_h1 >= 2:
        score += 8
    elif pc_h1 < -5:
        score -= 8

    # Age sweet spot — Solana versions often launch alongside or slightly after ETH
    age = token.get("token_age_days")
    if age is not None:
        if age <= 7:
            score += 15
            factors.append(f"Young: {age:.1f} days old")
        elif age <= 21:
            score += 8
        elif age > 90:
            score -= 5   # old token catching a narrative wave — more skeptical

    # Liquidity
    liq = token.get("liquidity_usd", 0) or 0
    if 30_000 <= liq <= 3_000_000:
        score += 10
        factors.append(f"Liquidity: ${liq:,.0f}")
    elif liq > 3_000_000:
        score += 3   # large but less upside

    return max(0, min(score, 100)), factors


def _build_narrative_signal(token: dict, score: int, factors: list[str]) -> dict:
    """Build Telegram signal dict for a narrative import candidate."""
    symbol    = token.get("symbol", "?")
    narrative = token.get("narrative", "?")
    keyword   = token.get("matched_keyword", "")
    return {
        "alias":            f"Golem (narrative: {narrative})",
        "tier":             "A",
        "chain":            "solana",
        "bankroll_usd":     None,
        "copy_trade":       True,
        "action":           "BUY",
        "token_symbol":     symbol,
        "contract_address": token.get("contract"),
        "confidence":       "high" if score >= 70 else "medium",
        "signal_text":      (
            f"🌐 Narrative import: ${symbol} — {narrative} hot on ETH/BASE\n"
            f"Score {score}/100 | keyword: {keyword}\n"
            + "\n".join(f"• {f}" for f in factors)
        ),
        "source":           "narrative_import",
        "timestamp":        datetime.now(timezone.utc).isoformat(),
        "original_text":    f"Narrative import ({narrative}): {score}/100 — {'; '.join(factors[:2])}",
        "narrative":        narrative,
        "matched_keyword":  keyword,
        "narrative_score":  score,
    }


# ─── MAIN ENTRY POINT ─────────────────────────────────────────────────────────

def run_narrative_scan(callback, seen_contracts: set = None) -> int:
    """
    Full cross-chain narrative import pipeline.
    Called every 2 hours from fomo_scanner.start_scanner().

    Args:
        callback:        signal callback (same as scanner callback)
        seen_contracts:  set of contracts already signalled this session
    Returns:
        number of signals emitted
    """
    seen_contracts = seen_contracts or set()
    signals_emitted = 0

    log.info("Narrative scanner: checking ETH/BASE for hot narratives...")
    eth_tokens = get_eth_base_trending()
    if not eth_tokens:
        log.warning("Narrative scanner: no ETH/BASE tokens fetched — skipping")
        return 0

    hot = detect_hot_narratives(eth_tokens)
    if not hot:
        return 0

    # Process each hot narrative — highest heat first
    for narrative, heat in sorted(hot.items(), key=lambda x: -x[1]):
        log.info(f"Narrative scanner: searching Solana for '{narrative}' (heat={heat})")
        candidates = search_solana_by_narrative(narrative, seen_contracts)
        log.info(f"Narrative scanner: {len(candidates)} Solana candidates for {narrative}")

        if not candidates:
            continue

        # Score all candidates
        scored = []
        for c in candidates:
            score, factors = _score_narrative_candidate(c, heat)
            if score >= NARRATIVE_SCORE_THRESHOLD:
                scored.append((score, factors, c))

        # Emit top N per narrative — sorted by score descending
        emitted_this_narrative = 0
        for score, factors, token in sorted(scored, key=lambda x: -x[0]):
            if emitted_this_narrative >= MAX_SIGNALS_PER_NARRATIVE:
                break
            log.info(
                f"NARRATIVE SIGNAL: ${token['symbol']} ({token['contract'][:8]}…) "
                f"narrative={narrative} score={score}/100"
            )
            signal = _build_narrative_signal(token, score, factors)
            try:
                callback(signal)
                signals_emitted      += 1
                emitted_this_narrative += 1
                seen_contracts.add(token["contract"])
            except Exception as e:
                log.error(f"Narrative callback error: {e}")

    log.info(f"Narrative scanner: {signals_emitted} signal(s) emitted")
    return signals_emitted
