"""Low-level suggestions: Hazards (issue 15, from the Kossmann Table 1 triage)."""

from __future__ import annotations

import duckdb
import pytest

from sqlassert import suggest
from sqlassert.__main__ import main
from sqlassert.hazards import Concern

SCHEMA = """
CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR, score INTEGER);
CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER);
"""


@pytest.mark.parametrize(
    ("sql", "loose"),
    [
        ("SELECT id, name FROM t ORDER BY score LIMIT 3", "('id', 'name')"),
        ("SELECT id, name FROM t LIMIT 3", None),
        ("SELECT score, count(*) AS n FROM t GROUP BY score ORDER BY n DESC LIMIT 1", "('score')"),
        ("SELECT name FROM t ORDER BY score OFFSET 2", "('name')"),
        ("WITH top AS (SELECT id FROM t ORDER BY score LIMIT 3) SELECT id FROM top", "('id')"),
    ],
)
def test_a_limit_whose_order_leaves_different_rows_tied_is_a_hazard(sql: str, loose: str | None):
    hazards = suggest(SCHEMA + sql).hazards

    assert [hazard.concern for hazard in hazards] == [Concern.ARBITRARY_LIMIT]
    assert (loose or "with no ORDER BY") in hazards[0].message


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
def test_a_determined_or_unreadable_limit_is_not_a_hazard(sql: str):
    assert suggest(SCHEMA + sql).hazards == ()


