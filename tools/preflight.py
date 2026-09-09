#!/usr/bin/env python3
"""
tools/preflight.py — Run this BEFORE every push. Non-zero exit means don't.

WHY THIS EXISTS
On 2026-08-26 roughly six bugs were introduced *while fixing other bugs*:

  · a crash-loop alarm that fired on normal deploys
  · a screener diagnostic printing +0% because it read `percent_change`
    when the producer returns `pct`
  · "test isolation" that set an env var the module never read, so every test
    wrote to the live file
  · a funnel counting `pass: None` (unknown) as a rejection, because
    `not None` is True
  · a stripped `global` declaration that silently made a setter a no-op
  · a claim in a diagnostic that was simply wrong about its own data

None were caught by compiling. None were caught by reading. Every one was
caught by RUNNING something and comparing the result to what was expected —
or not caught at all until it produced a confusing number days later.

So this is deliberately behavioural, not a linter. Each check below maps to a
bug that actually shipped.

USAGE
    PAPER_TEST_MODE=1 python3 tools/preflight.py

Exit code is the number of blocking findings.
"""

import ast
import builtins
import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
ACTIVE = ("kalshi_", "fomo_", "stock_", "vision", "reconcile")

BLOCKING: list = []
WARNING: list = []

# ─── ACKNOWLEDGED FALSE POSITIVES ─────────────────────────────────────────────
# (kind, file, line) -> why it is safe. Checked, not guessed.
#
# WHY THIS EXISTS. These three printed on EVERY push for a full session and were
# scrolled past every time — by both of us. A warning you learn to skip is worse
# than no warning, because it hides the real one that arrives beside it. Same
# failure as an alarm firing four times a night: the noise trains you to ignore
# the signal.
#
# Anything listed here was verified by reading the code, and the reason is
# recorded so a future reader can re-check rather than re-trust. If a line
# number drifts, the entry stops matching and the warning comes back — which is
# the correct behaviour, since the code it was about has moved.
ACKNOWLEDGED = {
    ("TRI-STATE", "kalshi_tracker.py", 1248):
        "evaluate() has 7 returns and every one sets 'trade' to an explicit "
        "True/False. None is not reachable, so `not x.get('trade')` is safe.",
    ("TRI-STATE", "kalshi_tracker.py", 468):
        "deposit() has exactly two returns, both with an explicit 'ok'. "
        "None is not reachable.",
    # Line moved 2608 -> 2629 when real_wallets() was added above it. The
    # acknowledgement is line-anchored on purpose, so it stopped matching and
    # the warning came back for re-verification — which is the point.
    ("KEY-MISMATCH", "fomo_tracker.py", 2629):
        "Second half of `get('liquidity_usd') or get('liquidity') or 0` — the "
        "correct key is read first and this is a defensive fallback. Also sits "
        "inside the Golem independent path, disabled 2026-09-08.",
}

SILENCED: list = []


def block(kind, where, msg):
    BLOCKING.append((kind, where, msg))


def warn(kind, where, msg):
    # `where` looks like "kalshi_tracker.py:1248"
    fname, _, lineno = str(where).partition(":")
    key = (kind, fname, int(lineno) if lineno.isdigit() else -1)
    if key in ACKNOWLEDGED:
        SILENCED.append((kind, where, ACKNOWLEDGED[key]))
        return
    WARNING.append((kind, where, msg))


# ─── 1. STRIPPED `global` ─────────────────────────────────────────────────────

def check_stripped_global(path: pathlib.Path, tree: ast.AST):
    """
    A function that assigns to a module-level name without declaring `global`
    creates a local and silently does nothing.

    This is how _save_funnel stopped persisting: an edit removed the `global`
    line, the function still "worked", and the value never left the function.
    Compiles fine, reads fine, does nothing.
    """
    module_names = {
        t.id
        for n in tree.body if isinstance(n, (ast.Assign, ast.AnnAssign))
        for t in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets)
        if isinstance(t, ast.Name)
    }
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        declared = {
            name for n in ast.walk(fn) if isinstance(n, ast.Global)
            for name in n.names
        }
        params = {a.arg for a in fn.args.args + fn.args.kwonlyargs}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if (isinstance(t, ast.Name) and t.id in module_names
                            and t.id not in declared and t.id not in params
                            and not t.id.isupper()):
                        block("STRIPPED-GLOBAL", f"{path.name}:{t.lineno}",
                              f"{fn.name}() assigns module var '{t.id}' without "
                              f"`global` — the write is discarded")


