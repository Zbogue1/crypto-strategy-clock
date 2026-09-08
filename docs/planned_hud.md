# Planned — JARVIS HUD

Design notes. Started 2026-09-08. `/state` on FOMO is built; everything else is
pending.

## The core requirement

A **live candlestick chart of what Stock Golem is actually doing** — not a
summary of it. Every entry and exit drawn on the bars they happened on.

This is the headline feature, and the reason is diagnostic rather than
decorative:

> "that would also let me know if there is a bug like the one we caught earlier
> with the candlesticks closing out before they even completed."

That is exactly right, and it is the strongest argument for building it.

### The bug this would have caught on sight

`check_exit_signals` read `bars[-1]` — the bar still forming — and the monitor
polls every 20s. On an unfinished candle the body is near zero, so any upper
wick clears `upper >= 2 * body` (topping tail), and `h > prev_high and
c < prev_high` is the normal state of a candle mid-breakout (false breakout).
Both are severity "high", and any high signal closes the position.

Stock Golem would have exited nearly every trade within a minute of entry. On a
P&L report that reads as a bad strategy. **On a chart it is unmissable** — you
would see the exit marker land in the middle of a candle that then closed green.

It was found by reasoning about the code. A chart would have shown it in one
glance, which is the whole case for this feature.

## What has to be on the chart

Minimum for the diagnostic purpose:

- 1-minute candles for the traded symbol, the session around the trade
- **Entry marker** — price and exact bar
- **Exit marker** — price, exact bar, and the REASON (`stop_loss`, `target`,
  `topping_tail`, `false_breakout`, `volume_decay`, `max_hold`, `eod`)
- Stop and target as horizontal lines from entry
- VWAP and the 9 EMA overlaid — these are the pullback validity rules
  (`detect_pullback` rejects on losing either), so seeing them makes a rejection
  legible
- Volume bars — the surge-vs-pullback comparison is a pullback condition

**The one non-obvious requirement:** each marker must show whether the bar it
fired on was **closed or still forming** at the moment of the decision. That is
the specific thing that makes the forming-candle class of bug visible instead of
inferable. Without it the chart looks fine and the bug stays hidden.

## Also worth drawing: the trades that did NOT happen

Stock Golem's problem has not been bad trades, it has been **zero** trades.
Charting candidates that cleared the 5 Pillars and then failed `detect_pullback`
would show whether the pullback gate is too strict or whether no setup was
forming. The cumulative funnel gives counts; a chart would give the reason.

## Architecture — what is already known

**`/state` exists on FOMO Golem** (`fomo_tracker.py`), token-protected via
`HUD_TOKEN`, read-only, fails closed when the token is unset, returns 404 rather
than 401 so a scanner learns nothing.

**Each bot is a separate Railway service with its own Upstash database.** This
was learned the hard way: the first `/state` imported `kalshi_portfolio` and
`stock_portfolio` from inside FOMO's process, which found FOMO's Redis, missed
their keys, and **constructed fresh default books that it served as fact** —
Kalshi events reading $1,000 against a real $79.10, Stock reading $2,000 against
a real $10,000. Every value plausible, every value false.

So: **each service must report its own book.** Kalshi and Stock have no web
server (they are Telegram-poll loops), so each needs a small HTTP thread added.
The HUD then polls three URLs and merges.

## Voice

The HUD is a web page, so Chrome supplies both directions free —
`SpeechRecognition` to listen, `SpeechSynthesis` to speak. No Python voice
assistant and no ElevenLabs bill.

Split the intents:

- **Queries — unrestricted.** "What's my P&L." "What is Stock Golem doing."
  "Why did nothing trade today." Read-only, no confirmation.
- **Actions — voice PROPOSES, the human CONFIRMS.** The codebase already has the
  right pattern in `create_pending_buy_alert`: a message with buttons that must
  be pressed. Voice feeds that same mechanism and never executes on a heard word.

A misheard command that moves money is the same failure class as the tranche
flag, the forming candle and the silent halt: something happened that nobody
confirmed.

## Build order

1. `/state` on Kalshi and Stock (each needs an HTTP thread)
2. Candlestick view for Stock Golem, with entry/exit markers and the
   closed-vs-forming flag — **the feature that pays for the rest**
3. Fork [stark-systems](https://github.com/jarvis-openclaw-assistant/stark-systems)
   for the HUD shell; bind its panels to the three `/state` feeds
4. Voice queries
5. Voice proposals into the pending-confirmation flow

## Hard rules

- Read-only endpoints. No route that opens or closes a position, ever.
- Token-protected, failing closed.
- The chart must never invent data. If a bar or a marker is missing, show a gap
  and say so — a dashboard that fabricates is worse than no dashboard, which the
  first `/state` demonstrated within a minute of going live.
