#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════╗
║          BTC CRYSTAL BALL  v2.0                      ║
║   Self-Improving Bitcoin Intelligence Agent          ║
║                                                      ║
║  Runs hourly. Produces plain-English buy/sell        ║
║  signals. Gets smarter after every prediction.       ║
╚══════════════════════════════════════════════════════╝

SETUP (5 minutes):
  1. pip install anthropic requests apscheduler feedparser
  2. Get free FRED key: https://fred.stlouisfed.org/docs/api/api_key.html
  3. Set env vars (see README)
  4. python btc_oracle_v2.py --once    ← test run
  5. python btc_oracle_v2.py           ← starts hourly monitoring

REQUIRED:  ANTHROPIC_API_KEY
OPTIONAL:  FRED_API_KEY (macro data), COINGLASS_API_KEY (richer derivatives),
           ALERT_EMAIL / ALERT_EMAIL_PASSWORD (Gmail alerts),
           DISCORD_WEBHOOK_URL (Discord alerts)

LEARNING LOOP:
  Every cycle the agent:
  1. Checks if any past predictions have matured (7d / 30d)
  2. Evaluates them as CORRECT / PARTIAL / INCORRECT
  3. Asks Claude to do a deep post-mortem on each
  4. Rebuilds its lessons database from all post-mortems
  5. Injects those lessons into the NEXT prediction

  Over time the agent builds institutional memory of its
  own hits and misses, steadily improving accuracy.
