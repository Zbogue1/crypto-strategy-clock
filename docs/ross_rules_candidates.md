# Ross Cameron rule candidates — extraction log

Source-of-truth file for strategy claims pulled from Ross Cameron videos.
**Nothing here is live code.** A row becomes code only after it has a
`tools/simulate.py` scenario that FAILS against current behaviour first.

Status vocabulary:

- `CONFIRMS` — matches a threshold already in the repo. Value is the confirmation, no change needed.
- `NEW` — codeable and not currently implemented.
- `CONFLICT` — Ross is uncertain, or it contradicts something we have.
- `HUMAN-ONLY` — corrects a human failure mode the bot structurally cannot have. **Do not implement.**

---

## Video 01 — "MAX LOSS RED DAY (mistakes were made)"

- URL: https://www.youtube.com/watch?v=kIZS4bU2Jpo
- Duration: 22:28 · 619 caption segments · transcript-only pass
- Extracted: 2026-08-30

| # | Claim | Timestamp | Status | Notes |
|---|---|---|---|---|
| 1 | RVOL 4.46 is "a little lower than I'd prefer" | 03:57–04:04 | `CONFIRMS` | `MIN_RVOL = 5.0` in stock_signals.py:29. His floor sits just above 4.46. Threshold validated, leave alone. |
| 2 | RVOL is understated on stocks with recent failed high-volume spikes | 04:04–04:11 | `NEW` | "partly because you've had a few of these high volume days where it pops up and rejects." Prior pump days inflate avg volume → RVOL reads low on exactly the stocks that keep failing. Our RVOL has no awareness of prior rejection days. |
| 3 | Reject on wide spread regardless of momentum | 07:15–07:19 | `NEW` | On MMF: "I can't trade that. The spreads are too big." No spread filter exists in stock_signals.py. |
| 4 | Off-theme setups are lower quality | 02:14–02:21 | `NEW` | Israeli stock on a day themed around Chinese stocks — "a little off the theme." Implies a same-session sector/catalyst coherence check. |
| 5 | A prior sharp flush in-session is a warning against re-entry size | 03:19–03:23 | `NEW` | "Should I have seen that coming? We had had a big flush right here." |
| 6 | Don't take max size at high-of-day on a stock that's already been erratic | 21:54–22:08 | `NEW` | Bought 45,000 shares at HOD on impulse. Overlaps #5. |
| 7 | MACD curling while still positive may itself be the exit signal | 02:32–02:43 | `CONFLICT` | He explicitly does not know: "Maybe the curling — I don't know." Do not encode. Log and look for a second source. |
| 8 | Stop trading for the day after a large green | 01:40–01:45 | `HUMAN-ONLY` | "I wish I had just taken it off the table." Exists to stop *him* giving back profits emotionally. A bot has no such drift — implementing this would cap upside for no edge. |
| 9 | Don't trade while emotionally compromised / after a big loss | 05:23–05:36, 19:00 | `HUMAN-ONLY` | The actual cause of this $64k day. Already structurally impossible + covered by the daily-loss halt. |
| 10 | Divided attention across two accounts degrades execution | 11:00–13:10 | `HUMAN-ONLY` | Ross's own leading theory for the loss. No bot analogue. |
| 11 | "Give the loser more room" instead of cutting at stop | 12:54–13:08 | `HUMAN-ONLY` — with a caveat | No bot analogue *emotionally*, but our failure mode is identical in effect: a stop that doesn't execute. See the tranche/stop-loss integrity work. Worth noting, not coding. |

### Headline

Six of eleven extractable claims are corrections for human failure modes.
Ross's own verdict at 21:40: *"the strategy is pretty solid... most of them can
be chalked up to emotion."*

This is the main finding, and it cuts against the reason we built the pipeline:
the highest-frequency content in a red-day recap is psychology, and copying
psychology rules into a bot adds constraints with no underlying edge. #8 is the
trap — it sounds like a risk rule and is actually a bias patch.

