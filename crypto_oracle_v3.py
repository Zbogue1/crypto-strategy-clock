#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════╗
║         CRYPTO STRATEGY CLOCK  v3.0                      ║
║   Swing Trading Intelligence Agent                        ║
║                                                           ║
║  Scans top 20 cryptocurrencies. Identifies the single     ║
║  best swing trade setup. Learns from every call it makes. ║
║                                                           ║
║  Timeframe: Days to 1-2 weeks                             ║
╚══════════════════════════════════════════════════════════╝

SETUP:
  pip install anthropic requests apscheduler feedparser
  set ANTHROPIC_API_KEY=sk-ant-...
  set FRED_API_KEY=...   (free at fred.stlouisfed.org)
  python crypto_oracle_v3.py --once

NEWS SOURCES (all free):
  CoinDesk · Blockworks · CoinTelegraph · CryptoSlate (RSS)

RESEARCH SOURCES (all free):
  Messari · DeFiLlama · CoinGecko

COMMUNITY SOURCES (all free):
  Reddit (r/CryptoCurrency, r/Bitcoin, r/ethereum, r/solana)
"""

# ─── IMPORTS ─────────────────────────────────────────────────────────────────
import os, sys, json, time, math, re, smtplib, argparse, logging, textwrap
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, List, Dict

import requests
import anthropic

try:
    import feedparser; HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    from apscheduler.schedulers.blocking import BlockingScheduler; HAS_SCHEDULER = True
except ImportError:
    HAS_SCHEDULER = False

try:
    from crystal_ball_memory import (
        save_prediction, run_learning_cycle,
        load_intelligence_context, get_performance_stats
    )
    HAS_MEMORY = True
except ImportError:
    HAS_MEMORY = False

try:
    from position_manager import (
        get_mode, get_position_context, build_mode_prompt,
        process_signal as pm_process_signal,
        get_display_state, get_trade_summary,
        force_set_position, reset_to_hunting
    )
    HAS_POSITION = True
except ImportError:
    HAS_POSITION = False

try:
    from paper_portfolio import (
        execute_buy, execute_sell,
        check_stop_loss, check_take_profit,
        get_portfolio_value, get_portfolio_summary_lines,
        build_portfolio_html, reset_portfolio
    )
    HAS_PAPER = True
except ImportError:
    HAS_PAPER = False

try:
    from signal_intelligence import (
        log_trade_entry, log_trade_exit,
        build_credibility_context
    )
    HAS_SIGNAL_INTEL = True
except ImportError:
    HAS_SIGNAL_INTEL = False
    def log_trade_entry(*a, **kw): pass
    def log_trade_exit(*a, **kw): pass
    def build_credibility_context(): return ""

# ─── CONFIG ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
FRED_API_KEY        = os.environ.get("FRED_API_KEY", "")
ALERT_EMAIL         = os.environ.get("ALERT_EMAIL", "")
ALERT_EMAIL_PASS    = os.environ.get("ALERT_EMAIL_PASSWORD", "")
ALERT_EMAIL_TO      = os.environ.get("ALERT_EMAIL_TO", ALERT_EMAIL)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
GITHUB_TOKEN        = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO         = "Zbogue1/crypto-strategy-clock"

MODEL      = "claude-opus-4-6"
LOG_FILE   = "crypto_history.jsonl"
HTML_FILE  = "crypto_dashboard.html"

TOP_N_SCAN = 20    # scan top N coins by market cap
TOP_N_DEEP = 5     # deep-dive on top N candidates

NEWS_SOURCES = [
    ("https://www.coindesk.com/arc/outboundfeeds/rss/",  "CoinDesk"),
    ("https://cointelegraph.com/rss",                     "CoinTelegraph"),
    ("https://blockworks.co/feed",                        "Blockworks"),
    ("https://cryptoslate.com/feed/",                     "CryptoSlate"),
]

REDDIT_SUBS = ["CryptoCurrency", "Bitcoin", "ethereum", "solana", "altcoin"]

HEADERS = {"User-Agent": "CryptoOracle/3.0 (swing trading research; non-commercial)"}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crypto_oracle")


# ─── SIGNAL DISPLAY ──────────────────────────────────────────────────────────
SIGNAL_DISPLAY_HUNTING = {
    "STRONG_BUY": "BUY", "BUY": "BUY",
    "HOLD": "HOLD",
    "SELL": "WAIT", "STRONG_SELL": "WAIT",
}
SIGNAL_DISPLAY_HOLDING = {
    "HOLD": "HOLD", "SELL": "SELL", "STRONG_SELL": "SELL NOW",
}
SIGNAL_DISPLAY_WAITING = {
    "RE_ENTER": "BUY BACK", "WAIT_LONGER": "NOT YET",
}
SIGNAL_STYLES = {
    "BUY":      ("\033[92m", "B U Y",           "#00c853"),
    "HOLD":     ("\033[93m", "H O L D",          "#ff9800"),
    "WAIT":     ("\033[91m", "W A I T",          "#f44336"),
    "SELL":     ("\033[91m", "S E L L",          "#ef5350"),
    "SELL NOW": ("\033[91m", "S E L L  N O W",   "#ff1744"),
    "BUY BACK": ("\033[92m", "B U Y  B A C K",  "#00e676"),
    "NOT YET":  ("\033[93m", "N O T  Y E T",     "#ff9800"),
}
RESET = "\033[0m"
BOLD  = "\033[1m"

def resolve_display_signal(internal_sig: str, mode: str) -> str:
    if mode == "HOLDING":   return SIGNAL_DISPLAY_HOLDING.get(internal_sig, "HOLD")
    elif mode == "WAITING": return SIGNAL_DISPLAY_WAITING.get(internal_sig, "NOT YET")
    else:                   return SIGNAL_DISPLAY_HUNTING.get(internal_sig, "HOLD")


# ─── DATA MODELS ─────────────────────────────────────────────────────────────

@dataclass
class Signal:
    name:          str
    value:         float        # -2.0 (very bearish) → +2.0 (very bullish)
    data_quality:  float        # 0.0 → 1.0
    source:        str
    plain_english: str
    category:      str          # momentum | news | community | onchain | macro

    @property
    def emoji(self):
        if self.value >= 1.5: return "🟢"
        if self.value >= 0.5: return "🟩"
        if self.value >= -0.4: return "🟡"
        if self.value >= -1.4: return "🟧"
        return "🔴"


@dataclass
class CoinScan:
    """Quick scan result for a single coin from top-20 sweep."""
    id:             str
    ticker:         str
    name:           str
    price:          float
    change_24h:     float
    change_7d:      float
    volume_24h:     float
    market_cap:     float
    ath_change_pct: float
    rank:           int
    ohlc:           list = field(default_factory=list)   # populated in deep-dive
    technicals:     dict = field(default_factory=dict)
    messari:        dict = field(default_factory=dict)
    signals:        List[Signal] = field(default_factory=list)


@dataclass
class MarketSnapshot:
    """Everything collected in one run."""
    timestamp:      str
    top_coins:      List[CoinScan]  = field(default_factory=list)
    candidates:     List[CoinScan]  = field(default_factory=list)   # deep-dive set
    fear_greed:     dict            = field(default_factory=dict)
    macro:          dict            = field(default_factory=dict)
    defi_summary:   dict            = field(default_factory=dict)
    btc_price:      float           = 0.0
    btc_dominance:  float           = 0.0
    news:           list            = field(default_factory=list)   # all sources
    reddit_posts:   list            = field(default_factory=list)
    all_signals:    List[Signal]    = field(default_factory=list)   # aggregate


# ─── PHASE 1: TOP-20 MARKET SCAN ─────────────────────────────────────────────

def scan_top_coins(snap: MarketSnapshot):
    """CoinGecko: top 20 coins by market cap — one batch request."""
    # Retry up to 3 times with backoff on 429 rate-limit responses
    for attempt in range(3):
        try:
            r = requests.get(
                "https://api.coingecko.com/api/v3/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": TOP_N_SCAN,
                    "page": 1,
                    "sparkline": "false",
                    "price_change_percentage": "24h,7d",
                },
                timeout=15, headers=HEADERS)
            if r.status_code == 429:
                wait = 15 * (attempt + 1)   # 15s, 30s, 45s
                log.warning(f"CoinGecko 429 rate-limit — waiting {wait}s (attempt {attempt+1}/3)")
                time.sleep(wait)
                continue
            r.raise_for_status()
            break   # success — exit retry loop
        except Exception as e:
            if attempt < 2:
                time.sleep(15)
                continue
            log.warning(f"Top-20 scan failed after retries: {e}")
            return
    else:
        log.warning("Top-20 scan: all retries exhausted")
        return
    try:
        coins = []
        for c in r.json():
            coins.append(CoinScan(
                id=c["id"],
                ticker=c["symbol"].upper(),
                name=c["name"],
                price=c["current_price"] or 0,
                change_24h=round(c.get("price_change_percentage_24h") or 0, 2),
                change_7d=round(c.get("price_change_percentage_7d_in_currency") or 0, 2),
                volume_24h=c.get("total_volume") or 0,
                market_cap=c.get("market_cap") or 1,
                ath_change_pct=round(c.get("ath_change_percentage") or 0, 1),
                rank=c.get("market_cap_rank") or 99,
            ))
        snap.top_coins = coins

        # Extract BTC data for market context
        btc = next((c for c in coins if c.ticker == "BTC"), None)
        if btc:
            snap.btc_price = btc.price

        log.info(f"Scanned {len(coins)} coins. BTC: ${snap.btc_price:,.0f}")

        # CoinGecko global for BTC dominance
        time.sleep(1.2)
        rg = requests.get("https://api.coingecko.com/api/v3/global", timeout=10, headers=HEADERS)
        if rg.status_code == 200:
            snap.btc_dominance = round(rg.json().get("data", {}).get("market_cap_percentage", {}).get("btc", 0), 1)
            log.info(f"BTC dominance: {snap.btc_dominance}%")
    except Exception as e:
        log.warning(f"Top-20 scan failed: {e}")


def select_candidates(snap: MarketSnapshot) -> List[CoinScan]:
    """
    Score each coin for swing trade potential and return top N candidates.
    Scoring heuristic (Claude will do final arbitration):
      - Large 7d dip → mean reversion potential
      - Strong 24h momentum (breakout setup)
      - High volume/market-cap ratio (unusual activity)
      - Not too far from ATH AND not at ATH (room to move)
    """
    scored = []
    for c in snap.top_coins:
        if c.ticker in ("USDT", "USDC", "BUSD", "DAI", "TUSD", "USDP"):
            continue  # skip stablecoins

        # Mean-reversion play: significant 7d dip
        dip_score = max(0, -c.change_7d / 5)

        # Momentum play: strong 24h move
        mom_score = abs(c.change_24h) / 3

        # Volume activity (volume / market cap — higher = more active)
        vol_ratio_score = min(20, (c.volume_24h / c.market_cap) * 200)

        # ATH distance sweet spot (not at ATH, not in permanent decline)
        ath_score = 10 if -60 < c.ath_change_pct < -5 else 0

        total = dip_score + mom_score + vol_ratio_score + ath_score
        scored.append((total, c))

    scored.sort(key=lambda x: x[0], reverse=True)
    candidates = [c for _, c in scored[:TOP_N_DEEP]]
    log.info(f"Top candidates: {[c.ticker for c in candidates]}")
    return candidates


# ─── PHASE 2: DEEP DIVE ON CANDIDATES ────────────────────────────────────────

def collect_coin_ohlc(coin: CoinScan):
    """CoinGecko OHLC for one coin — 90 days of daily candles."""
    try:
        r = requests.get(
            f"https://api.coingecko.com/api/v3/coins/{coin.id}/ohlc",
            params={"vs_currency": "usd", "days": 90},
            timeout=12, headers=HEADERS)
        r.raise_for_status()
        coin.ohlc = r.json()
        time.sleep(1.3)  # respect rate limit
    except Exception as e:
        log.warning(f"OHLC {coin.ticker}: {e}")


def collect_messari_metrics(coin: CoinScan):
    """Messari free API: key metrics for a coin (no key required for basic)."""
    slug_map = {
        "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
        "BNB": "binance-coin", "XRP": "xrp", "ADA": "cardano",
        "AVAX": "avalanche", "DOT": "polkadot", "MATIC": "polygon",
        "LINK": "chainlink", "DOGE": "dogecoin", "SHIB": "shiba-inu",
        "LTC": "litecoin", "UNI": "uniswap", "ATOM": "cosmos",
    }
    slug = slug_map.get(coin.ticker, coin.name.lower().replace(" ", "-"))
    try:
        r = requests.get(
            f"https://data.messari.io/api/v1/assets/{slug}/metrics",
            timeout=10, headers=HEADERS)
        if r.status_code == 200:
            d = r.json().get("data", {})
            mkt  = d.get("market_data", {})
            dev  = d.get("developer_activity", {})
            on   = d.get("on_chain_data", {})
            coin.messari = {
                "volume_last_24h":       mkt.get("volume_last_24_hours"),
                "real_volume_last_24h":  mkt.get("real_volume_last_24_hours"),
                "github_commits_30d":    dev.get("commits_last_3_months"),
                "active_addresses":      on.get("active_addresses"),
                "tx_count_last_24h":     on.get("transaction_count_last_24_hours"),
            }
    except Exception as e:
        log.warning(f"Messari {coin.ticker}: {e}")


def build_technicals_for_coin(coin: CoinScan):
    """RSI, MACD, SMAs, volume spike from OHLC data."""
    ohlc = coin.ohlc
    if not ohlc or len(ohlc) < 20:
        return

    closes  = [row[4] for row in ohlc]
    volumes = [row[5] if len(row) > 5 else 0 for row in ohlc]

    def sma(data, n):
        return sum(data[-n:]) / n if len(data) >= n else None

    def ema(data, n):
        if len(data) < n: return []
        res = [sum(data[:n]) / n]
        k = 2 / (n + 1)
        for p in data[n:]:
            res.append(p * k + res[-1] * (1 - k))
        return res

    def rsi(data, n=14):
        if len(data) < n + 1: return None
        deltas = [data[i] - data[i-1] for i in range(1, len(data))]
        gains  = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        ag = sum(gains[:n]) / n; al = sum(losses[:n]) / n
        for i in range(n, len(gains)):
            ag = (ag * (n-1) + gains[i]) / n
            al = (al * (n-1) + losses[i]) / n
        return round(100 - 100/(1 + ag/al), 2) if al else 100.0

    t = {}
    t["rsi_14"]  = rsi(closes, 14)
    t["rsi_7"]   = rsi(closes, 7)
    t["sma_20"]  = round(sma(closes, 20), 4)  if sma(closes, 20)  else None
    t["sma_50"]  = round(sma(closes, 50), 4)  if sma(closes, 50)  else None
    t["sma_200"] = round(sma(closes, 200), 4) if sma(closes, 200) else None

    e12 = ema(closes, 12); e26 = ema(closes, 26)
    if e12 and e26:
        ml = min(len(e12), len(e26))
        macd_line = [e12[-(ml-i)] - e26[-(ml-i)] for i in range(ml)]
        sig = ema(macd_line, 9)
        if sig:
            t["macd"]       = round(macd_line[-1], 4)
            t["macd_sig"]   = round(sig[-1], 4)
            t["macd_hist"]  = round(macd_line[-1] - sig[-1], 4)
            t["macd_cross"] = "bullish" if macd_line[-1] > sig[-1] else "bearish"

    # Volume spike: current vs 20d average
    if len(volumes) >= 20 and volumes[-1] > 0:
        vol_avg_20 = sma(volumes, 20)
        if vol_avg_20:
            t["volume_vs_avg"] = round(volumes[-1] / vol_avg_20, 2)
            t["volume_spike"]  = t["volume_vs_avg"] > 2.0

    # Price position vs key MAs
    price = closes[-1]
    if t.get("sma_20"):  t["above_sma20"]  = price > t["sma_20"]
    if t.get("sma_50"):  t["above_sma50"]  = price > t["sma_50"]
    if t.get("sma_200"): t["above_sma200"] = price > t["sma_200"]

    coin.technicals = t


def build_signals_for_coin(coin: CoinScan, snap: MarketSnapshot) -> List[Signal]:
    """Score one candidate coin's swing trade opportunity."""
    sigs = []
    t = coin.technicals

    # ── MOMENTUM ──────────────────────────────────────────────
    rsi_v = t.get("rsi_14")
    if rsi_v:
        v = 2.0 if rsi_v < 30 else 1.0 if rsi_v < 42 else 0 if rsi_v < 58 else -1.0 if rsi_v < 70 else -2.0
        sigs.append(Signal("RSI (14-day)", v, 1.0, "CoinGecko",
            f"{coin.ticker} RSI is {rsi_v:.0f}. " +
            ("Oversold — historically a short-term bounce zone." if v >= 1.5 else
             "Approaching oversold — potential entry." if v == 1.0 else
             "Overbought — elevated correction risk." if v <= -1.0 else "Neutral."),
            "momentum"))

    macd_c = t.get("macd_cross")
    if macd_c:
        v = 1.0 if macd_c == "bullish" else -1.0
        sigs.append(Signal("MACD Cross", v, 0.9, "CoinGecko",
            f"MACD showing {macd_c} crossover. " +
            ("Momentum turning up — buyers gaining control." if v > 0 else
             "Momentum turning down — sellers gaining control."),
            "momentum"))

    vol_ratio = t.get("volume_vs_avg")
    if vol_ratio:
        v = 1.5 if vol_ratio > 3 else 1.0 if vol_ratio > 1.8 else 0 if vol_ratio > 0.6 else -0.5
        sigs.append(Signal("Volume vs 20d Average", v, 0.95, "CoinGecko",
            f"Volume is {vol_ratio:.1f}x the 20-day average. " +
            ("Unusual spike — potential breakout or catalyst." if v >= 1.0 else
             "Volume is normal." if v == 0 else "Low volume — weak conviction."),
            "momentum"))

    if t.get("sma_20") and t.get("sma_50"):
        price = coin.price
        above20 = t.get("above_sma20")
        above50 = t.get("above_sma50")
        above200 = t.get("above_sma200")
        if above20 is not None:
            trend_score = sum([above20 or False, above50 or False, above200 or False])
            v = 1.0 if trend_score >= 2 else 0 if trend_score == 1 else -1.0
            sigs.append(Signal("Trend (MA alignment)", v, 0.9, "CoinGecko",
                f"{coin.ticker} is {'above' if above20 else 'below'} its 20-day MA, "
                f"{'above' if above50 else 'below'} 50-day MA. " +
                ("Price is trending well." if v > 0 else
                 "Price is below key moving averages." if v < 0 else "Mixed trend."),
                "momentum"))

    # ── 7D PRICE ACTION ───────────────────────────────────────
    chg7 = coin.change_7d
    if chg7 is not None:
        v = 2.0 if chg7 < -20 else 1.0 if chg7 < -8 else 0 if abs(chg7) < 5 else -1.0 if chg7 < 20 else -1.5
        sigs.append(Signal("7-Day Price Action", v, 1.0, "CoinGecko",
            f"{coin.ticker} moved {chg7:+.1f}% over 7 days. " +
            ("Sharp dip — potential mean-reversion bounce setup." if v >= 1.5 else
             "Mild pullback — possible entry opportunity." if v == 1.0 else
             "Strong recent move — may be extended." if v < 0 else "Price is stable."),
            "momentum"))

    # ── ONCHAIN / MESSARI ─────────────────────────────────────
    if coin.messari.get("active_addresses"):
        addr = coin.messari["active_addresses"]
        # We don't have a baseline, so this is informational
        sigs.append(Signal("Active Addresses (Messari)", 0.5, 0.7, "Messari",
            f"{coin.ticker} has {addr:,.0f} active addresses on-chain. Network is active.",
            "onchain"))

    # ── MACRO CONTEXT ─────────────────────────────────────────
    fg = snap.fear_greed.get("current")
    if fg is not None:
        v = 1.5 if fg < 25 else 0.5 if fg < 45 else 0 if fg < 55 else -0.5 if fg < 75 else -1.5
        sigs.append(Signal("Market Fear & Greed", v, 1.0, "Alternative.me",
            f"Crypto market mood: '{snap.fear_greed.get('label')}' ({fg}/100). " +
            ("Extreme fear = good time to consider buying." if v >= 1.5 else
             "Extreme greed = elevated risk of correction." if v <= -1.5 else "Neutral market mood."),
            "macro"))

    btc_dom = snap.btc_dominance
    if btc_dom:
        # Rising dominance = alts suffer; falling = alt season
        if coin.ticker != "BTC":
            v = 1.0 if btc_dom < 48 else 0 if btc_dom < 55 else -1.0
            sigs.append(Signal("BTC Dominance (Alt Season)", v, 0.85, "CoinGecko",
                f"Bitcoin dominance is {btc_dom:.1f}%. " +
                ("Low dominance = altcoin season, good for {coin.ticker}." if v > 0 else
                 "High dominance = Bitcoin is taking market share from alts." if v < 0 else
                 "Neutral dominance — no strong alt/BTC preference."),
                "macro"))

    # ── DEFI TVL (for DeFi coins) ─────────────────────────────
    defi_coins = {"ETH", "SOL", "AVAX", "MATIC", "DOT", "ATOM", "UNI", "AAVE", "CRV"}
    if coin.ticker in defi_coins:
        chain_data = snap.defi_summary.get("chains", {}).get(coin.name, {})
        tvl_change = chain_data.get("tvl_change_7d")
        if tvl_change is not None:
            v = 1.5 if tvl_change > 10 else 0.5 if tvl_change > 2 else 0 if abs(tvl_change) < 2 else -0.5 if tvl_change > -10 else -1.5
            sigs.append(Signal("DeFi TVL Change (DeFiLlama)", v, 0.85, "DeFiLlama",
                f"{coin.name} ecosystem TVL changed {tvl_change:+.1f}% in 7 days. " +
                ("TVL growing = more capital flowing in. Bullish." if v > 0 else
                 "TVL declining = capital leaving the ecosystem. Bearish." if v < 0 else "TVL stable."),
                "onchain"))

    coin.signals = sigs
    return sigs


