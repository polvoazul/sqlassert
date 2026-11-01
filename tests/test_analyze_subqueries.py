"""WHERE subqueries as semi and anti joins (Paulley §4.4.2).

`EXISTS (SELECT ... FROM r WHERE p)` keeps the rows some row of r satisfies
`p` for, a semi join on `p`; `NOT EXISTS` keeps the others, an anti join;
`x IN (SELECT a ...)` is a semi join on `x = a`. `p` may read the outer row:
the join is dependent, so the IR stays a DAG. Each keeps or drops whole left
rows, so the left input's keys and dependencies hold; a semi join's ON holds
on every row it keeps, which proves non-null columns and Foreign Keys.
`NOT IN` stays opaque: one NULL among the values makes it reject every row.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
CREATE TABLE events (id INTEGER PRIMARY KEY, user_id INTEGER, kind VARCHAR);
"""

CASES = [
    # Foreign Keys: a kept row's value equals a matched right row's.
    ("IN onto a key", "SELECT user_id FROM events WHERE user_id IN (SELECT id FROM users) /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", Outcome.PROVED),
    ("IN onto a filtered key", "SELECT user_id FROM events WHERE user_id IN (SELECT id FROM users WHERE name = 'x') /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", Outcome.PROVED),
    ("correlated EXISTS", "SELECT user_id FROM events e WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id) /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", Outcome.PROVED),
    ("EXISTS with more conditions", "SELECT user_id FROM events e WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id AND u.name <> e.kind) /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", Outcome.PROVED),
    ("explicit SEMI JOIN", "SELECT * FROM events e SEMI JOIN users u ON u.id = e.user_id /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", Outcome.PROVED),
    ("NOT EXISTS", "SELECT user_id FROM events e WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id) /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", Outcome.UNKNOWN),
    ("NOT IN", "SELECT user_id FROM events WHERE user_id NOT IN (SELECT id FROM users) /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/", Outcome.UNKNOWN),
    ("IN onto a non-key", "SELECT user_id FROM events WHERE user_id IN (SELECT user_id FROM events) /**ASSERT FOREIGN KEY(user_id) REFERENCES events(user_id)**/", Outcome.UNKNOWN),
    # Non-null: a NULL equals nothing, so no row matches it.
    ("IN rejects NULL", "SELECT user_id FROM events WHERE user_id IN (SELECT id FROM users) /**ASSERT NOT NULL(user_id)**/", Outcome.PROVED),
    ("EXISTS rejects NULL", "SELECT user_id FROM events e WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id) /**ASSERT NOT NULL(user_id)**/", Outcome.PROVED),
    ("NOT EXISTS keeps NULL", "SELECT user_id FROM events e WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id) /**ASSERT NOT NULL(user_id)**/", Outcome.UNKNOWN),
    # Keys and literals: whole left rows are kept or dropped.
    ("key kept by EXISTS", "SELECT u.id, u.name FROM users u WHERE EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id) /**ASSERT UNIQUE(id)**/", Outcome.PROVED),
    ("key kept by NOT EXISTS", "SELECT u.id, u.name FROM users u WHERE NOT EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id) /**ASSERT UNIQUE(id)**/", Outcome.PROVED),
    ("outer literal in EXISTS", "SELECT e.kind FROM events e WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id AND e.kind = 'a') /**ASSERT ENUM(kind IN ('a'))**/", Outcome.PROVED),
    ("outer literal in NOT EXISTS", "SELECT e.kind FROM events e WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = e.user_id AND e.kind = 'a') /**ASSERT ENUM(kind IN ('a'))**/", Outcome.UNKNOWN),
    ("EXISTS no longer multiplies", "SELECT u.name FROM users u WHERE EXISTS (SELECT 1 FROM events e WHERE e.user_id = u.id) AND u.id = 1 /**ASSERT UNIQUE(name)**/", Outcome.PROVED),
]


@pytest.mark.parametrize(("label", "sql", "outcome"), CASES)
def test_a_where_subquery_is_a_semi_or_anti_join(label: str, sql: str, outcome: Outcome):
    report = analyze(SCHEMA + sql)

    assert not report.diagnostics, report.diagnostics
    assert [assertion.outcome for assertion in report.assertions] == [outcome], label


def test_a_right_semi_join_keeps_right_rows():
    # Spark's RIGHT SEMI JOIN (DuckDB has none) keeps the matched right rows.
    sql = "SELECT * FROM events RIGHT SEMI JOIN users ON events.user_id = users.id /**ASSERT UNIQUE(id)**/"
    assert [assertion.outcome for assertion in analyze(SCHEMA + sql, dialect="spark").assertions] == [Outcome.PROVED]


def test_a_join_marker_inside_an_exists_subquery_is_analyzed():
    sql = "SELECT e.id FROM events e WHERE EXISTS (SELECT 1 FROM events x /**ASSERT UNIQUE**/ JOIN users u ON u.id = x.user_id WHERE x.id = e.id)"
    report = analyze(SCHEMA + sql)

    assert not report.diagnostics, report.diagnostics
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
