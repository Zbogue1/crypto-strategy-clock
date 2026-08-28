#!/usr/bin/env python3
"""
tools/install_hooks.py — install the pre-push gate. Run once per clone.

    python3 tools/install_hooks.py

Git hooks live in .git/hooks/, which is NOT version controlled — so a hook
committed to the repo does nothing until it's copied into place. That's why
this installer exists rather than just a file in tools/hooks/.

Verifies the install afterwards instead of assuming it worked: on Windows the
executable bit is meaningless, and a hook that isn't run is indistinguishable
from a hook that passes.
"""

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tools" / "hooks" / "pre-push"


def main() -> int:
    try:
        hooks_dir = Path(subprocess.run(
            ["git", "rev-parse", "--git-path", "hooks"],
            cwd=str(ROOT), capture_output=True, text=True, check=True
        ).stdout.strip())
    except Exception as e:
        print(f"Could not locate .git/hooks: {e}")
        return 1

    if not hooks_dir.is_absolute():
        hooks_dir = ROOT / hooks_dir
    hooks_dir.mkdir(parents=True, exist_ok=True)
    dest = hooks_dir / "pre-push"

    if not SRC.exists():
        print(f"Missing {SRC}")
        return 1

    if dest.exists():
        print(f"  existing hook found at {dest}")
        print("  backing it up to pre-push.bak")
        shutil.copy2(dest, dest.with_suffix(".bak"))

    shutil.copy2(SRC, dest)
    # chmod is a no-op on Windows but required everywhere else.
    dest.chmod(dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    print(f"  installed -> {dest}")

    # Verify rather than assume. A hook that silently isn't executed looks
    # exactly like a hook that passes.
    ok = dest.exists() and dest.read_text(encoding="utf-8").strip().startswith("#!")
    print(f"  readable and has a shebang: {ok}")
    if not ok:
        print("  INSTALL LOOKS WRONG — check the file manually.")
        return 1

    print()
    print("Done. `git push` now runs preflight first and refuses on a blocking")
    print("result. Bypass deliberately with `git push --no-verify`.")
    print()
    print("Test it without pushing anything:")
    print("    git push --dry-run origin master")
    return 0


if __name__ == "__main__":
    sys.exit(main())