# ─── 2. PRODUCER / CONSUMER KEY MISMATCH ──────────────────────────────────────

def collect_produced_keys(tree: ast.AST) -> dict:
    """function name -> set of dict keys it returns in a literal."""
    out = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # If ANY return is not a dict literal (json.loads, a variable, a
        # helper call) then the shape is unknown at parse time and every
        # .get() on it would be reported as a mismatch. vision.extract()
        # returns json.loads(...) and produced six false positives that way.
        returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return) and n.value]
        if any(not isinstance(r.value, ast.Dict) for r in returns):
            continue

        keys = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
                for k in n.value.keys:
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        keys.add(k.value)
            # list comprehensions returning dicts, e.g. [{...} for x in y]
            if isinstance(n, ast.Dict) and isinstance(
                    getattr(n, "parent", None), ast.ListComp):
                pass
        if keys:
            out[fn.name] = keys
    return out


def check_key_mismatch(files: dict):
    """
    A consumer .get()ing a key its producer never returns.

    get_movers() returns {"symbol","pct","price","change"} and the diagnostic
    read m.get("percent_change") — the raw Alpaca field name it renames. Every
    gainer printed +0% while the filter behind it worked correctly.
    """
    # Scope producers PER FILE. Every module has its own _load()/_save(), and
    # merging them globally made fomo_aftermath._load() resolve to
    # kalshi_postmortem's, producing a page of false mismatches. An audit that
    # cries wolf gets ignored, which is the failure mode it exists to prevent.
    per_file = {path: collect_produced_keys(tree) for path, tree in files.items()}

    for path, tree in files.items():
        produced = per_file[path]
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            origin = {}   # var name -> producer fn name
            for n in ast.walk(fn):
                if (isinstance(n, ast.Assign) and len(n.targets) == 1
                        and isinstance(n.targets[0], ast.Name)
                        and isinstance(n.value, ast.Call)):
                    f = n.value.func
                    name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
                    if name in produced:
                        origin[n.targets[0].id] = name
                # for x in producer(): -> x has the element shape, skip
            for n in ast.walk(fn):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and n.func.attr == "get" and n.args
                        and isinstance(n.func.value, ast.Name)
                        and isinstance(n.args[0], ast.Constant)):
                    var = n.func.value.id
                    key = n.args[0].value
                    src = origin.get(var)
                    if src and isinstance(key, str) and key not in produced[src]:
                        warn("KEY-MISMATCH", f"{path.name}:{n.lineno}",
                             f"{var}.get({key!r}) but {src}() returns "
                             f"{sorted(produced[src])[:6]}")


# ─── 3. TRI-STATE TRUTHINESS ──────────────────────────────────────────────────

TRI_STATE = {"pass", "ok", "settled", "trade", "won", "valid", "readable",
             "qualifies", "available"}


def check_tristate(path: pathlib.Path, tree: ast.AST):
    """
    `not x.get("pass")` is True for False AND for None.

    The scan funnel counted every unavailable float lookup as a rejection this
    way, which pointed the investigation at a threshold that was never the
    problem.
    """
    for n in ast.walk(tree):
        if not (isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not)):
            continue
        inner = n.operand
        if (isinstance(inner, ast.Call) and isinstance(inner.func, ast.Attribute)
                and inner.func.attr == "get" and len(inner.args) == 1
                and isinstance(inner.args[0], ast.Constant)
                and inner.args[0].value in TRI_STATE):
            warn("TRI-STATE", f"{path.name}:{n.lineno}",
                 f'not X.get("{inner.args[0].value}") — None and False both '
                 f'match; use `is False` if they differ')


# ─── 4. HARDCODED VALUE BESIDE A COMPUTED ONE ─────────────────────────────────

def check_hardcoded_pct(path: pathlib.Path, src: str):
    """
    A literal percentage in an f-string that also formats a computed price.

    The FOMO buy confirmation printed "Stop: {computed} (-15%)" while the stop
    was computed from -35%, understating the risk on every trade by 20 points.
    """
    import re
    for i, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if re.search(r"f[\"'].*\{[^}]+\}.*\(-?\+?\d{1,3}%\)", line):
            block("HARDCODED-PCT", f"{path.name}:{i}",
                  f"literal % beside a computed value: {stripped[:60]}")


