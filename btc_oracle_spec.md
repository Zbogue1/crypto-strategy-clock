# BTC Oracle Agent — Master Specification

## Purpose
Hourly-running agent that collects data across four intelligence categories, scores each signal, and produces a composite **BUY / SELL / HOLD** output with a confidence level (0–100) and supporting reasoning. Designed to run inside Claude Code using the Anthropic Python SDK.

---

## Signal Architecture

Every signal is scored on a scale of **-2 to +2**:
| Score | Meaning |
|-------|---------|
| +2 | Strong bullish |
| +1 | Mild bullish |
| 0 | Neutral |
| -1 | Mild bearish |
| -2 | Strong bearish |

The weighted composite score maps to a final signal:
- **+1.5 to +2.0** → STRONG BUY
- **+0.5 to +1.4** → BUY
- **-0.4 to +0.4** → HOLD
- **-1.4 to -0.5** → SELL
- **-2.0 to -1.5** → STRONG SELL

---

## Category 1: On-Chain Data (Weight: 30%)

### Signals & Thresholds

**MVRV Z-Score** *(Most predictive cycle indicator — historically pinpoints tops to within 2 weeks)*
- Source: Glassnode (paid) OR checkonchain.com (scrape) OR bitcoinmagazinepro.com
- MVRV < 0: Score +2 (deep undervaluation, historical buy zone)
- MVRV 0–1: Score +1
- MVRV 1–3: Score 0
- MVRV 3–6: Score -1
- MVRV > 6: Score -2 (cycle top zone)
- Current reading (March 2026): ~1.2 → mild bullish

**aSOPR (Adjusted Spent Output Profit Ratio)**
- Source: Glassnode, checkonchain.com
- aSOPR < 1.0: Score +2 (holders selling at loss = capitulation, historical buy)
- aSOPR 1.0–1.02: Score +1
- aSOPR > 1.02: Score 0 to -1 (profit-taking)

**Exchange Reserves (BTC on exchanges)**
- Source: CryptoQuant, Glassnode, CoinGlass
- Declining reserves (coins leaving for cold storage): Score +1 to +2
- Rising reserves (coins returning for sale): Score -1 to -2
- Current (2026): 2.21M BTC — 7-year low → Score +2

**Hash Ribbon (30d MA vs 60d MA of hash rate)**
- Source: Bitbo.io (scrape) OR calculate from mining pool APIs
- 30d MA crosses ABOVE 60d MA (recovery): Score +2 (87.5% historical accuracy, avg +557% to next peak)
- 30d MA crosses BELOW 60d MA (capitulation begins): Score -1 (not a top signal, just distress)
- 30d MA flat above 60d: Score +1

**Puell Multiple** *(Daily miner revenue / 365d MA of miner revenue)*
- Source: Glassnode, bitcoinmagazinepro.com
- Puell < 0.5: Score +2 (miners unprofitable = capitulation bottom)
- Puell 0.5–1.0: Score +1
- Puell 1.0–2.0: Score 0
- Puell > 4.0: Score -2 (miners extremely profitable = cycle top)

---

## Category 2: Macro & Fed Policy (Weight: 25%)

### Signals & Thresholds

**Global M2 Money Supply Momentum** *(Leading indicator — M2 leads BTC by ~110 days)*
- Source: FRED API (free) — series WM2NS for US M2; combine with ECB, BoJ, PBOC data
- M2 YoY growth > 5%: Score +2
- M2 YoY growth 2–5%: Score +1
- M2 YoY growth 0–2%: Score 0
- M2 YoY growth negative: Score -2
- Key insight: 1% increase in M2 = 2.65% increase in BTC price (cointegration study)

**DXY (US Dollar Index)**
- Source: FRED API (series DTWEXBGS) or stlouisfed.org
- Correlation with BTC: -0.58 (strong inverse)
- DXY falling (weakening dollar): Score +1 to +2
- DXY rising (strengthening dollar): Score -1 to -2

**Federal Funds Rate / Fed Pivot Signals**
- Source: FRED API (series FEDFUNDS), Fed meeting calendars, news
- Rate cut cycle active: Score +1
- Rate hold (neutral): Score 0
- Rate hike cycle active: Score -1 to -2

**Real Yields (10Y Treasury - Inflation)**
- Source: FRED API (series DFII10 — 10-Year TIPS)
- Real yields falling: Score +1
- Real yields rising: Score -1

---

## Category 3: Sentiment & News (Weight: 20%)

### Signals & Thresholds