The genuinely new material is #2 and #3, and #2 is the better one because it
identifies a case where **our existing RVOL number is misleading rather than
merely absent** — the failure mode we keep finding everywhere else.

---

## Frames pass — 02:00–04:10, 1024px, 8 frames

Read off his actual scanner in `frame_0005.jpg` (t=03:25) and `frame_0008.jpg`
(t=03:50). These are visual-only findings — **none of this is stated in the
transcript.**

### Top Gainers panel, 10:17–10:22, as displayed

| Symbol | Price | Float | RVOL (daily) | RVOL (5 min %) |
|---|---|---|---|---|
| MDXH | 8.01 | 32.67M | 1,415.00 | 125,015 |
| HHS | 4.33 | 5.44M | 662.78 | 73,688 |
| WETO | 8.92 | 657.20K | 50.40 | 11,490 |
| AKAN | 7.66 | 477.40K | 63.29 | 5,447 |
| LFS | 3.10 | 6.81M | 61.95 | 3,729 |
| ME | 16.60 | 1.35M | 30.87 | 7,014 |
| ONFO | 3.96 | 627.30K | 22.66 | 39,919 |
| CAPR | 7.59 | 45.69M | 17.33 | 2,231 |
| DAAQ | 10.42 | *check float* | 8.94 | 316 |
| **STKH** | **4.09** | **930.23K** | **4.46** | **208** |

| # | Finding | Status | Notes |
|---|---|---|---|
| 12 | STKH was **last of ten** on daily RVOL and last on 5-min RVOL | `NEW` | Upgrades claim #1. Not "a little lower than I'd prefer" — bottom of his own list, 3.8x below the next-worst and ~300x below the leader. Argues for ranking candidates against each other, not only against a fixed floor. |
| 13 | 5-minute relative volume separates far harder than daily RVOL | `NEW` | Daily spread across the list is ~150x; the 5-min spread is ~600x, and STKH is isolated at the bottom by 15x. `stock_data.py` computes only session-projected *daily* RVOL (`rvol`, `rvol_raw`). No short-window metric exists. |
| 14 | **STKH never appeared in his own "5 Pillars Scan"** | `NEW` | Visible in both frames, 25s apart: the 5 Pillars panel lists only WETO, ONFO, LFS. He took the trade off the raw Top Gainers list, overriding his own qualification system. This is the actual mechanical error and it is nowhere in the audio. |
| 15 | `MIN_RVOL = 5.0` would have rejected this trade | `CONFIRMS` | STKH at 4.46 fails our existing floor. Stock Golem does not take Ross's $45,000 loss. |

### Why the frames mattered

The transcript's version is *"I had a bias on it. I'm wrong, but the thing is
going, so I'm going to jump in."* — self-criticism with no mechanism.