def test_the_cli_lists_hazards_last(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT id, name FROM t ORDER BY score LIMIT 3")

    assert main(["suggest", str(path)]) == 0

    last = capsys.readouterr().out.splitlines()[-1]
    assert last.split()[:2] == ["hazard", "line"]
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
    """The hazards of `sql` but its many-to-many joins, tested on their own below."""
    return [hazard.concern for hazard in suggest(TRAPS + sql).hazards if hazard.concern is not Concern.MANY_TO_MANY]


@pytest.mark.parametrize(
    ("sql", "true_total", "concern"),
    [(FAN, 7, Concern.FAN_TRAP), (CHASM, 100, Concern.CHASM_TRAP)],
    ids=["fan", "chasm"],
)
def test_a_trap_is_a_hazard_and_multiplies_the_total_on_data(sql: str, true_total: int, concern: Concern):
    assert concerns(sql) == [concern]
    # The trap reports the join under the aggregate; it is not a hazard twice.
    assert joins(sql) == []
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


# Many-to-many join --------------------------------------------------------------

JOIN = "SELECT o.id, p.id AS payment_id FROM orders o JOIN payments p ON p.customer_id = o.customer_id"


def joins(sql: str) -> list[Concern]:
    return [hazard.concern for hazard in suggest(TRAPS + sql).hazards if hazard.concern is Concern.MANY_TO_MANY]


def test_an_unmarked_join_that_may_be_many_to_many_is_a_hazard_and_repeats_both_inputs_on_data():
    hazards = [hazard for hazard in suggest(TRAPS + JOIN).hazards if hazard.concern is Concern.MANY_TO_MANY]
    assert [hazard.site.label for hazard in hazards] == ["JOIN payments AS p"]
    assert "many to many" in hazards[0].message
    connection = duckdb.connect()
    connection.execute(TRAPS + ROWS)
    rows = connection.execute(JOIN).fetchall()
    # Each order appears twice, and so does each payment.
    assert sorted(row[0] for row in rows) == [10, 10, 11, 11] and sorted(row[1] for row in rows) == [20, 20, 21, 21]


@pytest.mark.parametrize(
    "sql",
    [
        # Many to one: each order matches one customer.
        "SELECT o.id FROM orders o JOIN customers c ON c.id = o.customer_id",
        # A marker states the cardinality: check proves it or reports it, not suggest.
        "SELECT o.id FROM orders o /**ASSERT NOT MANY TO MANY**/ JOIN payments p ON p.customer_id = o.customer_id",
        # An explicit CROSS JOIN pairs every row with every row on purpose.
        "SELECT o.id, p.id AS pid FROM orders o CROSS JOIN payments p",
        # A semi join never repeats a row.
        "SELECT o.id FROM orders o SEMI JOIN payments p ON p.customer_id = o.customer_id",
        # No known Unique Set on an input: its rows have no identity to repeat.
        "SELECT o.id FROM orders o JOIN logs l ON l.customer_id = o.customer_id",
        # DISTINCT merges the rows the join repeats.
        "SELECT DISTINCT o.customer_id FROM orders o JOIN payments p ON p.customer_id = o.customer_id",
        # On a text key equated with a column of unknown type: the proof cannot
        # bind it ('01' = 1), but the join is on a key, so a fact is missing,
        # not the join's cardinality.
        "CREATE TABLE regions (code VARCHAR PRIMARY KEY, name VARCHAR); "
        "WITH r AS (SELECT DISTINCT code, tag FROM raw) SELECT r.tag, g.name FROM r JOIN regions g ON g.code = r.code",
    ],
)
def test_a_join_that_cannot_repeat_both_inputs_or_is_marked_is_not_a_many_to_many_hazard(sql: str):
    assert joins(sql) == []


def test_a_left_join_that_may_be_many_to_many_is_a_hazard():
    assert joins("SELECT o.id FROM orders o LEFT JOIN payments p ON p.customer_id = o.customer_id") == [Concern.MANY_TO_MANY]


# Implicit cast -------------------------------------------------------------------

CASTS = """
CREATE TABLE codes (code VARCHAR PRIMARY KEY, n INTEGER);
CREATE TABLE nums (v INTEGER);
"""


def test_a_join_equality_that_casts_is_a_hazard_and_repeats_rows_on_data():
    sql = "SELECT * FROM nums JOIN codes ON codes.code = nums.v"
    assert [hazard.concern for hazard in suggest(CASTS + sql).hazards] == [Concern.IMPLICIT_CAST]
    connection = duckdb.connect()
    connection.execute(CASTS + "INSERT INTO codes VALUES ('01', 0), ('1', 0); INSERT INTO nums VALUES (1);")
    assert connection.execute("SELECT count(*) FROM nums JOIN codes ON codes.code = nums.v").fetchone() == (2,)


@pytest.mark.parametrize(
    "on",
    ["codes.n = nums.v", "codes.code = CAST(nums.v AS VARCHAR)"],
    ids=["same type", "explicit cast"],
)
def test_a_join_equality_without_an_implicit_cast_is_not_a_hazard(on: str):
    assert suggest(CASTS + f"SELECT * FROM nums JOIN codes ON {on}").hazards == ()


# Float equality -------------------------------------------------------------------

FLOATS = """
CREATE TABLE a (id BIGINT PRIMARY KEY, i INTEGER, s SMALLINT, d DECIMAL(10, 2), w DECIMAL(18, 3));
CREATE TABLE d (x DOUBLE, y DOUBLE, r REAL, b BIGINT, e DECIMAL(10, 2));
"""


def float_equality(sql: str, dialect: str = "duckdb") -> list[str]:
    return [hazard.message for hazard in suggest(FLOATS + sql, dialect=dialect).hazards if hazard.concern is Concern.FLOAT_EQUALITY]


def test_a_join_equality_with_a_double_is_a_hazard_and_matches_two_ids_on_data():
    sql = "SELECT * FROM a JOIN d ON a.id = d.x"
    hazard, = suggest(FLOATS + sql).hazards
    assert hazard.concern is Concern.FLOAT_EQUALITY
    assert hazard.site.label == "JOIN d"
    assert hazard.message == (
        "a.id = d.x compares a floating-point value for exact equality; compare within a tolerance or use an exact type"
    )
    connection = duckdb.connect()
    connection.execute(FLOATS + "INSERT INTO a (id) VALUES (9007199254740992), (9007199254740993); INSERT INTO d (x) VALUES (9007199254740992);")
    assert connection.execute("SELECT count(*) FROM a JOIN d ON a.id = d.x").fetchone() == (2,)


def test_a_double_equal_to_a_decimal_literal_is_a_hazard_and_misses_on_data():
    sql = "SELECT x FROM d WHERE x = 0.3"
    found, = float_equality(sql)
    assert found.startswith("x = 0.3 compares a floating-point value for exact equality")
    connection = duckdb.connect()
    connection.execute(FLOATS + "INSERT INTO d (x) VALUES (0.1::DOUBLE + 0.2::DOUBLE);")
    assert connection.execute(sql).fetchall() == []


@pytest.mark.parametrize(("sql", "written"), [
    # Any equality with an operand of a known floating type, in any dialect.
    ("SELECT d.x FROM d WHERE d.x = d.y", "d.x = d.y"),
    ("SELECT a.id FROM a JOIN d ON a.i = d.r", "a.i = d.r"),
    ("SELECT a.id FROM a JOIN d ON a.s = d.r", "a.s = d.r"),
    ("SELECT a.id FROM a JOIN d ON a.d = d.x", "a.d = d.x"),
    ("SELECT x FROM d WHERE x = 0.1", "x = 0.1"),
    ("SELECT x FROM d WHERE 1 = r", "1 = r"),
    ("SELECT x FROM d WHERE x <> y", "x <> y"),
    ("SELECT x FROM d WHERE x != 1", "x <> 1"),
    ("SELECT x FROM d WHERE x IS DISTINCT FROM y", "x IS DISTINCT FROM y"),
    ("SELECT x FROM d WHERE x IS NOT DISTINCT FROM b", "x IS NOT DISTINCT FROM b"),
    ("SELECT x FROM d WHERE x IN (1, 2.5)", "x IN (1, 2.5)"),
    ("SELECT x FROM d WHERE x NOT IN (1, 2.5)", "x IN (1, 2.5)"),
    ("SELECT b FROM d WHERE b IN (1, x)", "b IN (1, x)"),
    ("SELECT x FROM d WHERE b > 0 AND (x = 1 OR y = 2)", "x = 1"),
    # In a subquery's correlation, IN or EXISTS, and in HAVING.
    ("SELECT id FROM a WHERE id IN (SELECT x FROM d)", "id IN (SELECT x FROM d)"),
    ("SELECT id FROM a WHERE EXISTS (SELECT 1 FROM d WHERE d.x = a.id)", "d.x = a.id"),
    ("SELECT id FROM a WHERE NOT EXISTS (SELECT 1 FROM d WHERE d.x IS NOT DISTINCT FROM a.id)", "d.x IS NOT DISTINCT FROM a.id"),
    ("SELECT x FROM d GROUP BY x HAVING x = 1", "x = 1"),
])
def test_an_equality_with_a_float_operand_is_a_hazard(sql: str, written: str):
    found = float_equality(sql)
    assert f"{written} compares a floating-point value for exact equality; compare within a tolerance or use an exact type" in found


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "sqlite", "clickhouse", "mysql"])
def test_float_equality_is_a_hazard_in_every_dialect(dialect: str):
    assert len(float_equality("SELECT a.id FROM a JOIN d ON a.id = d.x", dialect=dialect)) == 1


