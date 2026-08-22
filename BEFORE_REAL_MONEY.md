# Before Deploying Real Capital

> Written 2026-08-21. Both bots currently assume **perfect execution**. Paper
> results will overstate real-world performance until the items below are built.
> This is the single biggest gap between "the paper account is up" and "this
> would have made money."

---

## 1. Neither bot models slippage or execution lag

### Kalshi Golem

- The paper engine fills at the **quoted price with zero slippage**.
- Some perp markets are extremely thin. On 2026-08-21 a ZEC position was opened
  on a market with **$235 of 24H volume** against a **$250 notional** order.
  A real order that size moves the price against itself materially.
- Golem's own reasoning flagged this at the time ("thin liquidity means price
  can whip both ways fast, and a small seller could trigger a stop-run") — but
  the paper engine charged nothing for it.
- **Funding** is applied on a naive 8-hour timer, not at Kalshi's actual
  settlement times, so carry costs are approximate.

### FOMO Golem

- Entries use the **DexScreener price at signal time**.
- Signals arrive via Solscan email with **5-15 minutes of lag**.
- So the bot systematically buys *later* than the wallet it's copying, at a
  worse price — and paper trading never charges it for that.
- On fast memecoin moves this gap is large and **always unfavourable**. It is
  the single most common reason copy-trading strategies fail in practice.

---

## 2. What to build

- [ ] Log the **bid/ask spread and available liquidity at entry**, not just mid price
- [ ] Log the **delta between the source wallet's tx timestamp and our entry**
- [ ] Apply an estimated slippage cost as a function of order size vs market depth
- [ ] Show **"assumed fill" P&L vs "slippage-adjusted" P&L** side by side in `/report`
- [ ] Add a minimum-liquidity floor for Kalshi perps (the ZEC market would fail it)

The side-by-side comparison is the important one. It converts an invisible
assumption into a number you can look at.

---

## 3. Why this is deferred, not ignored

As of 2026-08-21 there is almost no closed-trade data:

| System | Closed trades |
|---|---|
| Kalshi | 0 |
| FOMO | 3 (all from a single wallet, all losses) |

Measuring execution cost is pointless until we know whether the **directional
calls** are any good. If call accuracy turns out to be a coin flip, slippage is
irrelevant — the strategy fails regardless.

**Trigger to build this:** once `/report` shows a meaningful call-accuracy
sample (roughly 30+ closed conviction groups) AND that accuracy looks like a
real edge. At that point slippage becomes the deciding factor between a
paper edge and a real one.

---

## 4. Related known gaps

- **Kalshi paper positions never fail to fill.** Real Kalshi orders can sit
  unfilled or partially fill. The engine assumes instant, complete fills.
- **FOMO exit prices** come from the same lagged DexScreener source, so exits
  are optimistic in the same direction as entries.
- **No fee modelling on Kalshi.** Kalshi charges per-trade; a ~3-point edge is
  roughly break-even after fees. The `/ask` analyst accounts for this in its
  reasoning, but the paper portfolio does not deduct it.