# ─── MARKET-WIDE DATA COLLECTORS ─────────────────────────────────────────────

def collect_fear_greed(snap: MarketSnapshot):
    try:
        r = requests.get("https://api.alternative.me/fng/",
            params={"limit": 14, "format": "json"}, timeout=8)
        r.raise_for_status()
        data = r.json()["data"]
        vals = [int(d["value"]) for d in data]
        snap.fear_greed = {
            "current":    vals[0],
            "label":      data[0]["value_classification"],
            "yesterday":  vals[1],
            "week_avg":   round(sum(vals[:7]) / 7, 1),
            "trend":      "improving" if vals[0] > vals[6] else "worsening" if vals[0] < vals[6] else "flat",
        }
        log.info(f"Fear & Greed: {vals[0]} ({data[0]['value_classification']})")
    except Exception as e:
        log.warning(f"Fear & Greed: {e}")


def collect_macro(snap: MarketSnapshot):
    if not FRED_API_KEY:
        snap.macro = {"_note": "Set FRED_API_KEY for macro data"}
        return
    series = {"m2": "WM2NS", "fed_rate": "FEDFUNDS", "dxy": "DTWEXBGS"}
    snap.macro = {}
    for key, sid in series.items():
        try:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": sid, "api_key": FRED_API_KEY,
                        "file_type": "json", "sort_order": "desc", "limit": 13},
                timeout=10)
            r.raise_for_status()
            obs = [o for o in r.json()["observations"] if o["value"] != "."]
            if obs:
                latest = float(obs[0]["value"])
                prev   = float(obs[min(12, len(obs)-1)]["value"])
                snap.macro[key] = {
                    "value":   latest,
                    "date":    obs[0]["date"],
                    "mom_pct": round((latest - prev) / prev * 100, 2) if prev else None,
                }
            time.sleep(0.4)
        except Exception as e:
            log.warning(f"FRED {sid}: {e}")


