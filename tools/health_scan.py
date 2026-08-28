#!/usr/bin/env python3
"""
tools/health_scan.py — Static self-check across the trading bots.

WHY THIS LIVES IN THE REPO
These checks were written ad-hoc in a scratch directory during a debugging
session and every one of them found a real, shipped bug:

  · catalyst_data referenced but never defined — every relayed BUY signal
    raised NameError after creating the alert, so the button never sent.
  · `raw_text if "raw_text" in dir() else ""` — dir() inside a function lists
    locals, so research received an empty string on every relayed signal.
  · reconcile.py emitting HTML through a sender that only strips Markdown, so
    users saw literal <b> tags.
  · fomo_tracker.send_telegram() called with a parse_mode it did not accept.

A scratch directory gets wiped. Checks that find bugs belong in the repo where
they can run on a schedule.

WHAT IT CHECKS
  UNDEFINED    names used but never bound in any enclosing scope
  NO-TIMEOUT   requests calls that can hang forever
  FMT-NONE     numeric format specs applied to .get() with no default
  HTML-IN-MD   HTML tags in modules whose sender defaults to Markdown
  MOJIBAKE     double-encoded UTF-8 left in source
  DEAD-IMPORT  functions imported but never called (how the FOMO time-exit
               and the whole event scanner sat dead for days)

Exit code is the number of findings, so a scheduler can alert on non-zero.
"""

import ast
import builtins
import re
import sys
from pathlib import Path

BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
}
ACTIVE_PREFIXES = ("kalshi_", "fomo_", "stock_", "vision", "reconcile")

# Types and helpers whose import is routinely wider than their use — flagging
# an unused `timedelta` is noise, and noise is how a checker gets ignored.
# The signal we want is a FUNCTION that was imported to be called and isn't:
# check_fomo_auto_exits sat imported-and-uncalled for days, which is why the
# FOMO time-exit never ran.
# Reviewed and accepted — kept so the scan can exit 0 and gate a push.
# Anything NOT listed here is a genuine finding.
ACCEPTED_DEAD = {
    ("kalshi_tracker.py", "format_portfolio_telegram"),
    ("kalshi_tracker.py", "score_all_markets"),
}

IGNORE_UNUSED = {
    "datetime", "timezone", "timedelta", "date", "time",
    "Optional", "Any", "Callable", "Union", "Dict", "List", "Tuple",
    "dataclass", "field", "annotations", "Path", "defaultdict", "Enum",
}


class Scope:
    def __init__(self, parent=None):
        self.names, self.parent = set(), parent

    def has(self, n):
        s = self
        while s:
            if n in s.names:
                return True
            s = s.parent
        return False


class Binder(ast.NodeVisitor):
    """Collect every name bound in one scope."""
    def __init__(self, scope):
        self.s = scope

    def visit_FunctionDef(self, n):
        self.s.names.add(n.name)
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, n):
        self.s.names.add(n.name)

    def visit_Lambda(self, n):
        pass

    def visit_Import(self, n):
        for a in n.names:
            self.s.names.add((a.asname or a.name).split(".")[0])

    def visit_ImportFrom(self, n):
        for a in n.names:
            self.s.names.add(a.asname or a.name)

    def visit_Name(self, n):
        if isinstance(n.ctx, (ast.Store, ast.Del)):
            self.s.names.add(n.id)
        self.generic_visit(n)

    def visit_arg(self, n):
        self.s.names.add(n.arg)

    def visit_ExceptHandler(self, n):
        if n.name:
            self.s.names.add(n.name)
        self.generic_visit(n)

    def visit_Global(self, n):
        for x in n.names:
            self.s.names.add(x)
    visit_Nonlocal = visit_Global


def bind(node, scope):
    b = Binder(scope)
    for child in ast.iter_child_nodes(node):
        b.visit(child)


