# BTC Oracle Agent — Quick Start Guide

## What This Does
Runs every hour, pulls data from 6+ sources, and asks Claude to produce a **BUY / HOLD / SELL** signal with a confidence score, category breakdown, and reasoning narrative.

---

## Step 1: Install Dependencies

```bash
pip install anthropic requests apscheduler feedparser
```

---

## Step 2: Get Your API Keys

| Key | Where to Get It | Cost |
|-----|----------------|------|
| `ANTHROPIC_API_KEY` | console.anthropic.com | Pay-per-use (~$0.01–0.05/run) |
| `FRED_API_KEY` | fred.stlouisfed.org/docs/api/api_key.html | Free |
| `COINGLASS_API_KEY` | coinglass.com (optional) | Free tier |
| `GLASSNODE_API_KEY` | glassnode.com (optional) | $999/mo — skip for now |

---

## Step 3: Set Environment Variables

**Mac/Linux:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export FRED_API_KEY="your_fred_key"
```

**Windows:**
```cmd
set ANTHROPIC_API_KEY=sk-ant-...
set FRED_API_KEY=your_fred_key
```

---

## Step 4: Run

```bash
# Run once (test it)
python btc_oracle_agent.py --once

# Run hourly (continuous)
python btc_oracle_agent.py

# Run every 30 minutes
python btc_oracle_agent.py --interval 30
```

---

## What You'll See

```
════════════════════════════════════════════════════════════
  BTC Oracle  │  2026-06-09 14:00 UTC
════════════════════════════════════════════════════════════
  BTC Price:   $   63,245.50
  Signal:      BUY  (confidence: 74%)
  Score:       +1.20  (range: -2.0 to +2.0)

  Category Scores:
    On-Chain    (30%): +1.8  [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░]  MVRV 1.2, Reserves 7yr low
    Macro       (25%): +0.5  [▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░]  M2 expanding, DXY weakening
    Sentiment   (20%): +1.0  [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░]  F&G 32 (Fear), Funding -0.02%
    Technical   (15%): +0.8  [▓▓▓▓▓▓▓▓▓▓▓▓▓░░░░░░░]  RSI 42, above 200 DMA
    Esoteric    (10%): +1.5  [▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓░░░]  Pi Cycle safe, Rainbow: Accumulate

  Analysis:
    Five on-chain signals are converging for the first time
    since mid-2022, a setup that preceded 300%+ rallies within
    18 months in every prior occurrence. Fear & Greed at 32
    combined with negative funding rates suggests the market
    is positioned bearishly — a contrarian buy setup. M2
    expanding at 3.2% YoY with a 110-day lead implies price
    support emerging in Q4 2026.

  Watch (48h):
    • Fed meeting outcome June 11
    • M2 weekly update June 10
    • BTC holding above $61,000 support

  Cycle Position: Accumulation
  Oct 5 Thesis: Remains valid — cycle bottom timing, Uptober
    seasonality, and on-chain signals all converge on Q4 2026
════════════════════════════════════════════════════════════
```

---

## Signal Log

Every run appends to `btc_oracle_log.jsonl` — a machine-readable history you can analyze later.

---

## Data Sources Used

| Source | Data | API Key Needed? |
|--------|------|----------------|
| CoinGecko | Price, OHLC, volume | No (free tier) |
| Alternative.me | Fear & Greed Index | No |
| FRED (St. Louis Fed) | M2, Fed rate, real yields, DXY | Yes (free) |
| Binance | Funding rates (fallback) | No |
| CoinGlass | Derivatives, OI, liquidations | Optional |
| Glassnode | MVRV, SOPR, hash ribbon | Optional (paid) |
| CoinDesk / CT RSS | News headlines | No |

---

## Upgrading Signal Quality

To get live MVRV Z-Score and Hash Ribbon data (currently using March 2026 estimates):
1. Subscribe to Glassnode Studio Professional ($999/mo)
2. Set `GLASSNODE_API_KEY` environment variable
3. The agent will automatically switch to live data

For now, the agent flags when it's using estimates vs. live data.

---

## Pasting Into Claude Code

In Claude Code, run:
```
claude -p "Build and run the BTC Oracle agent. The code is in btc_oracle_agent.py in the current directory. Install dependencies and execute it with --once to test."
```

Or open Claude Code in the folder containing `btc_oracle_agent.py` and Claude Code will automatically have access to it.
