"""Joins inside CTE, view, and FROM-subquery bodies are lowered like the Root Select's.

Their markers are analyzed, and every fact (keys, non-null, Equality Groups,
Enumerated Domains, Foreign Keys) flows through them to the queries that read
the body.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze, suggest
from sqlassert.diagnostics import UNANALYZED_ASSERTION

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), status VARCHAR);
CREATE TABLE items (order_id INTEGER);
"""


def outcomes(sql: str) -> list[Outcome]:
    report = analyze(SCHEMA + sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


@pytest.mark.parametrize("sql", [
    pytest.param("WITH c AS (SELECT * FROM orders /**ASSERT UNIQUE**/ JOIN users ON users.id = orders.user_id) SELECT * FROM c", id="cte"),
    pytest.param(
        "CREATE VIEW v AS SELECT orders.id FROM orders /**ASSERT UNIQUE**/ JOIN users ON users.id = orders.user_id;"
        " SELECT * FROM v", id="view",
    ),
    pytest.param(
        "SELECT * FROM (SELECT orders.id FROM orders /**ASSERT UNIQUE**/ JOIN users ON users.id = orders.user_id) AS s",
        id="from subquery",
    ),
    pytest.param(
        "SELECT * FROM items JOIN (SELECT orders.id FROM orders /**ASSERT UNIQUE**/ JOIN users ON users.id = orders.user_id) AS s"
        " ON s.id = items.order_id", id="joined subquery",
    ),
    pytest.param(
        "WITH c AS (SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON users.id = orders.user_id) SELECT * FROM c",
        id="exactly one in a cte",
    ),
])
def test_a_join_marker_inside_a_body_is_proved(sql):
    assert outcomes(sql) == [Outcome.PROVED]


def test_an_unprovable_join_marker_inside_a_cte_is_unknown():
    assert outcomes(
        "WITH c AS (SELECT * FROM users /**ASSERT UNIQUE**/ JOIN orders ON users.id = orders.user_id) SELECT * FROM c"
    ) == [Outcome.UNKNOWN]


def test_a_cte_referenced_twice_states_its_assertion_once():
    assert outcomes(
        "WITH c AS (SELECT orders.id FROM orders /**ASSERT UNIQUE**/ JOIN users ON users.id = orders.user_id) "
        "SELECT * FROM c AS a JOIN c AS b ON a.id = b.id"
    ) == [Outcome.PROVED]


def test_nested_ctes_with_joins_are_lowered():
    assert outcomes(
        """
        WITH outer_cte AS (
            WITH inner_cte AS (SELECT orders.id FROM orders /**ASSERT UNIQUE**/ JOIN users ON users.id = orders.user_id)
            SELECT inner_cte.id FROM inner_cte /**ASSERT UNIQUE**/ JOIN orders ON orders.id = inner_cte.id
        ),
        later AS (SELECT items.order_id FROM items /**ASSERT UNIQUE**/ JOIN outer_cte ON outer_cte.id = items.order_id)
        SELECT * FROM later
        """
    ) == [Outcome.PROVED, Outcome.PROVED, Outcome.PROVED]


def test_a_view_joining_orders_to_users_by_key_keeps_the_orders_key():
    assert outcomes(
        "CREATE VIEW v AS SELECT orders.id, users.name FROM orders JOIN users ON users.id = orders.user_id;"
        " SELECT * FROM items /**ASSERT UNIQUE**/ JOIN v ON v.id = items.order_id;"
    ) == [Outcome.PROVED]


@pytest.mark.parametrize(("marker", "expected"), [
    ("/**ASSERT NOT NULL UNIQUE(id)**/", Outcome.PROVED),
    ("/**ASSERT NOT NULL(name)**/", Outcome.PROVED),
    ("/**ASSERT DETERMINES(user_id -> name)**/", Outcome.PROVED),
    ("/**ASSERT UNIQUE(name)**/", Outcome.UNKNOWN),
])
def test_facts_flow_through_a_join_in_a_cte(marker, expected):
    assert outcomes(
        "WITH c AS (SELECT orders.id, orders.user_id, users.name FROM orders JOIN users ON users.id = orders.user_id) "
        f"SELECT * FROM c {marker}"
    ) == [expected]


def test_a_left_join_in_a_cte_pads_its_right_side():
    assert outcomes(
        "WITH c AS (SELECT items.order_id, orders.status, orders.id FROM items LEFT JOIN orders ON orders.id = items.order_id) "
        "SELECT * FROM c /**ASSERT NOT NULL(id)**/"
    ) == [Outcome.UNKNOWN]


def test_an_enumerated_domain_flows_through_a_join_in_a_subquery():
    assert outcomes(
        "WITH o AS (SELECT * FROM orders /**DECLARE ENUM(status IN ('new', 'paid'))**/) "
        "SELECT status FROM (SELECT o.status FROM o JOIN users ON users.id = o.user_id) AS s "
        "/**ASSERT ENUM(status IN ('new', 'paid'))**/"
    ) == [Outcome.PROVED]


def test_a_set_operation_in_a_from_subquery_is_lowered():
    assert outcomes(
        "SELECT * FROM (SELECT id FROM orders UNION SELECT order_id FROM items) AS s /**ASSERT UNIQUE(id)**/"
    ) == [Outcome.PROVED]


def test_a_join_marker_in_an_unreferenced_cte_is_still_reported():
    """An unreferenced CTE is not lowered, like an unreferenced view."""
    report = analyze(
        SCHEMA + "WITH c AS (SELECT * FROM orders /**ASSERT UNIQUE**/ JOIN users ON users.id = orders.user_id) SELECT 1 AS one"
    )

    assert report.assertions == ()
    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNANALYZED_ASSERTION]


def test_a_join_inside_a_cte_is_an_attachment_site_for_suggest():
    report = suggest(
        SCHEMA + "WITH c AS (SELECT orders.id FROM orders\nJOIN users ON users.id = orders.user_id)\nSELECT * FROM c"
    )

    assert ("JOIN users", "/**ASSERT EXACTLY ONE**/") in [(s.site.label, s.marker) for s in report.suggestions]