def test_each_float_equality_in_a_condition_is_one_hazard():
    assert len(float_equality("SELECT x FROM d WHERE x = 1 OR y = 2")) == 2


@pytest.mark.parametrize("sql", [
    "SELECT a.id FROM a JOIN d ON a.i = d.b",  # INTEGER = BIGINT
    "SELECT a.id FROM a JOIN d ON a.d = d.e",  # DECIMAL = DECIMAL
    "SELECT a.id FROM a WHERE a.d = 0.1",  # DECIMAL = a decimal literal
    "SELECT d.x FROM d WHERE d.x < d.y",  # an ordering, not an equality
    "SELECT d.x FROM d WHERE d.x >= 0.1",
    "SELECT a.id FROM a JOIN f ON a.id = f.x",  # f's types are unknown
    "SELECT a.id FROM a WHERE a.i = 1e0",  # a literal's type is the dialect's: not guessed
    "SELECT a.id FROM a JOIN d ON a.id = CAST(d.x AS BIGINT)",  # compared in an exact type
])
def test_a_comparison_with_no_known_float_operand_or_no_equality_is_not_a_hazard(sql: str):
    assert float_equality(sql) == []


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
    hazards = suggest(EMPTY_SCHEMA + sql).hazards
    assert [hazard.concern for hazard in hazards] == [Concern.ALWAYS_EMPTY]
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
    assert Concern.ALWAYS_EMPTY not in [hazard.concern for hazard in suggest(EMPTY_SCHEMA + sql).hazards]


# A grouping key not selected ------------------------------------------------------
@pytest.mark.parametrize("sql", [
    "SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id, id",
    "SELECT count(*) AS n FROM orders GROUP BY customer_id",
    "SELECT customer_id, n FROM (SELECT customer_id, count(*) AS n FROM orders GROUP BY customer_id, id HAVING count(*) > 0) s",
])
def test_a_grouping_key_not_selected_is_a_hazard(sql: str):
    assert Concern.UNSELECTED_GROUPING in concerns(sql)


def test_a_grouping_key_not_selected_says_it_is_probably_a_mistake():
    [hazard] = [h for h in suggest(TRAPS + "SELECT count(*) AS n FROM orders GROUP BY customer_id").hazards
                if h.concern is Concern.UNSELECTED_GROUPING]
    assert hazard.message == (
        "GROUP BY term 'customer_id' does not appear in SELECT. This is probably a mistake: "
        "select it, or drop it from GROUP BY"
    )


@pytest.mark.parametrize("sql", [
    # The selected key determines the other.
    "SELECT id, count(*) AS n FROM orders GROUP BY id, customer_id",
    # Values for IN: equal rows are no surprise.
    "SELECT id FROM customers WHERE id IN (SELECT max(customer_id) FROM orders GROUP BY amount)",
])
def test_a_grouping_key_the_selected_keys_determine_or_not_read_as_rows_is_not_a_hazard(sql: str):
    assert Concern.UNSELECTED_GROUPING not in concerns(sql)


def test_a_grouping_key_not_selected_repeats_rows_on_data():
    db = duckdb.connect()
    db.execute("CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER); INSERT INTO u VALUES (1, 7), (2, 7)")
    rows = db.execute("SELECT t_id, count(*) AS n FROM u GROUP BY t_id, id").fetchall()
    assert rows == [(7, 1), (7, 1)]


def test_a_trap_under_having_is_still_a_hazard():
    # The call HAVING repeats is one.
    assert concerns(FAN + " HAVING sum(p.amount) > 0") == [Concern.FAN_TRAP]


def test_an_aggregate_only_having_writes_is_checked_for_traps_too():
    # count(*) counts the many-to-many join's rows, which decides the groups kept.
    assert concerns(FAN + " HAVING count(*) > 0") == [Concern.FAN_TRAP, Concern.FAN_TRAP]
