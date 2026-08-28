#!/usr/bin/env python3
"""
tools/meta_test.py — Test the test harness.

A simulator that always passes is worse than no simulator: it produces a green
light over untested code, which is the exact failure this whole effort exists
to stop. New verification architecture needs verifying like anything else.

Three questions:
  1. Can a simulation reach the LIVE book?           (must be: no)
  2. Does a genuinely broken action FAIL?            (must be: yes)
  3. Does an unmocked network call get caught?       (must be: yes)

Question 2 is the important one. It's checked by mutation: deliberately break
a real function, confirm the simulator turns red, then restore it. If the
simulator stays green against broken code, every green result it has ever
produced is meaningless.
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SIM = ROOT / "tools" / "simulate.py"

fails = []


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    if detail:
        print(f"          {detail}")
    if not ok:
        fails.append(name)


def run_sim(*args, env=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([sys.executable, str(SIM), *args],
                          cwd=str(ROOT), capture_output=True, text=True,
                          timeout=180, env=e)


print("META-TEST — verifying the simulator\n")

# ─── 1. ISOLATION ─────────────────────────────────────────────────────────────
print("1. Can a simulation touch live state?")

live_files = {}
for name in ("stock_portfolio.json", "kalshi_portfolio.json",
             "kalshi_event_portfolio.json", "fomo_portfolio.json"):
    p = ROOT / name
    live_files[name] = p.stat().st_mtime if p.exists() else None

r = run_sim("--all")
check("simulator ran", r.returncode == 0,
      f"exit {r.returncode}")

untouched = True
for name, before in live_files.items():
    p = ROOT / name
    after = p.stat().st_mtime if p.exists() else None
    if before != after:
        untouched = False
        check(f"{name} untouched", False, f"mtime changed {before} -> {after}")
check("no live portfolio file was written", untouched)

check("state redirected to a temp dir", "/tmp/sim_" in r.stdout or "sim_" in r.stdout,
      [l for l in r.stdout.splitlines() if "redirected" in l][:1])

# ─── 2. MUTATION — does a broken action actually fail? ────────────────────────
print("\n2. Does a genuinely broken action FAIL the simulation?")

target = ROOT / "fomo_exit.py"
# BYTES, not text. read_text/write_text applies universal-newline translation:
# a CRLF file read on Linux comes back with LF and is written back as LF, so
# "restoring" the original rewrote all 999 lines and left the file permanently
# modified in git. A harness that mutates real source MUST restore it
# byte-identically or it is itself a source of changes.
original = target.read_bytes()

# Reintroduce the exact bug that shipped: flag the tranche even when the sale
# aborts. If the simulator is real, tranche_aborts must go red.
# Match both LF and CRLF so the mutation works regardless of checkout style.
_nl = b"\r\n" if b"\r\n" in original else b"\n"
_anchor = b"    def _abort(why: str) -> float:" + _nl + b"        log.error("
_mutant = (b"    def _abort(why: str) -> float:" + _nl
           + b"        holding['tranche_1_sold'] = True   # MUTATION" + _nl
           + b"        log.error(")
broken = original.replace(_anchor, _mutant, 1)

if broken == original:
    check("mutation applied", False, "anchor not found — meta-test is stale")
else:
    try:
        target.write_bytes(broken)
        rb = run_sim("tranche_aborts")
        caught = rb.returncode != 0 and "FAIL" in rb.stdout
        check("simulator catches the reintroduced tranche bug", caught,
              "exit %d" % rb.returncode)
    finally:
        target.write_bytes(original)

    rr = run_sim("tranche_aborts")
    check("restored file passes again", rr.returncode == 0,
          f"exit {rr.returncode}")
    # The restore must be byte-identical, or the harness silently dirties the
    # working tree on every run.
    check("file restored byte-for-byte", target.read_bytes() == original,
          f"{len(target.read_bytes())} vs {len(original)} bytes")

# ─── 3. NETWORK HERMETICITY ───────────────────────────────────────────────────
print("\n3. Is an unmocked network call caught?")

# Write the probe to a TEMP dir, not the repo. The sandbox cannot delete
# files inside the mounted project folder (Operation not permitted), so a
# scratch file written there is permanent litter — which is exactly the
# accumulation problem generated tests are supposed to avoid.
probe = Path(tempfile.gettempdir()) / "_hermetic_probe.py"
probe.write_text(
    # ROOT is resolved at runtime — hardcoding a path would only work on
    # the machine that generated it.
    f"import sys\n"
    f"sys.path.insert(0, {str(ROOT)!r})\n"
    f"sys.argv = ['simulate.py', '--list']\n"
    f"import importlib.util\n"
    f"spec = importlib.util.spec_from_file_location('sim', {str(SIM)!r})\n"
    f"m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
    f"import requests\n"
    f"try:\n"
    f"    requests.get('https://example.com')\n"
    f"    print('NOT_BLOCKED')\n"
    f"except AssertionError:\n"
    f"    print('BLOCKED')\n",
    encoding="utf-8")
try:
    rp = subprocess.run([sys.executable, str(probe)], cwd=str(ROOT),
                        capture_output=True, text=True, timeout=60)
    check("a real HTTP call raises instead of going out",
          "BLOCKED" in rp.stdout, rp.stdout.strip()[-60:] or rp.stderr.strip()[-60:])
finally:
    probe.unlink(missing_ok=True)

# ─── VERDICT ──────────────────────────────────────────────────────────────────
print()
if fails:
    print(f"META-TEST FAILED: {', '.join(fails)}")
    print("The simulator cannot be trusted until these pass.")
    sys.exit(len(fails))
print("META-TEST CLEAN — the simulator is isolated, catches real bugs, "
      "and cannot reach the network.")
sys.exit(0)
