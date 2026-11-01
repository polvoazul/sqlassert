"""Functional dependencies: uniqueness and `DETERMINES` beyond pass-through columns.

Keys, equalities, constants, and grouping all create functional
dependencies; closure over them proves Unique Sets that column pass-through
alone cannot. Cases come from Paulley, *Exploiting Functional Dependence in
Query Optimization* (2000), and from docs/functional-dependencies.md. Every
PROVED case here is also judged by the soundness oracle; every UNKNOWN case
is a known counterexample to a plausible but unsound rule.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze

# Paulley's running schema, cut down to the columns the examples use.
PAULLEY = """
CREATE TABLE vendor (vendor_id INTEGER PRIMARY KEY, name VARCHAR UNIQUE, contact_name VARCHAR, address VARCHAR);
CREATE TABLE part (part_id INTEGER PRIMARY KEY, description VARCHAR, class_code VARCHAR, cost INTEGER, qty INTEGER);
CREATE TABLE supply (
    vendor_id INTEGER, part_id INTEGER, rating VARCHAR, supply_code VARCHAR, PRIMARY KEY (vendor_id, part_id)
);
CREATE TABLE quote (
    quote_id INTEGER PRIMARY KEY, vendor_id INTEGER, part_id INTEGER, effective_date DATE,
    qty_price INTEGER, min_order INTEGER
);
CREATE TABLE t (w INTEGER, x INTEGER, y INTEGER, z INTEGER);
CREATE TABLE s (a INTEGER, b INTEGER, c INTEGER, d INTEGER, e INTEGER);
"""


def outcomes(sql: str, schema: str = PAULLEY) -> list[Outcome]:
    report = analyze(schema + sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


# Paulley's counterexamples: each is a rule that looks sound and is not ---------


COUNTEREXAMPLES = [
    (
        "Fig. 2.1, p.38: lax transitivity needs a non-null middle",
        """
        CREATE TABLE r (x INTEGER, y INTEGER UNIQUE, z INTEGER);
        SELECT x, z FROM r WHERE y IS NULL /**ASSERT UNIQUE(x)**/
        """,
    ),
    (
        "Fig. 2.1, p.38: as a dependency",
        """
        CREATE TABLE r (x INTEGER, y INTEGER UNIQUE, z INTEGER);
        SELECT x, y, z FROM r WHERE y IS NULL /**ASSERT DETERMINES(x -> z)**/
        """,
    ),
    (
        "Example 16, p.85: a padded row breaks a constant of the null-supplying side",
        """
        SELECT p.class_code, s.code
        FROM part p LEFT JOIN (SELECT part_id, 'x' AS code FROM supply) AS s ON p.part_id = s.part_id
        /**ASSERT DETERMINES(class_code -> code)**/
        """,
    ),
    (
        "Example 18, p.91: a null-tolerant ON condition binds nothing",
        """
        SELECT t.x, s.b FROM t LEFT JOIN s ON (t.x = s.b) IS NOT FALSE /**ASSERT DETERMINES(x -> b)**/
        """,
    ),
    (
        "Example 19, p.92: an extra ON conjunct needs every preserved column",
        """
        SELECT s.vendor_id AS s_vendor, s.part_id AS s_part, q.vendor_id AS q_vendor, q.effective_date, q.qty_price
        FROM supply s LEFT JOIN quote q
            ON s.part_id = q.part_id AND s.vendor_id = q.vendor_id AND q.effective_date > DATE '1999-06-01'
        /**ASSERT DETERMINES(s_vendor -> q_vendor)**/
        """,
    ),
    (
        "Example 24, p.193: DISTINCT is needed",
        """
        SELECT s.supply_code, p.part_id, p.description
        FROM supply s, part p
        WHERE s.part_id = p.part_id AND p.cost > 100
        /**ASSERT UNIQUE(supply_code, part_id, description)**/
        """,
    ),
]


@pytest.mark.parametrize(("label", "sql"), COUNTEREXAMPLES)
def test_a_paulley_counterexample_stays_unknown(label: str, sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN], label


# Paulley's worked examples -------------------------------------------------------


WORKED_EXAMPLES = [
    (
        "Example 9, p.37: a lax key with its column in the set",
        """
        SELECT DISTINCT v.name, v.contact_name, v.address, s.part_id, s.rating
        FROM vendor v, supply s
        WHERE v.name LIKE 'A%' AND v.vendor_id = s.vendor_id
        /**ASSERT UNIQUE(name, part_id)**/
        """,
    ),
    (
        "Example 9, p.37: the DISTINCT is redundant",
        """
        SELECT v.name, v.contact_name, v.address, s.part_id, s.rating
        FROM vendor v, supply s
        WHERE v.name LIKE 'A%' AND v.vendor_id = s.vendor_id
        /**ASSERT UNIQUE(name, part_id)**/
        """,
    ),
    (
        "Example 16, p.85: a key of the null-supplying side determines its row",
        """
        SELECT p.part_id AS pid, p.description, s.vendor_id, s.part_id AS spid, s.rating, s.supply_code
        FROM part p LEFT JOIN supply s ON p.part_id = s.part_id
        /**ASSERT DETERMINES(vendor_id, spid -> supply_code)**/
        /**ASSERT DETERMINES(pid -> description)**/
        /**ASSERT DETERMINES(pid -> spid)**/
        """,
    ),
    (
        "Example 16, p.85: the preserved side's ON column determines a right constant",
        """
        SELECT p.part_id, s.code
        FROM part p LEFT JOIN (SELECT part_id, 'x' AS code FROM supply) AS s ON p.part_id = s.part_id
        /**ASSERT DETERMINES(part_id -> code)**/
        """,
    ),
    (
        "Example 17, p.86: a null-intolerant ON conjunct binds a right column",
        """
        SELECT p.part_id AS pid, s.vendor_id, s.rating, s.supply_code
        FROM part p LEFT JOIN supply s ON p.part_id = s.part_id AND s.rating = 'A'
        /**ASSERT DETERMINES(pid -> rating)**/
        """,
    ),
    (
        "Example 20, p.94: every preserved ON column together determines the bound right columns",
        """
        SELECT t.x, t.y, t.z, s.a, s.b, s.c, s.d
        FROM t LEFT JOIN s ON t.x = s.b AND t.y = s.c AND t.z = 5 AND s.a = s.b AND s.d = 2
        /**ASSERT DETERMINES(x, y, z -> a, b, c, d)**/
        """,
    ),
    (
        "Example 23, p.193: the DISTINCT is redundant",
        """
        SELECT s.vendor_id, p.part_id, p.description
        FROM supply s, part p
        WHERE s.part_id = p.part_id AND p.cost > 100
        /**ASSERT UNIQUE(vendor_id, part_id)**/
        """,
    ),
    (
        "Example 25, p.194: a constant drops out of a key",
        """
        SELECT s.vendor_id, s.supply_code, p.part_id, p.description
        FROM supply s, part p
        WHERE p.part_id = s.part_id AND s.vendor_id = 7
        /**ASSERT UNIQUE(part_id)**/
        """,
    ),
    (
        "Examples 26-27, p.197: a constant and an equality through DISTINCT",
        """
        SELECT DISTINCT s.vendor_id, s.supply_code, p.part_id, p.description
        FROM supply s, part p
        WHERE p.part_id = s.part_id AND s.vendor_id = 7
        /**ASSERT UNIQUE(part_id)**/
        """,
    ),
    (
        "Example 28, p.206: a lax key pinned to a constant",
        """
        SELECT DISTINCT v.vendor_id, p.part_id, p.description, p.qty
        FROM supply s, vendor v, part p
        WHERE v.name = 'Acme' AND v.vendor_id = s.vendor_id AND s.part_id = p.part_id
        /**ASSERT UNIQUE(part_id)**/
        """,
    ),
    (
        "Example 29, p.206: at most one part per quote",
        """
        SELECT q.quote_id, q.effective_date, q.qty_price
        FROM quote q, part p
        WHERE q.min_order > 10 AND q.part_id = p.part_id AND p.part_id = 7
        /**ASSERT UNIQUE(quote_id)**/
        """,
    ),
    (
        "Example 30, p.209: a key of one input through a DISTINCT over four",
        """
        SELECT DISTINCT v.vendor_id, v.name, v.address
        FROM vendor v, part p, supply s, quote q
        WHERE p.part_id = s.part_id AND v.vendor_id = s.vendor_id AND q.part_id = p.part_id
            AND q.vendor_id = s.vendor_id AND q.min_order < 500 AND p.qty > 1000
        /**ASSERT UNIQUE(vendor_id)**/
        """,
    ),
    (
        "Example 31, p.212: INTERSECT ALL with a duplicate-free left input",
        """
        WITH parts AS (
            SELECT p.part_id FROM part p WHERE p.class_code = 'BX'
            INTERSECT ALL
            SELECT q.part_id FROM quote q WHERE q.min_order < 500
        )
        SELECT part_id FROM parts /**ASSERT UNIQUE(part_id)**/
        """,
    ),
]


@pytest.mark.parametrize(("label", "sql"), WORKED_EXAMPLES)
def test_a_paulley_worked_example_is_proved(label: str, sql: str):
    assert set(outcomes(sql)) == {Outcome.PROVED}, label


# The design doc's own cases --------------------------------------------------


DESIGN_CASES = [
    (
        "a constant drops out of a composite key",
        """
        CREATE TABLE items (tenant_id INTEGER, id INTEGER, name VARCHAR, PRIMARY KEY (tenant_id, id));
        SELECT id, name FROM items WHERE tenant_id = 5 /**ASSERT UNIQUE(id)**/
        """,
    ),
    (
        "an equal column stands in for a key column",
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (user_id INTEGER, order_no INTEGER, PRIMARY KEY (user_id, order_no));
        SELECT u.id, o.order_no FROM orders o JOIN users u ON o.user_id = u.id /**ASSERT UNIQUE(id, order_no)**/
        """,
    ),
    (
        "grouping by a key and a column it determines",
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE events (user_id INTEGER);
        SELECT u.id, u.name, count(*) AS n FROM events e JOIN users u ON e.user_id = u.id
        GROUP BY u.id, u.name /**ASSERT UNIQUE(id)**/
        """,
    ),
    (
        "grouping by a key: one group per input row",
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE);
        SELECT id, email, count(*) AS n FROM users GROUP BY id, email /**ASSERT UNIQUE(email)**/
        """,
    ),
    (
        "TPC-H Q3: the order key determines the other grouping columns",
        """
        CREATE TABLE customer (c_custkey INTEGER PRIMARY KEY, c_mktsegment VARCHAR);
        CREATE TABLE orders (o_orderkey INTEGER PRIMARY KEY, o_custkey INTEGER, o_orderdate DATE, o_shippriority INTEGER);
        CREATE TABLE lineitem (
            l_orderkey INTEGER, l_linenumber INTEGER, l_extendedprice INTEGER, l_discount INTEGER,
            PRIMARY KEY (l_orderkey, l_linenumber)
        );
        SELECT l_orderkey, sum(l_extendedprice * (1 - l_discount)) AS revenue, o_orderdate, o_shippriority
        FROM customer, orders, lineitem
        WHERE c_mktsegment = 'BUILDING' AND c_custkey = o_custkey AND l_orderkey = o_orderkey
            AND o_orderdate < DATE '1995-03-15'
        GROUP BY l_orderkey, o_orderdate, o_shippriority
        /**ASSERT UNIQUE(l_orderkey)**/
        """,
    ),
    (
        "the Redmine query: a constant-bound key part makes DISTINCT redundant",
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, login VARCHAR);
        CREATE TABLE members (id INTEGER PRIMARY KEY, user_id INTEGER, project_id INTEGER, UNIQUE (user_id, project_id));
        SELECT users.id, users.login FROM users JOIN members ON members.user_id = users.id
        WHERE members.project_id = 2
        /**ASSERT UNIQUE(id)**/
        """,
    ),
    (
        "a key pinned to a constant leaves at most one row",
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
        SELECT name FROM users WHERE id = 3 /**ASSERT UNIQUE(name)**/
        """,
    ),
    (
        "a declared dependency extends a key",
        """
        CREATE TABLE addresses (zip VARCHAR, city VARCHAR NOT NULL, street VARCHAR, UNIQUE (city, street));
        WITH a AS (SELECT zip, city, street FROM addresses /**DECLARE DETERMINES(zip -> city)**/)
        SELECT zip, city, street FROM a /**ASSERT DETERMINES(zip -> city)**/ /**ASSERT UNIQUE(zip, street)**/
        """,
    ),
]


