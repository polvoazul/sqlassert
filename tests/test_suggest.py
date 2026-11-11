"""`suggest`: the properties sqlassert can prove about unannotated SQL, as markers to paste."""

from __future__ import annotations

import io

import pytest

from sqlassert import (
    ExactlyOneJoin, ManyToOneJoin, NonNullColumns, NonNullUniqueSet, Outcome, UniqueJoin, UniqueSet, analyze, suggest,
)
from sqlassert.__main__ import main
from sqlassert.properties import EnumeratedDomain, ExhaustiveCase
from sqlassert.ir.convert import IrParser
from sqlassert.sql_parse import SqlParser
from sqlassert.suggest import annotate, candidates

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE, name VARCHAR NOT NULL, nick VARCHAR);
CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);
"""


def markers(sql: str, **kwargs) -> list[tuple[str, str]]:
    return [(s.site.label, s.marker) for s in suggest(sql, **kwargs).suggestions]


def test_a_provable_join_is_suggested_in_its_most_specific_form():
    report = suggest(SCHEMA + "SELECT * FROM sessions\nLEFT JOIN users ON sessions.user_id = users.id")

    join, = (s for s in report.suggestions if isinstance(s.property, ManyToOneJoin))
    assert (join.site.label, join.site.line, join.marker) == ("LEFT JOIN users", 5, "/**ASSERT MANY TO ONE**/")
    assert join.evidence.value


def test_a_join_that_matches_exactly_one_row_is_suggested_in_that_stronger_form():
    sql = SCHEMA + """
    CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id));
    SELECT orders.id, users.name FROM orders JOIN users ON users.id = orders.user_id
    """
    join, = (s for s in suggest(sql).suggestions if s.site.label == "JOIN users")
    assert (type(join.property), join.marker) == (ExactlyOneJoin, "/**ASSERT EXACTLY ONE**/")


def test_a_join_already_marked_is_not_suggested_again():
    sql = SCHEMA + """
    CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id));
    SELECT orders.id, users.name FROM orders /**ASSERT MANY TO ONE**/ JOIN users ON users.id = orders.user_id
    """
    assert "JOIN users" not in [s.site.label for s in suggest(sql).suggestions]


def test_a_join_whose_left_input_is_unique_is_suggested_as_one_to_many():
    report = suggest(SCHEMA + "SELECT * FROM users JOIN sessions ON sessions.user_id = users.id")
    # The inner join's ON equality rejects a NULL user_id.
    assert [s.marker for s in report.suggestions] == ["/**ASSERT ONE TO MANY**/", "/**ASSERT NOT NULL(id, name, user_id)**/"]


def test_a_many_to_many_join_is_not_suggested():
    sql = SCHEMA + "SELECT s.user_id FROM sessions AS s JOIN sessions AS t ON t.user_id = s.user_id"
    assert not any(s.site.label.startswith("JOIN") for s in suggest(sql).suggestions)


def test_only_the_strongest_form_of_each_minimal_unique_set_is_suggested():
    assert markers(SCHEMA + "SELECT id, email, nick FROM users") == [
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(id)**/"),
        ("Root Select", "/**ASSERT UNIQUE(email)**/"),
    ]


def test_non_null_columns_not_already_implied_by_a_suggested_key_are_suggested_together():
    assert markers(SCHEMA + "SELECT id, name, nick, 1 AS one FROM users") == [
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(id)**/"),
        ("Root Select", "/**ASSERT NOT NULL(name, one)**/"),
    ]


def test_a_composite_grouping_key_is_suggested_without_its_supersets():
    assert markers(SCHEMA + "SELECT user_id, ts, count(*) AS n FROM sessions GROUP BY user_id, ts") == [
        ("Root Select", "/**ASSERT UNIQUE(user_id, ts)**/"),
        ("Root Select", "/**ASSERT NOT NULL(n)**/"),
    ]


def test_a_unique_set_wider_than_max_size_is_still_found_and_shrunk_to_a_minimal_one():
    sql = """
    CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER, d INTEGER, e INTEGER);
    SELECT a, b, c, d, max(e) AS e FROM t GROUP BY a, b, c, d
    """
    assert markers(sql, max_size=2) == [("Root Select", "/**ASSERT UNIQUE(a, b, c, d)**/")]


def test_every_select_expression_is_an_attachment_site():
    sql = SCHEMA + """
    CREATE VIEW active_users AS SELECT id FROM users WHERE id > 0;
    WITH recent AS (SELECT DISTINCT user_id FROM sessions)
    SELECT s.user_id
    FROM (SELECT id AS user_id FROM active_users) AS s
    JOIN recent ON recent.user_id = s.user_id
    """
    assert markers(sql) == [
        ("view active_users", "/**ASSERT NOT NULL UNIQUE(id)**/"),
        ("CTE recent", "/**ASSERT UNIQUE(user_id)**/"),
        ("subquery s", "/**ASSERT NOT NULL UNIQUE(user_id)**/"),
        ("JOIN recent", "/**ASSERT ONE TO ONE**/"),
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(user_id)**/"),
    ]


def test_a_set_operation_is_a_site_after_its_last_token():
    # The first member has a site of its own; the last one ends where the
    # whole query does, so a marker there is the whole query's.
    sql = SCHEMA + "SELECT id FROM users UNION SELECT user_id FROM sessions"
    assert markers(sql) == [
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(id)**/"),
        ("Root Select", "/**ASSERT UNIQUE(id)**/"),
    ]
    annotated = suggest(sql).annotated()
    assert annotated.endswith("FROM sessions /**ASSERT UNIQUE(id)**/")
    assert all(a.outcome.value == "proved" for a in analyze(annotated).assertions)


def test_columns_a_marker_cannot_name_unambiguously_are_left_out():
    sql = SCHEMA + "SELECT users.id, other.id FROM users JOIN users AS other ON other.id = users.id"
    # A self join on the key matches exactly one row.
    assert [m for _, m in markers(sql)] == ["/**ASSERT EXACTLY ONE**/"]


def test_a_join_inside_a_select_lowered_as_opaque_is_not_suggested():
    sql = SCHEMA + "SELECT s.user_id FROM sessions s LEFT JOIN users u ON u.id = s.user_id GROUP BY s.user_id HAVING max(u.name) = 'a'"
    assert not any(isinstance(s.property, UniqueJoin) for s in suggest(sql).suggestions)


def test_suggest_skips_a_subquery_inside_an_opaque_select():
    sql = """
    SELECT id FROM accounts
    WHERE id IN (SELECT user_id FROM users)
    QUALIFY ROW_NUMBER() OVER (PARTITION BY other ORDER BY id) = 1
    """
    assert suggest(sql).suggestions == ()


def test_a_quoted_column_name_is_quoted_in_its_marker():
    sql = SCHEMA + 'SELECT id AS "user id" FROM users'
    assert markers(sql) == [("Root Select", '/**ASSERT NOT NULL UNIQUE("user id")**/')]


def test_properties_already_asserted_or_declared_are_not_suggested_again():
    sql = SCHEMA + """
    WITH named AS (
        SELECT id, name, nick FROM users /**DECLARE UNIQUE(nick)**/ /**ASSERT NOT NULL UNIQUE(id)**/
    )
    SELECT * FROM named
    """
    assert markers(sql) == [
        ("CTE named", "/**ASSERT NOT NULL(name)**/"),
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(id)**/"),
        ("Root Select", "/**ASSERT UNIQUE(nick)**/"),
        ("Root Select", "/**ASSERT NOT NULL(name)**/"),
    ]


def test_suggestions_carry_their_property():
    report = suggest(SCHEMA + "SELECT id, email, name FROM users")
    assert [type(s.property) for s in report.suggestions] == [NonNullUniqueSet, UniqueSet, NonNullColumns]


def test_a_program_that_cannot_be_parsed_suggests_nothing_and_says_why():
    report = suggest("SELECT FROM (")
    assert report.suggestions == ()
    assert report.diagnostics


ORDERS = "CREATE TABLE orders (id INTEGER PRIMARY KEY, status VARCHAR NOT NULL, qty INTEGER);\n"


def test_a_column_with_a_small_enumerated_domain_is_suggested_as_an_enum():
    sql = ORDERS + "SELECT id, status FROM orders WHERE status IN ('paid', 'new')"
    assert markers(sql) == [
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(id)**/"),
        ("Root Select", "/**ASSERT NOT NULL(status)**/"),
        ("Root Select", "/**ASSERT ENUM(status IN ('new', 'paid'))**/"),
    ]


def test_enum_values_are_listed_in_value_order():
    sql = ORDERS + "SELECT qty FROM orders WHERE qty IN (10, 2, -1)"
    assert ("Root Select", "/**ASSERT ENUM(qty IN (-1, 2, 10))**/") in markers(sql)


def test_a_large_enumerated_domain_is_not_suggested():
    values = ", ".join(str(n) for n in range(11))
    sql = ORDERS + f"SELECT qty FROM orders WHERE qty IN ({values})"
    assert not any(isinstance(s.property, EnumeratedDomain) for s in suggest(sql).suggestions)


def test_a_literal_column_is_not_suggested_as_an_enum():
    sql = ORDERS + "SELECT id, 'x' AS tag FROM orders"
    assert not any(isinstance(s.property, EnumeratedDomain) for s in suggest(sql).suggestions)


EXHAUSTIVE_SOURCE = ORDERS + "WITH o AS (SELECT * FROM orders WHERE status IN ('new', 'paid')) "


def test_a_simple_case_proved_exhaustive_is_suggested():
    sql = EXHAUSTIVE_SOURCE + "SELECT id, CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n FROM o"
    report = suggest(sql)
    exhaustive = [s for s in report.suggestions if isinstance(s.property, ExhaustiveCase)]
    assert [(s.site.label, s.marker) for s in exhaustive] == [("CASE status", "/**ASSERT EXHAUSTIVE**/")]
    assert "SELECT id, /**ASSERT EXHAUSTIVE**/ CASE status" in report.annotated(exhaustive)


@pytest.mark.parametrize(
    "case",
    [
        "CASE status WHEN 'new' THEN 1 END",
        "CASE qty WHEN 1 THEN 1 END",
        "CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 WHEN upper('x') THEN 3 END",
        "/**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END",
        "max(CASE status WHEN 'new' THEN 'a' WHEN 'paid' THEN 'b' END) OVER ()",
    ],
    ids=["an arm missing", "no finite domain", "a WHEN no marker allows", "already marked", "inside an opaque expression"],
)
def test_a_case_not_provably_exhaustive_or_not_markable_is_not_suggested(case: str):
    sql = EXHAUSTIVE_SOURCE + f"SELECT id, {case} AS n FROM o"
    assert not any(isinstance(s.property, ExhaustiveCase) for s in suggest(sql).suggestions)


ROUND_TRIP_PROGRAMS = [
    SCHEMA + "SELECT * FROM sessions LEFT JOIN users ON sessions.user_id = users.id",
    SCHEMA + "SELECT id, email, name, nick, coalesce(nick, 'x') AS label FROM users WHERE id > 0",
    SCHEMA + "SELECT user_id, max(ts) AS ts FROM sessions GROUP BY user_id",
    SCHEMA + """
    CREATE VIEW active_users AS SELECT id, email FROM users WHERE id > 0;
    WITH latest AS (
        SELECT user_id, ts FROM sessions
        QUALIFY row_number() OVER (PARTITION BY user_id ORDER BY ts DESC) = 1
    )
    SELECT a.id, l.ts
    FROM (SELECT * FROM active_users) AS a
    JOIN latest AS l ON l.user_id = a.id
    LEFT JOIN users AS u ON u.id = a.id
    ORDER BY 1;
    """,
    SCHEMA + "SELECT id FROM users UNION SELECT DISTINCT user_id FROM sessions ORDER BY 1",
    SCHEMA + 'SELECT DISTINCT name AS "full name", nick FROM users',
    SCHEMA + """
    CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id));
    SELECT orders.id, users.name FROM orders JOIN users ON users.id = orders.user_id
    """,
    """
    CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER, d INTEGER, e INTEGER, PRIMARY KEY (a, b));
    SELECT a, b, c, d, max(e) AS m FROM t GROUP BY a, b, c, d
    """,
    ORDERS + """
    WITH o AS (SELECT id, status, qty FROM orders WHERE status IN ('new', 'paid') AND qty IN (1, 2.5))
    SELECT id, status,
        CASE status WHEN 'new' THEN 'n' WHEN 'paid' THEN 'p' END AS code,
        CASE qty WHEN 1 THEN 'one' WHEN 2.5 THEN 'more' ELSE 'none' END AS amount
    FROM o
    """,
]


@pytest.mark.parametrize("sql", ROUND_TRIP_PROGRAMS)
def test_every_suggested_marker_pasted_into_the_sql_is_proved(sql: str):
    suggestions = suggest(sql)
    assert suggestions.suggestions

    report = analyze(suggestions.annotated())

    assert report.diagnostics == ()
    assert len(report.assertions) == len(suggestions.suggestions)
    assert all(assertion.outcome is Outcome.PROVED for assertion in report.assertions)
    # Pasting the markers locks the facts in, so nothing is left to suggest.
    assert suggest(suggestions.annotated()).suggestions == ()


@pytest.mark.parametrize("sql", ROUND_TRIP_PROGRAMS)
def test_every_candidate_marker_pasted_into_the_sql_becomes_its_own_assertion(sql: str):
    program = IrParser("duckdb").parse(SqlParser("duckdb").parse(sql)).program
    found = list(candidates(program, max_size=2))

    report = analyze(annotate(sql, found))

    assert report.diagnostics == ()
    assert len(report.assertions) == len(found)
    assert {type(c.property) for c in found} >= {UniqueSet, NonNullUniqueSet, NonNullColumns}


def test_the_cli_prints_one_line_per_suggestion(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT * FROM sessions\nJOIN users ON sessions.user_id = users.id")

    assert main(["suggest", str(path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["marker", "line", "5", "JOIN", "users", "Many", "To", "One", "Join", "/**ASSERT", "MANY", "TO", "ONE**/"]
    assert len(lines) == len(suggest(path.read_text()).suggestions)


def test_the_cli_names_every_suggested_property(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text("CREATE TABLE t (a INT NOT NULL, s TEXT NOT NULL);\nSELECT CASE s WHEN 'p' THEN 'x' ELSE 'y' END AS k FROM t")

    assert main(["suggest", str(path)]) == 0

    assert "Enumerated Domain  /**ASSERT ENUM(k IN ('x', 'y'))**/" in capsys.readouterr().out


def test_the_cli_can_explain_the_evidence(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT id FROM users")

    assert main(["suggest", "--explain", str(path)]) == 0
    assert "PROVED" in capsys.readouterr().out


def test_the_cli_reports_diagnostics_and_fails(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text("SELECT FROM (")

    assert main(["suggest", str(path)]) == 1
    assert "sql-parse-failed" in capsys.readouterr().err


def test_the_cli_prints_connection_warnings_without_failing(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text("SELECT id FROM accounts")

    assert main(["suggest", str(path)]) == 0
    assert "warning: undeclared-table: table accounts" in capsys.readouterr().err


def test_the_cli_annotates_a_file_in_place(tmp_path, capsys):
    path = tmp_path / "query.sql"
    sql = SCHEMA + "SELECT * FROM sessions\nJOIN users ON sessions.user_id = users.id"
    path.write_text(sql)

    assert main(["suggest", "--annotate", str(path)]) == 0

    assert path.read_text() == suggest(sql).annotated()
    assert len(capsys.readouterr().out.splitlines()) == len(suggest(sql).suggestions)
    assert analyze(path.read_text()).proved
    assert suggest(path.read_text()).suggestions == ()


def test_the_cli_annotates_stdin_to_stdout(monkeypatch, capsys):
    sql = SCHEMA + "SELECT * FROM sessions\nJOIN users ON sessions.user_id = users.id"
    monkeypatch.setattr("sys.stdin", io.StringIO(sql))

    assert main(["suggest", "--annotate", "-"]) == 0
    assert capsys.readouterr().out == suggest(sql).annotated()
