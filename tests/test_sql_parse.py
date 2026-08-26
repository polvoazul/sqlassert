import pytest
import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.tokens import TokenType

from sqlassert.sql_parse import (
    ParsedProgram,
    SqlParser,
    SqlassertToken,
    _unresolved_markers,
    sqlassert_dialect,
    unique_set_assertions,
)


def test_parsed_program_coercion():
    stmt = sqlglot.parse_one("CREATE TABLE t (id INT)")
    query = sqlglot.parse_one("SELECT * FROM t")

    # Pass lists (enumerables) instead of tuples: ParsedProgram's own
    # constructor accepts any iterable even though its fields are typed as
    # tuples, since that's what every field actually stores.
    program = ParsedProgram(
        create_statements=[stmt],  # ty: ignore[invalid-argument-type]
        root_select=query,  # ty: ignore[invalid-argument-type]
        diagnostics=[],  # ty: ignore[invalid-argument-type]
    )

    assert isinstance(program.create_statements, tuple)
    assert program.create_statements == (stmt,)
    assert isinstance(program.diagnostics, tuple)
    assert program.diagnostics == ()


# Adding a token to a dialect is only safe while it stays invisible to every
# other rule. These guard that, since a silent collision would change how
# ordinary SQL parses.

ORDINARY_SQL = [
    "CREATE TABLE t (id INT UNIQUE, x INT)",
    "CREATE TABLE t (a INT, b INT, UNIQUE (a, b))",
    "CREATE TABLE u (id INT PRIMARY KEY, name VARCHAR)",
    "SELECT unique_id FROM t AS unique_t",
    'SELECT * FROM t AS "unique"',
    "SELECT * FROM a JOIN b ON a.x = b.x",
    "SELECT * FROM a LEFT JOIN b ON a.x = b.x",
    "WITH c AS (SELECT 1 AS id) SELECT * FROM c",
    "SELECT 2 ** 3 FROM t",
    "SELECT 2 ** 3 / 1 FROM t",
    "SELECT POWER(x, 2) FROM t",
    "SELECT * FROM t WHERE (a + b) / 2 > 1",
]


@pytest.mark.parametrize("sql", ORDINARY_SQL)
def test_the_marker_dialect_parses_ordinary_sql_exactly_as_its_base_does(sql: str):
    base = [statement.sql(dialect="duckdb") for statement in sqlglot.parse(sql, read="duckdb") if statement]
    ours = [
        statement.sql(dialect="duckdb")
        for statement in sqlglot.parse(sql, read=sqlassert_dialect("duckdb"))
        if statement
    ]

    assert ours == base


@pytest.mark.parametrize("dialect", ["oracle", "redshift"])
def test_the_marker_leaves_legacy_outer_join_syntax_alone(dialect: str):
    """`(+)` is SQLGlot's own JOIN_MARKER token, which ours must not collide with."""
    sql = "SELECT * FROM a, b WHERE a.x = b.x(+)"

    ours = sqlglot.parse_one(sql, read=sqlassert_dialect(dialect)).sql(dialect=dialect)

    assert ours == sqlglot.parse_one(sql, read=dialect).sql(dialect=dialect)


def test_the_marker_token_can_equal_nothing_but_itself():
    """A plain Enum, never an IntEnum: SQLGlot's TokenType is an IntEnum, so a
    member of ours sharing an integer value would be swallowed by its keyword
    sets."""
    marker = SqlassertToken.SQLASSERT_UNIQUE
    parser = Dialect.get_or_raise("duckdb").parser_class

    assert not any(marker == token_type for token_type in TokenType)
    assert marker not in parser.ID_VAR_TOKENS
    assert marker not in parser.TABLE_ALIAS_TOKENS


def test_one_dialect_class_is_built_per_base_dialect():
    """SQLGlot registers dialect classes by name, so they must be cached."""
    assert sqlassert_dialect("duckdb") is sqlassert_dialect("duckdb")
    assert sqlassert_dialect("duckdb") is not sqlassert_dialect("postgres")


def test_parsing_registers_no_dialect_beyond_the_one_it_needs():
    """The parser resolves its dialect once and reuses it.

    Passing the resolved class back into the factory builds a second, doubly
    wrapped dialect and defeats the cache.
    """
    before = set(Dialect.classes)
    parser = SqlParser("duckdb")

    parser.parse("SELECT * FROM a /**UNIQUE**/ JOIN b ON a.x = b.x")
    parser.parse("SELEC * FROM")  # the failure path re-tokenizes

    assert set(Dialect.classes) - before <= {"sqlassertduckdb"}


