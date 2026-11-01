"""Equality groups: the output columns of a relation that carry one value.

An internal analysis with no Property: tests drive `_equality_groups` and
`_equal_columns` in an engine session. Only strict equalities merge groups:
`WHERE` and inner-join `ON` conjuncts, never an outer join's `ON`, which
holds on matched rows only.
"""

from __future__ import annotations

import pytest

from sqlassert import ir
from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine, Stratum
from sqlassert.rules.equality import _equal_columns, _equality_groups
from sqlassert.sql_parse import SqlParser

SCHEMA = """
CREATE TABLE t (x INTEGER, y INTEGER, z INTEGER);
CREATE TABLE s (a INTEGER, b INTEGER);
CREATE TABLE nodes (id INTEGER, parent INTEGER);
"""


def program(sql: str) -> ir.Program:
    conversion = IrParser("duckdb").parse(SqlParser("duckdb").parse(SCHEMA + sql))
    return conversion.program


def groups(sql: str) -> set[frozenset[str]]:
    """The root's groups of more than one column, by output column name."""
    parsed = program(sql)
    assert parsed.root is not None
    with Engine().session(parsed):
        found = _equality_groups(parsed.root).value
    return {frozenset(column.name for column in group) for group in found.groups if len(group) > 1}


MERGED = [
    ("WHERE equality", "SELECT x, y, z FROM t WHERE x = y", {frozenset({"x", "y"})}),
    ("chained WHERE equalities", "SELECT x, y, z FROM t WHERE x = y AND z = y", {frozenset({"x", "y", "z"})}),
    ("inner join ON", "SELECT t.x, s.a FROM t JOIN s ON t.x = s.a", {frozenset({"x", "a"})}),
    ("comma join with WHERE", "SELECT t.x, s.a FROM t, s WHERE s.a = t.x", {frozenset({"x", "a"})}),
    ("WHERE after a LEFT JOIN", "SELECT t.x, s.a FROM t LEFT JOIN s ON t.y = s.b WHERE t.x = s.a", {frozenset({"x", "a"})}),
    (
        "through a dropped column",
        "SELECT t.x, s.a FROM t JOIN nodes n ON t.x = n.id JOIN s ON n.id = s.a",
        {frozenset({"x", "a"})},
    ),
    ("one column selected twice", "SELECT x AS p, x AS q FROM t", {frozenset({"p", "q"})}),
    ("renamed", "SELECT x AS p, y AS q FROM t WHERE x = y", {frozenset({"p", "q"})}),
    (
        "self join through aliases",
        "SELECT child.id AS child_id, up.id AS parent_id, child.parent FROM nodes child JOIN nodes up ON child.parent = up.id",
        {frozenset({"parent_id", "parent"})},
    ),
    (
        "view",
        "CREATE VIEW v AS SELECT x AS p, y AS q FROM t WHERE x = y; SELECT p AS r, q AS w FROM v",
        {frozenset({"r", "w"})},
    ),
    (
        "CTE",
        "WITH c AS (SELECT x, y FROM t WHERE x = y) SELECT c.x AS r, c.y AS w FROM c",
        {frozenset({"r", "w"})},
    ),
    (
        "a join over a view",
        "CREATE VIEW v AS SELECT x AS p, y AS q FROM t WHERE x = y; SELECT v.p, s.a FROM v JOIN s ON v.q = s.a",
        {frozenset({"p", "a"})},
    ),
    (
        "FROM subquery",
        "SELECT sub.p, sub.q FROM (SELECT x AS p, y AS q FROM t WHERE x = y) sub",
        {frozenset({"p", "q"})},
    ),
    (
        "the preserved side of a LEFT JOIN",
        "SELECT l.x, l.y, s.a FROM (SELECT x, y FROM t WHERE x = y) l LEFT JOIN s ON l.x = s.a",
        {frozenset({"x", "y"})},
    ),
    (
        "inside the padded side of a LEFT JOIN",
        "SELECT t.z, sub.p, sub.q FROM t LEFT JOIN (SELECT x AS p, y AS q FROM t WHERE x = y) sub ON t.z = sub.p",
        {frozenset({"p", "q"})},
    ),
    ("GROUP BY columns", "SELECT x, y, count(*) AS n FROM t WHERE x = y GROUP BY x, y", {frozenset({"x", "y"})}),
    ("DISTINCT", "SELECT DISTINCT x, y FROM t WHERE x = y", {frozenset({"x", "y"})}),
]


