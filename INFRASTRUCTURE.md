# Railway Infrastructure Map

> Written 2026-08-21. Two Railway projects contain services with **identical
> names**, which has repeatedly caused variables to be added to the wrong place.
> Check this file before touching any Railway setting.

---

## The confusing part, stated plainly

There are **two different services both called `crypto-strategy-clock`**, in two
different projects. They are unrelated. Adding a variable to the wrong one does
nothing and looks like the code is broken.

| | Project `precious-reprieve` | Project `Crypto Strategy Clock` |
|---|---|---|
| Contains | `crypto-strategy-clock`, `btc-scalper` | `desirable-insight`, `crypto-strategy-clock` |
| Which is live | `crypto-strategy-clock` (Online) | `desirable-insight` (Online) |

---

## Service directory

### 🟢 KALSHI GOLEM — the live one
- **Project:** `precious-reprieve`
- **Service:** `crypto-strategy-clock`
- **Status:** Online (long-running process)
- **Runs:** `kalshi_tracker.py`
- **Telegram bot:** the dedicated Kalshi bot (`KALSHI_TELEGRAM_TOKEN`)
- **Key variables:** `FORCE_KALSHI`, `KALSHI_DEFAULT_MARGIN`, `KALSHI_TELEGRAM_TOKEN`,
  `KALSHI_USE_DEMO`, `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- **⚠️ MISSING:** `UPSTASH_REDIS_URL`, `UPSTASH_REDIS_TOKEN`
  → without these, **all trade history is wiped on every redeploy**

**→ Add Kalshi variables HERE.**

---

### 🟢 FOMO GOLEM — the live one
- **Project:** `Crypto Strategy Clock`
- **Service:** `desirable-insight`
- **Status:** Online (Flask server, port 8080)
- **Runs:** `fomo_tracker.py`
- **Persistence:** GitHub `data` branch (not Redis)
- **Key variables:** `FOMO_DEPOSIT_TO`, `FOMO_REPAIR_CASH`, `TELEGRAM_BOT_TOKEN`,
  `GITHUB_TOKEN`, `GMAIL_*`, `HELIUS_*`, `ALCHEMY_*`, `PARSE_BOT_API_KEY`
- **Routing:** currently matched by service NAME in `start.sh`
  → **do not rename** unless `FORCE_FOMO=true` is added first

**→ Add FOMO variables HERE.**

---

### 🔴 DUPLICATE KALSHI — should be removed
- **Project:** `Crypto Strategy Clock`
- **Service:** `crypto-strategy-clock`
- **Status:** Cron, runs every ~2 hours
- **Problem:** has `FORCE_KALSHI=true`, so it launches a **second Kalshi bot**
  that scans and opens paper positions alongside the real one
- **Also:** this is where `UPSTASH_REDIS_URL` / `UPSTASH_REDIS_TOKEN` were
  mistakenly added — they belong on the precious-reprieve service
- **Action:** delete the service, or clear `FORCE_KALSHI` and its cron schedule

---

### ⚫ BTC SCALPER — retired
- **Project:** `precious-reprieve`
- **Service:** `btc-scalper`
- **Status:** Offline (deployment removed 2026-08-21)
- Code preserved in repo; not running.

---

## Quick reference: where does a variable go?

| Variable prefix | Project | Service |
|---|---|---|
| `KALSHI_*`, `UPSTASH_REDIS_*` | `precious-reprieve` | `crypto-strategy-clock` |
| `FOMO_*`, `GMAIL_*`, `HELIUS_*`, `ALCHEMY_*` | `Crypto Strategy Clock` | `desirable-insight` |

---

## How to confirm which service you're in

The Kalshi bot reports its own location. Send `/health` in Telegram:

```
📍 This bot is running in:
   Project: precious-reprieve
   Service: crypto-strategy-clock
```

Startup logs print the same thing. Trust that over the Railway UI, since the
names are ambiguous.

---

## Known traps

1. **Two `crypto-strategy-clock` services.** Always check the project name in
   Railway's top-left dropdown before editing variables.
2. **Branch mismatch.** `desirable-insight` was connected to `main` while pushes
   went to `master`, so it ran stale code for hours. Both should be on `master`.
3. **Name-based routing.** `start.sh` matches `desirable-insight` by name. Renaming
   that service without adding `FORCE_FOMO=true` first will start the wrong bot.
4. **Upstash variable naming.** Upstash's dashboard exports
   `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN`. The code now accepts
   those, the short forms, and the Vercel `KV_REST_API_*` names.

---

## Persistence, per system

| System | Store | Failure mode |
|---|---|---|
| Kalshi | Upstash Redis | No Redis vars → history lost on redeploy |
| FOMO | GitHub `data` branch | Local save without push → lost on restart |

Kalshi has `/health` to verify storage, `/trades` for the full audit ledger, and
`/archives` to restore a previous snapshot.