@pytest.mark.parametrize(("label", "sql"), DESIGN_CASES)
def test_a_design_case_is_proved(label: str, sql: str):
    assert set(outcomes(sql, schema="")) == {Outcome.PROVED}, label


def test_a_dependency_is_not_assumed_without_a_source():
    assert outcomes(
        """
        CREATE TABLE addresses (zip VARCHAR, city VARCHAR);
        SELECT zip, city FROM addresses /**ASSERT DETERMINES(zip -> city)**/
        """,
        schema="",
    ) == [Outcome.UNKNOWN]


def test_an_equality_on_an_outer_join_is_not_a_strict_equivalence():
    """`ON o.user_id = u.id` holds only on matched rows, so `u.id` does not
    stand in for `o.user_id` the way it does on an inner join."""
    assert outcomes(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (user_id INTEGER, order_no INTEGER, PRIMARY KEY (user_id, order_no));
        SELECT u.id, o.order_no FROM orders o LEFT JOIN users u ON o.user_id = u.id /**ASSERT UNIQUE(id, order_no)**/
        """,
        schema="",
    ) == [Outcome.PROVED]


def test_a_declared_dependency_onto_a_nullable_column_does_not_extend_a_key():
    """`zip -> city` lets two rows share a zip with a NULL city, and a NULL
    city is exempt from `UNIQUE (city, street)`, so `(zip, street)` repeats."""
    assert outcomes(
        """
        CREATE TABLE addresses (zip VARCHAR, city VARCHAR, street VARCHAR, UNIQUE (city, street));
        WITH a AS (SELECT zip, city, street FROM addresses /**DECLARE DETERMINES(zip -> city)**/)
        SELECT zip, city, street FROM a /**ASSERT UNIQUE(zip, street)**/
        """,
        schema="",
    ) == [Outcome.UNKNOWN]


@pytest.mark.parametrize(
    "marker",
    ["/**ASSERT DETERMINES(zip city)**/", "/**ASSERT DETERMINES(-> city)**/", "/**ASSERT DETERMINES(zip ->)**/",
     "/**ASSERT DETERMINES(zip, zip -> city)**/", "/**DECLARE DETERMINES(zip -> city**/"],
)
def test_a_malformed_dependency_marker_is_reported(marker: str):
    report = analyze(f"CREATE TABLE addresses (zip VARCHAR, city VARCHAR); SELECT zip, city FROM addresses {marker}")
    assert report.assertions == ()
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["unattached-marker"], marker
    assert "functional dependency" in report.diagnostics[0].message


def test_a_dependency_marker_naming_an_unknown_column_is_reported():
    report = analyze("CREATE TABLE addresses (zip VARCHAR, city VARCHAR); SELECT zip FROM addresses /**ASSERT DETERMINES(zip -> city)**/")
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["unknown-asserted-column"]


# Scalar functions (Paulley §3.1.4) ------------------------------------------

FUNCTIONS = """
CREATE TABLE f (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, s VARCHAR, n VARCHAR COLLATE NOCASE, d DOUBLE);
"""

FUNCTION_CASES = [
    # X -> f(X): grouping by a and a function of it groups by a alone.
    ("arithmetic", "SELECT a, a + 1 AS k, count(*) AS c FROM f GROUP BY a, a + 1 /**ASSERT UNIQUE(a)**/", Outcome.PROVED),
    ("two arguments", "SELECT a, b, a * b AS k, count(*) AS c FROM f GROUP BY a, b, a * b /**ASSERT UNIQUE(a, b)**/", Outcome.PROVED),
    ("built-in function", "SELECT s, upper(s) AS k, count(*) AS c FROM f GROUP BY s, upper(s) /**ASSERT UNIQUE(s)**/", Outcome.PROVED),
    ("cast", "SELECT a, CAST(a AS VARCHAR) AS k, count(*) AS c FROM f GROUP BY a, CAST(a AS VARCHAR) /**ASSERT UNIQUE(a)**/", Outcome.PROVED),
    ("distinct", "SELECT DISTINCT a, a * 2 AS k FROM f /**ASSERT UNIQUE(a)**/", Outcome.PROVED),
    ("asserted dependency", "SELECT a, b, a + b AS k FROM f /**ASSERT DETERMINES(a, b -> k)**/", Outcome.PROVED),
    ("through a CTE", "WITH g AS (SELECT a, b, a + b AS k FROM f) SELECT a, b, k FROM g /**ASSERT DETERMINES(a, b -> k)**/", Outcome.PROVED),
    # Not a function of its columns.
    ("user-defined function", "SELECT a, my_udf(a) AS k, count(*) AS c FROM f GROUP BY a, my_udf(a) /**ASSERT UNIQUE(a)**/", Outcome.UNKNOWN),
    ("random", "SELECT a, a + random() AS k, count(*) AS c FROM f GROUP BY a, a + random() /**ASSERT UNIQUE(a)**/", Outcome.UNKNOWN),
    # Equal is not identical: 'x' = 'X' under NOCASE, -0.0 = 0.0 as DOUBLE.
    ("collated text", "SELECT n, n || '' AS k, count(*) AS c FROM f GROUP BY n, n || '' /**ASSERT UNIQUE(n)**/", Outcome.UNKNOWN),
    ("double", "SELECT d, CAST(d AS VARCHAR) AS k, count(*) AS c FROM f GROUP BY d, CAST(d AS VARCHAR) /**ASSERT UNIQUE(d)**/", Outcome.UNKNOWN),
    # f(X) does not determine X: no injectivity.
    ("not injective", "SELECT a, a % 2 AS k, count(*) AS c FROM f GROUP BY a, a % 2 /**ASSERT UNIQUE(k)**/", Outcome.UNKNOWN),
]


@pytest.mark.parametrize(("label", "sql", "outcome"), FUNCTION_CASES)
def test_a_deterministic_function_is_determined_by_its_arguments(label: str, sql: str, outcome: Outcome):
    assert outcomes(sql, FUNCTIONS) == [outcome], label