def collect_defi_summary(snap: MarketSnapshot):
    """DeFiLlama: chain TVL summary. Free, no key."""
    try:
        r = requests.get("https://api.llama.fi/v2/chains", timeout=12, headers=HEADERS)
        r.raise_for_status()
        chains = r.json()
        # Build quick lookup by chain name
        chain_lookup = {}
        for ch in chains[:30]:
            name = ch.get("name", "")
            chain_lookup[name] = {
                "tvl":         ch.get("tvl", 0),
                "tvl_change_7d": ch.get("change_7d") or 0,
            }
        snap.defi_summary = {
            "chains":     chain_lookup,
            "total_tvl":  sum(c.get("tvl", 0) for c in chains[:10]),
            "source":     "DeFiLlama (live)",
        }
        log.info(f"DeFiLlama: {len(chain_lookup)} chains loaded")
    except Exception as e:
        log.warning(f"DeFiLlama: {e}")


# ─── NEWS & COMMUNITY ─────────────────────────────────────────────────────────

def collect_news(snap: MarketSnapshot):
    """Collect from all 4 news RSS feeds."""
    all_headlines = []
    for url, source in NEWS_SOURCES:
        try:
            if HAS_FEEDPARSER:
                feed = feedparser.parse(url)
                for entry in feed.entries[:6]:
                    all_headlines.append({
                        "title":  entry.get("title", ""),
                        "source": source,
                        "date":   entry.get("published", ""),
                        "link":   entry.get("link", ""),
                    })
            else:
                r = requests.get(url, timeout=8, headers=HEADERS)
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', r.text)
                for t in titles[1:5]:
                    all_headlines.append({"title": t, "source": source, "date": "", "link": ""})
        except Exception as e:
            log.warning(f"RSS {source}: {e}")
    snap.news = all_headlines[:30]
    log.info(f"News: {len(snap.news)} headlines from {len(NEWS_SOURCES)} sources")


def collect_reddit(snap: MarketSnapshot):
    """Reddit hot posts from key crypto subreddits. No auth needed."""
    posts = []
    for sub in REDDIT_SUBS[:3]:  # limit to 3 to stay in rate limits
        try:
            r = requests.get(
                f"https://www.reddit.com/r/{sub}/hot.json",
                params={"limit": 10},
                timeout=10, headers=HEADERS)
            if r.status_code == 200:
                data = r.json().get("data", {}).get("children", [])
                for post in data[:5]:
                    p = post["data"]
                    posts.append({
                        "title":  p.get("title", ""),
                        "score":  p.get("score", 0),
                        "sub":    sub,
                        "comments": p.get("num_comments", 0),
                        "upvote_ratio": p.get("upvote_ratio", 0),
                    })
            time.sleep(0.8)
        except Exception as e:
            log.warning(f"Reddit r/{sub}: {e}")
    snap.reddit_posts = posts
    log.info(f"Reddit: {len(posts)} posts from {len(REDDIT_SUBS[:3])} subreddits")


# ─── AGGREGATE SIGNAL SCORE ──────────────────────────────────────────────────

def build_market_signals(snap: MarketSnapshot):
    """
    Build aggregate market-wide signals and attach to each candidate.
    Called after deep-dive data is collected.
    """
    all_sigs = []
    for coin in snap.candidates:
        coin_sigs = build_signals_for_coin(coin, snap)
        all_sigs.extend(coin_sigs)
    snap.all_signals = all_sigs


# ─── CLAUDE SYNTHESIS ─────────────────────────────────────────────────────────

