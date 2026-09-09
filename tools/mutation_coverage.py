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

 # ── Added 2026-09-08. Coverage was 8 of 24 scenarios: everything built in one
 # long session was hand-mutated once and never again. Hand verification is
 # true at the moment it is done and decays silently — if an edit later stops a
 # scenario catching anything, only this file would notice.
 #
 # These five were chosen by what a silent regression would COST, not by
 # scenario order. The remainder are listed in UNCOVERED below.

 # Exits judging an unfinished candle closed every position within a minute of
 # entry. The single most expensive bug found in that session.
 "forming_candle_no_exit": ("stock_signals.py",
    b"    bars = [b for b in bars if _bar_closed(b, timeframe_sec)]",
    b"    bars = list(bars)  # MUT: judge forming candles again"),

 # A broken spread filter fails in both directions: reject every candidate, or
 # reject none and trade names that cannot be exited cleanly.
 "spread_filter": ("stock_signals.py",
    b"    wide_spread = spread_ok is False",
    b"    wide_spread = False  # MUT: spread never disqualifies"),

 # Demotion that costs nothing is how a 0%-win-rate wallet kept full size.
 "tier_b_halves_size": ("fomo_tracker.py",
    b'    return "B", "wallet not on the watchlist"',
    b'    return "A", "wallet not on the watchlist"  # MUT: unknown = trusted'),

 # This exception is OUR rule, not Ross's. If it stops being bounded it becomes
 # "catalyst optional", which is not what he says at all.
 "obvious_mover_exception": ("stock_signals.py",
    b"            obvious = (pct is not None and pct >= OBVIOUS_PCT",
    b"            obvious = True or (pct is not None and pct >= OBVIOUS_PCT"),

 # A token check that fails OPEN publishes the whole portfolio on a public URL.
 "hud_state_endpoint": ("fomo_tracker.py",
    b"    if not HUD_TOKEN:\n        return False",
    b"    if not HUD_TOKEN:\n        return True  # MUT: open to everyone"),

 # Placeholder wallets eating the API error budget silently disabled
 # auto-removal — a safety feature switched off by arithmetic.
 "revet_error_budget": ("fomo_tracker.py",
    b"    return [w for w in (wallets or [])\n            if w.get(\"wallet\")",
    b"    return list(wallets or [])  # MUT: placeholders back in\n    return [w for w in (wallets or [])\n            if w.get(\"wallet\")"),

 # The original sin: flagging a tranche as harvested when the sale ABORTED.
 # $MADE was marked sold while still holding 100% of its units. This scenario
 # existed the whole time and was in NEITHER list — the coverage report said
 # "13 covered, 10 uncovered" out of 24, and nobody noticed the arithmetic.
 "tranche_aborts": ("fomo_exit.py",
    b"    def _abort(why: str) -> float:\n        log.error(",
    b"    def _abort(why: str) -> float:\n        holding['tranche_1_sold'] = True  # MUT\n        log.error("),
}

# Scenarios with NO automated mutation yet. Each was hand-mutated when written,
# so it is verified as of 2026-09-08 — but that verification does not repeat.
# A regression in any of these would go unnoticed by this tool.
#
# Ordered by consequence, worst first:
UNCOVERED = [
    "decision_snapshot",        # evidence capture; a silent break loses the
                                # only record of why a trade happened
    "float_failure_retries",    # a throttled lookup cached as "no float" for a
                                # day suppresses qualification invisibly
    "underfunded_book_alarms",  # a halted book going quiet again
    "fresh_token_dropped_early",# wasted research spend on pre-rejected tokens
    "report_covers_both_books", # a losing book hidden from the headline
    "no_bet_price_display",     # inverted entry price corrupts calibration
    "funnel_accumulates",       # diagnostics that misattribute rejections
    "rvol_lookback_50d",        # threshold drift back to 30 days
    "circuit_breakers",         # pre-existing, never covered
    "position_sizing",          # pre-existing, never covered
]

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

# ─── ACCOUNT FOR EVERY SCENARIO ───────────────────────────────────────────────
# Read the real scenario list instead of trusting the two lists above.
#
# The first version of this report printed "13 covered, 10 uncovered" against a
# suite of 24, and the missing one was `tranche_aborts` — the scenario guarding
# the most expensive bug this project has had. A hand-maintained inventory
# drifts the moment anyone adds a scenario, and a coverage report with a hole in
# its own accounting is the exact failure it exists to prevent.
#
# So: derive the truth, and fail loudly on anything unaccounted for.
unaccounted = []
try:
    sys.path.insert(0, str(ROOT / "tools"))
    import importlib.util
    _spec = importlib.util.spec_from_file_location("_sim_probe", SIM)
    _mod = importlib.util.module_from_spec(_spec)
    _argv, sys.argv = sys.argv, ["simulate.py", "--list"]
    os.environ["PAPER_TEST_MODE"] = "1"
    try:
        _spec.loader.exec_module(_mod)
    except SystemExit:
        pass
    finally:
        sys.argv = _argv
    every = set(_mod.SCENARIOS)
    unaccounted = sorted(every - set(MUTATIONS) - set(UNCOVERED))
    ghosts = sorted((set(MUTATIONS) | set(UNCOVERED)) - every)
    print(f"COVERAGE: {len(MUTATIONS)} of {len(every)} scenario(s) have an "
          f"automated mutation.")
    if ghosts:
        print(f"          {len(ghosts)} listed scenario(s) no longer exist: "
              f"{', '.join(ghosts)}")
except Exception as e:                      # noqa: BLE001
    print(f"COVERAGE: {len(MUTATIONS)} scenario(s) covered "
          f"(could not read the full list: {e})")

if UNCOVERED:
    print(f"          {len(UNCOVERED)} known-uncovered — hand-verified once, "
          f"not re-checked:")
    for s in UNCOVERED:
        print(f"            - {s}")
    print("          A regression in those is invisible to this tool.")

if unaccounted:
    print()
    print(f"  UNACCOUNTED: {len(unaccounted)} scenario(s) are in NEITHER list —")
    print(f"  neither covered nor knowingly skipped: {', '.join(unaccounted)}")
    print("  A scenario nobody has decided about is worse than one skipped on")
    print("  purpose. Add a mutation, or add it to UNCOVERED deliberately.")

print()
if useless:
    print(f"{len(useless)} scenario(s) PROVE NOTHING: {', '.join(useless)}")
    sys.exit(len(useless))
if unaccounted:
    # Non-zero exit so preflight blocks on it. An unaccounted scenario is a
    # silent hole in the one report whose job is finding silent holes.
    print(f"{len(unaccounted)} scenario(s) UNACCOUNTED FOR — decide about them.")
    sys.exit(len(unaccounted))
print("Every COVERED scenario fails when its target is broken, and every "
      "scenario is accounted for.")
