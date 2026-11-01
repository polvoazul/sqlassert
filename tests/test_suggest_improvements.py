"""`suggest`, High level: native SQL improvements justified by a proof.

Every rewrite an improvement proposes must preserve the program's result. The
equivalence tests below run the original and the rewritten SQL on random tiny
instances in DuckDB and compare the results as multisets.
"""

from __future__ import annotations

from collections import Counter

import pytest

from soundness_oracle import SearchResult, _Database
from sqlassert import suggest
from sqlassert.__main__ import main
from sqlassert.sql_parse import SqlParser
from sqlassert.suggest import Level, Rewrite

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE, name VARCHAR NOT NULL, nick VARCHAR);
CREATE TABLE sessions (user_id INTEGER, ts INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), total INTEGER);
"""


def improvements(sql: str) -> list[tuple[Rewrite, str, int]]:
    return [(i.rewrite, i.site.label, i.site.line) for i in suggest(sql).improvements]


def rewrites(sql: str) -> list[Rewrite]:
    return [i.rewrite for i in suggest(sql).improvements]


# LEFT JOIN that cannot pad ----------------------------------------------------


def test_a_left_join_whose_padding_the_where_clause_rejects_is_an_inner_join():
    sql = SCHEMA + "SELECT s.ts, u.name FROM sessions s\nLEFT JOIN users u ON u.id = s.user_id\nWHERE u.nick = 'x'"

    improvement, = suggest(sql).improvements
    assert (improvement.rewrite, improvement.site.label, improvement.site.line) == (Rewrite.INNER_JOIN, "LEFT JOIN users AS u", 6)
    assert improvement.level is Level.HIGH
    assert "WHERE" in improvement.message
    assert improvement.evidence.value
    assert "padded by the LEFT JOIN" in improvement.evidence.explain()


def test_a_later_inner_join_on_a_padded_column_also_removes_the_padding():
    sql = SCHEMA + """
    SELECT s.ts, u.name, o.total FROM sessions s
    LEFT JOIN users u ON u.id = s.user_id
    JOIN orders o ON o.user_id = u.id
    """
    assert rewrites(sql) == [Rewrite.INNER_JOIN]


def test_a_right_join_whose_padding_is_rejected_is_an_inner_join():
    sql = SCHEMA + "SELECT s.ts, u.name FROM users u RIGHT JOIN sessions s ON u.id = s.user_id WHERE u.name = 'x'"
    assert rewrites(sql) == [Rewrite.INNER_JOIN]


def test_a_left_join_that_matches_exactly_one_row_pads_nothing():
    sql = SCHEMA + "SELECT o.total, u.name FROM orders o LEFT JOIN users u ON u.id = o.user_id"
    improvement, = suggest(sql).improvements
    assert improvement.rewrite is Rewrite.INNER_JOIN
    assert "exactly one" in improvement.message


@pytest.mark.parametrize("where", [
    "",  # Nothing removes the padded rows.
    "WHERE u.nick IS NULL",  # Keeps them.
    "WHERE coalesce(u.nick, 'x') = 'x'",  # Not a column the padding NULLs.
    "WHERE s.ts = 1",  # The preserved side.
])
def test_a_left_join_whose_padding_may_survive_is_left_alone(where: str):
    sql = SCHEMA + f"SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id {where}"
    assert Rewrite.INNER_JOIN not in rewrites(sql)


def test_a_filter_in_an_enclosing_query_is_not_taken_as_removing_padding():
    # Outside the join's own FROM and WHERE, a LIMIT, window, or aggregate may
    # see the padded rows before the filter does.
    sql = SCHEMA + """
    SELECT * FROM (SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id LIMIT 1) AS x
    WHERE x.name = 'a'
    """
    assert Rewrite.INNER_JOIN not in rewrites(sql)


# Redundant joins --------------------------------------------------------------


def test_a_unique_left_join_none_of_whose_columns_is_used_is_redundant():
    sql = SCHEMA + "SELECT s.ts FROM sessions s\nLEFT JOIN users u ON u.id = s.user_id"
    improvement, = suggest(sql).improvements
    assert (improvement.rewrite, improvement.site.line) == (Rewrite.REDUNDANT_JOIN, 6)
    assert improvement.evidence.value


def test_an_exactly_one_inner_join_none_of_whose_columns_is_used_is_redundant():
    sql = SCHEMA + "SELECT o.total FROM orders o JOIN users u ON u.id = o.user_id"
    assert rewrites(sql) == [Rewrite.REDUNDANT_JOIN]


def test_a_unique_inner_join_none_of_whose_columns_is_used_only_filters():
    sql = SCHEMA + "SELECT s.ts FROM sessions s JOIN users u ON u.id = s.user_id"
    improvement, = suggest(sql).improvements
    assert improvement.rewrite is Rewrite.FILTER_JOIN
    assert "EXISTS" in improvement.message


def test_a_filter_only_join_followed_by_a_full_join_is_left_alone():
    # The filter would then apply after the FULL JOIN, which can no longer pad
    # the rows the join used to drop first.
    sql = SCHEMA + "SELECT s.ts FROM sessions s JOIN users u ON u.id = s.user_id FULL JOIN orders o ON o.id = s.ts"
    assert Rewrite.FILTER_JOIN not in rewrites(sql)


@pytest.mark.parametrize("query", [
    "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id",
    "SELECT s.ts, name FROM sessions s LEFT JOIN users u ON u.id = s.user_id",
    "SELECT * FROM sessions s LEFT JOIN users u ON u.id = s.user_id",
    "SELECT u.* FROM sessions s LEFT JOIN users u ON u.id = s.user_id",
    "SELECT s.ts FROM sessions s LEFT JOIN users u ON u.id = s.user_id ORDER BY u.name",
    "SELECT count(u.nick) AS n FROM sessions s LEFT JOIN users u ON u.id = s.user_id",
    "SELECT s.ts FROM sessions s LEFT JOIN users u ON u.id = s.user_id LEFT JOIN orders o ON o.user_id = u.id",
    "SELECT s.ts FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
    "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)",
    "SELECT s.user_id FROM sessions s LEFT JOIN users u ON u.id = s.user_id GROUP BY s.user_id HAVING max(u.name) = 'a'",
])
def test_a_join_whose_columns_are_used_is_not_redundant(query: str):
    assert not {Rewrite.REDUNDANT_JOIN, Rewrite.FILTER_JOIN} & set(rewrites(SCHEMA + query))


def test_a_join_that_may_multiply_rows_is_not_redundant():
    sql = SCHEMA + "SELECT u.name FROM users u LEFT JOIN sessions s ON s.user_id = u.id"
    assert rewrites(sql) == []


# Redundant DISTINCT and GROUP BY columns ----------------------------------------


def test_distinct_over_rows_that_already_differ_is_redundant():
    sql = SCHEMA + "SELECT DISTINCT id, nick FROM users"
    improvement, = suggest(sql).improvements
    assert (improvement.rewrite, improvement.site.label) == (Rewrite.REDUNDANT_DISTINCT, "Root Select")
    assert "id" in improvement.message


@pytest.mark.parametrize("query", [
    "SELECT DISTINCT nick FROM users",
    # A Unique Set allows several NULLs, which DISTINCT collapses into one.
    "SELECT DISTINCT email FROM users",
    "SELECT DISTINCT u.id FROM users u JOIN sessions s ON s.user_id = u.id",
])
def test_distinct_that_may_remove_a_row_is_kept(query: str):
    assert Rewrite.REDUNDANT_DISTINCT not in rewrites(SCHEMA + query)


def test_a_grouping_column_another_one_determines_is_redundant():
    sql = SCHEMA + """
    SELECT u.id, u.name, count(*) AS n FROM users u JOIN sessions s ON s.user_id = u.id
    GROUP BY u.id, u.name
    """
    improvement, = suggest(sql).improvements
    assert improvement.rewrite is Rewrite.REDUNDANT_GROUPING
    assert "name" in improvement.message and "any_value" in improvement.message


@pytest.mark.parametrize("grouping", ["1, 2", "id, name"])
def test_a_grouping_reference_uses_the_resolved_select_expression(grouping: str):
    sql = SCHEMA + f"SELECT id, name AS label, count(*) AS n FROM users GROUP BY {grouping}"
    improvement, = suggest(sql).improvements
    assert "any_value(name) AS label" in improvement.message


def test_a_grouping_improvement_remains_available_before_a_limit():
    sql = SCHEMA + "SELECT id, name, count(*) AS n FROM users GROUP BY id, name LIMIT 5"
    assert rewrites(sql) == [Rewrite.REDUNDANT_GROUPING]


def test_a_natural_join_warns_and_prevents_rewrites(tmp_path, capsys):
    sql = """
        CREATE TABLE s (id INTEGER);
        CREATE TABLE u (uid INTEGER PRIMARY KEY, v INTEGER);
        CREATE TABLE o (id INTEGER, v INTEGER);
        SELECT s.id FROM s LEFT JOIN u ON s.id = u.uid NATURAL JOIN o
    """
    report = suggest(sql)
    assert report.suggestions == report.improvements == ()
    assert any(diagnostic.code == "unsupported-natural-join" for diagnostic in report.diagnostics)
    path = tmp_path / "natural.sql"
    path.write_text(sql)
    assert main(["suggest", str(path)]) == 1
    assert "NATURAL JOIN" in capsys.readouterr().err


def test_a_natural_join_in_an_unused_cte_also_warns():
    sql = SCHEMA + "WITH unused AS (SELECT * FROM users NATURAL JOIN orders) SELECT id FROM users"
    assert any(diagnostic.code == "unsupported-natural-join" for diagnostic in suggest(sql).diagnostics)


def test_a_grouping_column_determined_only_by_a_nullable_one_is_kept():
    sql = SCHEMA + "SELECT email, name, count(*) AS n FROM users GROUP BY email, name"
    assert rewrites(sql) == []


def test_the_last_grouping_column_is_never_redundant():
    # Grouping by nothing would turn an empty input into one row.
    sql = SCHEMA + "SELECT name, count(*) AS n FROM users WHERE name = 'a' GROUP BY name"
    assert rewrites(sql) == []


def test_mutually_determined_grouping_columns_drop_only_one():
    sql = SCHEMA + "SELECT id, user_id, count(*) AS n FROM orders WHERE id = user_id GROUP BY id, user_id"
    assert rewrites(sql) == [Rewrite.REDUNDANT_GROUPING]


# Unused CTEs ------------------------------------------------------------------


def test_a_cte_nothing_references_is_dead_code():
    sql = SCHEMA + """
    WITH unused AS (SELECT id FROM users),
    used AS (SELECT user_id FROM sessions)
    SELECT user_id FROM used
    """
    improvement, = suggest(sql).improvements
    assert (improvement.rewrite, improvement.site.label, improvement.site.line) == (Rewrite.UNUSED_CTE, "CTE unused", 6)


def test_a_cte_referenced_only_by_an_unused_cte_is_dead_too():
    sql = SCHEMA + """
    WITH a AS (SELECT id FROM users), b AS (SELECT id FROM a)
    SELECT user_id FROM sessions
    """
    assert sorted(label for _, label, _ in improvements(sql)) == ["CTE a", "CTE b"]


def test_a_cte_used_in_a_nested_query_is_alive():
    sql = SCHEMA + """
    WITH a AS (SELECT id FROM users)
    SELECT user_id FROM sessions WHERE user_id IN (SELECT id FROM a)
    """
    assert rewrites(sql) == []


# Output -----------------------------------------------------------------------


def test_the_cli_lists_improvements_before_markers(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT u.id, s.ts FROM sessions s\nLEFT JOIN users u ON u.id = s.user_id\nWHERE u.name = 'a'")

    assert main(["suggest", str(path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:6] == ["high", "line", "6", "LEFT", "JOIN", "users"]
    assert "INNER JOIN" in lines[0]
    assert all(line.startswith("mid") for line in lines[1:])
    assert len(lines) == 1 + len(suggest(path.read_text()).suggestions)


# Rewrites preserve the result ---------------------------------------------------

# (original, rewritten, the improvement that justifies the rewrite)
EQUIVALENT = [
    # Issue 33: padding traced through a later LEFT JOIN whose ON rejects the padded column.
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = u.id WHERE o.total = 1",
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = u.id WHERE o.total = 1",
        Rewrite.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = u.id JOIN users v ON v.id = o.user_id",
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = u.id JOIN users v ON v.id = o.user_id",
        Rewrite.INNER_JOIN,
    ),
    # A WHERE EXISTS is a semi join, whose ON rejects NULL like a later inner join's.
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)",
        SCHEMA + "SELECT s.ts, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)",
        Rewrite.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = s.user_id) AND u.nick = 'x'",
        SCHEMA + "SELECT s.ts, u.name FROM sessions s JOIN users u ON u.id = s.user_id "
        "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = s.user_id) AND u.nick = 'x'",
        Rewrite.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT id, name, count(*) AS n FROM users GROUP BY 1, 2",
        SCHEMA + "SELECT id, any_value(name) AS name, count(*) AS n FROM users GROUP BY 1",
        Rewrite.REDUNDANT_GROUPING,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id WHERE u.nick = '1'",
        SCHEMA + "SELECT s.ts, u.name FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.nick = '1'",
        Rewrite.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "FULL JOIN orders o ON o.total = s.ts WHERE u.name IS NOT NULL",
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s JOIN users u ON u.id = s.user_id "
        "FULL JOIN orders o ON o.total = s.ts WHERE u.name IS NOT NULL",
        Rewrite.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = s.user_id AND o.total = 1 JOIN orders p ON p.id = o.id",
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "JOIN orders o ON o.user_id = s.user_id AND o.total = 1 JOIN orders p ON p.id = o.id",
        Rewrite.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT o.total, u.name FROM orders o LEFT JOIN users u ON u.id = o.user_id",
        SCHEMA + "SELECT o.total, u.name FROM orders o JOIN users u ON u.id = o.user_id",
        Rewrite.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, count(*) AS n FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "AND u.nick = '1' GROUP BY s.ts",
        SCHEMA + "SELECT s.ts, count(*) AS n FROM sessions s GROUP BY s.ts",
        Rewrite.REDUNDANT_JOIN,
    ),
    (
        SCHEMA + "SELECT o.total FROM orders o JOIN users u ON u.id = o.user_id",
        SCHEMA + "SELECT o.total FROM orders o",
        Rewrite.REDUNDANT_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, o.total FROM sessions s JOIN users u ON u.id = s.user_id AND u.nick = '1' "
        "LEFT JOIN orders o ON o.total = s.ts",
        SCHEMA + "SELECT s.ts, o.total FROM sessions s LEFT JOIN orders o ON o.total = s.ts "
        "WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = s.user_id AND u.nick = '1')",
        Rewrite.FILTER_JOIN,
    ),
    (
        SCHEMA + "SELECT DISTINCT o.id, u.name FROM orders o LEFT JOIN users u ON u.id = o.user_id",
        SCHEMA + "SELECT o.id, u.name FROM orders o LEFT JOIN users u ON u.id = o.user_id",
        Rewrite.REDUNDANT_DISTINCT,
    ),
    (
        SCHEMA + "SELECT DISTINCT s.user_id, u.nick FROM (SELECT DISTINCT user_id FROM sessions) AS s "
        "LEFT JOIN users u ON u.id = s.user_id",
        SCHEMA + "SELECT s.user_id, u.nick FROM (SELECT DISTINCT user_id FROM sessions) AS s "
        "LEFT JOIN users u ON u.id = s.user_id",
        Rewrite.REDUNDANT_DISTINCT,
    ),
    (
        SCHEMA + "SELECT DISTINCT user_id, total FROM (SELECT DISTINCT user_id, total FROM orders) AS t",
        SCHEMA + "SELECT user_id, total FROM (SELECT DISTINCT user_id, total FROM orders) AS t",
        Rewrite.REDUNDANT_DISTINCT,
    ),
    (
        SCHEMA + "SELECT u.id, u.name, count(s.ts) AS n FROM users u LEFT JOIN sessions s ON s.user_id = u.id "
        "GROUP BY u.id, u.name",
        SCHEMA + "SELECT u.id, any_value(u.name) AS name, count(s.ts) AS n FROM users u "
        "LEFT JOIN sessions s ON s.user_id = u.id GROUP BY u.id",
        Rewrite.REDUNDANT_GROUPING,
    ),
    (
        SCHEMA + "SELECT s.user_id, u.nick, count(*) AS n FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "GROUP BY s.user_id, u.nick",
        SCHEMA + "SELECT s.user_id, any_value(u.nick) AS nick, count(*) AS n FROM sessions s "
        "LEFT JOIN users u ON u.id = s.user_id GROUP BY s.user_id",
        Rewrite.REDUNDANT_GROUPING,
    ),
    (
        SCHEMA + "WITH dead AS (SELECT id FROM users) SELECT user_id FROM sessions",
        SCHEMA + "SELECT user_id FROM sessions",
        Rewrite.UNUSED_CTE,
    ),
]


def differing_instance(original: str, rewritten: str) -> SearchResult:
    """Search for an instance on which the two programs differ as multisets."""
    database = _Database(SqlParser("duckdb").parse(original))
    queries = []
    for sql in (original, rewritten):
        root = SqlParser("duckdb").parse(sql).root_select
        assert root is not None
        queries.append(root.sql(dialect="duckdb"))

    def differs(_) -> bool:
        before, after = (Counter(database.connection.execute(query).fetchall()) for query in queries)
        return before != after

    return database.search(differs, max_examples=400)


@pytest.mark.parametrize(("original", "rewritten", "rewrite"), EQUIVALENT)
def test_each_rewrite_preserves_the_result_on_random_instances(original: str, rewritten: str, rewrite: Rewrite):
    assert rewrite in rewrites(original)

    search = differing_instance(original, rewritten)
    assert search.status == "no-counterexample", search
    assert search.valid >= 100


def test_the_equivalence_check_finds_a_rewrite_that_changes_the_result():
    # Guards the test above: dropping a join that may multiply rows is caught.
    search = differing_instance(
        SCHEMA + "SELECT u.name FROM users u LEFT JOIN sessions s ON s.user_id = u.id",
        SCHEMA + "SELECT u.name FROM users u",
    )
    assert search.status == "counterexample"
    assert search.instance is not None


def test_a_later_left_join_on_a_column_that_is_not_padded_does_not_chain():
    sql = SCHEMA + (
        "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.total = s.ts WHERE o.total = 1"
    )
    assert [label for rewrite, label, _ in improvements(sql) if rewrite is Rewrite.INNER_JOIN] == ["LEFT JOIN orders AS o"]
