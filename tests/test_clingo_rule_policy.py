from __future__ import annotations

import re
from pathlib import Path

import pytest


RULES_DIR = Path(__file__).parents[1] / "sqlassert" / "rules"


def _forbidden_constructs(program: str) -> set[str]:
    source = re.sub(r"%[^\n]*", "", program)
    violations = set()

    if re.search(r"(?m)^\s*(?:\d+\s*)?\{", source):
        violations.add("choice rule")
    if re.search(r"(?m)^\s*#(?:minimize|maximize)\b", source):
        violations.add("optimization directive")
    if re.search(r"(?m)^\s*:~", source):
        violations.add("weak constraint")

    for statement in source.split("."):
        head = statement.partition(":-")[0]
        if "|" in head or ";" in head:
            violations.add("disjunctive head")

    return violations


def test_clingo_rule_files_avoid_nondeterministic_constructs():
    for rule_file in RULES_DIR.glob("*.lp"):
        violations = _forbidden_constructs(rule_file.read_text())
        assert not violations, f"{rule_file}: forbidden Clingo constructs: {', '.join(sorted(violations))}"


@pytest.mark.parametrize(
    ("program", "expected"),
    [
        ("{ selected(X) } :- candidate(X).", "choice rule"),
        ("selected(X) | rejected(X) :- candidate(X).", "disjunctive head"),
        ("selected(X); rejected(X) :- candidate(X).", "disjunctive head"),
        ("#minimize { Cost,X : selected(X), cost(X,Cost) }.", "optimization directive"),
        (":~ selected(X), cost(X,Cost). [Cost@1,X]", "weak constraint"),
    ],
)
def test_policy_guard_recognizes_main_forbidden_constructs(program: str, expected: str):
    assert expected in _forbidden_constructs(program)
