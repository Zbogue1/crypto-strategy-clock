# Deploying the Crypto Strategy Clock — Run Without Your Computer

The agent runs on a free cloud server and emails you the HTML dashboard after each run.
Your computer does **not** need to be on.

---

## What You'll Need

1. A **"sender" Gmail** — create a free one just for this (e.g. `cryptobot.zach@gmail.com`)
2. Your **Anthropic API key** (already set up)
3. Your **FRED API key** (free at fred.stlouisfed.org — optional but recommended)
4. A **Railway** account (free, no credit card required for the free tier)

---

## Step 1 — Create a Gmail App Password

Your "sender" Gmail needs an App Password so the agent can log in without 2FA prompts.

1. Go to myaccount.google.com → Security → 2-Step Verification (enable it if not on)
2. After enabling, go to myaccount.google.com/apppasswords
3. Name it "Crypto Clock" → click Create
4. Copy the 16-character password (looks like: `abcd efgh ijkl mnop`)
5. Save it — you'll only see it once

---

## Step 2 — Deploy to Railway (Recommended — Free)

Railway gives you a free container that runs 24/7.

### 2a — Push your files to GitHub

1. Go to github.com → New repository → Name it `crypto-strategy-clock` → Private → Create
2. On your computer, open Command Prompt in your project folder:

```
cd "C:\Users\Zachg\Claude\Projects\Investment Strategy Clock"
git init
git add crypto_oracle_v3.py crystal_ball_memory.py position_manager.py requirements.txt
git commit -m "Initial deploy"
git remote add origin https://github.com/YOUR-USERNAME/crypto-strategy-clock.git
git push -u origin main
```

### 2b — Deploy on Railway

1. Go to railway.app → Log in with GitHub
2. Click **New Project** → **Deploy from GitHub repo**
3. Select `crypto-strategy-clock`
4. Railway auto-detects Python and installs `requirements.txt`

### 2c — Set Environment Variables on Railway

In your Railway project → **Variables** tab, add:

| Variable | Value |
|---|---|
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `FRED_API_KEY` | your FRED key (or leave blank) |
| `ALERT_EMAIL` | cryptobot.zach@gmail.com |
| `ALERT_EMAIL_PASSWORD` | your 16-char App Password |
| `ALERT_EMAIL_TO` | zachgreenwell@live.com |

### 2d — Set the Start Command

In Railway → **Settings** → **Deploy** → **Start Command**:

```
python crypto_oracle_v3.py --once
```

### 2e — Add a Cron Schedule

In Railway → **Settings** → **Cron Schedule**:

```
0 */4 * * *
```

This runs every 4 hours (12:00 AM, 4:00 AM, 8:00 AM, 12:00 PM, 4:00 PM, 8:00 PM UTC).
The agent checks for day trading opportunities 6 times per day and emails you the report each time.

That's it. Railway will run the agent daily and your inbox will get the report.

---

## Alternative: PythonAnywhere (Also Free)

If you prefer PythonAnywhere:

1. Sign up at pythonanywhere.com (free tier)
2. Go to **Files** → upload `crypto_oracle_v3.py`, `crystal_ball_memory.py`, `position_manager.py`, `requirements.txt`
3. Open a **Bash console** and run:
   ```
   pip install -r requirements.txt --user
   ```
4. Go to **Tasks** → Add a new scheduled task:
   - Command: `python /home/YOUR-USERNAME/crypto_oracle_v3.py --once`
   - Hour: pick your preferred time
5. Set environment variables in your `.bashrc`:
   ```
   export ANTHROPIC_API_KEY="sk-ant-..."
   export ALERT_EMAIL="cryptobot.zach@gmail.com"
   export ALERT_EMAIL_PASSWORD="abcd efgh ijkl mnop"
   export ALERT_EMAIL_TO="zachgreenwell@live.com"
   ```

**Note:** PythonAnywhere free tier restricts outbound HTTP to a whitelist.
CoinGecko, Reddit, and most RSS feeds are whitelisted. Messari may not be.
If you hit whitelist errors, upgrade to the $5/month Hacker plan.

---

## What Happens Every 4 Hours

1. Agent wakes up on Railway (6 times per day)
2. Checks open position for stop-loss or take-profit triggers — acts immediately
3. Scans top 20 cryptos, deep-dives the best 5 candidates
4. Reads news from CoinDesk, CoinTelegraph, Blockworks, CryptoSlate
5. Reads Reddit sentiment from r/CryptoCurrency, r/Bitcoin, r/ethereum
6. Sends everything to Claude for synthesis
7. Auto-executes a virtual trade if signal is strong enough (BUY/SELL)
8. Emails you the full HTML dashboard with current portfolio P&L → **zachgreenwell@live.com**
9. Logs the prediction for later evaluation (the learning loop)

You just open your email to see what it did. No computer needed.

---

## Checking Logs on Railway

Railway → your project → **Deployments** → click the latest run → **View Logs**

You'll see the full output of each run, including any errors.

---

## Paper Trading Commands (run locally anytime)

```
# See current portfolio balance and open position
python crypto_oracle_v3.py --paper-status

# Reset portfolio back to $1,000 (start fresh)
python crypto_oracle_v3.py --reset-paper

# Run one cycle manually (also executes any pending trades)
python crypto_oracle_v3.py --once
```

---

## Quick Reference — Cron Time Zones

| Your Time | UTC Cron |
|---|---|
| 7 AM EST | `0 12 * * *` |
| 8 AM EST | `0 13 * * *` |
| 9 AM EST | `0 14 * * *` |
| 7 AM PST | `0 15 * * *` |
| 8 AM PST | `0 16 * * *` |
