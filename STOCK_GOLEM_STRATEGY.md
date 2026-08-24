# Stock Golem — Strategy Specification

> Extracted from Ross Cameron / Warrior Trading source material (2026 Small Account
> Challenge PDFs + the "$2k to $65,662 in 30 Days" class transcript).
> This is the methodology, restated as implementable rules.
>
> **Results disclaimer, from the source's own material:** his results are not
> typical. Warrior Trading's own disclosure cites research finding fewer than 3%
> of day traders are predictably profitable. Treat the headline numbers as
> marketing; let paper trading establish the real expectancy.

---

## STEP 1 — Stock Selection: The 5 Pillars

A stock must meet **at least 4 of 5** to be tradeable ("A quality").

| # | Pillar | Threshold |
|---|---|---|
| 1 | **Relative volume** | ≥ **5x** the 30-day average |
| 2 | **Already moving** | Up ≥ **10%** on the day (exception: continuation setup holding prior-day gains) |
| 3 | **News catalyst** | A headline that justifies the move. Preferred, not absolutely required |
| 4 | **Price** | **$1–$20** general · **$5–$10** for the small account (no leverage under $5) |
| 5 | **Float** | < **20M** shares in a hot market · < **10M** in a cold market. Lower is better |

**Additional selection rules:**
- Focus on the **top 2–3 leading percentage gainers**. "If it's obvious to me, it's
  obvious to tens of thousands of other traders — the stock trades more predictably."
- Only take setups with **20–30% move potential**. If a stock can only run 5–10¢,
  it isn't worth the trade in a small account.
- Some days **no stock meets 4 of 5**. That is a valid outcome — do not trade.

---

## STEP 2 — Entry: The First Pullback Pattern

Timeframes: **1-minute and 5-minute**.

### Pullback validity — all four must hold

| # | Condition |
|---|---|
| 1 | Retraces **≤ 50%** of the prior move |
| 2 | Volume **higher on green candles** than red — light volume on the pullback |
| 3 | Does **NOT** break below **VWAP** |
| 4 | Does **NOT** break below the **9 EMA** |

> "A perfect pullback pattern will have a nice big green candle, maybe two or three,
> but as it pulls back it doesn't retrace more than 50%, high volume on the green
> candles, light on the red, doesn't break VWAP, doesn't break the 9 EMA."

### Entry trigger

**The crossing candle** — the *first candle to make a new high*, i.e. the candle
that crosses over the high of the previous candle.

### Stop

**The low of the pullback.** Hard stop, not mental.

> Worked example from the transcript: entry **$7.60**, hard stop **$7.45** → **$0.15/share risk**.

### Warning signs during the squeeze
- **Topping tails** (upper wicks) are inherently bearish
- **Bottoming tails** (lower wicks) are bullish
- Avoid buying into an area where heavy selling previously occurred

---

## STEP 3 — Exit: The 6 Exit Indicators

**Any single indicator triggers an exit — regardless of P&L.**

> "These are exit indicators whether I'm up 5 cents a share, 50 cents a share, or
> $5 a share… I gotta just jump right out because the exit indicator has to be respected."

| # | Indicator | Automatable? |
|---|---|---|
| 1 | **Big seller on Level 2** — a resting order (50k–1M shares), not a flashing one | ❌ needs L2 |
| 2 | **Hidden seller / iceberg** — heavy buying but price won't advance | ❌ needs L2 |
| 3 | **Large burst of red on the tape** — surge of selling | ❌ needs time & sales |
| 4 | **Dramatic reversal** — topping tail + false breakout | ✅ from OHLCV |
| 5 | **Buying slowing down** — visible on time & sales | ⚠️ partial (volume decay) |
| 6 | **Topping tail candle or red candle forming** | ✅ from OHLCV |

### Profit target philosophy
- Nominal target **50¢–$1.00/share**, but **do not auto-sell at target**.
- "If I picked a setup going up $2–3/share, I want to benefit from as much of that
  move as possible. So I will hold until I see an exit indicator."
- Target defines *minimum acceptable* reward, not the exit.

---

## STEP 4 — Risk Management

| Rule | Value |
|---|---|
| Profit/loss ratio | **≥ 2:1** minimum |
| Risk per trade (week 1) | **$50** to make **$100** |
| Daily max loss | **−$100** |
| **Circuit breaker** | **3 consecutive losers → done for the day** |
| Target accuracy | 75% (aspirational); 2:1 P/L means 33% breakeven |

**Conflict between sources — resolve in config:**

| Source | Risk/trade | Daily max |
|---|---|---|
| Small Account Strategy PDF | $50 on $2k = **2.5%** | $100 = **5%** |
| Trading Plan Worksheet | **5%** | **10%** |

Use the conservative figures as defaults; expose both as env vars.

### Profit Trifecta benchmarks (calibration targets)

| Metric | Novice | Beginner | Advanced | Pro |
|---|---|---|---|---|
| Consistency | 1 wk green | 2 wks | 3–5 wks | 5+ wks |
| Accuracy | 40–50% | 50–60% | 60–70% | >70% |
| P/L ratio | 0.5–1 | 1.0–1.5 | 1.5–2.0 | >2.0¹ |

¹ Source PDF prints ">1.0" for Pro, which is lower than Advanced — evidently a typo.

---

## STEP 5 — Timing & Discipline

- **Trading window:** 7:00–11:00 AM EST
- His own metric review: *"if I stop at 10 a.m., that would have improved my
  performance for the last 30 days"* → **strong argument for a 10:00 AM cutoff**
- **Don't return after leaving.** "If I leave and then come back, I'm apt to give
  into FOMO because I feel like I've missed something."
- Trade aggressively only when the market is hot; sit tight when cold
- **Base hits over home runs.** "Home runs often come at the cost of base hits."
- Cut losses faster. Don't hold and hope.

---

## Pre-trading checklist

1. Market strength today, 0–10?
2. Am I centered, rested, ready?
3. How should I adjust based on the above?
4. **What is the obvious stock today?**
5. When was the last stock that did a massive squeeze?
6. Are we in a hot cycle or a cold cycle?

---

## What we CAN and CANNOT automate

### Fully automatable from OHLCV + fundamentals
- All 5 pillars (RVOL, % gain, price, float, news presence)
- Pullback validity: 50% retrace, volume profile, VWAP, 9 EMA
- Crossing-candle entry trigger
- Stop at pullback low
- Exit indicators **#4 and #6** (topping tail, red candle)
- All risk rules, circuit breaker, time windows

### Requires Level 2 / time & sales — NOT in free data
- Exit **#1** big resting seller
- Exit **#2** hidden seller / iceberg
- Exit **#3** burst of red on the tape
- Exit **#5** buying momentum decay (partial proxy possible via volume)

**Implication:** an automated version can implement roughly **two-thirds of the
exit logic**. The missing third is the discretionary tape-reading that is arguably
where an experienced trader's edge actually lives. Stock Golem will need
compensating rules — e.g. a tighter trailing stop or a time-based exit — and we
should measure whether that degrades results.

### Data requirements
- **1-minute intraday bars**, real-time or near-real-time, during 7–11 AM EST
- Pre-market gap scanner data (7:00 AM onward — before the 9:30 open)
- Float data per ticker (not in most free feeds)
- News/catalyst feed
- **Free tiers will not support this.** Delayed quotes and 15-minute bars cannot
  trade a pattern where the stop is 15¢ and holds last seconds to minutes.
