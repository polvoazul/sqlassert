"""Joins inside CTE, view, and FROM-subquery bodies are lowered like the Root Select's.

Their markers are analyzed, and every fact (keys, non-null, Equality Groups,
Enumerated Domains, Foreign Keys) flows through them to the queries that read
the body.
"""

from __future__ import annotations


from sqlassert import suggest

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), status VARCHAR);
CREATE TABLE items (order_id INTEGER);
"""


def test_a_join_inside_a_cte_is_an_attachment_site_for_suggest():
    report = suggest(
        SCHEMA + "WITH c AS (SELECT orders.id FROM orders\nJOIN users ON users.id = orders.user_id)\nSELECT * FROM c"
    )

    assert ("JOIN users", "/**ASSERT EXACTLY ONE**/") in [(s.site.label, s.marker) for s in report.suggestions]
