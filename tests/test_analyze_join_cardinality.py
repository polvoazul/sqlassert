"""Join cardinality markers, read in FROM order: the left input is everything
before the join, the right its joined source.

`MANY TO ONE` repeats no left row, `ONE TO MANY` no right row, `ONE TO ONE`
neither. `UNIQUE` (or `NOT MANY TO MANY`) is their disjunction: the join
repeats rows of at most one input.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze

SCHEMA = """
CREATE TABLE customers (id INTEGER PRIMARY KEY);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER);
CREATE TABLE payments (customer_id INTEGER);
CREATE TABLE profiles (customer_id INTEGER PRIMARY KEY);
"""

# (left, right, ON): right unique only, left unique only, both, neither.
RIGHT_ONLY = ("orders", "customers", "orders.customer_id = customers.id")
LEFT_ONLY = ("customers", "orders", "orders.customer_id = customers.id")
BOTH = ("customers", "profiles", "profiles.customer_id = customers.id")
NEITHER = ("orders", "payments", "payments.customer_id = orders.customer_id")


def outcome(marker: str, case: tuple[str, str, str], join: str = "JOIN") -> Outcome:
    left, right, on = case
    report = analyze(SCHEMA + f"SELECT * FROM {left} /**ASSERT {marker}**/ {join} {right} ON {on}")
    assert not report.diagnostics, report.diagnostics
    assertion, = report.assertions
    return assertion.outcome


@pytest.mark.parametrize("marker", ["UNIQUE", "NOT MANY TO MANY"])
@pytest.mark.parametrize(
    ("case", "expected"),
    [(RIGHT_ONLY, Outcome.PROVED), (LEFT_ONLY, Outcome.PROVED), (BOTH, Outcome.PROVED), (NEITHER, Outcome.UNKNOWN)],
    ids=["right only", "left only", "both", "neither"],
)
def test_not_many_to_many_holds_when_either_input_is_unique(marker: str, case, expected: Outcome):
    assert outcome(marker, case) is expected


@pytest.mark.parametrize(
    ("marker", "case", "expected"),
    [
        ("MANY TO ONE", RIGHT_ONLY, Outcome.PROVED),
        ("MANY TO ONE", LEFT_ONLY, Outcome.UNKNOWN),
        ("ONE TO MANY", LEFT_ONLY, Outcome.PROVED),
        ("ONE TO MANY", RIGHT_ONLY, Outcome.UNKNOWN),
        ("ONE TO ONE", BOTH, Outcome.PROVED),
        ("ONE TO ONE", LEFT_ONLY, Outcome.UNKNOWN),
        ("ONE TO ONE", RIGHT_ONLY, Outcome.UNKNOWN),
    ],
)
def test_each_direction_is_proved_from_its_own_input(marker: str, case, expected: Outcome):
    assert outcome(marker, case) is expected


@pytest.mark.parametrize("join", ["LEFT JOIN", "RIGHT JOIN", "FULL JOIN"])
def test_padding_never_repeats_a_right_row(join: str):
    # Each right row appears once per matching left row, or once padded.
    assert outcome("ONE TO MANY", LEFT_ONLY, join) is Outcome.PROVED