"""

# ─── IMPORTS ──────────────────────────────────────────────────────────────────
import os, sys, json, time, math, re, smtplib, argparse, logging, textwrap
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional, List
from email.mime.text import MIMEText

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
    log = logging.getLogger("crystal_ball")
    log.warning("crystal_ball_memory.py not found — learning loop disabled")

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
    log = logging.getLogger("crystal_ball")
    log.info("position_manager.py not found — lifecycle tracking disabled")

# ─── CONFIG ───────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY   = os.environ.get("ANTHROPIC_API_KEY", "")
FRED_API_KEY        = os.environ.get("FRED_API_KEY", "")
COINGLASS_API_KEY   = os.environ.get("COINGLASS_API_KEY", "")
ALERT_EMAIL         = os.environ.get("ALERT_EMAIL", "")           # Gmail address to send FROM
ALERT_EMAIL_PASS    = os.environ.get("ALERT_EMAIL_PASSWORD", "")  # Gmail app password
ALERT_EMAIL_TO      = os.environ.get("ALERT_EMAIL_TO", ALERT_EMAIL)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")

MODEL               = "claude-opus-4-6"
LOG_FILE            = "crystal_ball_history.jsonl"
HTML_REPORT         = "btc_dashboard.html"

# October 5th target date
TARGET_DATE         = datetime(2026, 10, 5)
ATH_DATE            = datetime(2025, 10, 6)
ATH_PRICE           = 126200

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("crystal_ball")


# ─── SIGNAL DATA MODEL ────────────────────────────────────────────────────────

@dataclass
class Signal:
    """
    A single market signal with confidence weighting.
    value:        -2.0 (very bearish) to +2.0 (very bullish)
    data_quality: 0.0 (stale estimate) to 1.0 (live, verified data)
    """
    name:         str
    value:        float
    data_quality: float   # affects final confidence, not the score itself
    source:       str
    plain_english: str    # one-line explanation for laymen
    category:     str     # on_chain | macro | sentiment | technical | esoteric

    @property
    def emoji(self) -> str:
        if self.value >= 1.5:  return "🟢"
        if self.value >= 0.5:  return "🟩"
        if self.value >= -0.4: return "🟡"
        if self.value >= -1.4: return "🟧"
        return "🔴"

    @property
    def label(self) -> str:
        if self.value >= 1.5:  return "Strong Buy"
        if self.value >= 0.5:  return "Buy"
        if self.value >= -0.4: return "Neutral"
        if self.value >= -1.4: return "Sell"
        return "Strong Sell"


@dataclass
class MarketSnapshot:
    """Everything collected in one run"""
    timestamp:    str
    price:        dict = field(default_factory=dict)
    fear_greed:   dict = field(default_factory=dict)
    macro:        dict = field(default_factory=dict)
    derivatives:  dict = field(default_factory=dict)
    onchain:      dict = field(default_factory=dict)
    etf_flows:    dict = field(default_factory=dict)
    hash_ribbon:  dict = field(default_factory=dict)
    technicals:   dict = field(default_factory=dict)
    headlines:    list = field(default_factory=list)
    signals:      List[Signal] = field(default_factory=list)


# ─── DATA COLLECTORS ──────────────────────────────────────────────────────────

def collect_price(snap: MarketSnapshot):
    """CoinGecko: price, OHLC history, market cap. Free."""
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/bitcoin",
            params={"localization":"false","tickers":"false","market_data":"true","community_data":"false"},
            timeout=12)
        r.raise_for_status()
        d = r.json()["market_data"]
        snap.price = {
            "usd":            d["current_price"]["usd"],
            "change_24h":     round(d["price_change_percentage_24h"], 2),
            "change_7d":      round(d["price_change_percentage_7d"], 2),
            "change_30d":     round(d["price_change_percentage_30d"], 2),
            "volume_24h":     d["total_volume"]["usd"],
            "market_cap":     d["market_cap"]["usd"],
            "circulating":    d["circulating_supply"],
            "ath":            d["ath"]["usd"],
            "ath_change_pct": round(d["ath_change_percentage"]["usd"], 1),
        }
        time.sleep(1.2)
        r2 = requests.get("https://api.coingecko.com/api/v3/coins/bitcoin/ohlc",
            params={"vs_currency":"usd","days":365}, timeout=12)
        r2.raise_for_status()
        snap.price["ohlc"] = r2.json()
        log.info(f"Price: ${snap.price['usd']:,.0f} ({snap.price['change_24h']:+.1f}% 24h)")
    except Exception as e:
        log.warning(f"CoinGecko failed: {e}")


def collect_fear_greed(snap: MarketSnapshot):
    """Alternative.me Fear & Greed Index. Free, no key."""
    try:
        r = requests.get("https://api.alternative.me/fng/", params={"limit":30,"format":"json"}, timeout=8)
        r.raise_for_status()
        data = r.json()["data"]
        values = [int(d["value"]) for d in data]
        snap.fear_greed = {
            "current":        values[0],
            "label":          data[0]["value_classification"],
            "yesterday":      values[1],
            "week_ago":       values[6] if len(values) > 6 else None,
            "month_ago":      values[-1] if len(values) >= 30 else None,
            "30d_average":    round(sum(values) / len(values), 1),
            "trend":          "improving" if values[0] > values[6] else "worsening" if values[0] < values[6] else "flat"
        }
        log.info(f"Fear & Greed: {snap.fear_greed['current']} ({snap.fear_greed['label']})")
    except Exception as e:
        log.warning(f"Fear & Greed failed: {e}")


def collect_macro(snap: MarketSnapshot):
    """FRED API: M2, Fed rate, real yields, DXY proxy. Free with key."""
    if not FRED_API_KEY:
        snap.macro = {"_note": "Set FRED_API_KEY for macro data (free at fred.stlouisfed.org)"}
        return
    series = {
        "m2":          "WM2NS",
        "fed_rate":    "FEDFUNDS",
        "real_yield":  "DFII10",
        "dxy":         "DTWEXBGS",
    }
    snap.macro = {}
    for key, sid in series.items():
        try:
            r = requests.get("https://api.stlouisfed.org/fred/series/observations",
                params={"series_id":sid,"api_key":FRED_API_KEY,"file_type":"json",
                        "sort_order":"desc","limit":56}, timeout=10)
            r.raise_for_status()
            obs = [o for o in r.json()["observations"] if o["value"] != "."]
            if obs:
                latest = float(obs[0]["value"])
                prev52 = float(obs[min(52,len(obs)-1)]["value"])
                snap.macro[key] = {
                    "value":    latest,
                    "date":     obs[0]["date"],
                    "yoy_pct":  round((latest - prev52) / prev52 * 100, 2) if prev52 else None
                }
            time.sleep(0.4)
        except Exception as e:
            log.warning(f"FRED {sid}: {e}")


def collect_hash_ribbon(snap: MarketSnapshot):
    """
    mempool.space API (FREE, no key) — real hash rate history.
    Calculates actual 30d vs 60d MA crossover (Hash Ribbon indicator).
    87.5% historical accuracy for buy signals.
    """
    try:
        r = requests.get("https://mempool.space/api/v1/mining/hashrate/3m", timeout=12)
        r.raise_for_status()
        data = r.json()
        hashrates = [h["avgHashrate"] for h in data.get("hashrates", [])]
        if len(hashrates) >= 60:
            ma30 = sum(hashrates[-30:]) / 30
            ma60 = sum(hashrates[-60:]) / 60
            prev_ma30 = sum(hashrates[-31:-1]) / 30
            prev_ma60 = sum(hashrates[-61:-1]) / 60
            crossover = None
            if prev_ma30 < prev_ma60 and ma30 > ma60:
                crossover = "bullish_cross"   # recovery signal — buy
            elif prev_ma30 > prev_ma60 and ma30 < ma60:
                crossover = "bearish_cross"   # capitulation start

            snap.hash_ribbon = {
                "ma30":             round(ma30 / 1e18, 2),   # EH/s
                "ma60":             round(ma60 / 1e18, 2),
                "status":           "recovery" if ma30 > ma60 else "capitulation",
                "crossover":        crossover,
                "current_hashrate": round(hashrates[-1] / 1e18, 2),
                "data_source":      "mempool.space (live)",
                "data_quality":     1.0
            }
            log.info(f"Hash Ribbon: {snap.hash_ribbon['status']} | "
                     f"30d={snap.hash_ribbon['ma30']} EH/s, 60d={snap.hash_ribbon['ma60']} EH/s")
    except Exception as e:
        log.warning(f"Hash ribbon (mempool.space): {e}")
        snap.hash_ribbon = {"status": "unknown", "data_quality": 0.0}


def collect_derivatives(snap: MarketSnapshot):
    """Binance public API (free) + CoinGlass for derivatives signals."""
    deriv = {}

    # Binance funding rate (free, no key)
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/premiumIndex",
            params={"symbol":"BTCUSDT"}, timeout=8)
        r.raise_for_status()
        d = r.json()
        deriv["funding_rate_pct"] = round(float(d["lastFundingRate"]) * 100, 5)
        deriv["mark_price"]       = float(d["markPrice"])
        log.info(f"Funding rate: {deriv['funding_rate_pct']:.4f}%")
    except Exception as e:
        log.warning(f"Binance funding rate: {e}")

    # Binance open interest (free)
    try:
        r = requests.get("https://fapi.binance.com/fapi/v1/openInterest",
            params={"symbol":"BTCUSDT"}, timeout=8)
        r.raise_for_status()
        deriv["open_interest_btc"] = round(float(r.json()["openInterest"]), 0)
    except Exception as e:
        log.warning(f"Binance OI: {e}")

    # Binance long/short ratio (free)
    try:
        r = requests.get("https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
            params={"symbol":"BTCUSDT","period":"1h","limit":1}, timeout=8)
        r.raise_for_status()
        d = r.json()
        if d:
            deriv["long_pct"]  = round(float(d[0]["longAccount"]) * 100, 1)
            deriv["short_pct"] = round(float(d[0]["shortAccount"]) * 100, 1)
    except Exception as e:
        log.warning(f"Binance L/S ratio: {e}")

    # CoinGlass ETF flows (free public endpoint)
    try:
        headers = {"coinglassSecret": COINGLASS_API_KEY} if COINGLASS_API_KEY else {}
        r = requests.get("https://open-api.coinglass.com/public/v2/futures/openInterest/chart",
            params={"symbol":"BTC","interval":"0","limit":7}, headers=headers, timeout=8)
        if r.status_code == 200 and r.json().get("success"):
            deriv["oi_7d_history"] = r.json().get("data", [])
    except Exception as e:
        log.warning(f"CoinGlass OI history: {e}")

    # Liquidation data (Binance public)
    try:
        r = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol":"BTCUSDT","period":"1h","limit":24}, timeout=8)
        r.raise_for_status()
        data = r.json()
        if data:
            buy_sells = [float(d["buySellRatio"]) for d in data]
            deriv["taker_buy_sell_ratio_24h_avg"] = round(sum(buy_sells)/len(buy_sells), 3)
            deriv["taker_buy_sell_ratio_current"] = buy_sells[-1]
    except Exception as e:
        log.warning(f"Binance taker ratio: {e}")

    snap.derivatives = deriv


def collect_etf_flows(snap: MarketSnapshot):
    """
    Scrape Bitcoin ETF daily flows from Farside Investors (free, widely trusted by analysts).
    Falls back to CoinGlass ETF endpoint if scraping fails.
    """
    etf = {}

    # Try Farside Investors (https://farside.co.uk/btc/)
    try:
        r = requests.get("https://farside.co.uk/btc/",
            headers={"User-Agent": "Mozilla/5.0 (compatible; BTCOracle/2.0)"},
            timeout=12)
        r.raise_for_status()
        html = r.text

        # Extract the most recent total net flow from the HTML table
        # Farside table has rows with date and flow numbers
        rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
        flows_found = []
        for row in rows[-10:]:  # check last 10 rows
            cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
            if len(cells) >= 2:
                # Last cell is typically total
                total_cell = re.sub(r'<[^>]+>', '', cells[-1]).strip()
                try:
                    val = float(total_cell.replace(',','').replace('(','').replace(')',''))
                    if total_cell.startswith('('):
                        val = -val
                    flows_found.append(val)
                except:
                    pass

        if flows_found:
            etf["latest_net_flow_m"] = flows_found[-1]
            etf["5d_total_flow_m"]   = sum(flows_found[-5:])
            etf["source"]            = "Farside Investors (live)"
            etf["data_quality"]      = 0.9
            log.info(f"ETF flows: latest ${flows_found[-1]:.0f}M, 5d total ${etf['5d_total_flow_m']:.0f}M")
    except Exception as e:
        log.warning(f"Farside scrape failed: {e}")

    # Fallback: CoinGlass
    if not etf.get("latest_net_flow_m"):
        try:
            headers = {"coinglassSecret": COINGLASS_API_KEY} if COINGLASS_API_KEY else {}
            r = requests.get("https://open-api.coinglass.com/public/v2/etf/btc-etf-flow",
                headers=headers, timeout=8)
            if r.status_code == 200:
                d = r.json()
                if d.get("success") and d.get("data"):
                    flows = d["data"]
                    etf["latest_net_flow_m"] = flows[0].get("netInflow", 0) / 1e6 if flows else 0
                    etf["source"]            = "CoinGlass"
                    etf["data_quality"]      = 0.8
        except Exception as e:
            log.warning(f"CoinGlass ETF: {e}")

    if not etf:
        etf = {"_note": "ETF flow data unavailable this cycle", "data_quality": 0.0}

    snap.etf_flows = etf


def collect_onchain_free(snap: MarketSnapshot):
    """
    blockchain.com Charts API (completely free, no key) +
    MVRV approximation from market cap and 200d MA realized cap proxy.
    """
    onchain = {}

    # blockchain.com: hash rate (last 90 days)
    try:
        r = requests.get("https://api.blockchain.info/charts/hash-rate",
            params={"timespan":"90days","format":"json","sampled":"true"}, timeout=10)
        r.raise_for_status()
        vals = [p["y"] for p in r.json().get("values", [])]
        if vals:
            onchain["hashrate_current_ths"]  = round(vals[-1], 0)
            onchain["hashrate_30d_avg_ths"]  = round(sum(vals[-30:])/30, 0)
            onchain["hashrate_90d_change_pct"] = round((vals[-1]-vals[0])/vals[0]*100, 1) if vals[0] else 0
        time.sleep(0.5)
    except Exception as e:
        log.warning(f"blockchain.com hashrate: {e}")

    # blockchain.com: exchange balance (coins on exchanges)
    try:
        r = requests.get("https://api.blockchain.info/charts/exchange-balance",
            params={"timespan":"1year","format":"json","sampled":"true"}, timeout=10)
        r.raise_for_status()
        vals_raw = r.json().get("values", [])
        if vals_raw:
            recent = [p["y"] for p in vals_raw[-30:]]
            onchain["exchange_balance_btc"]    = round(vals_raw[-1]["y"] * 1e8, 0)  # satoshis to BTC
            onchain["exchange_balance_30d_change"] = round((recent[-1] - recent[0]) / recent[0] * 100, 2) if recent[0] else 0
            log.info(f"Exchange balance trend: {onchain.get('exchange_balance_30d_change', '?')}% (30d)")
        time.sleep(0.5)
    except Exception as e:
        log.warning(f"blockchain.com exchange balance: {e}")

    # blockchain.com: transaction count (network usage signal)
    try:
        r = requests.get("https://api.blockchain.info/charts/n-transactions",
            params={"timespan":"30days","format":"json","sampled":"true"}, timeout=10)
        r.raise_for_status()
        vals = [p["y"] for p in r.json().get("values", [])]
        if vals:
            onchain["daily_transactions"]       = round(vals[-1], 0)
            onchain["tx_30d_avg"]               = round(sum(vals)/len(vals), 0)
        time.sleep(0.5)
    except Exception as e:
        log.warning(f"blockchain.com transactions: {e}")

    snap.onchain = onchain


def collect_news(snap: MarketSnapshot):
    """News headlines from CoinDesk, CoinTelegraph, The Block RSS feeds."""
    feeds = [
        ("https://www.coindesk.com/arc/outboundfeeds/rss/", "CoinDesk"),
        ("https://cointelegraph.com/rss", "CoinTelegraph"),
        ("https://www.theblock.co/rss.xml", "The Block"),
    ]
    headlines = []
    for url, source in feeds:
        try:
            if HAS_FEEDPARSER:
                feed = feedparser.parse(url)
                for entry in feed.entries[:4]:
                    headlines.append({"title": entry.get("title",""), "source": source,
                                      "date": entry.get("published","")})
            else:
                r = requests.get(url, timeout=8,
                    headers={"User-Agent":"Mozilla/5.0"})
                titles = re.findall(r'<title><!\[CDATA\[(.*?)\]\]></title>', r.text)
                for t in titles[1:5]:
                    headlines.append({"title": t, "source": source})
        except Exception as e:
            log.warning(f"RSS {source}: {e}")
    snap.headlines = headlines[:15]
    log.info(f"Headlines collected: {len(snap.headlines)}")


# ─── TECHNICAL INDICATORS ─────────────────────────────────────────────────────

def build_technicals(snap: MarketSnapshot):
    """Calculate RSI, MACD, MAs, Pi Cycle, Rainbow Band from OHLC data."""
    ohlc = snap.price.get("ohlc", [])
    closes = [row[4] for row in ohlc]
    if not closes:
        snap.technicals = {}
        return

    def sma(data, n):
        return sum(data[-n:]) / n if len(data) >= n else None

    def ema(data, n):
        if len(data) < n: return []
        result = [sum(data[:n]) / n]
        k = 2 / (n + 1)
        for p in data[n:]:
            result.append(p * k + result[-1] * (1 - k))
        return result

    def rsi(data, n=14):
        if len(data) < n + 1: return None
        deltas = [data[i] - data[i-1] for i in range(1, len(data))]
        gains = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        ag = sum(gains[:n]) / n
        al = sum(losses[:n]) / n
        for i in range(n, len(gains)):
            ag = (ag * (n-1) + gains[i]) / n
            al = (al * (n-1) + losses[i]) / n
        return round(100 - 100/(1 + ag/al), 2) if al else 100.0

    t = {}
    t["rsi_14"]    = rsi(closes, 14)
    t["rsi_7"]     = rsi(closes, 7)
    t["sma_50"]    = round(sma(closes, 50), 2) if sma(closes, 50) else None
    t["sma_200"]   = round(sma(closes, 200), 2) if sma(closes, 200) else None
    t["sma_111"]   = round(sma(closes, 111), 2) if sma(closes, 111) else None
    t["sma_350"]   = round(sma(closes, 350), 2) if sma(closes, 350) else None

    e12 = ema(closes, 12)
    e26 = ema(closes, 26)
    if e12 and e26:
        ml = min(len(e12), len(e26))
        macd_line = [e12[-(ml-i)] - e26[-(ml-i)] for i in range(ml)]
        sig = ema(macd_line, 9)
        if sig:
            t["macd"]      = round(macd_line[-1], 0)
            t["macd_sig"]  = round(sig[-1], 0)
            t["macd_hist"] = round(macd_line[-1] - sig[-1], 0)
            t["macd_cross"] = "bullish" if macd_line[-1] > sig[-1] else "bearish"

    # Pi Cycle Top
    if t.get("sma_111") and t.get("sma_350"):
        target = t["sma_350"] * 2
        gap = round((target - t["sma_111"]) / target * 100, 1)
        t["pi_cycle_gap_pct"] = gap
        t["pi_cycle_status"]  = (
            "⛔ DANGER: Top signal imminent" if gap < 3 else
            "⚠️  Warning: Getting close"    if gap < 10 else
            "✅ Safe: Far from top")

    # Rainbow Band (logarithmic regression approximation)
    price = snap.price.get("usd")
    if price:
        days = (datetime.now() - datetime(2009, 1, 3)).days
        log_fair = 10 ** (5.84 * math.log10(days) - 17.01)
        ratio = price / log_fair
        t["rainbow_ratio"] = round(ratio, 3)
        t["rainbow_band"]  = (
            "🔥 Fire Sale — Extreme undervalue"  if ratio < 0.1 else
            "💚 Buy — Strong value zone"          if ratio < 0.2 else
            "📈 Accumulate"                       if ratio < 0.4 else
            "💡 Still Cheap"                      if ratio < 0.6 else
            "🤝 Hold"                             if ratio < 0.9 else
            "🤔 Is this a bubble?"                if ratio < 1.3 else
            "😰 FOMO Intensifies"                 if ratio < 1.8 else
            "🚨 Maximum Bubble Territory")

    # MVRV approximation: market cap vs 200d MA × circulating supply ("realized cap proxy")
    market_cap = snap.price.get("market_cap")
    circulating = snap.price.get("circulating")
    if market_cap and circulating and t.get("sma_200"):
        realized_cap_proxy = t["sma_200"] * circulating
        mvrv_approx = round(market_cap / realized_cap_proxy, 3)
        t["mvrv_approx"]       = mvrv_approx
        t["mvrv_approx_note"]  = "Approximation (200d MA × supply). For exact MVRV, add Glassnode key."
        t["mvrv_data_quality"] = 0.6   # lower confidence than live Glassnode

    snap.technicals = t
    log.info(f"RSI: {t.get('rsi_14')} | MACD: {t.get('macd_cross')} | Rainbow: {t.get('rainbow_band','?')[:15]}")


# ─── SIGNAL BUILDER ───────────────────────────────────────────────────────────

def build_signals(snap: MarketSnapshot):
    """Convert raw data into scored Signal objects."""
    sigs = []
    price = snap.price.get("usd", 0)
    t = snap.technicals

    # ── SENTIMENT ──
    fg = snap.fear_greed.get("current")
    if fg is not None:
        v = 2.0 if fg<25 else 1.0 if fg<45 else 0 if fg<55 else -1.0 if fg<75 else -2.0
        sigs.append(Signal("Fear & Greed Index", v, 1.0, "alternative.me",
            f"Market mood is '{snap.fear_greed.get('label')}' ({fg}/100). "
            + ("Extreme fear is historically a buying opportunity." if v>=1 else
               "Extreme greed is historically a warning to be cautious." if v<=-1 else
               "Market mood is neutral — no strong signal."),
            "sentiment"))

    # Funding rate
    fr = snap.derivatives.get("funding_rate_pct")
    if fr is not None:
        v = 2.0 if fr<-0.05 else 1.0 if fr<-0.01 else 0 if abs(fr)<0.02 else -1.0 if fr<0.08 else -2.0
        sigs.append(Signal("Futures Funding Rate", v, 1.0, "Binance",
            f"Funding rate is {fr:.4f}%. "
            + ("Negative funding means short-sellers are paying longs — bullish." if v>0 else
               "Very high positive funding: too many people betting on price rising, risky." if v<-1 else
               "Funding rate is balanced — no clear signal."),
            "sentiment"))

    # Long/Short ratio
    lp = snap.derivatives.get("long_pct")
    if lp is not None:
        v = 1.0 if lp<40 else 0 if lp<60 else -1.0
        sigs.append(Signal("Long/Short Ratio", v, 0.9, "Binance",
            f"{lp:.0f}% of futures traders are long (betting price rises). "
            + ("Majority are bearish — contrarian buy signal." if v>0 else
               "Too many people are long — crowded trade, higher crash risk." if v<0 else
               "Balanced positioning between bulls and bears."),
            "sentiment"))

    # ── ON-CHAIN ──
    hr = snap.hash_ribbon
    if hr.get("status"):
        v = 2.0 if hr.get("crossover") == "bullish_cross" else \
            1.0 if hr.get("status") == "recovery" else \
           -1.0 if hr.get("crossover") == "bearish_cross" else -0.5
        q = hr.get("data_quality", 0.5)
        sigs.append(Signal("Hash Ribbon (Miner Strength)", v, q, "mempool.space",
            f"Bitcoin miners are in '{hr['status']}' phase. "
            + ("🟢 Miners are recovering — historically a strong buy signal with 87% accuracy." if v>=1.5 else
               "Miners are operating steadily — neutral to mildly positive." if v>0 else
               "Miners may be under stress — watch this signal."),
            "on_chain"))

    exc_chg = snap.onchain.get("exchange_balance_30d_change")
    if exc_chg is not None:
        v = 2.0 if exc_chg < -3 else 1.0 if exc_chg < 0 else -1.0 if exc_chg > 3 else 0
        sigs.append(Signal("Exchange Reserves Trend", v, 0.85, "blockchain.com",
            f"Bitcoin balances on exchanges changed {exc_chg:+.1f}% over 30 days. "
            + ("Coins leaving exchanges = people moving to safe storage = accumulation. Bullish." if v>0 else
               "Coins flowing to exchanges = people preparing to sell. Bearish." if v<0 else
               "Exchange balances are stable — neutral signal."),
            "on_chain"))

    mvrv = t.get("mvrv_approx")
    if mvrv is not None:
        v = 2.0 if mvrv<0.8 else 1.0 if mvrv<1.0 else 0 if mvrv<1.5 else -1.0 if mvrv<2.5 else -2.0
        q = t.get("mvrv_data_quality", 0.6)
        sigs.append(Signal("MVRV Ratio (Value Indicator)", v, q, "CoinGecko (approx)",
            f"MVRV ~{mvrv:.2f} — this compares Bitcoin's market value to its 'fair value'. "
            + ("Below 1.0 = Bitcoin is statistically undervalued. Strong historical buy zone." if v>=1.5 else
               "Near fair value — mild buying opportunity." if v==1.0 else
               "At or above fair value — hold or wait for dips." if v<=0 else
               "Overvalued territory — caution."),
            "on_chain"))

    # ── MACRO ──
    m2 = snap.macro.get("m2", {})
    m2_yoy = m2.get("yoy_pct")
    if m2_yoy is not None:
        v = 2.0 if m2_yoy>5 else 1.0 if m2_yoy>2 else 0 if m2_yoy>0 else -2.0
        sigs.append(Signal("M2 Money Supply Growth", v, 0.9, "FRED (St. Louis Fed)",
            f"Global money supply is growing at {m2_yoy:+.1f}% year-over-year. "
            "Note: M2 growth leads Bitcoin price by ~110 days. "
            + ("Strong money printing = more dollars chasing Bitcoin. Bullish in ~4 months." if v>=1 else
               "Money supply is contracting — historically bearish for Bitcoin." if v<0 else
               "Modest money growth — mild support."),
            "macro"))

    dxy = snap.macro.get("dxy", {})
    dxy_yoy = dxy.get("yoy_pct")
    if dxy_yoy is not None:
        v = 2.0 if dxy_yoy<-3 else 1.0 if dxy_yoy<0 else -1.0 if dxy_yoy<3 else -2.0
        sigs.append(Signal("US Dollar Strength (DXY)", v, 0.9, "FRED (St. Louis Fed)",
            f"The US Dollar index changed {dxy_yoy:+.1f}% vs last year. "
            "Bitcoin has a -0.58 correlation with the dollar. "
            + ("Weakening dollar is historically very bullish for Bitcoin." if v>=1 else
               "Strengthening dollar puts pressure on Bitcoin prices." if v<0 else
               "Dollar is stable — neutral for Bitcoin."),
            "macro"))

    fed = snap.macro.get("fed_rate", {})
    fed_v = fed.get("value")
    fed_yoy = fed.get("yoy_pct")
    if fed_v is not None:
        v = 1.0 if (fed_yoy or 0) < -0.5 else 0 if abs(fed_yoy or 0) < 0.5 else -1.0
        sigs.append(Signal("Federal Reserve Rate", v, 0.9, "FRED (St. Louis Fed)",
            f"Fed funds rate: {fed_v:.2f}%. "
            + ("Rates are being cut — easier money conditions. Bullish." if v>0 else
               "Rates are rising — tighter money conditions. Bearish." if v<0 else
               "Rates are holding steady — neutral."),
            "macro"))

    # ── TECHNICAL ──
    rsi_v = t.get("rsi_14")
    if rsi_v:
        v = 2.0 if rsi_v<30 else 1.0 if rsi_v<45 else 0 if rsi_v<55 else -1.0 if rsi_v<70 else -2.0
        sigs.append(Signal("RSI Momentum (14-day)", v, 1.0, "CoinGecko (calculated)",
            f"RSI is {rsi_v:.0f}/100. "
            + ("Bitcoin is oversold — historically a buying opportunity." if v>=1.5 else
               "RSI is in buy territory." if v==1.0 else
               "RSI is neutral." if v==0 else
               "Bitcoin is overbought — elevated risk of correction." if v<=-1 else
               "RSI in mild sell territory."),
            "technical"))

    macd_c = t.get("macd_cross")
    if macd_c:
        v = 1.0 if macd_c == "bullish" else -1.0
        sigs.append(Signal("MACD Momentum Signal", v, 0.9, "CoinGecko (calculated)",
            f"MACD is showing a {macd_c} crossover. "
            + ("Price momentum is turning upward — buyers gaining control." if v>0 else
               "Price momentum is turning downward — sellers gaining control."),
            "technical"))

    sma200 = t.get("sma_200")
    if sma200 and price:
        v = 1.0 if price > sma200 else -1.0
        sigs.append(Signal("200-Day Moving Average", v, 1.0, "CoinGecko (calculated)",
            f"Bitcoin is {'above' if v>0 else 'below'} its 200-day average of ${sma200:,.0f}. "
            + ("Trading above the 200 DMA is a long-term bullish sign." if v>0 else
               "Trading below the 200 DMA — long-term caution."),
            "technical"))

    # ── ESOTERIC ──
    pc = t.get("pi_cycle_gap_pct")
    if pc is not None:
        v = 2.0 if pc>30 else 1.0 if pc>15 else 0 if pc>10 else -1.0 if pc>3 else -2.0
        sigs.append(Signal("Pi Cycle Top Indicator", v, 0.95, "Calculated",
            f"The Pi Cycle indicator shows the market is {pc:.0f}% away from a cycle top signal. "
            + ("Very far from a top — no danger from this indicator." if v>=1 else
               "Getting closer to a possible cycle top — monitor weekly." if v==0 else
               "⚠️ DANGER: Pi Cycle top signal imminent. This has called tops within 3 days historically." if v<=-1 else
               "Approaching top territory."),
            "esoteric"))

    rb = t.get("rainbow_band")
    rb_r = t.get("rainbow_ratio")
    if rb and rb_r:
        v = 2.0 if rb_r<0.1 else 1.5 if rb_r<0.2 else 1.0 if rb_r<0.4 else 0.5 if rb_r<0.6 else \
            0 if rb_r<0.9 else -0.5 if rb_r<1.3 else -1.5 if rb_r<1.8 else -2.0
        sigs.append(Signal("Rainbow Chart Band", v, 0.85, "Log regression (calculated)",
            f"Bitcoin is in the '{rb}' zone of the Rainbow Chart. "
            + ("Historically a very strong buying zone." if v>=1.5 else
               "Good buying area according to long-term valuation model." if v>=0.5 else
               "Fair value zone — no particular edge." if v==0 else
               "Entering historically risky territory." if v<0 else
               "Deep overvaluation — extreme caution."),
            "esoteric"))

    # ETF flow signal
    ef = snap.etf_flows.get("latest_net_flow_m")
    if ef is not None:
        v = 2.0 if ef>500 else 1.0 if ef>100 else 0 if ef>-100 else -1.0 if ef>-500 else -2.0
        q = snap.etf_flows.get("data_quality", 0.7)
        sigs.append(Signal("Bitcoin ETF Flows", v, q, snap.etf_flows.get("source","ETF tracker"),
            f"Bitcoin ETFs saw ${ef:+.0f}M net {'inflows' if ef>0 else 'outflows'} most recently. "
            + ("Strong institutional buying — major bullish signal." if v>=1.5 else
               "Positive ETF flows — institutions accumulating." if v>0 else
               "ETF flows are neutral — institutions on sidelines." if v==0 else
               "Institutions are selling through ETFs — bearish pressure." if v<0 else
               "Large ETF outflows — significant institutional selling."),
            "on_chain"))

    snap.signals = sigs
    log.info(f"Built {len(sigs)} signals")


# ─── CLAUDE SYNTHESIS ─────────────────────────────────────────────────────────

def ask_claude(snap: MarketSnapshot) -> dict:
    """Send all data to Claude for expert synthesis into a plain-English signal."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Calculate days to target
    days_to_target = (TARGET_DATE - datetime.now()).days
    days_from_ath   = (datetime.now() - ATH_DATE).days
    pct_from_ath    = round((snap.price.get("usd", 0) - ATH_PRICE) / ATH_PRICE * 100, 1)
    current_price   = snap.price.get("usd", 0)

    # ── Position / lifecycle context ────────────────────────────
    mode    = get_mode()            if HAS_POSITION else "HUNTING"
    pos_ctx = get_position_context(current_price) if HAS_POSITION else {"investment_mode": "HUNTING"}

    # Build signal summary
    sig_summary = []
    for s in snap.signals:
        sig_summary.append({
            "signal":        s.name,
            "score":         s.value,
            "data_quality":  s.data_quality,
            "plain_english": s.plain_english,
            "category":      s.category,
        })

    # Category scores (weighted, quality-adjusted)
    weights = {"on_chain":0.30,"macro":0.25,"sentiment":0.20,"technical":0.15,"esoteric":0.10}
    cat_scores = {}
    for cat in weights:
        cat_sigs = [s for s in snap.signals if s.category == cat]
        if cat_sigs:
            avg = sum(s.value for s in cat_sigs) / len(cat_sigs)
            avg_q = sum(s.data_quality for s in cat_sigs) / len(cat_sigs)
            cat_scores[cat] = {"score": round(avg, 2), "data_quality": round(avg_q, 2), "n_signals": len(cat_sigs)}

    composite = sum(cat_scores.get(c, {}).get("score", 0) * w for c, w in weights.items())
    avg_quality = sum(s.data_quality for s in snap.signals) / len(snap.signals) if snap.signals else 0.5

    context_payload = {
        "timestamp":           snap.timestamp,
        "btc_price":           current_price,
        "price_changes":       {"24h": snap.price.get("change_24h"), "7d": snap.price.get("change_7d"), "30d": snap.price.get("change_30d")},
        "days_from_ath":       days_from_ath,
        "pct_from_ath":        pct_from_ath,
        "days_to_oct5_target": days_to_target,
        "signal_scores":       sig_summary,
        "category_scores":     cat_scores,
        "composite_score":     round(composite, 2),
        "avg_data_quality":    round(avg_quality, 2),
        "headlines":           [h["title"] for h in snap.headlines[:8]],
        "fear_greed":          snap.fear_greed,
        "macro_summary":       {k: v.get("value") for k,v in snap.macro.items() if isinstance(v, dict)},
        "hash_ribbon":         snap.hash_ribbon.get("status"),
        "etf_net_flow_m":      snap.etf_flows.get("latest_net_flow_m"),
        "rainbow_band":        snap.technicals.get("rainbow_band"),
        "pi_cycle_gap":        snap.technicals.get("pi_cycle_gap_pct"),
        "investment_position": pos_ctx,   # ← lifecycle context injected here
    }

    # Load accumulated intelligence from past predictions
    intelligence_context = load_intelligence_context() if HAS_MEMORY else ""

    # Build mode-specific instructions for Claude
    mode_prompt = build_mode_prompt(mode, pos_ctx) if HAS_POSITION else ""

    # Build mode-specific JSON schema
    if mode == "HOLDING":
        signal_schema = '"signal": "HOLD | SELL | STRONG_SELL",'
        extra_fields  = """
  "exit_urgency": 1-10,
  "exit_recommendation": "hold_full | consider_partial | full_exit",
  "exit_thesis": "2-3 sentences: why hold or why sell right now based on cycle position","""
    elif mode == "WAITING":
        signal_schema = '"signal": "RE_ENTER | WAIT_LONGER",'
        extra_fields  = """
  "reentry_readiness": 0-100,
  "optimal_reentry_price": estimated ideal re-entry price as float or null,
  "reentry_thesis": "2-3 sentences: why re-enter or keep waiting","""
    else:
        signal_schema = '"signal": "STRONG_BUY | BUY | HOLD | SELL | STRONG_SELL",'
        extra_fields  = """
  "oct5_thesis_status": "ON_TRACK | STRENGTHENING | WEAKENING | INVALIDATED",
  "oct5_thesis_note": "1 sentence on whether the Oct 5 entry target remains valid","""

    system = f"""You are the world's best Bitcoin market analyst and investment strategist.
You combine on-chain expertise, macroeconomic insight, and cycle theory to make precise,
actionable decisions that maximize returns and minimize drawdown.

{intelligence_context if intelligence_context else ""}

IMPORTANT: If accumulated intelligence is provided above, apply those validated rules.
Call out in expert_reasoning if any past lessons are influencing this call.

{mode_prompt}

You receive a full market data snapshot including the investor's CURRENT POSITION STATE.
Your job is to make the right call for this specific investor at this specific moment.

Data quality scores matter: lower quality = lower final confidence.

OUTPUT (valid JSON only, no markdown):
{{
  {signal_schema}
  "confidence": 0-100,
  "composite_score": float,
  "layman_headline": "One punchy sentence a 10-year-old would understand",
  "layman_explanation": "3-4 sentences in plain English. No jargon. Speak directly to their situation.",
  "layman_action": "Exactly what a cautious long-term investor should do right now",
  "expert_reasoning": "3-5 sentences with technical justification for the signal",{extra_fields}
  "category_breakdown": {{
    "on_chain":  {{"score": float, "label": str, "key_finding": str}},
    "macro":     {{"score": float, "label": str, "key_finding": str}},
    "sentiment": {{"score": float, "label": str, "key_finding": str}},
    "technical": {{"score": float, "label": str, "key_finding": str}},
    "esoteric":  {{"score": float, "label": str, "key_finding": str}}
  }},
  "alert": null or "alert message if something critical is happening",
  "alert_type": null or "bullish | bearish | danger",
  "watch_list": ["3 specific things to monitor in the next 24-48 hours"],
  "cycle_position": "accumulation | early_bull | mid_bull | late_bull | distribution | bear",
  "cycle_position_plain": "Plain English cycle description",
  "context_banner": "One authoritative sentence that orients the user in the current cycle with specific numbers."
}}"""

    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=2200, system=system,
            messages=[{"role":"user","content": f"Analyze this Bitcoin market data and position state:\n\n{json.dumps(context_payload, indent=2, default=str)}"}])
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw.strip())
        result["_mode"] = mode   # stash mode so display functions can read it
        return result
    except json.JSONDecodeError:
        log.error("Claude returned non-JSON")
        return {"signal":"HOLD","_mode":mode,"confidence":0,
                "layman_headline":"Data error — try again shortly",
                "layman_explanation":"The analysis system encountered an error this cycle.",
                "layman_action":"No action recommended until next cycle.",
                "expert_reasoning":"JSON parse error from Claude response.","alert":None}
    except Exception as e:
        log.error(f"Claude API failed: {e}")
        return {"signal":"HOLD","_mode":mode,"confidence":0,
                "layman_headline":"Service temporarily unavailable",
                "layman_explanation":"Could not reach the analysis service this cycle.",
                "layman_action":"Wait for the next update.",
                "expert_reasoning":str(e),"alert":None}


