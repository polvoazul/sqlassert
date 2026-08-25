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
)


def test_parsed_program_coercion():
    stmt = sqlglot.parse_one("CREATE TABLE t (id INT)")
    query = sqlglot.parse_one("SELECT * FROM t")

    # Pass lists (enumerables) instead of tuples: ParsedProgram's own
    # constructor accepts any iterable even though its fields are typed as
    # tuples, since that's what every field actually stores.
    program = ParsedProgram(
        create_statements=[stmt],  # ty: ignore[invalid-argument-type]
        root_select=query,
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
