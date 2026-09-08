# Stock Golem — what it can see, and what it can't

Reference for the limits of the current build. Written 2026-09-08.

## Timeframes

| Timeframe | Ross uses it | We use it |
|---|---|---|
| 10-second | yes — *"This is a 10-second chart. Look at the 1-minute chart on this."* | no |
| 1-minute | yes — primary | **yes — every entry and exit decision** |
| 5-minute | yes — panel in his layout; his scanner has a "Rel Volume (5 min %)" column | no |
| Daily | yes — panel showing price against recent history | only for the 50-day volume average, never for pattern context |

**Every pattern decision runs on 1-minute bars.** `detect_pullback` and
`check_exit_signals` see nothing else. `1Day` bars are fetched in exactly one
place — `get_relative_volume` — to compute an average, not to read a chart.

### What that costs

- **No daily context.** A pullback breaking to a multi-day high and one running
  into three-day-old resistance look identical on the 1-minute chart.
- **No sub-minute resolution.** Ross enters on the crossing candle while
  watching 10-second bars — six times finer than ours.
- **No 5-minute relative volume.** In the red-day video frames this separated
  candidates far harder than daily RVOL: STKH read 208 while the rest of the
  field ran 11,000–125,000. We compute neither the bars nor the metric.

## Ross's six exit signals

| Signal | Status |
|---|---|
| Topping tail candle | ✅ implemented |
| Dramatic reversal / false breakout | ✅ implemented |
| Red candle after green | ✅ implemented |
| Buying slowing down | ✅ volume-decay proxy |
| Big seller on Level 2 | ❌ needs full order-book depth |
| Hidden seller (buys printing, ask not moving) | ❌ needs quote + trade stream |

Plus, from the same list, a **large burst of red on the tape** — needs the trade
stream.

## Data sources and their limits

**Bars and quotes — Alpaca, IEX feed (free tier).** IEX is roughly 2–3% of
consolidated US volume. The ratio cancels for RVOL because both sides come from
the same feed, but it means noisy bars on thin names and prices that can drift
from the consolidated tape.

**Bid/ask — available and currently DISCARDED.** `get_snapshot()` requests the
snapshot endpoint, whose response includes `latestQuote` (bid, ask, sizes). The
function reads `dailyBar`, `prevDailyBar` and `latestTrade` and drops the quote.
So the spread filter Ross states explicitly — *"MMF... I can't trade that. The
spreads are too big"* — is buildable with **zero additional API calls**.

**News — Alpaca/Benzinga only.** Coverage of $2–20 small caps is unverified;
`/diag` now probes it against the day's real gainers rather than AAPL.

**Float — yfinance.** Flaky and rate-limited. Failures now retry in 10 minutes
instead of being cached for a day as "no float".

**Polling, not streaming.** Scan every 120s, monitor every 20s. Ross watches
continuously. A crossing candle can be up to two minutes stale before we act.

## What decides which stock gets examined

Alpaca gainer screener (top 50, ≥10%) → 5 Pillars → `detect_pullback`.

That is the entire attention mechanism. Nothing selects *which chart* to study —
every survivor gets the same single-timeframe treatment.

## Buildable now, for free

1. **Spread filter** — the quote is already in a response we already fetch.
   Directly from Ross, and the one rejection rule we extracted and never built.
2. **5-minute and daily bars** — same endpoint, same cost, just another
   `timeframe` argument.
3. **5-minute relative volume** — confirmed twice from his scanner columns.
4. **Daily context** — distance to recent highs/lows from bars we can already
   pull.

## Needs a paid feed

Alpaca's SIP subscription (consolidated tape) would enable **tape-burst
detection** and probably the **hidden-seller** read, since both need the full
trade and quote stream rather than IEX's slice.

**Full Level 2 depth is a separate product again** — "big seller on the book"
needs order-book data that a consolidated trade/quote feed does not carry.

So of the two missing exit signals plus the tape read: perhaps two become
possible on a paid consolidated feed, and the order-book one needs depth data on
top. Worth pricing before assuming a subscription closes the gap.