def ask_claude(snap: MarketSnapshot) -> dict:
    """
    Send the full market picture to Claude.
    Claude decides: which coin is the best swing trade, and what's the signal.
    """
    # Direct requests call — bypasses httpx for Railway compatibility

    # Position/lifecycle context
    current_price = snap.btc_price
    mode    = get_mode()            if HAS_POSITION else "HUNTING"
    pos_ctx = get_position_context(current_price) if HAS_POSITION else {"investment_mode": "HUNTING"}
    mode_prompt = build_mode_prompt(mode, pos_ctx) if HAS_POSITION else ""

    # Intelligence from past predictions
    intelligence_context = load_intelligence_context() if HAS_MEMORY else ""

    # Signal credibility — which sources have actually been reliable vs misleading
    credibility_context = build_credibility_context()

    # Build candidate summaries
    candidate_summaries = []
    for c in snap.candidates:
        t = c.technicals
        candidate_summaries.append({
            "ticker":        c.ticker,
            "name":          c.name,
            "price":         c.price,
            "change_24h":    c.change_24h,
            "change_7d":     c.change_7d,
            "volume_vs_avg": t.get("volume_vs_avg"),
            "rsi_14":        t.get("rsi_14"),
            "macd_cross":    t.get("macd_cross"),
            "above_sma20":   t.get("above_sma20"),
            "above_sma50":   t.get("above_sma50"),
            "above_sma200":  t.get("above_sma200"),
            "ath_change_pct": c.ath_change_pct,
            "messari":       c.messari,
            "signals":       [{"name": s.name, "score": s.value, "text": s.plain_english} for s in c.signals],
        })

    # Top coins quick overview (non-candidates)
    top_overview = [
        {"ticker": c.ticker, "change_24h": c.change_24h, "change_7d": c.change_7d,
         "volume_m": round(c.volume_24h / 1e6, 1)}
        for c in snap.top_coins if c.ticker not in ("USDT", "USDC", "BUSD", "DAI")
    ][:15]

    # News by source
    news_by_source = {}
    for h in snap.news[:20]:
        news_by_source.setdefault(h["source"], []).append(h["title"])

    # Reddit trending
    reddit_titles = [p["title"] for p in sorted(snap.reddit_posts, key=lambda x: x["score"], reverse=True)[:10]]

    # DeFi TVL highlights
    defi_highlights = {
        k: v for k, v in snap.defi_summary.get("chains", {}).items()
        if k in ("Ethereum", "Solana", "BNB Chain", "Arbitrum", "Avalanche", "Polygon")
    }

    context_payload = {
        "timestamp":           snap.timestamp,
        "btc_price":           snap.btc_price,
        "btc_dominance_pct":   snap.btc_dominance,
        "fear_greed":          snap.fear_greed,
        "macro":               {k: v.get("value") for k, v in snap.macro.items() if isinstance(v, dict)},
        "top_20_overview":     top_overview,
        "swing_candidates":    candidate_summaries,
        "news":                news_by_source,
        "reddit_trending":     reddit_titles,
        "defi_chain_tvl":      defi_highlights,
        "investment_position": pos_ctx,
    }

    # Mode-specific signal schema
    if mode == "HOLDING":
        signal_field = '"signal": "HOLD | SELL | STRONG_SELL",'
        extra_fields = """
  "exit_urgency": 1-10,
  "exit_recommendation": "hold_full | consider_partial | full_exit",
  "exit_thesis": "why hold or sell right now","""
    elif mode == "WAITING":
        signal_field = '"signal": "RE_ENTER | WAIT_LONGER",'
        extra_fields = """
  "reentry_readiness": 0-100,
  "optimal_reentry_price": float or null,
  "reentry_thesis": "why re-enter or keep waiting","""
    else:
        signal_field = '"signal": "STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL",'
        extra_fields = """
  "entry_target": suggested entry price as float,
  "exit_target": profit-taking target price as float,
  "stop_loss": suggested stop-loss price as float,
  "expected_timeframe_days": expected trade duration as int,"""

    system = f"""You are a professional cryptocurrency swing trader and market analyst.
You have access to real-time data on the top 20 cryptocurrencies, news from CoinDesk,
Blockworks, CoinTelegraph, and CryptoSlate, Reddit community sentiment, DeFiLlama TVL data,
and Messari metrics.

{intelligence_context if intelligence_context else ""}

{credibility_context}

{mode_prompt}

YOUR PRIMARY JOB:
{"Find the single best swing trade opportunity from the candidates provided." if mode == "HUNTING" else "Manage the open position — should the investor hold or exit now?" if mode == "HOLDING" else "Determine if it's time to re-enter the market."}

SWING TRADING PRINCIPLES:
- Target: 5-20% gains over days to 2 weeks
- Best setups: oversold RSI bounce, MACD bullish cross, volume breakout, news catalyst
- Risk management: always consider downside. A good setup has clear stop-loss levels.
- Timing: news catalysts and community momentum drive short-term price action
- Avoid: coins in downtrends with no catalyst, overbought with negative news

For your selected coin, analyze:
1. Technical setup (RSI, MACD, volume, trend)
2. News catalyst (what's driving momentum or creating opportunity)
3. Community interest (Reddit buzz, sentiment direction)
4. DeFi health if applicable (TVL trends)
5. Market context (BTC dominance, overall Fear & Greed)

OUTPUT (valid JSON only, no markdown):
{{
  {signal_field}
  "selected_coin": "TICKER",
  "selected_coin_name": "Full Name",
  "selected_coin_price": float,
  "confidence": 0-100,
  "composite_score": float,
  "layman_headline": "One punchy sentence about this opportunity",
  "layman_explanation": "3-4 sentences in plain English. No jargon. Why this coin, why now.",
  "layman_action": "Exactly what a cautious investor should consider doing right now",
  "trade_catalyst": "The specific news/event/signal driving this setup in 1-2 sentences",
  "why_this_coin": "Why this coin over the others on the watchlist in 1-2 sentences",
  "expert_reasoning": "3-5 sentences with full technical + fundamental justification",{extra_fields}
  "risk_factors": ["2-3 specific risks that could invalidate this setup"],
  "watch_list": ["3 things to monitor over the next 48 hours"],
  "market_context": "1-2 sentences on overall crypto market conditions right now",
  "context_banner": "One authoritative sentence summarizing the current opportunity.",
  "alert": null or "critical alert message",
  "alert_type": null or "bullish | bearish | danger",
  "key_signals": [
    {{"source": "name of the news outlet, indicator, or community (e.g. CoinDesk, RSI, Reddit_CryptoCurrency, MACD, Fear_Greed, DeFiLlama, Messari, CoinTelegraph, Blockworks, CryptoSlate, funding_rate, volume_breakout, on_chain)", "signal_type": "news | technical | sentiment | onchain | macro", "description": "one sentence on what this signal said and why it mattered to your decision"}},
    ... up to 5 key signals that most influenced your BUY/SELL/HOLD decision
  ]
}}"""

    try:
        _r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": MODEL,
                "max_tokens": 2500,
                "system": system,
                "messages": [{"role": "user", "content":
                    f"Analyze this crypto market snapshot and identify the best swing trade:\n\n"
                    f"{json.dumps(context_payload, indent=2, default=str)}"}],
            },
            timeout=60,
        )
        _r.raise_for_status()
        raw = _r.json()["content"][0]["text"].strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"): raw = raw[4:]
        result = json.loads(raw.strip())
        result["_mode"] = mode
        return result
    except json.JSONDecodeError:
        log.error("Claude returned non-JSON")
        return {"signal": "HOLD", "_mode": mode, "confidence": 0,
                "selected_coin": "BTC", "selected_coin_name": "Bitcoin",
                "selected_coin_price": snap.btc_price,
                "layman_headline": "Analysis error — try again shortly",
                "layman_explanation": "The system encountered an error this cycle.",
                "layman_action": "No action recommended until next run.", "alert": None}
    except Exception as e:
        log.error(f"Claude API failed: {e}")
        return {"signal": "HOLD", "_mode": mode, "confidence": 0,
                "selected_coin": "BTC", "selected_coin_name": "Bitcoin",
                "selected_coin_price": snap.btc_price,
                "layman_headline": "Service temporarily unavailable",
                "layman_explanation": "Could not reach the analysis service.",
                "layman_action": "Wait for the next update.",
                "expert_reasoning": str(e), "alert": None}


# ─── DISPLAY ─────────────────────────────────────────────────────────────────

