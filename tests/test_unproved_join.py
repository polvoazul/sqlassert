"""Why a join has no proved Join Cardinality (`unproved_join`)."""

import pytest

from sqlassert import ir
from sqlassert.ir.convert import IrParser
from sqlassert.python_engine import Engine
from sqlassert.rules.join_cardinality import UnprovedJoin, unproved_join
from sqlassert.sql_parse import SqlParser

SCHEMA = """
CREATE TABLE a (id INTEGER PRIMARY KEY, k INTEGER, ts TIMESTAMP, doc VARCHAR, amount DECIMAL(10, 2), ref INTEGER);
CREATE TABLE b (id INTEGER PRIMARY KEY, k INTEGER, start_ts TIMESTAMP, end_ts TIMESTAMP, day DATE, amount DECIMAL(10, 2));
CREATE TABLE f (k REAL, x INTEGER);
CREATE TABLE ranges (k INTEGER, start_ts TIMESTAMP, end_ts TIMESTAMP, PRIMARY KEY (k, start_ts));
"""


def classify(sql: str) -> UnprovedJoin | None:
    """The last join written, the outermost."""
    conversion = IrParser("duckdb").parse(SqlParser("duckdb").parse(SCHEMA + sql))
    program = conversion.program
    joins = list(dict.fromkeys(node for _, node in program.attachment_sites if isinstance(node, ir.Join)))
    with Engine().session(program, conversion.knowledge):
        return unproved_join(joins[-1])


