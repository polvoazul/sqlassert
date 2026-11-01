"""`suggest`: the properties sqlassert can prove about unannotated SQL, as markers to paste."""

from __future__ import annotations

import pytest

from sqlassert import NonNullColumns, NonNullUniqueSet, Outcome, UniqueJoin, UniqueSet, analyze, suggest
from sqlassert.__main__ import main
from sqlassert.ir.convert import IrParser
from sqlassert.sql_parse import SqlParser
from sqlassert.suggest import annotate, candidates

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE, name VARCHAR NOT NULL, nick VARCHAR);
CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);
"""


def markers(sql: str, **kwargs) -> list[tuple[str, str]]:
    return [(s.site.label, s.marker) for s in suggest(sql, **kwargs).suggestions]


def test_a_provable_join_is_suggested_as_a_unique_join():
    report = suggest(SCHEMA + "SELECT * FROM sessions\nLEFT JOIN users ON sessions.user_id = users.id")

    join, = (s for s in report.suggestions if isinstance(s.property, UniqueJoin))
    assert (join.site.label, join.site.line, join.marker) == ("LEFT JOIN users", 5, "/**ASSERT UNIQUE**/")
    assert join.evidence.value


def test_an_unprovable_join_is_not_suggested():
    report = suggest(SCHEMA + "SELECT * FROM users JOIN sessions ON sessions.user_id = users.id")
    # The inner join's ON equality rejects a NULL user_id.
    assert [s.marker for s in report.suggestions] == ["/**ASSERT NOT NULL(id, name, user_id)**/"]


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
        ("JOIN recent", "/**ASSERT UNIQUE**/"),
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(user_id)**/"),
    ]


def test_each_member_of_a_set_operation_is_its_own_site():
    sql = SCHEMA + "SELECT id FROM users UNION ALL SELECT DISTINCT user_id FROM sessions"
    assert markers(sql) == [
        ("Root Select", "/**ASSERT NOT NULL UNIQUE(id)**/"),
        ("Root Select", "/**ASSERT UNIQUE(user_id)**/"),
    ]


def test_columns_a_marker_cannot_name_unambiguously_are_left_out():
    sql = SCHEMA + "SELECT users.id, other.id FROM users JOIN users AS other ON other.id = users.id"
    assert [m for _, m in markers(sql)] == ["/**ASSERT UNIQUE**/"]


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
    """
    CREATE TABLE t (a INTEGER, b INTEGER, c INTEGER, d INTEGER, e INTEGER, PRIMARY KEY (a, b));
    SELECT a, b, c, d, max(e) AS m FROM t GROUP BY a, b, c, d
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
    assert lines[0].split() == ["line", "5", "JOIN", "users", "Unique", "Join", "/**ASSERT", "UNIQUE**/"]
    assert len(lines) == len(suggest(path.read_text()).suggestions)


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
