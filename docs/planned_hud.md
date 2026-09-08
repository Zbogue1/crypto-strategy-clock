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

## Decision snapshots — a stored chart at every buy and sell

A saved chart of the moment of each buy and sell, with the action marked on the
bar it happened on. Purpose, in the user's words: intelligence on whether the
agent is making sense, a way to surface unknown bugs, and data the system
improves from.

### Store the DATA, not a screenshot

A PNG can only be looked at. The same information as JSON can be re-rendered
with different overlays later, and — more importantly — **queried**:

- every exit that fired on a bar that had not closed yet
- every entry where the pullback retrace was near the 50% limit
- the distribution of exit reasons across all trades
- every trade taken via the **obvious-mover catalyst exception** — that rule is
  ours, not Ross's, so isolating those trades is the only way to learn whether
  our invention helped or hurt

None of that is possible with an image. Store bars plus decision context, render
the chart on demand.

### Snapshot AT DECISION TIME. Never re-fetch.

This is the load-bearing requirement and it is easy to get wrong.

If the chart is rebuilt later by re-fetching bars from Alpaca, the bar that was
still forming when the decision fired **is now closed** — and the evidence of a
forming-candle bug is destroyed by the reconstruction. The snapshot has to
record the exact window the decision function was handed, as it was handed it.

Capture, at the moment of the call:

- the bar window passed to `detect_pullback` / `check_exit_signals`, capped to
  roughly ±60 bars around the decision rather than the whole session
- **whether the newest bar had closed**, computed the same way `_bar_closed()`
  computes it
- the decision and its reason
- entry / stop / target, and VWAP and 9 EMA at that instant
- the pillar scores, and whether the catalyst passed via the exception

### It extends `stock_postmortem`, it is not a new system

`log_entry()` already stores the full belief-state at entry — position,
snapshot, pillars, pullback, review. What is missing:

1. **The bars.** Without them no chart can be drawn, and the belief-state cannot
   be checked against what the market was actually doing.
2. **The exit side.** `log_outcome()` writes the result back but not the
   bar window or the state at the moment of exit — which is precisely where the
   forming-candle bug lives.
3. **The closed-vs-forming flag** on the newest bar.

### Honest note on "improves over time"

This creates a corpus for review. It is not automatic learning — nothing gets
better until someone or something analyses it and changes a rule. What it does
provide is the ability to ask questions that are unanswerable today, and to
answer them from evidence rather than by re-reading code. Every bug found
tonight was found that way.

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

1. **Decision snapshots** — extend `stock_postmortem` to capture the bar window
   and the closed-vs-forming flag at entry AND exit. Cheap, needs no HUD, and
   starts accumulating evidence from the very next trade. Do this first: every
   session without it is a session whose trades can never be reviewed properly.
2. `/state` on Kalshi and Stock (each needs an HTTP thread)
3. Candlestick view for Stock Golem, rendered from the snapshots — with entry
   and exit markers and the closed-vs-forming flag
4. Fork [stark-systems](https://github.com/jarvis-openclaw-assistant/stark-systems)
   for the HUD shell; bind its panels to the three `/state` feeds
5. Voice queries
6. Voice proposals into the pending-confirmation flow

Note the reordering: snapshots come before the HUD. The chart is how you *look*
at the data, but the data has to exist first, and it can only be captured while
the trade is happening. A missed snapshot cannot be reconstructed afterwards.

## Hard rules

- Read-only endpoints. No route that opens or closes a position, ever.
- Token-protected, failing closed.
- The chart must never invent data. If a bar or a marker is missing, show a gap
  and say so — a dashboard that fabricates is worse than no dashboard, which the
  first `/state` demonstrated within a minute of going live.