CASES = [
    ("proved", "SELECT a.id FROM a JOIN b ON a.id = b.id", None),
    ("semi join", "SELECT a.id FROM a SEMI JOIN b ON a.k = b.k", None),
    ("table function reading the left input", "SELECT a.id FROM a LEFT JOIN json_each(a.doc) AS e ON true", None),
    ("UNNEST reading the left input", "SELECT a.id FROM a CROSS JOIN UNNEST([a.k]) AS s(x)", None),
    ("table function not reading the left input", "SELECT a.id FROM a CROSS JOIN range(0, 6) AS r(n)", UnprovedJoin("unconstrained")),
    (
        "table function not reading the left input, bound to a key",
        "SELECT a.id FROM a JOIN json_each('[1]') AS e ON e.key = a.doc",
        UnprovedJoin("untyped-key", "opaque-relation"),
    ),
    ("table function of unknown columns", "SELECT a.id FROM a CROSS JOIN read_csv('f.csv') AS f", UnprovedJoin("unsupported", construct="a table function")),
    (
        "unqualified key beside a table function",
        "SELECT a.id FROM a CROSS JOIN json_each(a.doc) AS e JOIN b ON b.k = ref",
        UnprovedJoin("no-unique-set", "undeclared-key"),
    ),
    (
        "unqualified key beside a table function of unknown columns",
        "SELECT a.id FROM a CROSS JOIN read_csv('f.csv') AS f JOIN b ON b.k = ref",
        UnprovedJoin("unresolved-key", construct="a table function"),
    ),
    (
        "key of a relation over a table function",
        "SELECT a.id FROM (SELECT a.k, e.value FROM a CROSS JOIN json_each(a.doc) AS e) AS m JOIN b ON b.id = m.k",
        None,
    ),
    (
        "key of a relation over a table function, not unique",
        "SELECT a.id FROM (SELECT a.k, e.value FROM a CROSS JOIN json_each(a.doc) AS e) AS m JOIN b ON b.k = m.k",
        UnprovedJoin("no-unique-set", "undeclared-key"),
    ),
    (
        "inside an opaque SELECT",
        "SELECT a.k, unnest([1, 2]) AS v FROM a JOIN b ON a.id = b.id GROUP BY a.k",
        UnprovedJoin("unsupported", construct="a set-returning function beside GROUP BY, QUALIFY or DISTINCT ON"),
    ),
    ("cross join", "SELECT a.id FROM a CROSS JOIN b", UnprovedJoin("unconstrained")),
    ("range join", "SELECT a.id FROM a JOIN b ON a.ts >= b.start_ts AND a.ts < b.end_ts", UnprovedJoin("unconstrained")),
    ("ambiguous column", "SELECT a.id FROM a JOIN missing AS m ON ts = m.k", UnprovedJoin("unresolved-key")),
    (
        "VALUES key of mixed literals",
        "SELECT a.id FROM a JOIN (VALUES (1), (1.5)) AS v (k) ON a.k = v.k",
        UnprovedJoin("untyped-key", "values"),
    ),
    (
        "VALUES key that is an expression",
        "SELECT a.id FROM a JOIN (VALUES (1 + 1), (3)) AS v (k) ON a.k = v.k",
        UnprovedJoin("untyped-key", "values"),
    ),
    (
        "typed VALUES key against a decimal",
        "SELECT a.id FROM a JOIN (VALUES (1), (2)) AS v (k) ON a.amount = v.k",
        UnprovedJoin("untyped-key", "type-mismatch"),
    ),
    ("typed VALUES key", "SELECT a.id FROM a JOIN (VALUES (1), (2)) AS v (k) ON a.k = v.k", None),
    (
        "computed key",
        "SELECT a.id FROM a JOIN (SELECT date_trunc('month', ts) AS day FROM a) AS m ON m.day = a.id",
        UnprovedJoin("untyped-key", "computed"),
    ),
    (
        "opaque relation key",
        "SELECT a.id FROM a JOIN (missing JOIN b ON true) AS m ON a.k = m.k",
        UnprovedJoin("untyped-key", "opaque-relation"),
    ),
    (
        "opaque SELECT",
        "SELECT a.id FROM a JOIN (SELECT b.id, b.k, unnest([1, 2]) AS v FROM b JOIN f ON f.x = b.id GROUP BY b.id, b.k) AS m "
        "ON a.k = m.k",
        UnprovedJoin("unsupported", construct="a set-returning function beside GROUP BY, QUALIFY or DISTINCT ON"),
    ),
    (
        "key read from an opaque SELECT",
        "SELECT a.id FROM a JOIN (SELECT k FROM (SELECT b.id, b.k, unnest([1, 2]) AS v FROM b JOIN f ON f.x = b.id GROUP BY b.id, b.k)) "
        "AS m ON a.k = m.k",
        UnprovedJoin("untyped-key", "opaque-relation", "a set-returning function beside GROUP BY, QUALIFY or DISTINCT ON"),
    ),
    (
        "key read through a QUALIFY that drops rows",
        "SELECT a.id FROM a JOIN (SELECT k FROM b QUALIFY rank() OVER (ORDER BY id) = 1) AS m ON a.k = m.k",
        UnprovedJoin("no-unique-set", "undeclared-key"),
    ),
    (
        "partition key that is not selected",
        "SELECT a.id FROM a JOIN (SELECT k FROM b QUALIFY row_number() OVER (PARTITION BY k, day ORDER BY id) = 1) AS m ON a.k = m.k",
        UnprovedJoin("no-unique-set", "partial-key"),
    ),
    (
        "set operation key typed in both inputs by name",
        "SELECT s.id FROM (SELECT * FROM a UNION ALL BY NAME SELECT * FROM b) AS s JOIN b ON b.id = s.id",
        None,
    ),
    (
        "set operation key present in one input only",
        "SELECT a.id FROM a JOIN (SELECT id FROM a UNION ALL BY NAME SELECT k FROM b) AS s ON a.k = s.k",
        UnprovedJoin("untyped-key", "computed"),
    ),
    (
        "set operation key whose inputs do not pair",
        "SELECT a.id FROM a JOIN (SELECT * FROM a UNION ALL BY NAME SELECT * FROM missing) AS s ON a.k = s.k",
        UnprovedJoin("untyped-key", "set-operation"),
    ),
    ("undeclared table key", "SELECT a.id FROM a JOIN missing AS m ON a.k = m.k", UnprovedJoin("untyped-key", "table")),
    # A recursive CTE's column has its base case's type: a typed one binds, an untyped one loses its type where the base case does.
    (
        "recursive CTE key typed by its base case",
        "WITH RECURSIVE r AS (SELECT id, k FROM a UNION ALL SELECT r.id, r.k FROM r) SELECT a.id FROM a JOIN r ON a.k = r.k",
        UnprovedJoin("no-unique-set", "cascade", "a recursive CTE or view"),
    ),
    (
        "recursive CTE key whose base case is an undeclared table",
        "WITH RECURSIVE r AS (SELECT id, k FROM missing UNION ALL SELECT r.id, r.k FROM r) SELECT a.id FROM a JOIN r ON a.k = r.k",
        UnprovedJoin("untyped-key", "table"),
    ),
    (
        "recursive CTE key whose base case is a float",
        "WITH RECURSIVE r AS (SELECT k FROM f UNION ALL SELECT r.k FROM r) SELECT a.id FROM a JOIN r ON a.k = r.k",
        UnprovedJoin("untyped-key", "type-mismatch"),
    ),
    (
        "recursive CTE key whose base case is computed",
        "WITH RECURSIVE r AS (SELECT id, date_trunc('month', ts) AS k FROM a UNION ALL SELECT r.id, r.k FROM r) "
        "SELECT a.id FROM a JOIN r ON a.ts = r.k",
        UnprovedJoin("untyped-key", "computed"),
    ),
    ("type mismatch", "SELECT a.id FROM a JOIN f ON a.k = f.k", UnprovedJoin("untyped-key", "type-mismatch")),
    # An expression that binds a column is no expression-key: what is left is what binds nothing.
    ("cast key binds the matched column", "SELECT a.id FROM a JOIN b ON b.day = CAST(a.ts AS DATE)", UnprovedJoin("no-unique-set", "undeclared-key")),
    ("negated key binds both columns", "SELECT a.id FROM a JOIN b ON a.amount = -b.amount", UnprovedJoin("no-unique-set", "undeclared-key")),
    ("added literal key binds both columns", "SELECT a.id FROM a JOIN b ON a.k = b.k + 1", UnprovedJoin("no-unique-set", "undeclared-key")),
    ("one equality that binds beside one that does not", "SELECT a.id FROM a JOIN b ON a.k = b.k + 1 AND a.id = b.k / 2", UnprovedJoin("no-unique-set", "undeclared-key")),
    ("quotient key", "SELECT a.id FROM a JOIN b ON a.k = b.k / 2", UnprovedJoin("expression-key")),
    ("cast to a type the column is not", "SELECT a.id FROM a JOIN b ON b.day = CAST(a.ts AS TIMESTAMP)", UnprovedJoin("expression-key")),
    ("cast to a lossy type", "SELECT a.id FROM a JOIN b ON a.k = CAST(b.amount AS DOUBLE)", UnprovedJoin("expression-key")),
    ("expression that may be NULL for a value", "SELECT a.id FROM a JOIN b ON a.k = nullif(b.k, 0.5)", UnprovedJoin("expression-key")),
    ("an injective function against a product", "SELECT a.id FROM a JOIN b ON a.k + 1 = b.k / 2", UnprovedJoin("expression-key")),
    ("no unique set", "SELECT a.id FROM a JOIN b ON a.k = b.k", UnprovedJoin("no-unique-set", "undeclared-key")),
    # no-unique-set, by who can act: a cascade (ours), a partial key (nobody), an undeclared key (the project).
    (
        "undeclared key: no key beside the bound column, nothing unproved below",
        "SELECT a.id FROM a JOIN b ON a.id = b.id JOIN b AS c ON c.k = a.k",
        UnprovedJoin("no-unique-set", "undeclared-key"),
    ),
    (
        "undeclared key: a semi join below has no cardinality to prove",
        "SELECT a.id FROM a SEMI JOIN b ON a.k = b.k JOIN b AS c ON c.k = a.k",
        UnprovedJoin("no-unique-set", "undeclared-key"),
    ),
    (
        "partial key: a range over the rest of the key",
        "SELECT a.id FROM a JOIN ranges ON a.k = ranges.k AND a.ts >= ranges.start_ts AND a.ts < ranges.end_ts",
        UnprovedJoin("no-unique-set", "partial-key"),
    ),
    (
        "partial key: nothing else written",
        "SELECT a.id FROM a JOIN ranges ON a.k = ranges.k",
        UnprovedJoin("no-unique-set", "partial-key"),
    ),
    (
        "partial key boundary: an equality that binds nothing remains",
        "SELECT a.id FROM a JOIN ranges ON a.k = ranges.k AND a.id = ranges.k / 2",
        UnprovedJoin("no-unique-set", "undeclared-key"),
    ),
    (
        "cascade: an upstream join with no proved cardinality",
        "SELECT a.id FROM a JOIN b ON a.ts >= b.start_ts AND a.ts < b.end_ts JOIN b AS c ON c.k = a.k",
        UnprovedJoin("no-unique-set", "cascade"),
    ),
    (
        "cascade before partial key: the key is partly bound, but an input has an unproved join",
        "SELECT a.id FROM a JOIN b ON a.ts >= b.start_ts AND a.ts < b.end_ts JOIN ranges AS r ON r.k = a.k",
        UnprovedJoin("no-unique-set", "cascade"),
    ),
    (
        "cascade: an unsupported construct below, with an unproved join",
        "SELECT a.id FROM a JOIN (SELECT b.id, b.k, unnest([1, 2]) AS v FROM b JOIN f ON f.x = b.id GROUP BY b.id, b.k) AS m "
        "ON a.id = m.id JOIN b AS c ON c.k = a.k",
        UnprovedJoin("no-unique-set", "cascade", "a set-returning function beside GROUP BY, QUALIFY or DISTINCT ON"),
    ),
    (
        "cascade: only an unsupported construct below",
        "SELECT a.id FROM a JOIN (SELECT CAST(k AS INTEGER) AS k FROM (SELECT b.id, b.k, unnest([1, 2]) AS v FROM b JOIN f ON f.x = b.id "
        "GROUP BY b.id, b.k)) AS m ON a.k = m.k",
        UnprovedJoin("no-unique-set", "cascade", "a set-returning function beside GROUP BY, QUALIFY or DISTINCT ON"),
    ),
]


