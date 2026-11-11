"""Low-level suggestions: Smells (issue 15, from the Kossmann Table 1 triage)."""

from __future__ import annotations

import duckdb
import pytest

from sqlassert import suggest
from sqlassert.__main__ import main
from sqlassert.smells import Concern

SCHEMA = """
CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR, score INTEGER);
CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER);
"""


@pytest.mark.parametrize(
    ("sql", "loose"),
    [
        ("SELECT id, name FROM t ORDER BY score LIMIT 3", "(id, name)"),
        ("SELECT id, name FROM t LIMIT 3", None),
        ("SELECT score, count(*) AS n FROM t GROUP BY score ORDER BY n DESC LIMIT 1", "(score)"),
        ("SELECT name FROM t ORDER BY score OFFSET 2", "(name)"),
        ("WITH top AS (SELECT id FROM t ORDER BY score LIMIT 3) SELECT id FROM top", "(id)"),
    ],
)
def test_a_limit_whose_order_leaves_different_rows_tied_is_a_smell(sql: str, loose: str | None):
    smells = suggest(SCHEMA + sql).smells

    assert [smell.concern for smell in smells] == [Concern.ARBITRARY_LIMIT]
    assert (loose or "with no ORDER BY") in smells[0].message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM t ORDER BY id LIMIT 3",
        "SELECT id, name FROM t ORDER BY score, id LIMIT 3",
        "SELECT id, name FROM t ORDER BY 1 LIMIT 3",
        "SELECT name FROM t ORDER BY t.id LIMIT 3",
        # Rows tied on the keys are identical: either may come first.
        "SELECT name FROM t ORDER BY name LIMIT 3",
        # At most one row.
        "SELECT id, name FROM t WHERE id = 1 LIMIT 1",
        "SELECT count(*) AS n FROM t LIMIT 1",
        # No LIMIT: the order is the reader's business.
        "SELECT id, name FROM t ORDER BY score",
        # A key sqlassert cannot map to one column keeps it silent.
        "SELECT t.name FROM t JOIN u ON u.t_id = t.id ORDER BY id LIMIT 2",
        "SELECT id, name FROM t ORDER BY lower(name) LIMIT 3",
    ],
)
def test_a_determined_or_unreadable_limit_is_not_a_smell(sql: str):
    assert suggest(SCHEMA + sql).smells == ()