# ─── 5. STATE WRITE WHOSE RESULT IS IGNORED ───────────────────────────────────

PERSIST = {"_redis_set", "_push_named"}


def check_unchecked_persist(path: pathlib.Path, tree: ast.AST):
    """
    Redis is the only durable store; the local file is wiped on redeploy. A
    discarded write result means a lost trade that looked like a save.
    """
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            f = n.value.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in PERSIST:
                block("UNCHECKED-WRITE", f"{path.name}:{n.lineno}",
                      f"{name}() result discarded — a failed write is invisible")


# ─── 6. BEHAVIOURAL SUITES ────────────────────────────────────────────────────

def run_suite(label: str, script: str) -> bool:
    p = ROOT / "tools" / script
    if not p.exists():
        warn("MISSING-SUITE", script, "not found")
        return True
    r = subprocess.run([sys.executable, str(p)], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        block("SUITE-FAILED", script,
              (r.stdout + r.stderr).strip().splitlines()[-1][:100]
              if (r.stdout or r.stderr) else "non-zero exit")
        return False
    return True


def main() -> int:
    py = sorted(ROOT.glob("*.py"))
    files = {}

    print("PREFLIGHT\n")

    # compile everything first — nothing else is meaningful if it won't parse
    bad = 0
    for f in py:
        try:
            src = f.read_text(encoding="utf-8")
            files[f] = ast.parse(src)
        except SyntaxError as e:
            block("SYNTAX", f"{f.name}:{e.lineno}", str(e)[:70])
            bad += 1
    print(f"  parsed {len(files)}/{len(py)} files"
          + (f"  ({bad} SYNTAX ERRORS)" if bad else ""))
    if bad:
        return report()

    for f, tree in files.items():
        if not f.name.startswith(ACTIVE):
            continue
        src = f.read_text(encoding="utf-8")
        check_stripped_global(f, tree)
        check_tristate(f, tree)
        check_hardcoded_pct(f, src)
        check_unchecked_persist(f, tree)
    check_key_mismatch({f: t for f, t in files.items()
                        if f.name.startswith(ACTIVE)})
    print("  static checks done")

    check_imports([f for f in files if f.name.startswith(ACTIVE)])
    print("  import check done")

    run_suite("sell audit", "sell_audit.py")
    run_suite("health scan", "health_scan.py")
    # Fire the real actions, not just inspect the code. This is what catches a
    # change that compiles and reads correctly but behaves wrongly.
    run_suite("action simulation", "simulate.py")
    # And verify the simulator itself still catches a planted bug — a harness
    # that always passes is worse than none.
    run_suite("meta test", "meta_test.py")
    # Prove every scenario can fail, not just the one the meta-test
    # mutates. A scenario asserting something trivially true would stay
    # green forever and contribute nothing.
    run_suite("mutation coverage", "mutation_coverage.py")
    print("  behavioural suites done")

    return report()


# ─── 6. DOES IT ACTUALLY IMPORT? ──────────────────────────────────────────────

def check_imports(paths) -> None:
    """
    Import every active module in a subprocess and BLOCK if any fails.

    Every other check here reads the code. `ast.parse` proves a file is valid
    Python; it proves nothing about whether it runs. A module-level NameError, a
    bad `from x import y`, a decorator that raises — all parse perfectly, pass
    every static check, and die the moment Railway starts the service.

    That gap is real: a full session of edits touched module scope in six files
    and preflight would have reported CLEAN on a build that could not boot.

    Runs in a subprocess so a module with side effects at import time cannot
    contaminate this process, with PAPER_TEST_MODE forced so nothing can write
    to a live book while merely being imported.

    Third-party packages absent from THIS machine but present on Railway
    (anthropic, flask) are stubbed — their absence is an environment fact, not a
    defect in our code, and blocking on it would make preflight unrunnable
    locally.
    """
    names = sorted({p.stem for p in paths})
    probe = (
        "import sys, types, os\n"
        "os.environ['PAPER_TEST_MODE'] = '1'\n"
        "for _n, _a in (('anthropic', ('Anthropic',)), ):\n"
        "    _m = types.ModuleType(_n)\n"
        "    _m.Anthropic = lambda **k: None\n"
        "    _m.APIStatusError = Exception; _m.APIError = Exception\n"
        "    sys.modules.setdefault(_n, _m)\n"
        "if 'flask' not in sys.modules:\n"
        "    _f = types.ModuleType('flask')\n"
        "    class _App:\n"
        "        def __init__(s, *a, **k): pass\n"
        "        def route(s, *a, **k): return lambda fn: fn\n"
        "        def run(s, *a, **k): pass\n"
        "        def add_url_rule(s, *a, **k): pass\n"
        "    _f.Flask = _App\n"
        "    _f.request = types.SimpleNamespace(json=None, args={}, headers={},"
        " get_json=lambda *a, **k: None)\n"
        "    _f.jsonify = lambda *a, **k: (a[0] if len(a) == 1 else dict(**k))\n"
        "    _f.Response = lambda *a, **k: None\n"
        "    sys.modules['flask'] = _f\n"
        "import traceback, os.path\n"
        "_seen = set()\n"
        f"for _name in {names!r}:\n"
        "    try:\n"
        "        __import__(_name)\n"
        "    except Exception as _e:\n"
        "        # Blame the module where the traceback ORIGINATED, not the one\n"
        "        # that happened to import it first. A single NameError in\n"
        "        # stock_signals surfaced as three failures — stock_backtest,\n"
        "        # stock_signals, stock_tracker — with the innocent importer\n"
        "        # listed first. Three alarms for one bug, pointing at the\n"
        "        # wrong file, is how a real finding gets skimmed past.\n"
        "        _tb = _e.__traceback__\n"
        "        _origin = _name\n"
        "        while _tb is not None:\n"
        "            _fn = _tb.tb_frame.f_code.co_filename\n"
        "            if _fn.endswith('.py') and os.path.basename(_fn) != '<string>':\n"
        "                _origin = os.path.splitext(os.path.basename(_fn))[0]\n"
        "            _tb = _tb.tb_next\n"
        "        _key = (_origin, type(_e).__name__, str(_e))\n"
        "        if _key in _seen:\n"
        "            continue\n"
        "        _seen.add(_key)\n"
        "        _via = '' if _origin == _name else f' (reached via {_name})'\n"
        "        print(f'IMPORTFAIL {_origin} {type(_e).__name__}: {_e}{_via}')\n"
    )
    try:
        r = subprocess.run([sys.executable, "-B", "-c", probe], cwd=str(ROOT),
                           capture_output=True, text=True, timeout=180,
                           env={**os.environ, "PAPER_TEST_MODE": "1",
                                "PYTHONDONTWRITEBYTECODE": "1"})
    except Exception as e:                       # noqa: BLE001
        block("IMPORT-CHECK", "preflight", f"could not run the import probe: {e}")
        return

    for line in (r.stdout + r.stderr).splitlines():
        if line.startswith("IMPORTFAIL "):
            _, mod, rest = line.split(" ", 2)
            block("IMPORT-FAILED", f"{mod}.py",
                  f"{rest[:110]} — parses fine, will not start")


def report() -> int:
    print()
    # Say how many were silenced, never hide the fact. A suppression list you
    # cannot see is indistinguishable from a check that stopped running.
    if SILENCED:
        print(f"({len(SILENCED)} acknowledged false positive(s) suppressed — "
              f"see ACKNOWLEDGED in tools/preflight.py)")
        print()

    if not BLOCKING and not WARNING:
        print("CLEAN — safe to push.")
        return 0

    if WARNING:
        print(f"WARNINGS ({len(WARNING)}) — review, not blocking:")
        for k, w, m in WARNING[:12]:
            print(f"   {k:<16} {w:<28} {m}")
        print()

    if BLOCKING:
        print(f"BLOCKING ({len(BLOCKING)}) — fix before pushing:")
        for k, w, m in BLOCKING:
            print(f"   {k:<16} {w:<28} {m}")
        print()
        print("Each of these is a bug class that already shipped once.")
    else:
        print("No blocking findings — safe to push.")
    return len(BLOCKING)


if __name__ == "__main__":
    sys.exit(main())