# ─── DISPLAY ──────────────────────────────────────────────────────────────────

# ── Signal display mappings per investment mode ────────────────────────────
# HUNTING mode (no position): BUY / HOLD / WAIT
SIGNAL_DISPLAY = {
    "STRONG_BUY":  "BUY",
    "BUY":         "BUY",
    "HOLD":        "HOLD",
    "SELL":        "WAIT",
    "STRONG_SELL": "WAIT",
}

# HOLDING mode (position open): HOLD / SELL / SELL NOW
SIGNAL_DISPLAY_HOLDING = {
    "HOLD":        "HOLD",
    "SELL":        "SELL",
    "STRONG_SELL": "SELL NOW",
}

# WAITING mode (sold, hunting re-entry): BUY BACK / NOT YET
SIGNAL_DISPLAY_WAITING = {
    "RE_ENTER":    "BUY BACK",
    "WAIT_LONGER": "NOT YET",
}

SIGNAL_STYLES = {
    "BUY":      ("\033[92m", "B U Y",            "#00c853"),
    "HOLD":     ("\033[93m", "H O L D",           "#ff9800"),
    "WAIT":     ("\033[91m", "W A I T",           "#f44336"),
    "SELL":     ("\033[91m", "S E L L",           "#ef5350"),
    "SELL NOW": ("\033[91m", "S E L L  N O W",    "#ff1744"),
    "BUY BACK": ("\033[92m", "B U Y  B A C K",   "#00e676"),
    "NOT YET":  ("\033[93m", "N O T  Y E T",      "#ff9800"),
}