**Fear & Greed Index**
- Source: Alternative.me API (free, 60 req/min) — `https://api.alternative.me/fng/`
- 0–25 (Extreme Fear): Score +2 (contrarian buy — best historical entry signal)
- 25–45 (Fear): Score +1
- 45–55 (Neutral): Score 0
- 55–75 (Greed): Score -1
- 75–100 (Extreme Greed): Score -2 (contrarian sell)

**Funding Rates (Perpetual Futures)**
- Source: CoinGlass public API
- Highly negative funding (< -0.05%): Score +2 (shorts overpaying = likely bottom)
- Slightly negative: Score +1
- Near zero: Score 0
- Positive (0–0.05%): Score -1 (longs overpaying, crowded)
- Extremely positive (> 0.1%): Score -2 (overheated long crowding = correction risk)

**Open Interest Trend**
- Source: CoinGlass public API
- OI falling while price stabilizes: Score +1 (leverage flushed)
- OI rising with price rising: Score 0 (healthy)
- OI rising while price falling: Score -2 (short squeeze risk building OR cascade risk)

**News Sentiment**
- Source: Web search / RSS scraping of: CoinDesk, CoinTelegraph, The Block, Decrypt
- Agent performs sentiment analysis on top 10 headlines each hour
- Predominantly negative headlines + price stability: Score +1 (divergence = bullish)
- Predominantly positive headlines + parabolic price: Score -1 (euphoria risk)

---

## Category 4: Technical Indicators (Weight: 15%) + Esoteric Models (Weight: 10%)

### Technical Signals

**RSI (14-period daily)**
- Source: Calculated from CoinGecko price data
- RSI < 30: Score +2 (oversold)
- RSI 30–45: Score +1
- RSI 45–55: Score 0
- RSI 55–70: Score -1
- RSI > 70: Score -2 (overbought)

**MACD (12/26/9)**
- Source: Calculated from CoinGecko price data
- Bullish crossover (MACD line crosses above signal): Score +1
- Bearish crossover: Score -1
- Histogram widening positive: Score +1
- Histogram widening negative: Score -1

**200-Day Moving Average Position**
- Source: CoinGecko price data
- Price > 200 DMA and 200 DMA trending up: Score +1
- Price < 200 DMA: Score -1

### Esoteric / Lesser-Known Signals

**Pi Cycle Top Indicator**
- Source: Bitcoinmagazinepro.com OR calculate from price data
- Formula: 111 SMA vs (350 SMA × 2)
- When 111 SMA approaches 350 SMA × 2 (within 3%): Score -2 (cycle top warning — called within 3 days historically)
- When gap is > 30%: Score +1 (far from top)

**Bitcoin Rainbow Chart Band**
- Source: Bitbo.io or calculate logarithmic regression
- "Fire Sale" / "Buy" bands (bottom 2): Score +2
- "Accumulate" band: Score +1
- "Still Cheap" / "Hold": Score 0
- "Is this a bubble?" / "FOMO" bands: Score -1
- "Maximum Bubble Territory": Score -2

**Stock-to-Flow Deviation**
- Source: Lookintobitcoin.com (scrape) OR calculate
- Price significantly below S2F model (>50% below): Score +2
- Price at model: Score 0
- Price >100% above model: Score -2

**Miner Capitulation Signal (combination)**
- Hash rate declining > 15% over 30 days AND Puell < 0.6: Score +2
- (Historically this convergence has preceded major bottoms in 2015, 2018, 2022)

**Stablecoin Supply Ratio (SSR)**
- Ratio of BTC market cap to stablecoin market cap
- Low SSR (lots of stablecoin buying power relative to BTC): Score +1 to +2
- High SSR: Score -1

**Long/Short Ratio**
- Source: CoinGlass public API
- > 60% longs: Score -1 (crowded longs, squeeze risk)
- 40–60%: Score 0
- < 40% longs: Score +1 (bearish crowd = contrarian buy)

---

## API Reference Sheet

| Source | Data | Cost | Endpoint |
|--------|------|------|----------|
| CoinGecko | Price, volume, market cap, OHLC | Free (50 req/min) | `https://api.coingecko.com/api/v3/` |
| Alternative.me | Fear & Greed Index | Free (60 req/min) | `https://api.alternative.me/fng/` |
| FRED (St. Louis Fed) | M2, Fed Funds Rate, Real Yields, DXY | Free (API key required) | `https://api.stlouisfed.org/fred/series/observations` |
| CoinGlass | Funding rates, OI, liquidations, L/S ratio | Free public endpoints | `https://open-api.coinglass.com/public/v2/` |
| Glassnode | MVRV, SOPR, exchange flows, hash ribbon | Paid ($999/mo for API) | `https://api.glassnode.com/v1/metrics/` |
| Santiment | On-chain + social | Free (1,000 calls/mo) | `https://api.santiment.net/graphql` |
| Bitcoinmagazinepro.com | MVRV, Pi Cycle, Puell (visual) | Free (scrape) | `https://www.bitcoinmagazinepro.com/charts/` |
| CoinDesk RSS | News headlines | Free | `https://www.coindesk.com/arc/outboundfeeds/rss/` |
| CoinTelegraph RSS | News headlines | Free | `https://cointelegraph.com/rss` |