def display_layman(analysis: dict, snap: MarketSnapshot, perf: dict = None):
    """Crypto Strategy Clock terminal display."""
    internal_sig = analysis.get("signal", "HOLD")
    mode         = analysis.get("_mode", "HUNTING")
    display_sig  = resolve_display_signal(internal_sig, mode)
    conf         = analysis.get("confidence", 0)
    coin_ticker  = analysis.get("selected_coin", "?")
    coin_name    = analysis.get("selected_coin_name", "")
    coin_price   = analysis.get("selected_coin_price", snap.btc_price)
    color, label, _ = SIGNAL_STYLES.get(display_sig, SIGNAL_STYLES["HOLD"])

    W      = 60
    border = "╔" + "═"*W + "╗"
    mid    = "╠" + "═"*W + "╣"
    bottom = "╚" + "═"*W + "╝"
    blank  = "║" + " "*W + "║"
    row    = lambda s: "║  " + s.ljust(W-2) + "║"

    def acc_bar(pct, width=20):
        filled = max(0, min(width, int(pct / 100 * width)))
        return "█" * filled + "░" * (width - filled)

    print()
    print(border)
    print(row(f"  🔮  CRYPTO STRATEGY CLOCK  v3.0"))
    print(row(f"      {datetime.now().strftime('%B %d, %Y  ·  %I:%M %p')}"))
    print(mid)

    # ── COIN HERO ─────────────────────────────────────────────
    print(blank)
    coin_str  = f"{coin_ticker}  ·  {coin_name}  ·  ${coin_price:,.2f}"
    cpad      = (W - len(coin_str)) // 2
    print("║" + " "*cpad + f"\033[97m{BOLD}{coin_str}{RESET}" + " "*(W - cpad - len(coin_str)) + "║")
    print(blank)

    # ── SIGNAL ────────────────────────────────────────────────
    sig_str = f"{color}{BOLD}  {label}  {RESET}"
    pad     = (W - len(label) - 4) // 2
    print("║" + " "*pad + sig_str + " "*(W - pad - len(label) - 4) + "║")
    conf_str = f"Confidence: {conf}%"
    cpad2    = (W - len(conf_str)) // 2
    print("║" + " "*cpad2 + conf_str + " "*(W - cpad2 - len(conf_str)) + "║")
    print(blank)

    # ── CONTEXT BANNER ────────────────────────────────────────
    banner = analysis.get("context_banner", "")
    if banner:
        print(mid)
        print(blank)
        for line in textwrap.wrap(f'"{banner}"', W-4):
            lpad = (W - len(line)) // 2
            print("║" + " "*lpad + line + " "*(W - lpad - len(line)) + "║")
        print(blank)

    # ── POSITION STATUS ───────────────────────────────────────
    if HAS_POSITION and mode == "HOLDING":
        pos = get_display_state(coin_price)
        if pos.get("entry_price"):
            entry = pos["entry_price"]
            upct  = pos.get("unrealized_pct", 0)
            dheld = pos.get("days_held", 0)
            g_col = "\033[92m" if upct >= 0 else "\033[91m"
            print(mid)
            print(row(f"  📈  Open Position in {pos.get('coin_ticker', coin_ticker)}"))
            print(row(f"  Entered: ${entry:,.2f}  ·  {dheld} days ago"))
            print(row(f"  Unrealized: {g_col}{'▲' if upct>=0 else '▼'} {abs(upct):.1f}%{RESET}"))
            print(blank)

    elif HAS_POSITION and mode == "WAITING":
        pos = get_display_state(coin_price)
        if pos.get("sold_price"):
            sold  = pos["sold_price"]
            chgs  = pos.get("price_change_since_sale", 0)
            profit = pos.get("last_profit_pct", 0)
            print(mid)
            print(row(f"  💰  Sold at ${sold:,.2f}  ·  Profit: {'+' if profit>=0 else ''}{profit:.1f}%"))
            cheaper = chgs <= 0
            print(row(f"  Since sale: {'✅ ' if cheaper else ''}{'Cheaper' if cheaper else 'Higher'} by {abs(chgs):.1f}%"))
            rt = analysis.get("reentry_thesis", "")
            if rt:
                for line in textwrap.wrap(f"  {rt}", W-4):
                    print(row(f"  {line}"))
            print(blank)

    # ── CATALYST & EXPLANATION ────────────────────────────────
    print(mid)
    print(blank)
    catalyst = analysis.get("trade_catalyst", "")
    if catalyst and mode == "HUNTING":
        print(row(f"  ⚡ Why now:"))
        for line in textwrap.wrap(catalyst, W-6):
            print(row(f"     {line}"))
        print(blank)

    explanation = analysis.get("layman_explanation", "")
    for line in textwrap.wrap(explanation, W-4):
        print(row(f"  {line}"))
    print(blank)

    action = analysis.get("layman_action", "")
    if action:
        print(row(f"  👉 What to consider:"))
        for line in textwrap.wrap(action, W-6):
            print(row(f"     {line}"))
        print(blank)

    # ── PRICE TARGETS (HUNTING mode) ─────────────────────────
    if mode == "HUNTING":
        entry_t = analysis.get("entry_target")
        exit_t  = analysis.get("exit_target")
        stop    = analysis.get("stop_loss")
        days_t  = analysis.get("expected_timeframe_days")
        if entry_t or exit_t:
            print(mid)
            print(row(f"  📏  Trade Parameters"))
            if entry_t: print(row(f"  Entry target:  ${entry_t:,.2f}"))
            if exit_t:  print(row(f"  Exit target:   ${exit_t:,.2f}"))
            if stop:    print(row(f"  Stop-loss:     ${stop:,.2f}"))
            if days_t:  print(row(f"  Expected hold: {days_t} days"))
            print(blank)

    # ── ALERT ─────────────────────────────────────────────────
    alert = analysis.get("alert")
    if alert:
        at = analysis.get("alert_type", "")
        em = "🚨" if at == "danger" else "📣" if at == "bullish" else "⚠️"
        print(mid)
        for line in textwrap.wrap(f"  {em}  {alert}", W-2):
            print(row(line))
        print(blank)

    # ── ACCURACY GAUGE ────────────────────────────────────────
    if perf and perf.get("evaluated", 0) >= 3:
        acc     = perf.get("accuracy_7d") or 0
        acc_rec = perf.get("accuracy_recent") or acc
        n_eval  = perf.get("evaluated", 0)
        acc_col = "\033[92m" if acc >= 70 else "\033[93m" if acc >= 50 else "\033[91m"
        arrow   = "↑ getting sharper" if acc_rec > acc + 2 else \
                  "↓ needs improvement" if acc_rec < acc - 2 else "→ holding steady"
        print(mid)
        print(row(f"  📊  Crystal Ball Accuracy"))
        print(blank)
        print(row(f"  {acc_col}{acc_bar(acc)}  {acc:.0f}%{RESET}"))
        print(row(f"  Based on {n_eval} verified predictions  ·  {arrow}"))
        streak = perf.get("streak", 0)
        if streak >= 3:
            em2 = "🔥" if perf.get("streak_type") == "correct" else "❄️"
            print(row(f"  {em2} {streak} in a row"))
        print(blank)

    # ── THREE-TIER LEARNING LOG ───────────────────────────────
    if perf and perf.get("evaluated", 0) >= 3:
        print(mid)
        print(row(f"  🧠  What the Crystal Ball has learned:"))
        print(blank)
        recent = perf.get("most_recent_lesson", "")
        if recent:
            print(row(f"  Most recent lesson:"))
            for line in textwrap.wrap(f'"{recent}"', W-6):
                print(row(f"     {line}"))
            print(blank)
        top3 = perf.get("top_3_patterns", [])
        if top3:
            print(row(f"  Top patterns:"))
            for i, p in enumerate(top3[:3], 1):
                for line in textwrap.wrap(f"  {i}.  {p}", W-4):
                    print(row(f"  {line}"))
            print(blank)
        red_flag = perf.get("red_flag", "")
        if red_flag:
            print(row(f"  ⚠  Honest red flag:"))
            for line in textwrap.wrap(f'"{red_flag}"', W-6):
                print(row(f"     {line}"))
            print(blank)

    # ── RISK FACTORS ──────────────────────────────────────────
    risks = analysis.get("risk_factors", [])
    if risks:
        print(mid)
        print(row(f"  ⚠   Risks to watch:"))
        for r_item in risks[:3]:
            for line in textwrap.wrap(f"  •  {r_item}", W-2):
                print(row(line))
        print(blank)

    # ── WATCH LIST ────────────────────────────────────────────
    watch = analysis.get("watch_list", [])
    if watch:
        print(mid)
        print(row(f"  👁   Next 48 hours:"))
        for item in watch[:3]:
            for line in textwrap.wrap(f"  •  {item}", W-2):
                print(row(line))
        print(blank)

    print(bottom)
    print()


# ─── HTML REPORT ─────────────────────────────────────────────────────────────

def _display_portfolio(current_price: float = None):
    """Print paper portfolio card to terminal after the main display."""
    W      = 58
    BOLD   = "\033[1m"
    RESET  = "\033[0m"
    border = "╔" + "═"*W + "╗"
    bottom = "╚" + "═"*W + "╝"
    mid    = "╠" + "═"*W + "╣"
    blank  = "║" + " "*W + "║"
    def row(s):
        return "║" + s + " "*(W - len(s)) + "║"

    lines = get_portfolio_summary_lines(current_price)
    print()
    print(mid)
    for line in lines:
        # Strip ANSI for length calc
        import re as _re
        plain = _re.sub(r'\033\[[0-9;]*m', '', line)
        pad   = W - len(plain)
        print("║" + line + " "*pad + "║")
    print(bottom)
    print()


def generate_html_report(analysis: dict, snap: MarketSnapshot, perf: dict = None):
    """Crypto Strategy Clock HTML dashboard."""
    internal_sig = analysis.get("signal", "HOLD")
    mode         = analysis.get("_mode", "HUNTING")
    display_sig  = resolve_display_signal(internal_sig, mode)
    conf         = analysis.get("confidence", 0)
    coin_ticker  = analysis.get("selected_coin", "BTC")
    coin_name    = analysis.get("selected_coin_name", "Bitcoin")
    coin_price   = analysis.get("selected_coin_price", snap.btc_price)

    sig_hex = {"BUY": "#00c853", "HOLD": "#ff9800", "WAIT": "#f44336",
               "SELL": "#ef5350", "SELL NOW": "#ff1744",
               "BUY BACK": "#00e676", "NOT YET": "#ff9800"}
    color  = sig_hex.get(display_sig, "#9e9e9e")
    banner = analysis.get("context_banner", "")
    alert  = analysis.get("alert", "")
    at     = analysis.get("alert_type", "")

    alert_html = ""
    if alert:
        ac = "#f44336" if at == "danger" else "#00c853" if at == "bullish" else "#ff9800"
        em = "🚨" if at == "danger" else "📣" if at == "bullish" else "⚠️"
        alert_html = f'<div class="alert-strip" style="border-left:4px solid {ac};background:{ac}14">{em}&nbsp; {alert}</div>'

    # Trade parameters
    entry_t = analysis.get("entry_target")
    exit_t  = analysis.get("exit_target")
    stop    = analysis.get("stop_loss")
    days_t  = analysis.get("expected_timeframe_days")
    targets_html = ""
    if mode == "HUNTING" and (entry_t or exit_t):
        targets_html = f"""
  <div class="card targets-card">
    <div class="card-label">Trade Parameters</div>
    <div class="targets-grid">
      {f'<div class="target-item"><div class="target-lbl">Entry Target</div><div class="target-val">${entry_t:,.2f}</div></div>' if entry_t else ""}
      {f'<div class="target-item"><div class="target-lbl">Exit Target</div><div class="target-val green">${exit_t:,.2f}</div></div>' if exit_t else ""}
      {f'<div class="target-item"><div class="target-lbl">Stop-Loss</div><div class="target-val red">${stop:,.2f}</div></div>' if stop else ""}
      {f'<div class="target-item"><div class="target-lbl">Hold Time</div><div class="target-val">{days_t} days</div></div>' if days_t else ""}
    </div>
  </div>"""

    # Why this coin
    why_html = ""
    why = analysis.get("why_this_coin", "")
    catalyst = analysis.get("trade_catalyst", "")
    if (why or catalyst) and mode == "HUNTING":
        why_html = f"""
  <div class="card why-card">
    <div class="card-label">Why {coin_ticker} — Why Now</div>
    {f'<div class="why-catalyst"><span class="catalyst-em">⚡ Catalyst:</span> {catalyst}</div>' if catalyst else ""}
    {f'<div class="why-text">{why}</div>' if why else ""}
  </div>"""

    # News headlines
    news_by_source = {}
    for h in snap.news[:20]:
        news_by_source.setdefault(h["source"], []).append(h["title"])
    news_html = ""
    for source, titles in news_by_source.items():
        items = "".join(f"<li>{t}</li>" for t in titles[:3])
        news_html += f'<div class="news-source"><div class="news-src-lbl">{source}</div><ul>{items}</ul></div>'

    # Reddit trending
    reddit_html = ""
    if snap.reddit_posts:
        top_posts = sorted(snap.reddit_posts, key=lambda x: x["score"], reverse=True)[:5]
        reddit_items = "".join(f'<li>{p["title"]} <span class="hl-src">r/{p["sub"]} · {p["score"]} pts</span></li>'
                               for p in top_posts)
        reddit_html = f'<div class="card"><div class="card-label">Reddit Trending</div><ul>{reddit_items}</ul></div>'

    # Paper portfolio card
    portfolio_html = build_portfolio_html(coin_price) if HAS_PAPER else ""

    # Performance/learning section
    perf_html = _build_performance_html(perf) if perf and perf.get("evaluated", 0) >= 3 else ""

    # Risk factors
    risks = analysis.get("risk_factors", [])
    risk_html = ""
    if risks:
        risk_items = "".join(f"<li>{r}</li>" for r in risks)
        risk_html = f'<div class="card"><div class="card-label">⚠ Risks to Watch</div><ul class="risk-list">{risk_items}</ul></div>'

    # Watch list
    watch_items = "".join(f"<li>{w}</li>" for w in analysis.get("watch_list", [])[:3])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Crypto Strategy Clock — {coin_ticker} — {datetime.now().strftime('%b %d, %Y')}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;background:#07070f;color:#e0e0e0;padding:20px 16px;min-height:100vh}}