def resolve_display_signal(internal_sig: str, mode: str) -> str:
    """Map Claude's internal signal to the user-facing display label."""
    if mode == "HOLDING":
        return SIGNAL_DISPLAY_HOLDING.get(internal_sig, "HOLD")
    elif mode == "WAITING":
        return SIGNAL_DISPLAY_WAITING.get(internal_sig, "NOT YET")
    else:  # HUNTING
        return SIGNAL_DISPLAY.get(internal_sig, "HOLD")

RESET = "\033[0m"
BOLD  = "\033[1m"


def display_layman(analysis: dict, snap: MarketSnapshot, perf: dict = None):
    """
    Investment Strategy Clock terminal display.
    Signal is the hero. Everything else supports it.
    """
    internal_sig = analysis.get("signal", "HOLD")
    mode         = analysis.get("_mode", "HUNTING")
    display_sig  = resolve_display_signal(internal_sig, mode)
    conf         = analysis.get("confidence", 0)
    price        = snap.price.get("usd", 0)
    chg          = snap.price.get("change_24h", 0)
    color, label, _ = SIGNAL_STYLES.get(display_sig, SIGNAL_STYLES["HOLD"])

    W      = 58
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
    print(row(f"  🔮  INVESTMENT STRATEGY CLOCK"))
    print(row(f"      Bitcoin  ·  {datetime.now().strftime('%B %d, %Y  %I:%M %p')}"))
    print(mid)

    # ── HERO SIGNAL ───────────────────────────────────────────
    chg_sym = "▲" if chg >= 0 else "▼"
    chg_col = "\033[32m" if chg >= 0 else "\033[31m"
    print(blank)
    # Center the signal label
    sig_str = f"{color}{BOLD}  {label}  {RESET}"
    pad = (W - len(label) - 4) // 2
    print("║" + " "*pad + sig_str + " "*(W - pad - len(label) - 4) + "║")
    conf_str = f"Confidence: {conf}%"
    cpad = (W - len(conf_str)) // 2
    print("║" + " "*cpad + conf_str + " "*(W - cpad - len(conf_str)) + "║")
    price_str = f"Bitcoin  ${price:,.0f}  {chg_col}{chg_sym} {abs(chg):.1f}% today{RESET}"
    ppad = (W - len(f"Bitcoin  ${price:,.0f}  {chg_sym} {abs(chg):.1f}% today")) // 2
    print("║" + " "*ppad + price_str + " "*(W - ppad - len(f"Bitcoin  ${price:,.0f}  {chg_sym} {abs(chg):.1f}% today")) + "║")
    print(blank)

    # ── CONTEXT BANNER ────────────────────────────────────────
    banner = analysis.get("context_banner", analysis.get("cycle_position_plain", ""))
    if banner:
        print(mid)
        print(blank)
        for line in textwrap.wrap(f'"{banner}"', W-4):
            lpad = (W - len(line)) // 2
            print("║" + " "*lpad + line + " "*(W - lpad - len(line)) + "║")
        print(blank)

    # ── POSITION STATUS BLOCK ─────────────────────────────────
    if HAS_POSITION:
        pos = get_display_state(price)
        if mode == "HOLDING" and pos.get("entry_price"):
            entry = pos["entry_price"]
            upct  = pos.get("unrealized_pct", 0)
            dheld = pos.get("days_held", 0)
            gain_col = "\033[92m" if upct >= 0 else "\033[91m"
            gain_sym = "▲" if upct >= 0 else "▼"
            print(mid)
            print(row(f"  📈  Your Open Position"))
            print(row(f"  Entered at ${entry:,.0f}  ·  {dheld} days ago"))
            print(row(f"  Unrealized: {gain_col}{gain_sym} {abs(upct):.1f}%{RESET}  (current ${price:,.0f})"))
            # Show exit thesis if available
            et = analysis.get("exit_thesis","") or analysis.get("expert_reasoning","")[:120]
            if et:
                for line in textwrap.wrap(f"  {et}", W-4):
                    print(row(f"  {line}"))
            print(blank)

        elif mode == "WAITING" and pos.get("sold_price"):
            sold   = pos["sold_price"]
            chgs   = pos.get("price_change_since_sale", 0)
            profit = pos.get("last_profit_pct", 0)
            days_s = pos.get("days_since_sale", 0)
            profit_col = "\033[92m" if profit >= 0 else "\033[91m"
            chg_col2   = "\033[92m" if chgs <= 0 else "\033[91m"  # price falling = re-entry getting cheaper
            print(mid)
            print(row(f"  💰  Last Trade  —  Sold at ${sold:,.0f}"))
            print(row(f"  Profit: {profit_col}{profit:+.1f}%{RESET}  ·  {days_s} days ago"))
            print(row(f"  Bitcoin since your sale: {chg_col2}{chgs:+.1f}%{RESET}  {'(cheaper now ✅)' if chgs <= 0 else '(higher now)'}"))
            rt = analysis.get("reentry_thesis","")
            if rt:
                for line in textwrap.wrap(f"  {rt}", W-4):
                    print(row(f"  {line}"))
            print(blank)

    # ── EXPLANATION + ACTION ──────────────────────────────────
    print(mid)
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
        acc      = perf.get("accuracy_7d") or 0
        acc_rec  = perf.get("accuracy_recent") or acc
        n_eval   = perf.get("evaluated", 0)
        acc_col  = "\033[92m" if acc >= 70 else "\033[93m" if acc >= 50 else "\033[91m"
        trend_arrow = "↑ getting sharper" if acc_rec > acc + 2 else \
                      "↓ needs improvement" if acc_rec < acc - 2 else "→ holding steady"

        print(mid)
        print(row(f"  📊  Crystal Ball Accuracy"))
        print(blank)
        print(row(f"  {acc_col}{acc_bar(acc)}  {acc:.0f}%{RESET}"))
        print(row(f"  Based on {n_eval} verified predictions  ·  {trend_arrow}"))

        streak = perf.get("streak", 0)
        stype  = perf.get("streak_type", "")
        if streak >= 3:
            em = "🔥" if stype == "correct" else "❄️"
            print(row(f"  {em} {streak} {'correct' if stype=='correct' else 'incorrect'} calls in a row"))
        print(blank)

    # ── THREE-TIER LEARNING LOG ───────────────────────────────
    if perf and perf.get("evaluated", 0) >= 3:
        print(mid)
        print(row(f"  🧠  What the Crystal Ball has learned:"))
        print(blank)

        # Tier 1: Most recent lesson
        recent_lesson = perf.get("most_recent_lesson", "")
        if recent_lesson:
            print(row(f"  Most recent lesson:"))
            for line in textwrap.wrap(f'"{recent_lesson}"', W-6):
                print(row(f"     {line}"))
            print(blank)

        # Tier 2: Top 3 patterns (collective synthesis)
        top3 = perf.get("top_3_patterns", [])
        if top3:
            print(row(f"  Top patterns across all history:"))
            for i, p in enumerate(top3[:3], 1):
                for line in textwrap.wrap(f"  {i}.  {p}", W-4):
                    print(row(f"  {line}"))
            print(blank)

        # Tier 3: Honest red flag
        red_flag = perf.get("red_flag", "")
        if red_flag:
            print(row(f"  ⚠  Honest red flag:"))
            for line in textwrap.wrap(f'"{red_flag}"', W-6):
                print(row(f"     {line}"))
            print(blank)

    # ── WATCH LIST ────────────────────────────────────────────
    watch = analysis.get("watch_list", [])
    if watch:
        print(mid)
        print(row(f"  👁   Watch in the next 48 hours:"))
        for item in watch[:3]:
            for line in textwrap.wrap(f"  •  {item}", W-2):
                print(row(line))
        print(blank)

    print(bottom)
    print()