The screen shows the mechanism: his qualification scan excluded the stock, and
he traded it anyway from a different panel. That is a concrete, checkable rule
(#14) rather than a sentiment, and it was only recoverable visually.

Worth noting the reverse too: the transcript claim that most needed visual
confirmation, #2 (RVOL depressed by prior rejection days), is **not** verifiable
from these frames. The scanner shows the resulting number, not the historical
volume distribution behind it. It stays unconfirmed.

### Next

- Second source needed for #7 (MACD curl) before it leaves CONFLICT.
- #2 needs a daily-chart frame or an independent data check, not this segment.
- #13 and #14 are the two worth building. Each needs a `simulate.py` scenario
  that fails against current behaviour first.

---

## Video 02 — "Growing a $2k Account to $65,662.04 in 30 Days"

- URL: https://www.youtube.com/watch?v=xGIa8Vg0PWM
- Duration: 59:09 · Extracted: 2026-09-01
- **Two independent reads:** Gemini 3 Flash watching the video natively in
  Google AI Studio, and a full text transcript read separately.

Reconciliation vocabulary: `confirmed` = both reads agree · `single source` =
one read only · `conflict` = the reads disagree.

Limitation worth stating: both reads ultimately derive from the same spoken
words, so this is not fully independent. Gemini additionally read the on-screen
slides, which is where the numeric filters appear, so it corroborates the
figures rather than merely re-hearing them. The transcript carries no
timestamps, so **Gemini's timestamps are single-source and unverified.**

| # | Rule | Agreement | Status vs our code |
|---|---|---|---|
| 16 | RVOL ≥5x, measured against the **50-day** average | `confirmed` | ⚠️ **`get_relative_volume(days=30)`** — we use 30 |
| 17 | Catalyst **not required** if the move is obvious enough | `confirmed` | ⚠️ `pillars["catalyst"]["pass"] = n_news > 0` — we hard-require it |
| 18 | Price band **$2–$20** | `confirmed` | ⚠️ `PRICE_MIN = 1.0` |
| 19 | Entry window **7:00–10:00 ET** | `confirmed` | ⚠️ `ENTRY_END_ET = "10:30"` |
| 20 | Gain ≥10% on the day | `confirmed` | ✅ `MIN_PCT_CHANGE = 10.0` |
| 21 | Float <20M, lower is better | `confirmed` | ✅ `FLOAT_MAX_HOT_M = 20.0` |
| 22 | Entry = the **crossing candle** (first to take out the prior candle's high) | `confirmed` | ✅ matches `detect_pullback` |
| 23 | Stop = **low of the pullback** | `confirmed` | ✅ matches |
| 24 | Minimum **2:1** profit-to-loss | `confirmed` | check sizing honours this |
| 25 | Walk away after losing half the day's profit | `confirmed` | `HUMAN-ONLY` |
| 26 | Max daily loss = average daily gain | `confirmed` | partially — a daily-loss halt exists |
| 27 | Do not return after walking away | `confirmed` | `HUMAN-ONLY` |
| 28 | Level 2 / tape exit signals (big seller, hidden seller, red burst) | `single source` (Gemini) | not implemented; needs L2 data we don't have |

### Headline — the 50-day lookback closes an open question

Ross, verbatim: *"if a stock is trading on five times higher volume than the
**50-day average**, that's the day that I want to be trading on it."*

`get_relative_volume(symbol, days=30)` uses 30. That is not a rounding
difference — it is the mechanism behind **claim #2 from Video 01**, which was
logged as `NEW` and unverified.

A shorter lookback means a recent volume spike carries more weight in the
average. A stock that has popped and rejected a few times therefore shows an
inflated average and an artificially *low* RVOL — exactly what Ross described
when STKH read 4.46. A 50-day window dilutes those spikes.

Two videos, two independent extractions, same defect. Claim #2 moves from
`NEW/unverified` to **confirmed, with a one-line fix.**

### Second finding — the catalyst pillar is stricter than Ross is

*"The only exception that I'll make is that the stock doesn't need to have news
if it's clearly moving really well and is one of the most obvious stocks
today."*

Our catalyst pillar is a hard `n_news > 0`, and catalyst was the **top rejector
at 10 of 14** in the last Stock Golem scan. `MIN_PILLARS = 4` already permits
one failure — but catalyst failing *plus* any unknown (float was unavailable 3x
in that same scan) drops a stock to 3 and rejects it.

So the interaction, not the threshold, is doing the damage.

### Next

- #16 (50-day RVOL) is the cheapest high-value change in this whole document.
- #17 needs care: Ross's exception is qualitative ("most obvious stock today").
  A mechanical stand-in — e.g. top-N by gain *and* RVOL rank — is a `NEW` rule
  of our own invention, not his, and must be labelled that way.
- #18 and #19 are one-line config changes; decide deliberately rather than
  drifting.
- #28 stays single-source and unbuildable — we have no Level 2 feed.
