"""Exactly One Join Assertions: `/**ASSERT EXACTLY ONE**/` before a JOIN.

Every left row matches exactly one right row. The at-most-one half is a Unique
Join: the right-side predicate columns cover a Unique Set. The at-least-one half
needs an ON of cross-input column equalities only, and Referential Coverage of
the left columns as one tuple onto the exact right relation and columns; every
left column must be non-null. Any join kind but semi and anti: the left row's
matches decide it, whatever a right or full join pads on.
"""

from __future__ import annotations


from sqlassert import analyze

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
CREATE TABLE accounts (user_id INTEGER PRIMARY KEY);
CREATE TABLE sessions (user_id INTEGER);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    buyer INTEGER REFERENCES users(id),
    plain INTEGER NOT NULL
);
CREATE TABLE members (user_id INTEGER NOT NULL REFERENCES accounts(user_id));
CREATE TABLE events (user_id INTEGER);
"""


def report_of(sql: str):
    return analyze(f"{SCHEMA} {sql}")


def test_the_assertion_locates_its_join():
    report = report_of(
        "SELECT * FROM orders\n/**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id"
    )

    origin = report.assertions[0].origin
    assert origin.line == SCHEMA.count("\n") + 2
    assert "JOIN users" in origin.detail