def display_expert(analysis: dict, snap: MarketSnapshot):
    """Technical deep-dive for experienced investors."""
    print("\n" + "─"*60)
    print(f"  EXPERT VIEW  │  {snap.timestamp[:16]} UTC")
    print("─"*60)
    print(f"  Score: {analysis.get('composite_score',0):+.2f}  │  Signal: {analysis.get('signal')}  │  Confidence: {analysis.get('confidence')}%")
    print()
    print("  SIGNAL DETAIL:")
    for s in snap.signals:
        q_bar = "●" * int(s.data_quality * 5) + "○" * (5 - int(s.data_quality * 5))
        print(f"  {s.emoji} {s.name:<35} {s.value:+.1f}  Q:[{q_bar}]")
    er = analysis.get("expert_reasoning","")
    if er:
        print()
        print("  EXPERT ANALYSIS:")
        for line in textwrap.wrap(er, 56):
            print(f"  {line}")
    print("─"*60 + "\n")


# ─── HTML REPORT ──────────────────────────────────────────────────────────────

def _build_performance_html(perf: dict) -> str:
    """
    Builds the accuracy gauge + three-tier learning log section.
    Spec: how often has the crystal ball been right (simple gauge),
    then most recent lesson / top 3 patterns / honest red flag.
    """
    if not perf:
        return ""

    acc      = perf.get("accuracy_7d") or 0
    acc_rec  = perf.get("accuracy_recent") or acc
    n_eval   = perf.get("evaluated", 0)
    streak   = perf.get("streak", 0)
    stype    = perf.get("streak_type", "")
    summary  = perf.get("intelligence_summary", "")

    gauge_color  = "#00c853" if acc >= 70 else "#ff9800" if acc >= 50 else "#f44336"
    trend_label  = "Getting sharper ↑" if acc_rec > acc + 2 else \
                   "Needs improvement ↓" if acc_rec < acc - 2 else "Holding steady →"
    trend_color  = "#4caf50" if "sharper" in trend_label else \
                   "#f44336" if "Needs" in trend_label else "#888"

    streak_html = ""
    if streak >= 3:
        sc = "#4caf50" if stype == "correct" else "#f44336"
        em = "🔥" if stype == "correct" else "❄️"
        streak_html = f'<div class="streak-badge" style="background:{sc}18;color:{sc};border:1px solid {sc}40">{em} {streak} {"correct" if stype=="correct" else "incorrect"} in a row</div>'

    # Tier 1 — most recent individual lesson
    recent = perf.get("most_recent_lesson", "")
    tier1_html = f"""
    <div class="learning-tier tier1">
      <div class="tier-eyebrow">Most recent lesson</div>
      <div class="tier-text">{"<em>Still learning — check back after more predictions resolve.</em>" if not recent else f'"{recent}"'}</div>
    </div>""" if True else ""

    # Tier 2 — top 3 collective patterns
    top3 = perf.get("top_3_patterns", [])
    patterns_html = ""
    if top3:
        for i, p in enumerate(top3[:3], 1):
            patterns_html += f'<div class="pattern-row"><span class="pattern-num">{i}</span><span class="pattern-text">{p}</span></div>'
    else:
        patterns_html = '<div class="pattern-row"><span class="pattern-text"><em>Patterns emerge after more predictions are evaluated.</em></span></div>'

    tier2_html = f"""
    <div class="learning-tier tier2">
      <div class="tier-eyebrow">Top patterns across all history</div>
      {patterns_html}
    </div>"""

    # Tier 3 — red flag
    red_flag = perf.get("red_flag", "")
    tier3_html = f"""
    <div class="learning-tier tier3">
      <div class="tier-eyebrow">⚠ Honest red flag</div>
      <div class="tier-text">{"<em>No red flags identified yet — more data needed.</em>" if not red_flag else f'"{red_flag}"'}</div>
    </div>"""

    return f"""
  <div class="card accuracy-card">
    <div class="acc-header">
      <div>
        <h3>Crystal Ball Accuracy</h3>
        <div class="acc-sub">Based on {n_eval} verified predictions</div>
      </div>
      <div class="acc-pct" style="color:{gauge_color}">{acc:.0f}%</div>
    </div>
    <div class="gauge-track">
      <div class="gauge-fill" style="width:{min(acc,100):.0f}%;background:{gauge_color}"></div>
    </div>
    <div class="acc-trend" style="color:{trend_color}">{trend_label}</div>
    {streak_html}
    {f'<p class="intel-summary">{summary}</p>' if summary else ""}
  </div>

  <div class="card learning-card">
    <h3>What the Crystal Ball has learned</h3>
    {tier1_html}
    {tier2_html}
    {tier3_html}
  </div>

<style>
.accuracy-card h3 {{margin-bottom:4px}}
.acc-header {{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}}
.acc-pct {{font-size:3em;font-weight:800;line-height:1}}
.acc-sub {{color:#555;font-size:.85em;margin-top:2px}}
.gauge-track {{height:10px;background:#1e1e2e;border-radius:5px;overflow:hidden;margin-bottom:8px}}
.gauge-fill {{height:100%;border-radius:5px;transition:width 1s ease}}
.acc-trend {{font-size:.9em;font-weight:500;margin-bottom:8px}}
.streak-badge {{display:inline-block;padding:5px 14px;border-radius:20px;font-size:.85em;font-weight:600;margin:8px 0}}
.intel-summary {{color:#aaa;line-height:1.7;font-style:italic;margin-top:14px;padding:14px;background:#0d1117;border-radius:8px;border-left:3px solid #333;font-size:.92em}}
.learning-card h3 {{margin-bottom:20px}}
.learning-tier {{margin-bottom:22px;padding-bottom:22px;border-bottom:1px solid #1e1e2e}}
.learning-tier:last-child {{margin-bottom:0;padding-bottom:0;border-bottom:none}}
.tier-eyebrow {{font-size:.75em;text-transform:uppercase;letter-spacing:.1em;color:#555;margin-bottom:8px;font-weight:600}}
.tier1 .tier-eyebrow {{color:#4caf50}}
.tier2 .tier-eyebrow {{color:#64b5f6}}
.tier3 .tier-eyebrow {{color:#ef9a9a}}
.tier-text {{color:#ccc;line-height:1.75;font-size:.95em}}
.pattern-row {{display:flex;align-items:flex-start;gap:12px;margin-bottom:10px}}
.pattern-num {{font-size:1.3em;font-weight:800;color:#64b5f6;line-height:1.3;flex-shrink:0;width:20px}}
.pattern-text {{color:#ccc;line-height:1.75;font-size:.95em}}
</style>"""