---

## Context the Agent Monitors Every Hour

1. Current BTC price + 24h / 7d / 30d % change
2. Fear & Greed score (live)
3. Funding rate across top 5 exchanges (Binance, OKX, Bybit, dYdX, BitMEX)
4. Open interest (total, 24h change)
5. Liquidations (24h long vs short liquidations)
6. Long/Short ratio
7. M2 latest reading + 110-day forward projection
8. Fed funds rate current + next meeting date + market-implied rate probability
9. DXY 7-day trend
10. RSI (14d daily) + MACD status
11. MVRV Z-Score (updated daily — agent uses latest available)
12. Hash ribbon status (crossover or not)
13. Top 5 news headlines (sentiment tagged)
14. Exchange reserve trend (weekly change)
15. Pi Cycle Top proximity (% gap between 111 SMA and 350 SMA × 2)

---

## Output Format

```json
{
  "timestamp": "2026-06-09T14:00:00Z",
  "btc_price": 63245.50,
  "signal": "BUY",
  "confidence": 74,
  "composite_score": 1.2,
  "signal_breakdown": {
    "on_chain": {"score": 1.8, "weight": 0.30, "key_signals": ["MVRV at 1.2", "Exchange reserves 7yr low", "Hash ribbon recovery"]},
    "macro": {"score": 0.5, "weight": 0.25, "key_signals": ["M2 expanding", "DXY declining"]},
    "sentiment": {"score": 1.0, "weight": 0.20, "key_signals": ["F&G at 32 (Fear)", "Funding rate -0.02%"]},
    "technical": {"score": 0.8, "weight": 0.15, "key_signals": ["RSI 42", "Above 200 DMA"]},
    "esoteric": {"score": 1.5, "weight": 0.10, "key_signals": ["Pi Cycle far from top", "Rainbow: Accumulate band"]}
  },
  "reasoning": "Claude's narrative synthesis...",
  "alert": null,
  "next_watch": ["Fed meeting June 18", "M2 update June 25"]
}
```

---

## Key Research Findings Summary

**Why October 5th, 2026 is a high-conviction target:**
- ~220 days from the October 6, 2025 ATH ($126,200) lands in mid-October 2026
- "Uptober" effect: 83% positive rate, avg +20% monthly return historically
- MVRV Z-Score at 1.2 (mid-range, not overbought)
- Exchange reserves at 7-year low (2.21M BTC) — supply tightening
- 5 on-chain signals converging simultaneously (only occurred 3 prior times: late 2015, late 2018, mid-2022 — each preceded 300%+ rallies within 18 months)
- M2 leads BTC by 110 days — any M2 expansion now shows up in BTC price ~Oct/Nov 2026

**Key risk to watch:**
- Fed policy is now the dominant driver over halving cycles
- If Fed holds rates high through Q3 2026, bottom could extend to late 2026 / early 2027
- Four-year cycle is loosening, not breaking — precision entries are harder now

---

## Sources
- [Bitcoin On-Chain Bottom Signals: MVRV, SOPR & Whale Data — SpotedCrypto](https://www.spotedcrypto.com/bitcoin-onchain-bottom-signals-march-2026/)
- [Bitcoin's Macro Liquidity Cycle — OnRamp Bitcoin](https://onrampbitcoin.com/research/bitcoins-macro-liquidity-cycle)
- [M2-Bitcoin Relationship — CF Benchmarks](https://www.cfbenchmarks.com/blog/the-m2-bitcoin-relationship-what-the-data-actually-shows)
- [Hash Ribbon Buy Signal Accuracy — eCoinometrics](https://ecoinometrics.substack.com/p/bitcoins-hash-ribbon-buy-signal-how)
- [Pi Cycle Top Indicator — CoinMarketCap Academy](https://coinmarketcap.com/academy/article/what-is-the-pi-cycle-top-indicator-and-how-to-use-it)
- [Bitcoin Seasonality — Forecaster.biz](https://forecaster.biz/bitcoin-seasonality/)
- [FRED API Documentation](https://fred.stlouisfed.org/docs/api/fred/)
- [CoinGlass API](https://www.coinglass.com/CryptoApi)
- [Claude Agent SDK Overview](https://code.claude.com/docs/en/agent-sdk/overview)
