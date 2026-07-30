#!/usr/bin/env python3
"""Run every standalone contract test in an isolated subprocess.

The project intentionally keeps executable contract tests for now. This runner
provides deterministic discovery, process isolation, and one CI-friendly exit
code without relying on pytest importing stateful modules into one process.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent


def main() -> int:
    tests = sorted(ROOT.glob("test_*.py"))
    if not tests:
        print("no tests discovered", file=sys.stderr)
        return 1

    failures = []
    with tempfile.TemporaryDirectory(prefix="tradingcli-tests-") as directory:
        for test in tests:
            env = os.environ.copy()
            env["PAPERTRADE_DB"] = str(Path(directory) / f"{test.stem}.db")
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            completed = subprocess.run(
                [sys.executable, str(test)],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                timeout=120,
                check=False,
            )
            status = "PASS" if completed.returncode == 0 else "FAIL"
            print(f"{status} {test.name}")
            if completed.returncode:
                failures.append(test.name)
                if completed.stdout:
                    print(completed.stdout.rstrip())
                if completed.stderr:
                    print(completed.stderr.rstrip(), file=sys.stderr)

    print(f"{len(tests) - len(failures)}/{len(tests)} tests passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
