"""A Unique Join Assertion through every join kind.

A left row appears once per matching right row, or once padded, whatever the
kind: RIGHT and FULL joins pad right rows on but never repeat a left row more
than its matches do, so the right side's Unique Set proves them as it proves
INNER and LEFT joins. A CROSS join is an ON with no conjuncts: unique only when
the right input has at most one row. SEMI and ANTI joins keep or drop each
left row whole; the same proof applies to them, unchanged.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze

USERS_AND_SESSIONS = """
CREATE TABLE users (id INTEGER PRIMARY KEY);
CREATE TABLE sessions (user_id INTEGER);
"""


def outcomes(sql: str) -> list[Outcome]:
    report = analyze(USERS_AND_SESSIONS + sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


@pytest.mark.parametrize(
    "join",
    [
        "JOIN users ON sessions.user_id = users.id",
        "INNER JOIN users ON sessions.user_id = users.id",
        "LEFT JOIN users ON sessions.user_id = users.id",
        "LEFT OUTER JOIN users ON sessions.user_id = users.id",
        "RIGHT JOIN users ON sessions.user_id = users.id",
        "FULL JOIN users ON sessions.user_id = users.id",
        "FULL OUTER JOIN users ON sessions.user_id = users.id",
        "CROSS JOIN (SELECT count(*) AS n FROM users) AS c",
        "SEMI JOIN users ON sessions.user_id = users.id",
        "ANTI JOIN users ON sessions.user_id = users.id",
    ],
)
def test_a_join_against_a_primary_key_is_proved_whatever_its_kind(join: str):
    assert outcomes(f"SELECT * FROM sessions /**ASSERT UNIQUE**/ {join}") == [Outcome.PROVED]


@pytest.mark.parametrize(
    ("label", "join"),
    [
        ("right without a key", "RIGHT JOIN sessions AS s2 ON sessions.user_id = s2.user_id"),
        ("full without a key", "FULL JOIN sessions AS s2 ON sessions.user_id = s2.user_id"),
        ("cross onto many rows", "CROSS JOIN users"),
        ("positional", "POSITIONAL JOIN users"),
        ("right semi", "RIGHT SEMI JOIN users ON sessions.user_id = users.id"),
        ("right anti", "RIGHT ANTI JOIN users ON sessions.user_id = users.id"),
    ],
)
def test_unproved_joins_stay_unknown(label: str, join: str):
    assert outcomes(f"SELECT * FROM sessions /**ASSERT UNIQUE**/ {join}") == [Outcome.UNKNOWN], label


def test_a_cross_join_onto_one_row_keeps_the_left_key():
    sql = "SELECT u.id FROM users AS u CROSS JOIN (SELECT count(*) AS n FROM sessions) AS c /**ASSERT UNIQUE(id)**/"
    assert outcomes(sql) == [Outcome.PROVED]


def test_a_right_join_does_not_keep_the_left_key():
    # Right rows nothing matched share one padded (NULL) left row.
    sql = "SELECT u.id, s.user_id FROM users AS u RIGHT JOIN sessions AS s ON u.id = s.user_id /**ASSERT UNIQUE(id)**/"
    assert outcomes(sql) == [Outcome.UNKNOWN]


def test_a_positional_join_pads_both_sides():
    # A FULL JOIN on row position: the shorter input is padded with NULLs.
    sql = """
    CREATE TABLE a (x INTEGER NOT NULL);
    CREATE TABLE b (y INTEGER NOT NULL);
    SELECT x, y FROM a POSITIONAL JOIN b /**ASSERT NOT NULL(x)**/ /**ASSERT NOT NULL(y)**/
    """
    assert outcomes(sql) == [Outcome.UNKNOWN, Outcome.UNKNOWN]
