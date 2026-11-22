"""Shared test harness. No pytest dependency: plain pass/fail lines and an exit code.

Importing this module also puts the repository root on sys.path and exposes the
pipeline as `P`, so test modules stay three lines of boilerplate:

    from _harness import P, check, check_eq, finish
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _env  # noqa: E402,F401  (fake tool paths; must precede config use)

import pipeline as P  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition) -> None:
    print(("PASS  " if condition else "FAIL  ") + label)
    if not condition:
        FAILURES.append(label)


def check_eq(label: str, got, expected) -> None:
    ok = got == expected
    if not ok:
        FAILURES.append(label)
    print(f"{'PASS  ' if ok else 'FAIL  '}{label}: got={got!r} expected={expected!r}")


def finish() -> None:
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): " + "; ".join(FAILURES))
        raise SystemExit(1)
    print("\nALL GREEN")
