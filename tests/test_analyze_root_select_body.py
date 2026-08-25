"""The Root Select's own WHERE/GROUP BY/DISTINCT/QUALIFY/projection, modeled
the same way a CTE or view body's already is.

Before this, `_lower_query` only ever lowered a Root Select's FROM and JOINs,
ignoring everything else about its own body -- so a join inside a Root Select
that also filters, groups, deduplicates, or projects its own output must keep
proving exactly as it did when the Root Select modeled only FROM/JOIN, now
that the same body is wrapped in the tail nested selects already use. A Join
must stay reachable through that wrapping, or every one of these would
silently stop proving instead of loudly failing.
"""

from __future__ import annotations

from sqlassert import Outcome, analyze


def test_a_root_select_with_its_own_projection_still_proves_its_join():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT sessions.user_id, users.id
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_root_select_with_its_own_where_still_proves_its_join():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        WHERE sessions.ts > TIMESTAMP '2020-01-01'
        """
    )

    assert report.proved


def test_a_root_select_with_its_own_group_by_still_proves_its_join():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT sessions.user_id, count(*)
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        GROUP BY sessions.user_id
        """
    )

    assert report.proved


def test_a_root_select_with_its_own_distinct_still_proves_its_join():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT DISTINCT sessions.user_id
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert report.proved


def test_a_union_arm_with_its_own_where_still_proves_its_join():
    """A `UNION` arm is lowered through the same Root Select path, so it
    incidentally gains the same modeling -- its own WHERE no longer stands
    between it and the tail a CTE or view body already gets."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);
        CREATE TABLE orders (user_id INTEGER, ts TIMESTAMP);

        SELECT * FROM sessions /**UNIQUE**/ JOIN users ON sessions.user_id = users.id
            WHERE sessions.ts > TIMESTAMP '2020-01-01'
        UNION ALL
        SELECT * FROM orders /**UNIQUE**/ JOIN users ON orders.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED, Outcome.PROVED]