@pytest.mark.parametrize(("label", "sql", "expected"), CASES, ids=[label for label, _, _ in CASES])
def test_unproved_join(label: str, sql: str, expected: UnprovedJoin | None):
    assert classify(sql) == expected


# A table function or UNNEST of a dialect reading the left input is One To Many (proved: no reason). Comma joins
# have no marker site to assert, so this asks the analysis directly.
FUNCTION_DIALECTS = [
    ("bigquery", "SELECT t.id FROM t, UNNEST(t.arr) AS x", "CREATE TABLE t (id INT64 NOT NULL, arr ARRAY<INT64>, PRIMARY KEY (id) NOT ENFORCED);"),
    ("bigquery", "SELECT t.id FROM t CROSS JOIN UNNEST(t.arr) AS x", "CREATE TABLE t (id INT64 NOT NULL, arr ARRAY<INT64>, PRIMARY KEY (id) NOT ENFORCED);"),
    ("redshift", "SELECT t.id FROM t, t.arr AS x", "CREATE TABLE t (id INT NOT NULL, arr SUPER, PRIMARY KEY (id));"),
]


@pytest.mark.parametrize(("dialect", "sql", "schema"), FUNCTION_DIALECTS, ids=[sql for _, sql, _ in FUNCTION_DIALECTS])
def test_a_correlated_function_of_another_dialect_is_one_to_many(dialect: str, sql: str, schema: str):
    conversion = IrParser(dialect).parse(SqlParser(dialect).parse(schema + sql))
    joins = [relation for relation in conversion.program.relations() if isinstance(relation, ir.Join)]
    assert len(joins) == 1 and joins[0].lateral
    with Engine().session(conversion.program, conversion.knowledge):
        assert unproved_join(joins[0]) is None