.wrap{{max-width:660px;margin:0 auto}}
.header{{text-align:center;padding:24px 0 8px}}
.header-title{{font-size:.8em;text-transform:uppercase;letter-spacing:.18em;color:#444;font-weight:600}}
.header-ts{{font-size:.78em;color:#333;margin-top:6px}}
.alert-strip{{padding:14px 18px;border-radius:10px;margin:16px 0;line-height:1.6;font-size:.92em;color:#ddd}}
.hero-card{{background:linear-gradient(160deg,#0f0f1a,#141428);border:1px solid #1e1e3a;border-radius:16px;padding:40px 24px 32px;text-align:center;margin:16px 0;position:relative;overflow:hidden}}
.hero-card::before{{content:'';position:absolute;top:-60px;left:50%;transform:translateX(-50%);width:320px;height:320px;background:radial-gradient({color}18,transparent 70%);pointer-events:none}}
.hero-coin{{font-size:1.1em;font-weight:800;color:#fff;letter-spacing:.05em;margin-bottom:6px}}
.hero-price{{font-size:.9em;color:#666;margin-bottom:24px}}
.hero-signal{{font-size:4.5em;font-weight:900;letter-spacing:.15em;color:{color};line-height:1;margin-bottom:12px;text-shadow:0 0 40px {color}44}}
.hero-conf{{font-size:.95em;color:#666;margin-bottom:20px}}
.conf-track{{width:160px;height:4px;background:#1a1a2a;border-radius:2px;margin:0 auto 24px}}
.conf-fill{{height:100%;border-radius:2px;background:{color};width:{conf}%}}
.hero-banner{{font-size:.95em;color:#999;line-height:1.7;font-style:italic;max-width:500px;margin:0 auto 24px;padding:0 12px}}
.hero-explanation{{font-size:1em;color:#ccc;line-height:1.75;margin-bottom:20px;text-align:left;background:#0a0a14;border-radius:10px;padding:16px 18px}}
.hero-action{{background:#0f1f0f;border-left:3px solid #00c853;border-radius:0 8px 8px 0;padding:14px 16px;text-align:left;font-size:.92em;color:#ccc;line-height:1.7}}
.card{{background:#0f0f1a;border:1px solid #1a1a2a;border-radius:12px;padding:20px;margin-bottom:14px}}
.card-label{{font-size:.72em;text-transform:uppercase;letter-spacing:.12em;color:#444;font-weight:600;margin-bottom:14px}}
.targets-grid{{display:grid;grid-template-columns:1fr 1fr;gap:12px}}
.target-item{{background:#0a0a14;border-radius:8px;padding:12px 14px}}
.target-lbl{{font-size:.75em;color:#555;margin-bottom:4px}}
.target-val{{font-size:1.15em;font-weight:700;color:#fff}}
.target-val.green{{color:#00c853}}
.target-val.red{{color:#ef5350}}
.why-catalyst{{font-size:.9em;color:#aaa;margin-bottom:10px;line-height:1.6;padding:10px 14px;background:#0a0a14;border-left:3px solid {color};border-radius:0 6px 6px 0}}
.catalyst-em{{color:{color};font-weight:600}}
.why-text{{font-size:.9em;color:#888;line-height:1.65;margin-top:10px}}
.news-source{{margin-bottom:16px}}
.news-src-lbl{{font-size:.72em;text-transform:uppercase;letter-spacing:.1em;color:#444;font-weight:600;margin-bottom:8px}}
.card ul{{padding-left:18px;color:#999;line-height:2.1;font-size:.9em}}
.risk-list li{{color:#ef9a9a}}
.hl-src{{color:#333;font-size:.82em}}
.footer{{text-align:center;color:#252535;font-size:.75em;margin:24px 0 8px}}
/* Perf / learning (reuse from v2) */
.accuracy-card h3{{margin-bottom:4px}}
.acc-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}}
.acc-pct{{font-size:3em;font-weight:800;line-height:1}}
.acc-sub{{color:#555;font-size:.85em;margin-top:2px}}
.gauge-track{{height:10px;background:#1e1e2e;border-radius:5px;overflow:hidden;margin-bottom:8px}}
.gauge-fill{{height:100%;border-radius:5px;transition:width 1s ease}}
.acc-trend{{font-size:.9em;font-weight:500;margin-bottom:8px}}
.streak-badge{{display:inline-block;padding:5px 14px;border-radius:20px;font-size:.85em;font-weight:600;margin:8px 0}}
.learning-card h3{{margin-bottom:20px}}
.learning-tier{{margin-bottom:22px;padding-bottom:22px;border-bottom:1px solid #1e1e2e}}
.learning-tier:last-child{{margin-bottom:0;padding-bottom:0;border-bottom:none}}
.tier-eyebrow{{font-size:.75em;text-transform:uppercase;letter-spacing:.1em;color:#555;margin-bottom:8px;font-weight:600}}
.tier1 .tier-eyebrow{{color:#4caf50}}
.tier2 .tier-eyebrow{{color:#64b5f6}}
.tier3 .tier-eyebrow{{color:#ef9a9a}}
.tier-text{{color:#ccc;line-height:1.75;font-size:.95em}}
.pattern-row{{display:flex;align-items:flex-start;gap:12px;margin-bottom:10px}}
.pattern-num{{font-size:1.3em;font-weight:800;color:#64b5f6;line-height:1.3;flex-shrink:0;width:20px}}
.pattern-text{{color:#ccc;line-height:1.75;font-size:.95em}}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div class="header-title">🔮 Crypto Strategy Clock</div>
    <div class="header-ts">{datetime.now().strftime('%B %d, %Y  ·  %I:%M %p')}</div>
  </div>

  {alert_html}

  <div class="hero-card">
    <div class="hero-coin">{coin_ticker} &nbsp;·&nbsp; {coin_name}</div>
    <div class="hero-price">${coin_price:,.4f}</div>
    <div class="hero-signal">{display_sig}</div>
    <div class="hero-conf">Confidence: {conf}%</div>
    <div class="conf-track"><div class="conf-fill"></div></div>
    {f'<div class="hero-banner">{banner}</div>' if banner else ""}
    <div class="hero-explanation">{analysis.get("layman_explanation","")}</div>
    <div class="hero-action">👉 &nbsp;{analysis.get("layman_action","")}</div>
  </div>

  {portfolio_html}
  {targets_html}
  {why_html}
  {perf_html}
  {risk_html}
  {'<div class="card"><div class="card-label">Watch Next 48 Hours</div><ul>' + watch_items + '</ul></div>' if watch_items else ""}
  {'<div class="card"><div class="card-label">Latest News</div>' + news_html + '</div>' if news_html else ""}
  {reddit_html}

  <div class="footer">Crypto Strategy Clock · Powered by Claude AI · Not financial advice</div>
</div>
</body>
</html>"""

    with open(HTML_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"HTML saved: {HTML_FILE}")


def _build_performance_html(perf: dict) -> str:
    """Accuracy gauge + three-tier learning log (shared with v2)."""
    if not perf: return ""
    acc     = perf.get("accuracy_7d") or 0
    acc_rec = perf.get("accuracy_recent") or acc
    n_eval  = perf.get("evaluated", 0)
    streak  = perf.get("streak", 0)
    stype   = perf.get("streak_type", "")
    g_col   = "#00c853" if acc >= 70 else "#ff9800" if acc >= 50 else "#f44336"
    trend   = "Getting sharper ↑" if acc_rec > acc + 2 else \
              "Needs improvement ↓" if acc_rec < acc - 2 else "Holding steady →"
    t_col   = "#4caf50" if "sharper" in trend else "#f44336" if "Needs" in trend else "#888"
    streak_html = ""
    if streak >= 3:
        sc = "#4caf50" if stype == "correct" else "#f44336"
        em = "🔥" if stype == "correct" else "❄️"
        streak_html = f'<div class="streak-badge" style="background:{sc}18;color:{sc};border:1px solid {sc}40">{em} {streak} in a row</div>'
    recent = perf.get("most_recent_lesson", "")
    top3   = perf.get("top_3_patterns", [])
    rf     = perf.get("red_flag", "")
    patterns_html = "".join(
        f'<div class="pattern-row"><span class="pattern-num">{i}</span><span class="pattern-text">{p}</span></div>'
        for i, p in enumerate(top3[:3], 1)
    ) or '<div class="pattern-row"><span class="pattern-text"><em>Patterns emerge after more predictions.</em></span></div>'
    return f"""
  <div class="card accuracy-card">
    <div class="acc-header">
      <div><h3>Crystal Ball Accuracy</h3><div class="acc-sub">Based on {n_eval} verified predictions</div></div>
      <div class="acc-pct" style="color:{g_col}">{acc:.0f}%</div>
    </div>
    <div class="gauge-track"><div class="gauge-fill" style="width:{min(acc,100):.0f}%;background:{g_col}"></div></div>
    <div class="acc-trend" style="color:{t_col}">{trend}</div>
    {streak_html}
  </div>
  <div class="card learning-card">
    <h3>What the Crystal Ball has learned</h3>
    <div class="learning-tier tier1">
      <div class="tier-eyebrow">Most recent lesson</div>
      <div class="tier-text">{"<em>Still learning.</em>" if not recent else f'"{recent}"'}</div>
    </div>
    <div class="learning-tier tier2">
      <div class="tier-eyebrow">Top patterns across all history</div>
      {patterns_html}
    </div>
    <div class="learning-tier tier3">
      <div class="tier-eyebrow">⚠ Honest red flag</div>
      <div class="tier-text">{"<em>No red flags yet.</em>" if not rf else f'"{rf}"'}</div>
    </div>
  </div>"""


# ─── EMAIL REPORT ────────────────────────────────────────────────────────────

def send_daily_report(analysis: dict):
    """
    Emails the full HTML dashboard to ALERT_EMAIL_TO after every run.
    Requires:
      ALERT_EMAIL          — Gmail address you send FROM (e.g. cryptobot.zach@gmail.com)
      ALERT_EMAIL_PASSWORD — Gmail App Password (16-char, not your real password)
      ALERT_EMAIL_TO       — where to receive reports (your real inbox)

    Set these once:
      setx ALERT_EMAIL          "cryptobot.zach@gmail.com"
      setx ALERT_EMAIL_PASSWORD "abcd efgh ijkl mnop"
      setx ALERT_EMAIL_TO       "zachgreenwell@live.com"
    """
    if not (ALERT_EMAIL and ALERT_EMAIL_PASS and ALERT_EMAIL_TO):
        return  # silently skip if not configured

    try:
        from email.mime.multipart import MIMEMultipart
        from email.mime.text import MIMEText

        coin    = analysis.get("selected_coin", "CRYPTO")
        price   = analysis.get("selected_coin_price", 0)
        conf    = analysis.get("confidence", 0)
        mode    = analysis.get("_mode", "HUNTING")
        sig     = resolve_display_signal(analysis.get("signal", "HOLD"), mode)
        dt_str  = datetime.now().strftime("%b %d, %Y · %I:%M %p")

        subject = f"🔮 Crypto Clock [{sig}] {coin} ${price:,.2f} — {dt_str}"

        # Read the HTML file we just generated
        html_body = ""
        if os.path.exists(HTML_FILE):
            with open(HTML_FILE, encoding="utf-8") as f:
                html_body = f.read()

        # Plain-text fallback
        plain = f"""{sig} — {coin}
Price: ${price:,.2f} | Confidence: {conf}%

{analysis.get('layman_headline','')}

{analysis.get('layman_explanation','')}

Action: {analysis.get('layman_action','')}

Catalyst: {analysis.get('trade_catalyst','')}

— Crypto Strategy Clock v3.0"""

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = ALERT_EMAIL
        msg["To"]      = ALERT_EMAIL_TO
        msg.attach(MIMEText(plain, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))

        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
            srv.login(ALERT_EMAIL, ALERT_EMAIL_PASS)
            srv.sendmail(ALERT_EMAIL, [ALERT_EMAIL_TO], msg.as_string())

        log.info(f"📧 Daily report emailed → {ALERT_EMAIL_TO}")

    except Exception as e:
        log.warning(f"Email report failed: {e}")


# ─── ALERTS ──────────────────────────────────────────────────────────────────

def send_alerts(analysis: dict, snap: MarketSnapshot):
    sig   = analysis.get("signal", "HOLD")
    alert = analysis.get("alert")
    coin  = analysis.get("selected_coin", "CRYPTO")
    price = analysis.get("selected_coin_price", 0)
    conf  = analysis.get("confidence", 0)

    should_alert = sig in ("STRONG_BUY", "STRONG_SELL") or \
                   (alert and analysis.get("alert_type") == "danger")
    if not should_alert:
        return

    display_sig = resolve_display_signal(sig, analysis.get("_mode", "HUNTING"))
    subject = f"Crypto Clock: {display_sig} {coin} at ${price:,.2f} ({conf}% confidence)"
    body    = f"""{display_sig} — {coin}
Price: ${price:,.2f} | Confidence: {conf}%

{analysis.get('layman_headline','')}

{analysis.get('layman_explanation','')}

Action: {analysis.get('layman_action','')}

Catalyst: {analysis.get('trade_catalyst','')}

{f'ALERT: {alert}' if alert else ''}

— Crypto Strategy Clock v3.0"""

    if ALERT_EMAIL and ALERT_EMAIL_PASS:
        try:
            from email.mime.text import MIMEText
            import smtplib
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"]    = ALERT_EMAIL
            msg["To"]      = ALERT_EMAIL_TO
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
                srv.login(ALERT_EMAIL, ALERT_EMAIL_PASS)
                srv.sendmail(ALERT_EMAIL, [ALERT_EMAIL_TO], msg.as_string())
            log.info("Email alert sent")
        except Exception as e:
            log.warning(f"Email alert: {e}")

    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL,
                json={"content": f"**{subject}**\n\n{body[:1900]}"},
                timeout=8)
        except Exception as e:
            log.warning(f"Discord alert: {e}")


# ─── PERSISTENCE ─────────────────────────────────────────────────────────────

def save_log(analysis: dict, snap: MarketSnapshot):
    record = {
        "ts":           snap.timestamp,
        "coin":         analysis.get("selected_coin"),
        "price":        analysis.get("selected_coin_price"),
        "signal":       analysis.get("signal"),
        "confidence":   analysis.get("confidence"),
        "score":        analysis.get("composite_score"),
        "fg":           snap.fear_greed.get("current"),
        "btc_price":    snap.btc_price,
        "btc_dominance":snap.btc_dominance,
        "candidates":   [c.ticker for c in snap.candidates],
        "alert":        analysis.get("alert"),
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ─── MAIN CYCLE ──────────────────────────────────────────────────────────────

def run_cycle(expert_mode: bool = False):
    log.info("━"*55)
    log.info(f"Crypto Strategy Clock — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("━"*55)

    # ── 0. Restore state from GitHub ─────────────────────────
    # Railway filesystem resets between cron runs.
    # Pull portfolio + signal data from GitHub so memory survives.
    pull_state_from_github()

    snap = MarketSnapshot(timestamp=datetime.now(timezone.utc).isoformat())

    # ── 1. Market-wide context ───────────────────────────────
    collect_fear_greed(snap)
    collect_macro(snap)
    collect_defi_summary(snap)
    collect_news(snap)
    collect_reddit(snap)

    # ── 2. Top-20 scan ───────────────────────────────────────
    scan_top_coins(snap)
    if not snap.top_coins:
        log.warning("CoinGecko unavailable this cycle — will retry next run")
        return

    # ── 3. Select candidates for deep dive ───────────────────
    candidates = select_candidates(snap)
    snap.candidates = candidates

    # ── 4. Deep dive on each candidate ───────────────────────
    for coin in candidates:
        log.info(f"Deep dive: {coin.ticker}")
        collect_coin_ohlc(coin)
        collect_messari_metrics(coin)
        build_technicals_for_coin(coin)

    # ── 5. Build signals ─────────────────────────────────────
    build_market_signals(snap)

    # ── 6. Learning cycle ────────────────────────────────────
    perf = {}
    if HAS_MEMORY:
        if snap.btc_price:
            perf = run_learning_cycle(snap.btc_price)
        else:
            perf = get_performance_stats()

    # ── 7. Claude synthesis ──────────────────────────────────
    log.info("Sending to Claude for synthesis...")
    analysis = ask_claude(snap)

    # ── 8. Update position lifecycle ─────────────────────────
    if HAS_POSITION:
        signal     = analysis.get("signal", "HOLD")
        confidence = analysis.get("confidence", 0)
        coin_price = analysis.get("selected_coin_price", snap.btc_price)
        pre_trade_mode = analysis.get("_mode", "HUNTING")  # capture BEFORE pm changes it
        new_mode   = pm_process_signal(signal, coin_price, confidence)
        analysis["_mode"] = new_mode
        log.info(f"Mode: {new_mode}  |  Signal: {signal}  |  Coin: {analysis.get('selected_coin')}")

    # ── 9. Paper trading — auto-execute on signal ────────────
    if HAS_PAPER:
        coin_ticker  = analysis.get("selected_coin", "BTC")
        coin_id      = analysis.get("selected_coin_id",
                           coin_ticker.lower() if coin_ticker else "bitcoin")
        coin_price_  = analysis.get("selected_coin_price", snap.btc_price) or 0
        signal_      = analysis.get("signal", "HOLD")
        confidence_  = analysis.get("confidence", 0)
        mode_        = pre_trade_mode if HAS_POSITION else analysis.get("_mode", "HUNTING")
        exit_target_ = analysis.get("exit_target")
        stop_loss_   = analysis.get("stop_loss")
        entry_target_= analysis.get("entry_target")
        expected_d_  = analysis.get("expected_timeframe_days")

        # Check stop-loss and take-profit first (price-driven exits)
        # IMPORTANT: must use the HELD coin's current price, not the selected coin's price.
        # They can be completely different assets (e.g. holding BTC while Claude picks ETH).
        sl_trade = None
        tp_trade = None
        # Use the HELD coin's live price for stop-loss/take-profit checks.
        # The selected coin may be a completely different asset.
        held_state = load_portfolio()
        held_info  = held_state.get("holding")
        if held_info:
            held_ticker    = held_info.get("coin_ticker", "")
            held_coin_snap = next((c for c in snap.top_coins if c.ticker == held_ticker), None)
            held_price_    = held_coin_snap.price if held_coin_snap else None
            if held_price_ and held_price_ > 0:
                sl_trade = check_stop_loss(held_price_)
                if sl_trade:
                    log_trade_exit(
                        profit_pct=sl_trade.get("profit_pct", 0.0),
                        won=sl_trade.get("result", "LOSS") == "WIN",
                    )
                else:
                    tp_trade = check_take_profit(held_price_)
                    if tp_trade:
                        log_trade_exit(
                            profit_pct=tp_trade.get("profit_pct", 0.0),
                            won=tp_trade.get("result", "WIN") == "WIN",
                        )

        # Signal-driven trades
        if signal_ in ("BUY", "STRONG_BUY") and mode_ == "HUNTING":
            execute_buy(coin_ticker, coin_id, coin_price_, signal_, confidence_,
                        entry_target=entry_target_, exit_target=exit_target_,
                        stop_loss=stop_loss_, expected_days=expected_d_)
            # Log which signals drove this entry for future credibility scoring
            log_trade_entry(
                coin=coin_ticker,
                key_signals=analysis.get("key_signals", []),
                confidence=confidence_,
            )

        elif signal_ in ("SELL", "STRONG_SELL") and mode_ == "HOLDING":
            sold = execute_sell(coin_price_, signal_, reason="signal")
            if sold:
                log_trade_exit(
                    profit_pct=sold.get("profit_pct", 0.0),
                    won=sold.get("result", "LOSS") == "WIN",
                )

    # ── 10. Save prediction ──────────────────────────────────
    if HAS_MEMORY:
        save_prediction(snap, analysis)

    # ── 11. Output ───────────────────────────────────────────
    display_layman(analysis, snap, perf=perf)
    if HAS_PAPER:
        _display_portfolio(analysis.get("selected_coin_price", snap.btc_price))

    # ── 12. Artifacts ────────────────────────────────────────
    generate_html_report(analysis, snap, perf=perf)
    save_log(analysis, snap)
    send_daily_report(analysis)   # always email the HTML dashboard if configured
    send_alerts(analysis, snap)   # urgent-only alerts (STRONG_BUY / STRONG_SELL)

    log.info(f"Cycle complete. Dashboard: {HTML_FILE}")

    # ── 13. Push results to GitHub so local dashboard stays live ─
    push_results_to_github(analysis, snap)

    return analysis


# ─── GITHUB STATE SYNC ───────────────────────────────────────────────────────

def pull_state_from_github():
    """
    Pull persistent state files from GitHub at the start of each run.
    Railway's filesystem resets between cron executions — this restores memory.
    Files pulled: paper_portfolio.json, signal_credibility.json
    """
    if not GITHUB_TOKEN:
        return

    import base64

    gh_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    base_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents"

    for filename in ("paper_portfolio.json", "signal_credibility.json", "position_state.json"):
        try:
            r = requests.get(f"{base_url}/{filename}", headers=gh_headers, timeout=10)
            if r.status_code == 200:
                content = base64.b64decode(r.json()["content"]).decode()
                with open(filename, "w") as f:
                    f.write(content)
                log.info(f"GitHub ↓ restored {filename}")
            elif r.status_code == 404:
                log.info(f"GitHub: {filename} not on GitHub yet — will create after this run")
            else:
                log.warning(f"GitHub pull {filename}: {r.status_code}")
        except Exception as e:
            log.warning(f"GitHub pull error ({filename}): {e}")


def push_results_to_github(analysis: dict, snap: MarketSnapshot):
    """
    After every cycle, push key data files to GitHub.
    The local crypto_dashboard.html fetches these on open — always fresh.
    """
    if not GITHUB_TOKEN:
        return

    import base64

    # Build latest_analysis.json — everything the dashboard needs
    latest = {
        "timestamp":            snap.timestamp,
        "signal":               analysis.get("signal"),
        "selected_coin":        analysis.get("selected_coin"),
        "selected_coin_name":   analysis.get("selected_coin_name"),
        "selected_coin_price":  analysis.get("selected_coin_price"),
        "confidence":           analysis.get("confidence"),
        "layman_headline":      analysis.get("layman_headline"),
        "layman_explanation":   analysis.get("layman_explanation"),
        "layman_action":        analysis.get("layman_action"),
        "expert_reasoning":     analysis.get("expert_reasoning"),
        "risk_factors":         analysis.get("risk_factors", []),
        "watch_list":           analysis.get("watch_list", []),
        "market_context":       analysis.get("market_context"),
        "context_banner":       analysis.get("context_banner"),
        "btc_price":            snap.btc_price,
        "fear_greed":           snap.fear_greed,
        "btc_dominance":        snap.btc_dominance,
        "key_signals":          analysis.get("key_signals", []),
        "_mode":                analysis.get("_mode", "HUNTING"),
        "exit_target":          analysis.get("exit_target"),
        "stop_loss":            analysis.get("stop_loss"),
    }

    # Files to push: name → content string
    to_push = [("latest_analysis.json", json.dumps(latest, indent=2, default=str))]
    for fname in ("paper_portfolio.json", "signal_credibility.json"):
        if os.path.exists(fname):
            with open(fname) as f:
                to_push.append((fname, f.read()))

    gh_headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    base_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents"

    # Also push position_state.json so mode survives between Railway runs
    for fname in ("position_state.json",):
        if os.path.exists(fname):
            with open(fname) as f:
                to_push.append((fname, f.read()))

    for filename, content in to_push:
        try:
            # Fetch current SHA (required to update existing files)
            r = requests.get(f"{base_url}/{filename}", headers=gh_headers, timeout=10)
            sha = r.json().get("sha") if r.status_code == 200 else None

            payload = {
                "message": f"[bot] {filename} — {snap.timestamp[:16]}",
                "content": base64.b64encode(content.encode()).decode(),
                "branch":  "main",
            }
            if sha:
                payload["sha"] = sha

            pr = requests.put(f"{base_url}/{filename}", headers=gh_headers,
                              json=payload, timeout=20)
            if pr.status_code in (200, 201):
                log.info(f"GitHub ✓ pushed {filename}")
            else:
                log.warning(f"GitHub push {filename}: {pr.status_code} {pr.text[:120]}")
        except Exception as e:
            log.warning(f"GitHub push error ({filename}): {e}")


# ─── ENTRY POINT ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Crypto Strategy Clock v3.0")
    parser.add_argument("--once",     action="store_true", help="Run once and exit")
    parser.add_argument("--expert",   action="store_true", help="Show technical detail")
    parser.add_argument("--interval", type=int, default=60, help="Run interval in minutes")
    parser.add_argument("--set-position", choices=["HUNTING","HOLDING","WAITING"],
                        help="Manually set investment mode")
    parser.add_argument("--price",    type=float, default=None,
                        help="Price for --set-position")
    parser.add_argument("--reset-position", action="store_true",
                        help="Reset to HUNTING mode")
    parser.add_argument("--position-status", action="store_true",
                        help="Show position state and exit")
    parser.add_argument("--paper-status",   action="store_true",
                        help="Show paper portfolio balance and exit")
    parser.add_argument("--reset-paper",    action="store_true",
                        help="Reset paper portfolio back to $1,000")
    args = parser.parse_args()

    # Manual position overrides
    if HAS_POSITION:
        if args.reset_position:
            reset_to_hunting()
            print("✅  Position reset to HUNTING mode.")
            sys.exit(0)
        if args.set_position:
            if args.set_position in ("HOLDING", "WAITING") and not args.price:
                print(f"ERROR: requires --price (e.g. --price 185.50)")
                sys.exit(1)
            force_set_position(args.set_position, price=args.price)
            print(f"✅  Position set to {args.set_position}" +
                  (f" at ${args.price:,.2f}" if args.price else "") + ".")
            sys.exit(0)
        if args.position_status:
            mode = get_mode()
            ts   = get_trade_summary()
            print(f"\n  Mode: {mode}")
            if ts["trade_count"] > 0:
                print(f"  Trades: {ts['trade_count']}  Win rate: {ts['win_rate']}%  Total P&L: {ts['total_profit_pct']:+.1f}%")
            print()
            sys.exit(0)

    # Paper portfolio commands
    if HAS_PAPER:
        if args.reset_paper:
            reset_portfolio()
            print("✅  Paper portfolio reset to $1,000.")
            sys.exit(0)
        if args.paper_status:
            pv = get_portfolio_value()
            print(f"\n  Paper Portfolio")
            print(f"  Total value:  ${pv['total_value']:,.2f}  ({pv['total_return_pct']:+.2f}%)")
            print(f"  Cash:         ${pv['cash']:,.2f}")
            print(f"  Trades:       {pv['total_trades']}  |  Win rate: {pv['win_rate']:.0f}%")
            h = pv.get("holding")
            if h:
                print(f"  Holding:      {h['coin_ticker']}  ×{h['units']:.4f} units @ ${h['entry_price']:,.4f}")
            print()
            sys.exit(0)

    if not ANTHROPIC_API_KEY:
        print("\nERROR: Set ANTHROPIC_API_KEY\n  Get one at: console.anthropic.com\n")
        sys.exit(1)

    print("""
  ╔══════════════════════════════════════════════╗
  ║    🔮  CRYPTO STRATEGY CLOCK  v3.0          ║
  ║    Swing Trading Intelligence Agent          ║
  ╚══════════════════════════════════════════════╝
  """)

    if args.once or not HAS_SCHEDULER:
        if not HAS_SCHEDULER and not args.once:
            print("TIP: pip install apscheduler for scheduled runs\n")
        run_cycle(expert_mode=args.expert)
        return

    log.info(f"Starting — running every {args.interval} minutes")
    run_cycle(expert_mode=args.expert)
    scheduler = BlockingScheduler()
    scheduler.add_job(lambda: run_cycle(expert_mode=args.expert),
                      "interval", minutes=args.interval)
    try:
        scheduler.start()
    except KeyboardInterrupt:
        log.info("Stopped.")
        scheduler.shutdown()


if __name__ == "__main__":
    main()