@pytest.mark.parametrize(("label", "sql", "expected"), MERGED, ids=[label for label, *_ in MERGED])
def test_strict_equalities_merge_groups(label: str, sql: str, expected: set[frozenset[str]]):
    assert groups(sql) == expected


SEPARATE = [
    ("LEFT JOIN ON", "SELECT t.x, s.a FROM t LEFT JOIN s ON t.x = s.a"),
    ("RIGHT JOIN ON", "SELECT t.x, s.a FROM t RIGHT JOIN s ON t.x = s.a"),
    ("FULL JOIN ON", "SELECT t.x, s.a FROM t FULL JOIN s ON t.x = s.a"),
    ("an equality within the padded side", "SELECT t.z, s.a, s.b FROM t LEFT JOIN s ON t.z = s.a AND s.a = s.b"),
    ("an expression", "SELECT x, y FROM t WHERE x = y + 1"),
    ("a computed column", "SELECT x, x + 0 AS w FROM t"),
    ("a disjunction", "SELECT x, y FROM t WHERE x = y OR z = 1"),
    ("aggregates of equal columns", "SELECT z, min(x) AS mx, min(y) AS my FROM t WHERE x = y GROUP BY z"),
    ("UNION ALL", "SELECT x, y FROM t WHERE x = y UNION ALL SELECT x, y FROM t"),
    ("one self-join alias equal to the other", "SELECT l.parent AS lp, r.parent AS rp FROM nodes l JOIN nodes r ON l.id = r.id"),
    ("a semi join", "SELECT t.x FROM t SEMI JOIN s ON t.x = s.a"),
]


@pytest.mark.parametrize(("label", "sql"), SEPARATE, ids=[label for label, _ in SEPARATE])
def test_other_conditions_leave_columns_apart(label: str, sql: str):
    assert groups(sql) == set()


def test_groups_partition_the_output_columns_in_output_order():
    parsed = program("SELECT z, y, x FROM t WHERE x = y")
    assert parsed.root is not None
    with Engine().session(parsed):
        found = _equality_groups(parsed.root).value
    assert found.relation is parsed.root
    assert [[column.name for column in group] for group in found.groups] == [["z"], ["y", "x"]]


def test_equal_columns_names_each_equality_and_its_line():
    parsed = program("""SELECT t.x, s.a, n.id
FROM t
JOIN s ON t.x = s.a
JOIN nodes n ON n.id = t.x
WHERE t.y = t.z""")
    assert parsed.root is not None
    x, a, identifier = parsed.root.output_columns
    with Engine().session(parsed):
        equal = _equal_columns(x)
        assert equal.value == frozenset({x, a, identifier})
        assert _equal_columns(a).value == equal.value
        explanation = equal.explain()
    assert "ON equality" in explanation
    assert "t.x = s.a (line 7)" in explanation and "n.id = t.x (line 8)" in explanation  # The query starts on line 5.
    assert "WHERE equality" not in explanation  # y = z merges another group.


def test_a_column_without_equalities_is_equal_only_to_itself():
    parsed = program("SELECT x, y FROM t")
    assert parsed.root is not None
    x, _ = parsed.root.output_columns
    with Engine().session(parsed):
        assert _equal_columns(x).value == frozenset({x})


def test_equality_groups_sit_below_every_other_stratum():
    assert _equality_groups.stratum is Stratum.EQUALITY_GROUP
    assert Stratum.EQUALITY_GROUP == min(Stratum)


def test_every_relation_of_a_program_has_groups_without_a_cycle():
    parsed = program("""
        CREATE VIEW v AS SELECT x AS p, y AS q FROM t WHERE x = y;
        WITH c AS (SELECT DISTINCT p, q FROM v)
        SELECT c.p, s.a, n.parent FROM c JOIN s ON c.p = s.a LEFT JOIN nodes n ON n.id = c.q
        WHERE s.b IS NOT NULL
    """)
    with Engine().session(parsed):
        for relation in parsed.relations():
            groups = _equality_groups(relation).value.groups
            assert {column for group in groups for column in group} == set(relation.output_columns)
            assert sum(len(group) for group in groups) == len(relation.output_columns)
