# 🔮 BTC Crystal Ball v2.0

**Bitcoin market intelligence for everyone — from complete beginners to professional traders.**

---

## What It Does

Runs every hour. Monitors 20+ signals across 5 intelligence categories. Asks Claude AI to synthesize everything into plain English. Produces:

- ✅ A clear **BUY / HOLD / SELL** signal with confidence %
- 📖 Plain-English explanation (no jargon — designed for anyone)
- 📊 Technical breakdown for experts
- 🌐 A shareable **HTML dashboard** (`btc_dashboard.html`)
- 📧 **Email + Discord alerts** when strong signals fire
- ⏰ Live countdown to your **October 5 entry target**

---

## 5-Minute Setup

### Step 1 — Install
```bash
pip install anthropic requests apscheduler feedparser
```

### Step 2 — Get API Keys

| Key | Where | Cost |
|-----|-------|------|
| `ANTHROPIC_API_KEY` | [console.anthropic.com](https://console.anthropic.com) | ~$0.05/run |
| `FRED_API_KEY` | [fred.stlouisfed.org/docs/api/api_key.html](https://fred.stlouisfed.org/docs/api/api_key.html) | Free |
| `COINGLASS_API_KEY` | [coinglass.com](https://coinglass.com) | Free tier |

Optional alerts:
- **Email**: Set `ALERT_EMAIL` + `ALERT_EMAIL_PASSWORD` (Gmail app password) + `ALERT_EMAIL_TO`
- **Discord**: Create a webhook in any Discord channel → set `DISCORD_WEBHOOK_URL`

### Step 3 — Set Environment Variables

**Mac/Linux:**
```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export FRED_API_KEY="your_key_here"
```

**Windows (Command Prompt):**
```cmd
set ANTHROPIC_API_KEY=sk-ant-...
set FRED_API_KEY=your_key_here
```

**Windows (PowerShell):**
```powershell
$env:ANTHROPIC_API_KEY="sk-ant-..."
$env:FRED_API_KEY="your_key_here"
```

### Step 4 — Run
```bash
# Test run (runs once, shows everything)
python btc_oracle_v2.py --once

# With expert technical view
python btc_oracle_v2.py --once --expert

# Hourly monitoring (runs forever)
python btc_oracle_v2.py

# Every 30 minutes
python btc_oracle_v2.py --interval 30
```

---

## What You'll See

```
╔════════════════════════════════════════════════════════╗
║   🔮  BTC CRYSTAL BALL  v2.0                         ║
║   June 9, 2026  02:00 PM                             ║
╠════════════════════════════════════════════════════════╣
║  Bitcoin Price:  $63,245  ▼ 2.1% today               ║
╠════════════════════════════════════════════════════════╣
║                                                       ║
║           ✅ GOOD TIME TO BUY                         ║
║              Confidence: 74%                          ║
║                                                       ║
╠════════════════════════════════════════════════════════╣
║  What's happening:                                    ║
║                                                       ║
║  Bitcoin is down 50% from its peak, and the smart     ║
║  money is quietly buying while most people are        ║
║  scared. This pattern has historically preceded       ║
║  major recoveries.                                    ║
║                                                       ║
║  👉 Consider starting a small position now and        ║
║     adding more if price drops further. Don't         ║
║     invest more than you can afford to lose.          ║
║                                                       ║
╠════════════════════════════════════════════════════════╣
║  Signal Strength by Category:                         ║
║                                                       ║
║  🔗 On-Chain Data    ████████████████░░  Strong Buy   ║
║  🌍 Big Picture      ████████████░░░░░░  Buy          ║
║  😨 Market Mood      █████████░░░░░░░░░  Buy          ║
║  📊 Price Charts     ██████████░░░░░░░░  Buy          ║
║  🔮 Hidden Signals   ███████████████░░░  Strong Buy   ║
║                                                       ║
╠════════════════════════════════════════════════════════╣
║  ✅ Oct 5 Entry Target: ON TRACK                      ║
║     117 days away                                     ║
╠════════════════════════════════════════════════════════╣
║  👁  Watch in next 48 hours:                          ║
║  • Fed rate decision June 11                          ║
║  • Bitcoin holding above $61,000 support              ║
║  • ETF flow direction after weekend                   ║
╚════════════════════════════════════════════════════════╝
```

After every run, `btc_dashboard.html` is updated — open it in any browser for a clean visual version.

---

## Data Sources (All Free Without Glassnode)

| Source | Signals | Cost |
|--------|---------|------|
| CoinGecko | Price, OHLC, market cap | Free |
| Alternative.me | Fear & Greed Index | Free |
| mempool.space | Hash rate (real Hash Ribbon) | Free |
| Binance | Funding rates, OI, L/S ratio | Free |
| blockchain.com Charts | Hash rate history, exchange flows, tx count | Free |
| FRED (St. Louis Fed) | M2 money supply, Fed rate, DXY | Free with key |
| Farside Investors | Bitcoin ETF daily flows | Free (scraped) |
| CoinDesk / CoinTelegraph | News headlines | Free (RSS) |

---

## Signals Monitored

### On-Chain (30% weight)
- **Hash Ribbon** — miner health, 87.5% historical accuracy for buy signals
- **MVRV Ratio** — is Bitcoin over or undervalued? (approximated from free data)
- **Exchange Reserves** — are coins leaving exchanges? (accumulation signal)
- **ETF Flows** — what is institutional money doing?

### Macro (25% weight)
- **M2 Money Supply** — leads Bitcoin by ~110 days (1% M2 growth = 2.65% BTC price gain)
- **DXY Dollar Index** — -0.58 correlation with BTC
- **Federal Funds Rate** — rate cuts fuel Bitcoin rallies

### Sentiment (20% weight)
- **Fear & Greed Index** — extreme fear = contrarian buy
- **Futures Funding Rate** — negative = shorts overpaying = bullish
- **Long/Short Ratio** — crowded positions often reverse

### Technical (15% weight)
- **RSI 14** — overbought/oversold
- **MACD crossover** — momentum shift signal
- **200-day Moving Average** — long-term trend health

### Esoteric (10% weight)
- **Pi Cycle Top** — called the last 3 tops within 3 days
- **Rainbow Chart Band** — long-term log valuation model

---

## Alert Setup (Optional)

### Email Alerts (Gmail)
1. Enable 2FA on your Gmail account
2. Create an App Password: myaccount.google.com → Security → App Passwords
3. Set env vars: `ALERT_EMAIL`, `ALERT_EMAIL_PASSWORD`, `ALERT_EMAIL_TO`
4. Alerts fire automatically on STRONG_BUY, STRONG_SELL, or danger events

### Discord Alerts
1. In any Discord channel: Settings → Integrations → Webhooks → New Webhook
2. Copy the webhook URL
3. Set env var: `DISCORD_WEBHOOK_URL`

---

## Upgrading Signal Quality

The agent is designed to work entirely on free data, but optional upgrades improve accuracy:

| Upgrade | What It Unlocks | Cost |
|---------|----------------|------|
| Glassnode API key | Live MVRV Z-Score, aSOPR, real exchange reserves | $999/mo |
| CoinGlass paid tier | Richer derivatives data, OI history | ~$30/mo |
| Whale Alert API | Real whale transaction tracking | $49/mo |

The confidence score automatically reflects data quality — estimated data is flagged.

---

## History & Backtesting

Every run appends to `crystal_ball_history.jsonl`. Each line is a JSON record:
```json
{"ts":"2026-06-09T14:00:00Z","price":63245,"signal":"BUY","confidence":74,
 "score":1.2,"fg":32,"rsi":42,"rainbow":"📈 Accumulate","etf_flow_m":210}
```

You can load this file into Python/Excel for backtesting and performance analysis.

---

## Pasting Into Claude Code

Open Claude Code in the folder containing `btc_oracle_v2.py`, then run:

```
Build and test the BTC Crystal Ball agent:
1. Install: pip install anthropic requests apscheduler feedparser
2. Run a test cycle: python btc_oracle_v2.py --once
3. Open btc_dashboard.html in a browser to see the HTML report
```

---

*Not financial advice. Always invest only what you can afford to lose.*
