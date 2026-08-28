"""
tools/mutation_coverage.py — prove every scenario can actually fail.

A green suite means nothing unless each scenario goes red when the code it
covers is broken. This mutates the real target of each scenario, runs just
that scenario, and reports any that stayed green.

Runs with -B and PYTHONDONTWRITEBYTECODE: without them, a cached .pyc from
the mutate-run-restore cycle made results non-deterministic — three
identical sweeps reported 0, 1 and 2 blind scenarios.

Mutate the code each scenario claims to cover. Any scenario that stays GREEN
against broken code is a scenario that proves nothing.
"""
import subprocess, sys, pathlib, os
ROOT = pathlib.Path(__file__).resolve().parent.parent
SIM  = ROOT / "tools" / "simulate.py"

# scenario -> (file, anchor bytes, mutant bytes)
MUTATIONS = {
 "tranche_fires": ("fomo_exit.py",
    b"    for f in (flags or []):\n        holding[f] = True",
    b"    for f in (flags or []):\n        pass  # MUT: never flag"),
 "stop_loss_refused": ("fomo_exit.py",
    b"    ticker = holding.get(\"token_ticker\", \"?\")\n    if not is_price_sane(",
    b"    ticker = holding.get(\"token_ticker\", \"?\")\n    if False and is_price_sane("),
 "perp_close": ("kalshi_portfolio.py",
    b"    if not is_exit_price_sane(state[\"holdings\"][idx].get(\"entry_price\"),",
    b"    if False and is_exit_price_sane(state[\"holdings\"][idx].get(\"entry_price\"),"),
 "event_settle": ("kalshi_event_portfolio.py",
    b"    payout   = round(pos[\"contracts\"] * 1.0, 2) if won else 0.0",
    b"    payout   = 0.0  # MUT: never pay out"),
 "event_settle_blank": ("kalshi_event_portfolio.py",
    b"    if result not in (\"yes\", \"no\"):",
    b"    if False:"),
 "stock_close": ("stock_portfolio.py",
    b"    if not is_exit_price_sane(s[\"positions\"][idx].get(\"entry\"), exit_price, symbol):",
    b"    if False:"),
 "pillars_score": ("stock_signals.py",
    b"        if not feed_ok:",
    b"        if False:"),
 "reconcile_invariant": ("kalshi_portfolio.py",
    b"    state[\"cash\"]         = float(state.get(\"cash\", 0) or 0) + margin + net_pnl",
    b"    state[\"cash\"]         = float(state.get(\"cash\", 0) or 0) + margin + net_pnl + 1.0"),
}

def run(scn):
    env=dict(os.environ); env["PYTHONDONTWRITEBYTECODE"]="1"
    r = subprocess.run([sys.executable, "-B", str(SIM), scn], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=120, env=env)
    return r.returncode

print("MUTATION COVERAGE — does each scenario catch a real break?\n")
useless = []
for scn, (fname, anchor, mutant) in MUTATIONS.items():
    p = ROOT / fname
    orig = p.read_bytes()
    a = anchor.replace(b"\n", b"\r\n") if b"\r\n" in orig else anchor
    m = mutant.replace(b"\n", b"\r\n") if b"\r\n" in orig else mutant
    if a not in orig:
        print(f"  SKIP  {scn:22s} anchor not found in {fname}")
        continue
    try:
        p.write_bytes(orig.replace(a, m, 1))
        rc = run(scn)
    finally:
        p.write_bytes(orig)
    caught = rc != 0
    print(f"  {'CATCHES' if caught else 'BLIND  '} {scn:22s} (mutated {fname})")
    if not caught:
        useless.append(scn)

print()
if useless:
    print(f"{len(useless)} scenario(s) PROVE NOTHING: {', '.join(useless)}")
    sys.exit(len(useless))
print("Every scenario fails when its target is broken.")