def _build_position_html(analysis: dict, current_price: float) -> str:
    """Position status card for the HTML dashboard."""
    if not HAS_POSITION:
        return ""
    mode = analysis.get("_mode", "HUNTING")
    pos  = get_display_state(current_price)

    if mode == "HOLDING" and pos.get("entry_price"):
        entry  = pos["entry_price"]
        upct   = pos.get("unrealized_pct", 0)
        dheld  = pos.get("days_held", 0)
        g_col  = "#4caf50" if upct >= 0 else "#f44336"
        g_sym  = "▲" if upct >= 0 else "▼"
        et     = analysis.get("exit_thesis","")
        eu     = analysis.get("exit_urgency", 0)
        urgency_bar = ""
        if eu:
            u_col = "#4caf50" if eu <= 3 else "#ff9800" if eu <= 6 else "#f44336"
            urgency_bar = f'<div class="pos-urgency-row"><span>Exit urgency</span><div class="urgency-track"><div class="urgency-fill" style="width:{eu*10}%;background:{u_col}"></div></div><span style="color:{u_col};font-weight:700">{eu}/10</span></div>'
        return f"""
  <div class="card position-card holding-card">
    <div class="pos-eyebrow">📈 Open Position</div>
    <div class="pos-header">
      <div>
        <div class="pos-label">Entered at ${entry:,.0f}</div>
        <div class="pos-sub">{dheld} days ago</div>
      </div>
      <div class="pos-gain" style="color:{g_col}">{g_sym} {abs(upct):.1f}%</div>
    </div>
    {urgency_bar}
    {f'<div class="pos-thesis">{et}</div>' if et else ""}
  </div>"""

    elif mode == "WAITING" and pos.get("sold_price"):
        sold   = pos["sold_price"]
        chgs   = pos.get("price_change_since_sale", 0)
        profit = pos.get("last_profit_pct", 0)
        days_s = pos.get("days_since_sale", 0)
        tc     = pos.get("completed_trades", 0)
        p_col  = "#4caf50" if profit >= 0 else "#f44336"
        c_col  = "#4caf50" if chgs <= 0 else "#ff9800"
        c_msg  = "cheaper now ✓" if chgs <= 0 else "higher than your sale"
        rt     = analysis.get("reentry_thesis","")
        rr     = analysis.get("reentry_readiness", 0)
        rr_col = "#4caf50" if rr >= 70 else "#ff9800" if rr >= 40 else "#f44336"
        readiness_html = ""
        if rr:
            readiness_html = f"""<div class="pos-readiness">
              <span>Re-entry readiness</span>
              <div class="urgency-track"><div class="urgency-fill" style="width:{rr}%;background:{rr_col}"></div></div>
              <span style="color:{rr_col};font-weight:700">{rr}%</span>
            </div>"""
        return f"""
  <div class="card position-card waiting-card">
    <div class="pos-eyebrow">💰 Last Trade — Watching for Re-entry</div>
    <div class="pos-header">
      <div>
        <div class="pos-label">Sold at ${sold:,.0f}</div>
        <div class="pos-sub">{days_s} days ago · {tc} completed {'trade' if tc == 1 else 'trades'}</div>
      </div>
      <div class="pos-gain" style="color:{p_col}">{profit:+.1f}% profit</div>
    </div>
    <div class="pos-since-sale">Bitcoin is <strong style="color:{c_col}">{abs(chgs):.1f}% {c_msg}</strong> since your sale</div>
    {readiness_html}
    {f'<div class="pos-thesis">{rt}</div>' if rt else ""}
  </div>"""

    elif mode == "HUNTING":
        return """
  <div class="card position-card hunting-card">
    <div class="pos-eyebrow">🎯 Hunting Mode — No Position Held</div>
    <div class="pos-hunting-note">Watching for the optimal entry. The agent will automatically switch to HOLDING mode when it issues a BUY signal.</div>
  </div>"""

    return ""


