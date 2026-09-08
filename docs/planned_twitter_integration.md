# Planned — Twitter/CT signal for FOMO Golem

Parked 2026-09-08. Not started.

## The gap

Every rug-guard message prints `🐦 CT: Twitter not configured`, and the model's
own commentary keeps asking for the signal it doesn't have:

> "human conviction signals (tracked wallets, CT chatter, narrative thesis) are
> completely absent here, making this a pure data point with zero cultural alpha"

> "Wait for CT narrative to emerge or a REAL Tier A wallet to enter"

So the research layer is already reasoning about CT chatter as a factor and
scoring its absence against the token. Supplying it is a real feature, not a
nice-to-have.

## Chosen approach

**[Twikit](https://github.com/d60/twikit)** — `pip install twikit`, no API key.

```python
tweets = await client.search_tweet('$TICKER', 'Latest')
```

Logs in once and writes `cookies.json`; afterwards it loads cookies instead of
credentials. That matters because Railway containers have no browser — log in on
a local machine, then ship the cookie contents as a Railway variable and never
put a password on the server.

Fallback if it breaks: **[Scweet](https://github.com/Altimis/Scweet)** — same
idea, adds multi-account pooling and proxy support, verified against X's
GraphQL API March 2026.

Official X API search starts around $200/month, which is why every free option
is a scraper.

## Hard requirements

1. **Throwaway X account. Never the real one.** Both libraries scrape X's
   internal API, which breaks X's ToS. The practical risk is not legal, it is
   that scraping accounts get suspended. A burner also removes the "live session
   credentials to your own account" problem entirely.

2. **Optional enrichment, never a gate.** X auth churns — Twikit's README
   currently points at a separate login repo marked "under development". Any
   failure must return "unavailable" and let the rug guard proceed with the
   signal absent, exactly as it does today. A dead CT lookup must never block or
   approve a trade.

3. **Distinguish "no chatter" from "lookup failed."** Same discipline as
   `NEWS_FEED_OK` in stock_data.py and the float-failure fix: an empty result and
   a broken client look identical and mean opposite things.

## Shape

`fomo_twitter.py` — takes a ticker, returns mention count plus a few recent
texts and a status flag. Every path wrapped so nothing raises. Then
`fomo_research.py` swaps the hardcoded "not configured" line for real data.

Roughly an hour, most of it on the failure paths rather than the happy one.

## Also installed, 2026-09-08

`agent-reach` 1.5.0 (Panniantong/Agent-Reach, MIT) — safe-mode install, no
system changes. 5/15 channels active, and only two matter here: Jina Reader for
arbitrary web pages, and YouTube subtitles (already covered better by
`claude-video`, which also extracts frames). Twitter there needs the same
cookies and the same burner-account caveat, so it offers no shortcut.
