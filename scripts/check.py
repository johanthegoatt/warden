"""Runs both test suites. Installed as the git pre-commit hook by `python scripts/check.py --install`."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd, cwd, env=None) -> bool:
    print(f"$ {' '.join(cmd)}  (in {cwd.relative_to(ROOT) if cwd != ROOT else '.'})", flush=True)
    return subprocess.run(cmd, cwd=cwd, env=env).returncode == 0


def main() -> int:
    if "--install" in sys.argv:
        hook = ROOT / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexec python scripts/check.py\n", encoding="utf-8")
        print(f"installed {hook}")
        return 0
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(["." , "tests"])}
    ok = run(["cargo", "test", "--offline", "--quiet"], ROOT / "sensor")
    ok = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], ROOT / "brain", env) and ok
    print("all tests passed" if ok else "tests failed; nothing committed")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
