"""Traceability (BUILD_PLAN WP9): every P1 requirement and every automated acceptance
criterion is named by at least one test, and the tests name no requirement that does not
exist. Tests name what they cover in their docstrings, ranges allowed ("CAP-1–6",
"AC-2 to AC-7")."""

from __future__ import annotations

import csv
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ID = re.compile(r"\b([A-Z]{2,4})-(\d+)(?:\s*(?:–|-|to)\s*(?:\1-)?(\d+))?")
AUTOMATED_AC = range(2, 15)  # AC-1 (Claude Desktop run) and AC-15 (agent evaluation) are manual


def _requirements() -> list[dict[str, str]]:
    with (ROOT / "docs" / "requirements.csv").open(encoding="utf-8", newline="") as fh:
        return list(csv.DictReader(fh))


def _named_in_tests() -> set[str]:
    found: set[str] = set()
    for path in (ROOT / "tests").rglob("*.py"):
        if path.resolve() == Path(__file__).resolve():
            continue
        for m in ID.finditer(path.read_text(encoding="utf-8")):
            prefix, first, last = m.group(1), int(m.group(2)), m.group(3)
            found.update(f"{prefix}-{k}" for k in range(first, int(last or first) + 1))
    return found


def test_every_p1_requirement_has_a_test() -> None:
    named = _named_in_tests()
    missing = [r["ID"] for r in _requirements() if r["Phase"].strip() == "P1"
               and r["ID"] not in named]
    assert not missing, f"P1 requirements no test names: {missing}"


def test_every_automated_acceptance_criterion_has_a_test() -> None:
    named = _named_in_tests()
    missing = [f"AC-{k}" for k in AUTOMATED_AC if f"AC-{k}" not in named]
    assert not missing, f"Acceptance criteria no test names: {missing}"


def test_tests_name_only_real_requirements() -> None:
    rows = _requirements()
    known = {r["ID"] for r in rows}
    prefixes = {i.split("-")[0] for i in known}
    unknown = sorted(i for i in _named_in_tests()
                     if i.split("-")[0] in prefixes and i not in known)
    assert not unknown, f"Tests name requirements that do not exist: {unknown}"
