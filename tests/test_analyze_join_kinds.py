"""Uniqueness proof is sound only for INNER and LEFT equi-joins: those are
the only kinds where the right side's Unique Set says anything about how many
rows the join produces per left row. RIGHT, FULL, SEMI, ANTI, and CROSS joins
must never inherit that proof, even when the joined relation has every
property that would prove an INNER or LEFT join -- a PRIMARY KEY on the very
column the join predicate uses.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze

USERS_AND_SESSIONS = """
CREATE TABLE users (id INTEGER PRIMARY KEY);
CREATE TABLE sessions (user_id INTEGER);
"""


@pytest.mark.parametrize(
    ("label", "join_and_predicate"),
    [
        ("right", "RIGHT JOIN users ON sessions.user_id = users.id"),
        ("full", "FULL JOIN users ON sessions.user_id = users.id"),
        ("full outer", "FULL OUTER JOIN users ON sessions.user_id = users.id"),
        ("cross", "CROSS JOIN users"),
        ("semi", "SEMI JOIN users ON sessions.user_id = users.id"),
        ("left semi", "LEFT SEMI JOIN users ON sessions.user_id = users.id"),
        ("anti", "ANTI JOIN users ON sessions.user_id = users.id"),
        ("left anti", "LEFT ANTI JOIN users ON sessions.user_id = users.id"),
        ("right semi", "RIGHT SEMI JOIN users ON sessions.user_id = users.id"),
        ("right anti", "RIGHT ANTI JOIN users ON sessions.user_id = users.id"),
    ],
)
def test_unsupported_join_kinds_stay_unknown_even_against_a_primary_key(
    label: str, join_and_predicate: str
):
    report = analyze(
        f"""
        {USERS_AND_SESSIONS}
        SELECT * FROM sessions /**UNIQUE**/ {join_and_predicate}
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN], label
    assert not report.diagnostics, label
    assert report.proved is False, label


@pytest.mark.parametrize("join", ["JOIN", "INNER JOIN", "LEFT JOIN", "LEFT OUTER JOIN"])
def test_supported_join_kinds_are_proved_against_the_same_primary_key(join: str):
    """The control case: the exact same schema and predicate as the unsupported
    kinds above, proved for the kinds the engine does support."""
    report = analyze(
        f"""
        {USERS_AND_SESSIONS}
        SELECT * FROM sessions /**UNIQUE**/ {join} users ON sessions.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.proved is True