def generate_html_report(analysis: dict, snap: MarketSnapshot, perf: dict = None):
    """
    Investment Strategy Clock HTML dashboard.
    Signal is the hero. Context banner beneath. Accuracy gauge. Three-tier learning log.
    """
    internal_sig = analysis.get("signal", "HOLD")
    mode         = analysis.get("_mode", "HUNTING")
    display_sig  = resolve_display_signal(internal_sig, mode)
    conf         = analysis.get("confidence", 0)
    price        = snap.price.get("usd", 0)
    chg          = snap.price.get("change_24h", 0)
    days_to      = max(0, (TARGET_DATE - datetime.now()).days)

    sig_hex  = {"BUY": "#00c853", "HOLD": "#ff9800", "WAIT": "#f44336",
                "SELL": "#ef5350", "SELL NOW": "#ff1744", "BUY BACK": "#00e676", "NOT YET": "#ff9800"}
    color    = sig_hex.get(display_sig, "#9e9e9e")
    chg_col  = "#4caf50" if chg >= 0 else "#f44336"
    chg_sym  = "▲" if chg >= 0 else "▼"

    banner   = analysis.get("context_banner", analysis.get("cycle_position_plain", ""))
    alert    = analysis.get("alert", "")
    at       = analysis.get("alert_type", "")

    alert_html = ""
    if alert:
        ac = "#f44336" if at=="danger" else "#00c853" if at=="bullish" else "#ff9800"
        em = "🚨" if at=="danger" else "📣" if at=="bullish" else "⚠️"
        alert_html = f'<div class="alert-strip" style="border-left:4px solid {ac};background:{ac}14">{em}&nbsp; {alert}</div>'

    oct5_html = ""
    if days_to > 0:
        oc_st = analysis.get("oct5_thesis_status", "")
        oc_colors = {"ON_TRACK":"#4caf50","STRENGTHENING":"#00c853","WEAKENING":"#ff9800","INVALIDATED":"#f44336"}
        oc = oc_colors.get(oc_st, "#888")
        oc_note = analysis.get("oct5_thesis_note", "")
        oct5_html = f"""
  <div class="card oct5-card">
    <div class="oct5-inner">
      <div>
        <div class="oct5-label">October 5 Entry Target</div>
        <div class="oct5-status" style="color:{oc}">{oc_st.replace("_"," ") if oc_st else "Monitoring"}</div>
        {f'<div class="oct5-note">{oc_note}</div>' if oc_note else ""}
      </div>
      <div class="oct5-days-box">
        <div class="oct5-num">{days_to}</div>
        <div class="oct5-days-lbl">days away</div>
      </div>
    </div>
  </div>"""

    watch_items = "".join(f'<li>{w}</li>' for w in analysis.get("watch_list",[])[:3])
    headlines   = "".join(f'<li>{h["title"]} <span class="hl-src">— {h.get("source","")}</span></li>'
                          for h in snap.headlines[:6])

    perf_html     = _build_performance_html(perf) if perf and perf.get("evaluated",0) >= 3 else ""
    position_html = _build_position_html(analysis, price)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Investment Strategy Clock — {datetime.now().strftime('%b %d, %Y')}</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,sans-serif;background:#07070f;color:#e0e0e0;padding:20px 16px;min-height:100vh}}
.wrap{{max-width:640px;margin:0 auto}}