def test_a_marker_the_dialect_did_not_recognize_is_reported():
    """The safety net for the SQLGlot coupling in `sqlassert_dialect`.

    Parsing with the plain base dialect is what the world looks like if that
    coupling breaks at the tokenizer: the marker stays an ordinary comment and
    quietly becomes nothing. Reached directly because the public seam cannot
    produce it without breaking SQLGlot — and without this guard the program
    would report as proved.
    """
    sql = "CREATE TABLE u (id INTEGER PRIMARY KEY);\nSELECT * FROM s /**UNIQUE**/ JOIN u ON s.id = u.id"
    unrecognized = [statement for statement in sqlglot.parse(sql, read="duckdb") if statement]

    diagnostics = _unresolved_markers(sql, unrecognized)

    assert [diagnostic.code for diagnostic in diagnostics] == ["unrecognized-marker"]
    origin = diagnostics[0].origin
    assert origin is not None
    assert origin.line == 2


def test_recognized_markers_leave_the_reconciliation_quiet():
    sql = "CREATE TABLE u (id INTEGER PRIMARY KEY);\nSELECT * FROM s /**UNIQUE**/ JOIN u ON s.id = u.id"
    parser = SqlParser("duckdb")
    statements = [statement for statement in sqlglot.parse(sql, read=parser.dialect_class) if statement]

    assert _unresolved_markers(sql, statements) == []


# Unique Set Assertion markers -- `/**UNIQUE(...)**/` and
# `/**PRIMARY KEY(...)**/` -- attach at the one production shared by a Root
# Select, a CTE, a view body, and a subquery, trailing right before whatever
# closes that Select Expression.

UNIQUE_SET_ATTACHMENT_SITES = [
    ("root select", "SELECT id FROM t /**UNIQUE(id)**/", 1),
    ("view body", "CREATE VIEW v AS SELECT id FROM t /**UNIQUE(id)**/", 1),
    ("cte body", "WITH c AS (SELECT id FROM t /**UNIQUE(id)**/) SELECT * FROM c", 1),
    ("subquery body", "SELECT * FROM (SELECT id FROM t /**UNIQUE(id)**/) AS s", 1),
]


@pytest.mark.parametrize(("label", "sql", "expected_count"), UNIQUE_SET_ATTACHMENT_SITES)
def test_a_unique_set_assertion_marker_attaches_at_every_select_expression_site(
    label: str, sql: str, expected_count: int
):
    parser = SqlParser("duckdb")
    program = parser.parse(sql)

    assert program.diagnostics == (), label
    statements = (*program.create_statements, program.root_select)
    found = [
        markers
        for statement in statements
        if statement is not None
        for select in statement.find_all(exp.Select)
        for markers in [unique_set_assertions(select)]
        if markers
    ]
    assert len(found) == expected_count, label
    assert found[0] == (("unique", ("id",), 1),), label


def test_a_primary_key_flavored_marker_is_recognized():
    parser = SqlParser("duckdb")
    program = parser.parse("SELECT id FROM t /**PRIMARY KEY(id)**/")

    assert program.diagnostics == ()
    assert program.root_select is not None
    markers = unique_set_assertions(program.root_select)  # ty: ignore[invalid-argument-type]
    assert markers == (("key", ("id",), 1),)


def test_stacked_unique_set_assertion_markers_are_all_recognized():
    parser = SqlParser("duckdb")
    program = parser.parse("SELECT a, b FROM t /**UNIQUE(a)**/ /**PRIMARY KEY(a, b)**/")

    assert program.diagnostics == ()
    assert program.root_select is not None
    markers = unique_set_assertions(program.root_select)  # ty: ignore[invalid-argument-type]
    assert markers == (("unique", ("a",), 1), ("key", ("a", "b"), 1))


UNIQUE_SET_MARKER_SHAPES = [
    "/**UNIQUE(id)**/",
    "/**UNIQUE(id, name)**/",
    "/**PRIMARY KEY(id)**/",
    "/**PRIMARY KEY(id, name)**/",
]


@pytest.mark.parametrize("marker", UNIQUE_SET_MARKER_SHAPES)
def test_a_unique_set_assertion_marker_does_not_disturb_ordinary_sql_around_it(marker: str):
    sql = f"SELECT id, name FROM t {marker}"
    base = sqlglot.parse_one("SELECT id, name FROM t", read="duckdb").sql(dialect="duckdb")

    parser = SqlParser("duckdb")
    program = parser.parse(sql)

    assert program.diagnostics == ()
    assert program.root_select is not None
    assert program.root_select.sql(dialect="duckdb") == base