def test_the_cli_lists_smells_last_at_the_low_level(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT id, name FROM t ORDER BY score LIMIT 3")

    assert main(["suggest", str(path)]) == 0

    last = capsys.readouterr().out.splitlines()[-1]
    assert last.split()[:2] == ["low", "line"]
    assert "Arbitrary LIMIT" in last


# Join traps (issue 41) --------------------------------------------------------

TRAPS = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, credit INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER, amount INTEGER);
CREATE TABLE payments (id INTEGER PRIMARY KEY, customer_id INTEGER, amount INTEGER);
CREATE TABLE logs (customer_id INTEGER, amount INTEGER);
"""

# One customer with two orders and two payments.
ROWS = """
INSERT INTO customers VALUES (1, 100);
INSERT INTO orders VALUES (10, 1, 5), (11, 1, 7);
INSERT INTO payments VALUES (20, 1, 3), (21, 1, 4);
"""

FAN = "SELECT o.customer_id, sum(p.amount) AS s FROM orders o JOIN payments p ON p.customer_id = o.customer_id GROUP BY o.customer_id"
CHASM = "SELECT c.id, sum(c.credit) AS s FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id"


def concerns(sql: str) -> list[Concern]:
    return [smell.concern for smell in suggest(TRAPS + sql).smells]


@pytest.mark.parametrize(
    ("sql", "true_total", "concern"),
    [(FAN, 7, Concern.FAN_TRAP), (CHASM, 100, Concern.CHASM_TRAP)],
    ids=["fan", "chasm"],
)
def test_a_trap_is_a_smell_and_multiplies_the_total_on_data(sql: str, true_total: int, concern: Concern):
    assert concerns(sql) == [concern]
    connection = duckdb.connect()
    connection.execute(TRAPS + ROWS)
    (_, total), = connection.execute(sql).fetchall()
    assert total == 2 * true_total  # Each row was counted twice.


@pytest.mark.parametrize(
    ("sql", "concern"),
    [
        # Every aggregate: the join is wrong either way.
        ("SELECT max(c.credit) AS m FROM customers c JOIN orders o ON o.customer_id = c.id", Concern.CHASM_TRAP),
        ("SELECT count(DISTINCT p.amount) AS n FROM orders o JOIN payments p ON p.customer_id = o.customer_id", Concern.FAN_TRAP),
        # count(*) counts the join's rows: only a many-to-many join is a trap for it.
        ("SELECT count(*) AS n FROM orders o JOIN payments p ON p.customer_id = o.customer_id", Concern.FAN_TRAP),
        # Through a CTE that does the join.
        ("WITH j AS (SELECT c.credit FROM customers c JOIN orders o ON o.customer_id = c.id) SELECT sum(credit) AS s FROM j", Concern.CHASM_TRAP),
        # Through a chain: customers joined to orders is not unique on customers.id.
        (
            "SELECT c.id, sum(o.amount) AS s FROM customers c JOIN orders o ON o.customer_id = c.id "
            "JOIN payments p ON p.customer_id = c.id GROUP BY c.id",
            Concern.FAN_TRAP,
        ),
    ],
)
def test_more_traps(sql: str, concern: Concern):
    assert concerns(sql) == [concern]


@pytest.mark.parametrize(
    "sql",
    [
        # The many side's rows are never repeated.
        "SELECT c.id, sum(o.amount) AS s FROM customers c JOIN orders o ON o.customer_id = c.id GROUP BY c.id",
        "SELECT count(*) AS n FROM customers c JOIN orders o ON o.customer_id = c.id",
        # A semi join never repeats a row.
        "SELECT sum(c.credit) AS s FROM customers c SEMI JOIN orders o ON o.customer_id = c.id",
        # No known Unique Set on the repeated input: nothing to lose.
        "SELECT sum(l.amount) AS s FROM orders o JOIN logs l ON l.customer_id = o.customer_id",
        # An argument reading both inputs is not traced.
        "SELECT sum(o.amount * p.amount) AS s FROM orders o JOIN payments p ON p.customer_id = o.customer_id",
        # The filter keeps one order, so each customer's row appears once.
        "SELECT sum(c.credit) AS s FROM customers c JOIN orders o ON o.customer_id = c.id WHERE o.id = 1",
    ],
)
def test_an_aggregate_over_rows_no_join_repeats_is_not_a_trap(sql: str):
    assert concerns(sql) == []


# Implicit cast -------------------------------------------------------------------

CASTS = """
CREATE TABLE codes (code VARCHAR PRIMARY KEY, n INTEGER);
CREATE TABLE nums (v INTEGER);
"""


def test_a_join_equality_that_casts_is_a_smell_and_repeats_rows_on_data():
    sql = "SELECT * FROM nums JOIN codes ON codes.code = nums.v"
    assert [smell.concern for smell in suggest(CASTS + sql).smells] == [Concern.IMPLICIT_CAST]
    connection = duckdb.connect()
    connection.execute(CASTS + "INSERT INTO codes VALUES ('01', 0), ('1', 0); INSERT INTO nums VALUES (1);")
    assert connection.execute("SELECT count(*) FROM nums JOIN codes ON codes.code = nums.v").fetchone() == (2,)


@pytest.mark.parametrize(
    "on",
    ["codes.n = nums.v", "codes.code = CAST(nums.v AS VARCHAR)"],
    ids=["same type", "explicit cast"],
)
def test_a_join_equality_without_an_implicit_cast_is_not_a_smell(on: str):
    assert suggest(CASTS + f"SELECT * FROM nums JOIN codes ON {on}").smells == ()


# Always empty -----------------------------------------------------------------

EMPTY_SCHEMA = """
CREATE TABLE customers (id INTEGER PRIMARY KEY, name VARCHAR);
CREATE TABLE orders (id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers (id), note INTEGER REFERENCES customers (id));
"""


@pytest.mark.parametrize("sql", [
    "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = o.customer_id)",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON c.id = o.customer_id WHERE c.id IS NULL",
    "SELECT customer_id FROM orders EXCEPT SELECT id FROM customers",
])
def test_an_orphan_check_a_foreign_key_guarantees_is_always_empty(sql: str):
    smells = suggest(EMPTY_SCHEMA + sql).smells
    assert [smell.concern for smell in smells] == [Concern.ALWAYS_EMPTY]
    assert duckdb.connect().execute(EMPTY_SCHEMA + "INSERT INTO customers VALUES (1, 'a'); INSERT INTO orders VALUES (1, 1, NULL);" + sql).fetchall() == []


@pytest.mark.parametrize("sql", [
    # A nullable reference: a NULL note has no match.
    "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = o.note)",
    "SELECT o.id FROM orders o LEFT JOIN customers c ON c.id = o.note WHERE c.id IS NULL",
    "SELECT note FROM orders EXCEPT SELECT id FROM customers",
    # Covered the other way round.
    "SELECT c.id FROM customers c WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.customer_id = c.id)",
    # A scalar aggregate returns its row over no input.
    "SELECT count(*) AS n FROM orders o WHERE NOT EXISTS (SELECT 1 FROM customers c WHERE c.id = o.customer_id)",
])
def test_a_query_that_may_return_a_row_is_not_always_empty(sql: str):
    assert Concern.ALWAYS_EMPTY not in [smell.concern for smell in suggest(EMPTY_SCHEMA + sql).smells]