/* Header */
.header{{text-align:center;padding:24px 0 8px}}
.header-title{{font-size:.8em;text-transform:uppercase;letter-spacing:.18em;color:#444;font-weight:600}}
.header-ts{{font-size:.78em;color:#333;margin-top:6px}}

/* Alert */
.alert-strip{{padding:14px 18px;border-radius:10px;margin:16px 0;line-height:1.6;font-size:.92em;color:#ddd}}

/* Hero signal card */
.hero-card{{background:linear-gradient(160deg,#0f0f1a,#141428);border:1px solid #1e1e3a;border-radius:16px;padding:40px 24px 32px;text-align:center;margin:16px 0;position:relative;overflow:hidden}}
.hero-card::before{{content:'';position:absolute;top:-60px;left:50%;transform:translateX(-50%);width:300px;height:300px;background:radial-gradient({color}18,transparent 70%);pointer-events:none}}
.hero-price{{font-size:.85em;color:#555;margin-bottom:24px;letter-spacing:.05em}}
.hero-price span{{color:{chg_col}}}
.hero-signal{{font-size:4.5em;font-weight:900;letter-spacing:.15em;color:{color};line-height:1;margin-bottom:12px;text-shadow:0 0 40px {color}44}}
.hero-conf{{font-size:.95em;color:#666;margin-bottom:20px}}
.conf-track{{width:160px;height:4px;background:#1a1a2a;border-radius:2px;margin:0 auto 24px}}
.conf-fill{{height:100%;border-radius:2px;background:{color};width:{conf}%}}
.hero-banner{{font-size:.95em;color:#999;line-height:1.7;font-style:italic;max-width:480px;margin:0 auto 24px;padding:0 12px}}
.hero-explanation{{font-size:1em;color:#ccc;line-height:1.75;margin-bottom:20px;text-align:left;background:#0a0a14;border-radius:10px;padding:16px 18px}}
.hero-action{{background:#0f1f0f;border-left:3px solid #00c853;border-radius:0 8px 8px 0;padding:14px 16px;text-align:left;font-size:.92em;color:#ccc;line-height:1.7}}

/* Cards */
.card{{background:#0f0f1a;border:1px solid #1a1a2a;border-radius:12px;padding:20px;margin-bottom:14px}}
.card-label{{font-size:.72em;text-transform:uppercase;letter-spacing:.12em;color:#444;font-weight:600;margin-bottom:14px}}

/* Oct 5 */
.oct5-card{{background:#0f0f1a}}
.oct5-inner{{display:flex;justify-content:space-between;align-items:center}}
.oct5-label{{font-size:.72em;text-transform:uppercase;letter-spacing:.12em;color:#444;font-weight:600;margin-bottom:6px}}
.oct5-status{{font-size:1.15em;font-weight:700}}
.oct5-note{{font-size:.85em;color:#666;margin-top:6px;line-height:1.5;max-width:360px}}
.oct5-days-box{{text-align:center;flex-shrink:0;padding-left:20px}}
.oct5-num{{font-size:3em;font-weight:900;color:#fff;line-height:1}}
.oct5-days-lbl{{font-size:.75em;color:#444;margin-top:2px}}

/* Watch + headlines */
.card ul{{padding-left:18px;color:#999;line-height:2.2;font-size:.92em}}
.hl-src{{color:#333;font-size:.88em}}

/* Position cards */
.position-card{{margin-bottom:14px}}
.pos-eyebrow{{font-size:.72em;text-transform:uppercase;letter-spacing:.12em;color:#444;font-weight:600;margin-bottom:12px}}
.holding-card .pos-eyebrow{{color:#4caf50}}
.waiting-card .pos-eyebrow{{color:#ff9800}}
.hunting-card .pos-eyebrow{{color:#64b5f6}}
.pos-header{{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px}}
.pos-label{{font-size:1.1em;font-weight:700;color:#fff}}
.pos-sub{{font-size:.8em;color:#555;margin-top:3px}}
.pos-gain{{font-size:1.8em;font-weight:900;line-height:1}}
.pos-thesis{{color:#999;font-size:.88em;line-height:1.7;margin-top:12px;padding:10px 14px;background:#0a0a14;border-radius:8px;border-left:3px solid #1a1a2a}}
.pos-since-sale{{font-size:.9em;color:#888;margin-bottom:10px}}
.pos-urgency-row,.pos-readiness{{display:flex;align-items:center;gap:10px;margin:10px 0;font-size:.82em;color:#666}}
.urgency-track{{flex:1;height:6px;background:#1e1e2e;border-radius:3px;overflow:hidden}}
.urgency-fill{{height:100%;border-radius:3px;transition:width 1s ease}}
.pos-hunting-note{{color:#666;font-size:.9em;line-height:1.6}}

/* Footer */
.footer{{text-align:center;color:#252535;font-size:.75em;margin:24px 0 8px}}
</style>
</head>
<body>
<div class="wrap">

  <div class="header">
    <div class="header-title">🔮 Investment Strategy Clock</div>
    <div class="header-ts">{datetime.now().strftime('%B %d, %Y  ·  %I:%M %p')}</div>
  </div>

  {alert_html}

  <div class="hero-card">
    <div class="hero-price">Bitcoin &nbsp;${price:,.0f} &nbsp;<span>{chg_sym} {abs(chg):.1f}% today</span></div>
    <div class="hero-signal">{display_sig}</div>
    <div class="hero-conf">Confidence: {conf}%</div>
    <div class="conf-track"><div class="conf-fill"></div></div>
    {f'<div class="hero-banner">{banner}</div>' if banner else ""}
    <div class="hero-explanation">{analysis.get("layman_explanation","")}</div>
    <div class="hero-action">👉 &nbsp;{analysis.get("layman_action","")}</div>
  </div>

  {oct5_html}

  {position_html}

  {perf_html}

  {'<div class="card"><div class="card-label">Watch in the next 48 hours</div><ul>' + watch_items + '</ul></div>' if watch_items else ""}
  {'<div class="card"><div class="card-label">Latest Headlines</div><ul>' + headlines + '</ul></div>' if headlines else ""}

  <div class="footer">Investment Strategy Clock · Powered by Claude AI · Not financial advice</div>
</div>
</body>
</html>"""

    with open(HTML_REPORT, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"HTML report saved: {HTML_REPORT}")


# ─── ALERTS ───────────────────────────────────────────────────────────────────

def send_alerts(analysis: dict, snap: MarketSnapshot):
    """Fire email + Discord alerts on strong signals or critical alerts."""
    sig    = analysis.get("signal", "HOLD")
    alert  = analysis.get("alert")
    price  = snap.price.get("usd", 0)
    conf   = analysis.get("confidence", 0)

    should_alert = sig in ("STRONG_BUY", "STRONG_SELL") or (alert and analysis.get("alert_type") == "danger")
    if not should_alert:
        return

    label   = SIGNAL_STYLES.get(sig, SIGNAL_STYLES["HOLD"])[1]
    subject = f"BTC Crystal Ball: {label} at ${price:,.0f}"
    body    = f"""{label}
Bitcoin: ${price:,.0f}
Confidence: {conf}%

{analysis.get('layman_headline','')}

{analysis.get('layman_explanation','')}

Action: {analysis.get('layman_action','')}

{f'⚠ ALERT: {alert}' if alert else ''}

— BTC Crystal Ball v2.0"""

    # Email alert
    if ALERT_EMAIL and ALERT_EMAIL_PASS:
        try:
            msg            = MIMEText(body)
            msg["Subject"] = subject
            msg["From"]    = ALERT_EMAIL
            msg["To"]      = ALERT_EMAIL_TO
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as srv:
                srv.login(ALERT_EMAIL, ALERT_EMAIL_PASS)
                srv.sendmail(ALERT_EMAIL, [ALERT_EMAIL_TO], msg.as_string())
            log.info(f"Email alert sent to {ALERT_EMAIL_TO}")
        except Exception as e:
            log.warning(f"Email alert failed: {e}")

    # Discord webhook alert
    if DISCORD_WEBHOOK_URL:
        try:
            sig_emoji_map = {"STRONG_BUY":"🚀","BUY":"✅","HOLD":"⏸","SELL":"⚠️","STRONG_SELL":"🔴"}
            em = sig_emoji_map.get(sig,"📣")
            requests.post(DISCORD_WEBHOOK_URL,
                json={"content": f"**{em} {subject}**\n\n{body[:1900]}"},
                timeout=8)
            log.info("Discord alert sent")
        except Exception as e:
            log.warning(f"Discord alert failed: {e}")


# ─── PERSISTENCE ──────────────────────────────────────────────────────────────

def save_log(analysis: dict, snap: MarketSnapshot):
    """Append compact result to JSONL history file."""
    record = {
        "ts":         snap.timestamp,
        "price":      snap.price.get("usd"),
        "signal":     analysis.get("signal"),
        "confidence": analysis.get("confidence"),
        "score":      analysis.get("composite_score"),
        "fg":         snap.fear_greed.get("current"),
        "rsi":        snap.technicals.get("rsi_14"),
        "rainbow":    snap.technicals.get("rainbow_band"),
        "etf_flow_m": snap.etf_flows.get("latest_net_flow_m"),
        "hash_ribbon":snap.hash_ribbon.get("status"),
        "oct5_thesis":analysis.get("oct5_thesis_status"),
        "alert":      analysis.get("alert"),
    }
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


# ─── MAIN LOOP ────────────────────────────────────────────────────────────────

def run_cycle(expert_mode: bool = False):
    """One full collection + analysis + learning cycle."""
    log.info("━"*50)
    log.info(f"BTC Crystal Ball — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    log.info("━"*50)

    snap = MarketSnapshot(timestamp=datetime.now(timezone.utc).isoformat())

    # ── 1. Collect data ─────────────────────────────────────
    collect_price(snap)
    collect_fear_greed(snap)
    collect_hash_ribbon(snap)
    collect_derivatives(snap)
    collect_onchain_free(snap)
    collect_etf_flows(snap)
    collect_macro(snap)
    collect_news(snap)

    # ── 2. Technical indicators ──────────────────────────────
    build_technicals(snap)

    # ── 3. Score signals ─────────────────────────────────────
    build_signals(snap)

    # ── 4. Run learning cycle BEFORE Claude synthesis ────────
    #    This evaluates old predictions and injects lessons into
    #    the intelligence context Claude will read next.
    perf = {}
    if HAS_MEMORY:
        current_price = snap.price.get("usd", 0)
        if current_price:
            log.info("Running learning cycle (evaluating past predictions)...")
            perf = run_learning_cycle(current_price)
        else:
            perf = get_performance_stats()

    # ── 5. Claude synthesis (with accumulated intelligence) ──
    log.info("Sending to Claude for synthesis...")
    analysis = ask_claude(snap)

    # ── 5b. Update position lifecycle state ──────────────────
    if HAS_POSITION:
        signal     = analysis.get("signal", "HOLD")
        confidence = analysis.get("confidence", 0)
        new_mode   = pm_process_signal(signal, snap.price.get("usd", 0), confidence)
        analysis["_mode"] = new_mode   # keep mode in sync after any transition
        log.info(f"Investment mode: {new_mode}  (signal: {signal})")

    # ── 6. Save this prediction for future evaluation ────────
    if HAS_MEMORY:
        save_prediction(snap, analysis)

    # ── 7. Output ────────────────────────────────────────────
    display_layman(analysis, snap, perf=perf)
    if expert_mode:
        display_expert(analysis, snap)

    # ── 8. Save artifacts ────────────────────────────────────
    generate_html_report(analysis, snap, perf=perf)
    save_log(analysis, snap)
    send_alerts(analysis, snap)

    log.info(f"Cycle complete. HTML report: {HTML_REPORT}")
    return analysis


def main():
    parser = argparse.ArgumentParser(description="BTC Crystal Ball v2.0 — Investment Strategy Clock")
    parser.add_argument("--once",     action="store_true", help="Run once and exit")
    parser.add_argument("--expert",   action="store_true", help="Show expert technical detail")
    parser.add_argument("--interval", type=int, default=60, help="Run interval in minutes (default: 60)")

    # Manual position overrides
    parser.add_argument("--set-position", choices=["HUNTING","HOLDING","WAITING"],
                        help="Manually set your investment position mode")
    parser.add_argument("--price", type=float, default=None,
                        help="Entry or exit price for --set-position (e.g. --price 95000)")
    parser.add_argument("--reset-position", action="store_true",
                        help="Reset to HUNTING mode (clears current position)")
    parser.add_argument("--position-status", action="store_true",
                        help="Show current position state and exit")
    args = parser.parse_args()

    # ── Manual position overrides (no API key needed) ────────
    if HAS_POSITION:
        if args.reset_position:
            reset_to_hunting()
            print("✅  Position reset to HUNTING mode.")
            sys.exit(0)

        if args.set_position:
            if args.set_position in ("HOLDING", "WAITING") and not args.price:
                print(f"ERROR: --set-position {args.set_position} requires --price (e.g. --price 95000)")
                sys.exit(1)
            force_set_position(args.set_position, price=args.price)
            print(f"✅  Position set to {args.set_position}" +
                  (f" at ${args.price:,.0f}" if args.price else "") + ".")
            sys.exit(0)

        if args.position_status:
            mode = get_mode()
            ts   = get_trade_summary()
            print(f"\n  Mode: {mode}")
            if mode == "HOLDING":
                ctx = get_position_context(0)
                print(f"  Entry: ${ctx.get('entry_price',0):,.0f}  |  Days held: {ctx.get('days_held',0)}")
            elif mode == "WAITING":
                ctx = get_position_context(0)
                print(f"  Sold: ${ctx.get('sold_price',0):,.0f}  |  Last profit: {ctx.get('last_profit_pct',0):+.1f}%")
            if ts["trade_count"] > 0:
                print(f"  Completed trades: {ts['trade_count']}  |  Win rate: {ts['win_rate']}%  |  Total P&L: {ts['total_profit_pct']:+.1f}%")
            print()
            sys.exit(0)

    if not ANTHROPIC_API_KEY:
        print("\nERROR: Set ANTHROPIC_API_KEY environment variable\n"
              "Get one at: https://console.anthropic.com\n")
        sys.exit(1)

    print("""
  ╔════════════════════════════════════════════╗
  ║      🔮  BTC CRYSTAL BALL  v2.0           ║
  ║   Bitcoin Market Intelligence Agent        ║
  ╚════════════════════════════════════════════╝
  """)

    if args.once or not HAS_SCHEDULER:
        if not HAS_SCHEDULER and not args.once:
            print("TIP: pip install apscheduler for hourly auto-runs\n")
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
