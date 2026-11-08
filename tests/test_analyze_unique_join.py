from __future__ import annotations

from sqlassert import analyze


PROVABLE_PROGRAM = """
    CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
    CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

    SELECT *
    FROM sessions
    /**ASSERT MANY TO ONE**/ JOIN users
        ON sessions.user_id = users.id
"""


def test_assertion_provenance_locates_the_marked_join_in_the_source():
    report = analyze(PROVABLE_PROGRAM)

    origin = report.assertions[0].origin
    assert "users" in origin.detail
    assert origin.line == 7