class Checker(ast.NodeVisitor):
    def __init__(self, scope):
        self.s, self.found = scope, []

    def _sub(self, node, args=()):
        sc = Scope(self.s)
        for a in args:
            sc.names.add(a)
        bind(node, sc)
        old, self.s = self.s, sc
        for ch in ast.iter_child_nodes(node):
            self.visit(ch)
        self.s = old

    def visit_FunctionDef(self, n):
        for d in n.decorator_list:
            self.visit(d)
        a = n.args
        args = [x.arg for x in a.args + a.kwonlyargs + a.posonlyargs]
        if a.vararg:
            args.append(a.vararg.arg)
        if a.kwarg:
            args.append(a.kwarg.arg)
        for d in a.defaults + [x for x in a.kw_defaults if x]:
            self.visit(d)
        self._sub(n, args)
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, n):
        self._sub(n, [x.arg for x in n.args.args + n.args.kwonlyargs])

    def visit_ClassDef(self, n):
        for d in n.decorator_list + n.bases:
            self.visit(d)
        self._sub(n)

    def visit_Name(self, n):
        if (isinstance(n.ctx, ast.Load) and n.id not in BUILTINS
                and not self.s.has(n.id)):
            self.found.append((n.lineno, n.id))


def scan(root: Path) -> list:
    findings = []

    for f in sorted(root.glob("*.py")):
        try:
            src = f.read_text(encoding="utf-8")
            tree = ast.parse(src)
        except SyntaxError as e:
            findings.append(("SYNTAX", f.name, e.lineno or 0, str(e)))
            continue

        root_scope = Scope()
        bind(tree, root_scope)
        ck = Checker(root_scope)
        for c in ast.iter_child_nodes(tree):
            ck.visit(c)
        seen = set()
        for ln, name in ck.found:
            if name in seen:
                continue
            seen.add(name)
            findings.append(("UNDEFINED", f.name, ln,
                             f"{name} used but never bound"))

        if not f.name.startswith(ACTIVE_PREFIXES):
            continue

        lines = src.splitlines()
        imported_names = set()

        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                base = n.func.value.id if isinstance(n.func.value, ast.Name) else ""
                if base == "requests" and n.func.attr in ("get", "post", "put", "delete"):
                    if "timeout" not in {k.arg for k in n.keywords}:
                        findings.append(("NO-TIMEOUT", f.name, n.lineno,
                                         f"requests.{n.func.attr} can hang forever"))

            if isinstance(n, ast.FormattedValue) and n.format_spec is not None:
                spec = ast.unparse(n.format_spec)
                v = n.value
                if (re.search(r"[fd,%]", spec) and isinstance(v, ast.Call)
                        and isinstance(v.func, ast.Attribute)
                        and v.func.attr == "get" and len(v.args) == 1):
                    findings.append(("FMT-NONE", f.name, n.lineno,
                                     f"{ast.unparse(v)}:{spec} — None breaks this"))

            # Module-level `from x import y` where y is never called.
            if isinstance(n, ast.ImportFrom) and n.col_offset == 0:
                for a in n.names:
                    imported_names.add(a.asname or a.name)

        called = {
            (n.func.id if isinstance(n.func, ast.Name)
             else n.func.attr if isinstance(n.func, ast.Attribute) else "")
            for n in ast.walk(tree) if isinstance(n, ast.Call)
        }
        referenced = {n.id for n in ast.walk(tree)
                      if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        for name in sorted(imported_names):
            if (name[0].isupper() or name.startswith("_")
                    or name in IGNORE_UNUSED):
                continue
            if (f.name, name) in ACCEPTED_DEAD:
                continue
            if name not in called and name not in referenced:
                findings.append(("DEAD-IMPORT", f.name, 0,
                                 f"{name} imported but never called — "
                                 f"is it supposed to run?"))

        for i, ln in enumerate(lines, 1):
            if "Ã" in ln or "â€" in ln:
                findings.append(("MOJIBAKE", f.name, i, ln.strip()[:60]))
            if (f.name.startswith(("kalshi_", "stock_"))
                    and re.search(r"</?(b|i|code|pre)>", ln)
                    and "strip_html" not in ln):
                findings.append(("HTML-IN-MD", f.name, i, ln.strip()[:60]))

    return findings


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent.parent
    findings = scan(root)

    if not findings:
        print("HEALTH SCAN: clean — no findings.")
        return 0

    print(f"HEALTH SCAN: {len(findings)} finding(s)\n")
    by_kind = {}
    for kind, fn, ln, msg in findings:
        by_kind.setdefault(kind, []).append((fn, ln, msg))
    for kind in sorted(by_kind):
        print(f"{kind}  ({len(by_kind[kind])})")
        for fn, ln, msg in by_kind[kind]:
            loc = f"{fn}:{ln}" if ln else fn
            print(f"   {loc:<34} {msg}")
        print()
    return len(findings)


if __name__ == "__main__":
    sys.exit(main())
