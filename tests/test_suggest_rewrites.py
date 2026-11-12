"""`suggest`, rewrites: native SQL rewrites justified by a proof.

Every rewrite an rewrite proposes must preserve the program's result. The
equivalence tests below run the original and the rewritten SQL on random tiny
instances in DuckDB and compare the results as multisets.
"""

from __future__ import annotations

from collections import Counter

import duckdb
import pytest

from soundness_oracle import SearchResult, _Database
from sqlassert import suggest
from sqlassert.__main__ import main
from sqlassert.sql_parse import SqlParser
from sqlassert.suggest import SuggestType, RewriteKind

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR UNIQUE, name VARCHAR NOT NULL, nick VARCHAR);
CREATE TABLE sessions (user_id INTEGER, ts INTEGER);
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), total INTEGER);
"""


def rewrite_sites(sql: str) -> list[tuple[RewriteKind, str, int]]:
    return [(i.kind, i.site.label, i.site.line) for i in suggest(sql).rewrites]


def rewrites(sql: str) -> list[RewriteKind]:
    return [i.kind for i in suggest(sql).rewrites]


# LEFT JOIN that cannot pad ----------------------------------------------------


def test_a_left_join_whose_padding_the_where_clause_rejects_is_an_inner_join():
    sql = SCHEMA + "SELECT s.ts, u.name FROM sessions s\nLEFT JOIN users u ON u.id = s.user_id\nWHERE u.nick = 'x'"

    rewrite, = suggest(sql).rewrites
    assert (rewrite.kind, rewrite.site.label, rewrite.site.line) == (RewriteKind.INNER_JOIN, "LEFT JOIN users AS u", 6)
    assert rewrite.suggest_type is SuggestType.REWRITE
    assert "WHERE" in rewrite.message
    assert rewrite.evidence.value
    assert "padded by the LEFT JOIN" in rewrite.evidence.explain()


def test_a_later_inner_join_on_a_padded_column_also_removes_the_padding():
    sql = SCHEMA + """
    SELECT s.ts, u.name, o.total FROM sessions s
    LEFT JOIN users u ON u.id = s.user_id
    JOIN orders o ON o.user_id = u.id
    """
    assert rewrites(sql) == [RewriteKind.INNER_JOIN]


def test_a_right_join_whose_padding_is_rejected_is_an_inner_join():
    sql = SCHEMA + "SELECT s.ts, u.name FROM users u RIGHT JOIN sessions s ON u.id = s.user_id WHERE u.name = 'x'"
    assert rewrites(sql) == [RewriteKind.INNER_JOIN]


def test_a_left_join_that_matches_exactly_one_row_pads_nothing():
    sql = SCHEMA + "SELECT o.total, u.name FROM orders o LEFT JOIN users u ON u.id = o.user_id"
    rewrite, = suggest(sql).rewrites
    assert rewrite.kind is RewriteKind.INNER_JOIN
    assert "exactly one" in rewrite.message


@pytest.mark.parametrize("where", [
    "WHERE u.nick = 'a' OR s.ts = 1",  # A part TRUE on a padded row.
    "LEFT JOIN users v ON v.id = s.user_id WHERE u.nick = 'a' OR v.email = 'b'",  # Another join's padding.
    "",  # Nothing removes the padded rows.
    "WHERE u.nick IS NULL",  # Keeps them.
    "WHERE coalesce(u.nick, 'x') = 'x'",  # Not a column the padding NULLs.
    "WHERE s.ts = 1",  # The preserved side.
])
def test_a_left_join_whose_padding_may_survive_is_left_alone(where: str):
    sql = SCHEMA + f"SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id {where}"
    assert RewriteKind.INNER_JOIN not in rewrites(sql)


@pytest.mark.parametrize("outer", ["RIGHT", "FULL"])
def test_a_join_that_keeps_a_parenthesized_join_unmatched_keeps_its_padding(outer: str):
    sql = SCHEMA + (
        f"SELECT s.ts, u.name, o.total FROM sessions s {outer} JOIN (users u LEFT JOIN orders o ON o.user_id = u.id) "
        "ON o.total = s.ts"
    )
    assert RewriteKind.INNER_JOIN not in rewrites(sql)


def test_a_filter_in_an_enclosing_query_is_not_taken_as_removing_padding():
    # Outside the join's own FROM and WHERE, a LIMIT, window, or aggregate may
    # see the padded rows before the filter does.
    sql = SCHEMA + """
    SELECT * FROM (SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id LIMIT 1) AS x
    WHERE x.name = 'a'
    """
    assert RewriteKind.INNER_JOIN not in rewrites(sql)


# Redundant joins --------------------------------------------------------------


def test_a_unique_left_join_none_of_whose_columns_is_used_is_redundant():
    sql = SCHEMA + "SELECT s.ts FROM sessions s\nLEFT JOIN users u ON u.id = s.user_id"
    rewrite, = suggest(sql).rewrites
    assert (rewrite.kind, rewrite.site.line) == (RewriteKind.REDUNDANT_JOIN, 6)
    assert rewrite.evidence.value


def test_an_exactly_one_inner_join_none_of_whose_columns_is_used_is_redundant():
    sql = SCHEMA + "SELECT o.total FROM orders o JOIN users u ON u.id = o.user_id"
    assert rewrites(sql) == [RewriteKind.REDUNDANT_JOIN]


def test_a_unique_inner_join_none_of_whose_columns_is_used_only_filters():
    sql = SCHEMA + "SELECT s.ts FROM sessions s JOIN users u ON u.id = s.user_id"
    rewrite, = suggest(sql).rewrites
    assert rewrite.kind is RewriteKind.FILTER_JOIN
    assert "EXISTS" in rewrite.message


def test_a_filter_only_join_followed_by_a_full_join_is_left_alone():
    # The filter would then apply after the FULL JOIN, which can no longer pad
    # the rows the join used to drop first.
    sql = SCHEMA + "SELECT s.ts FROM sessions s JOIN users u ON u.id = s.user_id FULL JOIN orders o ON o.id = s.ts"
    assert RewriteKind.FILTER_JOIN not in rewrites(sql)


def test_a_where_on_the_other_side_of_a_chained_full_join_keeps_the_left_join():
    # Issue 40: the FULL JOIN pads o for the rows the LEFT JOIN pads, but the
    # WHERE rejects NULL in s.ts, which the FULL JOIN pads on other rows.
    sql = SCHEMA + (
        "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "FULL JOIN orders o ON o.total = u.id WHERE s.ts = 1"
    )
    assert RewriteKind.INNER_JOIN not in rewrites(sql)


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
    assert not {RewriteKind.REDUNDANT_JOIN, RewriteKind.FILTER_JOIN} & set(rewrites(SCHEMA + query))


def test_a_join_that_may_multiply_rows_is_not_redundant():
    sql = SCHEMA + "SELECT u.name FROM users u LEFT JOIN sessions s ON s.user_id = u.id"
    assert rewrites(sql) == []


# Redundant DISTINCT and GROUP BY columns ----------------------------------------


def test_distinct_over_rows_that_already_differ_is_redundant():
    sql = SCHEMA + "SELECT DISTINCT id, nick FROM users"
    rewrite, = suggest(sql).rewrites
    assert (rewrite.kind, rewrite.site.label) == (RewriteKind.REDUNDANT_DISTINCT, "Root Select")
    assert "id" in rewrite.message


@pytest.mark.parametrize("query", [
    "SELECT DISTINCT nick FROM users",
    # A Unique Set allows several NULLs, which DISTINCT collapses into one.
    "SELECT DISTINCT email FROM users",
    "SELECT DISTINCT u.id FROM users u JOIN sessions s ON s.user_id = u.id",
])
def test_distinct_that_may_remove_a_row_is_kept(query: str):
    assert RewriteKind.REDUNDANT_DISTINCT not in rewrites(SCHEMA + query)


def test_a_grouping_column_another_one_determines_is_redundant():
    sql = SCHEMA + """
    SELECT u.id, u.name, count(*) AS n FROM users u JOIN sessions s ON s.user_id = u.id
    GROUP BY u.id, u.name
    """
    rewrite, = suggest(sql).rewrites
    assert rewrite.kind is RewriteKind.REDUNDANT_GROUPING
    assert "name" in rewrite.message and "any_value" in rewrite.message


@pytest.mark.parametrize("grouping", ["1, 2", "id, name", "(), id, name"])
def test_a_grouping_reference_uses_the_resolved_select_expression(grouping: str):
    sql = SCHEMA + f"SELECT id, name AS label, count(*) AS n FROM users JOIN sessions ON sessions.user_id = users.id GROUP BY {grouping}"
    rewrite, = suggest(sql).rewrites
    assert "any_value(name) AS label" in rewrite.message


def test_a_grouping_rewrite_remains_available_before_a_limit():
    sql = SCHEMA + "SELECT id, name, count(*) AS n FROM users JOIN sessions ON sessions.user_id = users.id GROUP BY id, name LIMIT 5"
    assert rewrites(sql) == [RewriteKind.REDUNDANT_GROUPING]


def test_a_natural_join_warns_and_prevents_rewrites(tmp_path, capsys):
    sql = """
        CREATE TABLE s (id INTEGER);
        CREATE TABLE u (uid INTEGER PRIMARY KEY, v INTEGER);
        CREATE TABLE o (id INTEGER, v INTEGER);
        SELECT s.id FROM s LEFT JOIN u ON s.id = u.uid NATURAL JOIN o
    """
    report = suggest(sql)
    assert report.suggestions == report.rewrites == ()
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
    sql = SCHEMA + (
        "SELECT o.id, o.user_id, count(*) AS n FROM orders o JOIN sessions s ON s.user_id = o.id "
        "WHERE o.id = o.user_id GROUP BY o.id, o.user_id"
    )
    assert rewrites(sql) == [RewriteKind.REDUNDANT_GROUPING]


def test_a_redundant_grouping_column_beside_an_item_computed_over_aggregates():
    # The item is computed by a Project over the Aggregate, which the rewrite sees through.
    sql = SCHEMA + "SELECT u.name, max(s.ts) + 1 AS t FROM users u JOIN sessions s ON s.user_id = u.id GROUP BY u.id, u.name"
    rewrite, = suggest(sql).rewrites
    assert rewrite.message.endswith("drop 'u.name' from GROUP BY and select any_value(u.name) AS name")


def test_a_redundant_grouping_column_not_selected_is_dropped():
    sql = SCHEMA + "SELECT count(*) + 1 AS n FROM users u JOIN sessions s ON s.user_id = u.id GROUP BY u.id, u.name"
    rewrite, = suggest(sql).rewrites
    assert rewrite.message.endswith("drop 'u.name' from GROUP BY")


# A GROUP BY whose groups are single rows ----------------------------------------


def test_a_group_by_over_a_key_of_its_input_is_a_projection():
    sql = SCHEMA + (
        "SELECT id, count(*) AS n, count(nick) AS c, max(name) AS m, sum(DISTINCT id) AS s, any_value(nick) AS a "
        "FROM users GROUP BY id"
    )
    rewrite, = suggest(sql).rewrites
    assert rewrite.kind is RewriteKind.GROUPING_PROJECTION
    assert rewrite.suggest_type is SuggestType.REWRITE_WITH_CAVEAT
    assert rewrite.message.endswith(
        "drop GROUP BY and write SELECT id, 1 AS n, CASE WHEN nick IS NULL THEN 0 ELSE 1 END AS c, name AS m, id AS s, nick AS a. "
        "Caveat: 'n' changes from count's result type to an integer literal's type; "
        "'c' changes from count's result type to an integer literal's type; "
        "'s' changes from sum's result type to the type of id; cast them if later arithmetic needs the wider types"
    )


def test_a_group_by_kept_unique_through_a_many_to_one_join_is_a_projection():
    sql = SCHEMA + "SELECT o.id, max(u.name) AS name FROM orders o JOIN users u ON u.id = o.user_id GROUP BY o.id"
    rewrite, = suggest(sql).rewrites
    assert rewrite.kind is RewriteKind.GROUPING_PROJECTION
    assert rewrite.message.endswith("write SELECT o.id, u.name AS name")


@pytest.mark.parametrize("query", [
    # NULLs form one group: a nullable unique column is not enough.
    "SELECT email, count(*) AS n FROM users GROUP BY email",
    # Many sessions per user.
    "SELECT u.id, count(*) AS n FROM users u JOIN sessions s ON s.user_id = u.id GROUP BY u.id",
    # An aggregate read outside the select list, or with no one-row form here.
    "SELECT id, count(*) AS n FROM users GROUP BY id ORDER BY count(*)",
    "SELECT id, sum(id) FILTER (WHERE id > 1) AS s FROM users GROUP BY id",
    "SELECT id, string_agg(name, ',') AS s FROM users GROUP BY id",
    "SELECT id, sum(count(*)) OVER () AS s FROM users GROUP BY id",
    # An aggregate in a subquery is the subquery's (or, of outer columns only, the outer query's).
    "SELECT id, (SELECT max(ts) FROM sessions) AS m FROM users GROUP BY id",
])
def test_a_group_by_that_may_merge_rows_or_has_no_one_row_form_is_not_a_projection(query: str):
    assert RewriteKind.GROUPING_PROJECTION not in rewrites(SCHEMA + query)


ONE_ROW_SCHEMA = (
    "CREATE TABLE t (id INTEGER PRIMARY KEY, i INTEGER, b BIGINT, f DOUBLE, r REAL, d DECIMAL(10, 2), bo BOOLEAN, s VARCHAR);"
)
ONE_ROW_VALUES = "INSERT INTO t VALUES (1, 2147483647, 9007199254740993, 1.5, 2.5, 1.25, true, 'a')"


@pytest.mark.parametrize(("item", "replacement", "suggest_type", "types"), [
    # Every group is one row, so each aggregate is its value there; its type
    # may not be the aggregate's (DuckDB 2.0's, per typeof()).
    ("sum(i)", "i", SuggestType.REWRITE_WITH_CAVEAT, ("HUGEINT", "INTEGER")),
    ("sum(r)", "r", SuggestType.REWRITE_WITH_CAVEAT, ("DOUBLE", "FLOAT")),
    ("sum(d)", "d", SuggestType.REWRITE_WITH_CAVEAT, ("DECIMAL(38,2)", "DECIMAL(10,2)")),
    ("avg(i)", "i", SuggestType.REWRITE_WITH_CAVEAT, ("DOUBLE", "INTEGER")),
    ("avg(r)", "r", SuggestType.REWRITE_WITH_CAVEAT, ("DOUBLE", "FLOAT")),
    ("count(*)", "1", SuggestType.REWRITE_WITH_CAVEAT, ("BIGINT", "INTEGER")),
    ("count(s)", "CASE WHEN s IS NULL THEN 0 ELSE 1 END", SuggestType.REWRITE_WITH_CAVEAT, ("BIGINT", "INTEGER")),
    # A picked value, and a sum or average of a DOUBLE, keep the type.
    ("max(s)", "s", SuggestType.REWRITE, ("VARCHAR", "VARCHAR")),
    ("min(r) + 1", "r + 1", SuggestType.REWRITE, ("FLOAT", "FLOAT")),
    ("bool_and(bo)", "bo", SuggestType.REWRITE, ("BOOLEAN", "BOOLEAN")),
    ("sum(f)", "f", SuggestType.REWRITE, ("DOUBLE", "DOUBLE")),
    ("avg(f)", "f", SuggestType.REWRITE, ("DOUBLE", "DOUBLE")),
])
def test_a_one_row_aggregate_is_a_rewrite_with_caveat_when_its_type_changes(item, replacement, suggest_type, types):
    query = f"SELECT id, {item} AS v FROM t GROUP BY id"
    rewrite, = suggest(ONE_ROW_SCHEMA + query).rewrites
    assert (rewrite.kind, rewrite.suggest_type) == (RewriteKind.GROUPING_PROJECTION, suggest_type)
    written = f"drop GROUP BY and write SELECT id, {replacement} AS v"
    if suggest_type is SuggestType.REWRITE:
        assert rewrite.message.endswith(written)
    else:
        aggregate, _, argument = item.partition("(")
        target = "an integer literal's type" if aggregate == "count" else f"the type of {argument.rstrip(')')}"
        assert rewrite.message.endswith(
            f"{written}. Caveat: 'v' changes from {aggregate}'s result type to {target}; "
            "cast it if later arithmetic needs the wider type"
        )
    with duckdb.connect() as con:
        con.execute(ONE_ROW_SCHEMA + ONE_ROW_VALUES)
        before = con.execute(f"SELECT id, v, typeof(v) FROM ({query})").fetchall()
        after = con.execute(f"SELECT id, v, typeof(v) FROM (SELECT id, {replacement} AS v FROM t)").fetchall()
    assert [row[:2] for row in before] == [row[:2] for row in after]
    assert (before[0][2], after[0][2]) == types


@pytest.mark.parametrize(("item", "replacement", "caveat", "types"), [
    # A narrower type inside an expression may overflow (or, for a float,
    # round) where the aggregate's did not: DuckDB's i + 1 overflows here
    # (None), its type pinned per typeof().
    (
        "sum(i) + 1", "i + 1",
        "'v' runs its arithmetic in the type of i, not sum's result type, and may overflow where the aggregate's did not; "
        "cast it if later arithmetic needs the wider type",
        ("HUGEINT", None),
    ),
    (
        "count(*) + 2147483647", "1 + 2147483647",
        "'v' runs its arithmetic in an integer literal's type, not count's result type, and may overflow where the aggregate's did not; "
        "cast it if later arithmetic needs the wider type",
        ("BIGINT", None),
    ),
    (
        "sum(r) + 1", "r + 1",
        "'v' runs its arithmetic in the type of r, not sum's result type, and may lose precision where the aggregate's did not; "
        "cast it if later arithmetic needs the wider type",
        ("DOUBLE", "FLOAT"),
    ),
    # avg of a BIGINT, HUGEINT, UBIGINT or DECIMAL is a DOUBLE in some
    # dialects, which may round it: the argument is the exact average.
    (
        "avg(b)", "b",
        "'v' takes b's exact value and type, where avg converts to DOUBLE in some dialects and may lose precision "
        "(avg(9007199254740993) is 9007199254740992.0)",
        ("DOUBLE", "BIGINT"),
    ),
    (
        "avg(d)", "d",
        "'v' takes d's exact value and type, where avg converts to DOUBLE in some dialects and may lose precision "
        "(avg(9007199254740993) is 9007199254740992.0)",
        ("DOUBLE", "DECIMAL(10,2)"),
    ),
    (
        "avg(h)", "h",
        "'v' takes h's exact value and type, where avg converts to DOUBLE in some dialects and may lose precision "
        "(avg(9007199254740993) is 9007199254740992.0)",
        ("DOUBLE", "HUGEINT"),
    ),
    (
        "avg(u)", "u",
        "'v' takes u's exact value and type, where avg converts to DOUBLE in some dialects and may lose precision "
        "(avg(9007199254740993) is 9007199254740992.0)",
        ("DOUBLE", "UBIGINT"),
    ),
    (
        "avg(d) * 2", "d * 2",
        "'v' takes d's exact value and type, where avg converts to DOUBLE in some dialects and may lose precision "
        "(avg(9007199254740993) is 9007199254740992.0); "
        "'v' runs its arithmetic in the type of d, not avg's result type, and may overflow where the aggregate's did not; "
        "cast it if later arithmetic needs the wider type",
        ("DOUBLE", "DECIMAL(18,2)"),
    ),
])
def test_a_one_row_aggregate_whose_precision_or_type_may_change_is_a_rewrite_with_caveat(item, replacement, caveat, types):
    schema = ONE_ROW_SCHEMA.replace("s VARCHAR", "s VARCHAR, h HUGEINT, u UBIGINT")
    query = f"SELECT id, {item} AS v FROM t GROUP BY id"
    rewrite, = suggest(schema + query, dialect="duckdb").rewrites
    assert (rewrite.kind, rewrite.suggest_type) == (RewriteKind.GROUPING_PROJECTION, SuggestType.REWRITE_WITH_CAVEAT)
    assert rewrite.message.endswith(f"drop GROUP BY and write SELECT id, {replacement} AS v. Caveat: {caveat}")
    with duckdb.connect() as con:
        con.execute(schema + ONE_ROW_VALUES.replace("'a')", "'a', 9007199254740993, 9007199254740993)"))
        (_, before), = con.execute(f"SELECT v, typeof(v) FROM ({query})").fetchall()
        try:
            (_, after), = con.execute(f"SELECT {replacement}, typeof({replacement}) FROM t").fetchall()
        except duckdb.OutOfRangeException:
            after = None
    assert (before, after) == types


@pytest.mark.parametrize("item", [
    # avg of an integer inside an expression: an integer division truncates
    # (i / 2 in PostgreSQL or SQLite) where avg's did not.
    "avg(i) / 2",
    # sum of a BOOLEAN counts it; bool_and of an integer is not its value.
    "sum(bo)",
    "bool_and(i)",
    # Of an expression, the argument's type is unknown.
    "sum(i + 1)",
    "avg(i + 1)",
])
def test_a_one_row_aggregate_whose_value_may_change_or_whose_type_is_unknown_is_not_rewritten(item: str):
    assert not suggest(ONE_ROW_SCHEMA + f"SELECT id, {item} AS v FROM t GROUP BY id").rewrites


def test_a_rewrite_that_keeps_every_column_type_is_a_plain_rewrite():
    rewrite, = suggest(SCHEMA + "SELECT DISTINCT id, name FROM users").rewrites
    assert (rewrite.kind, rewrite.suggest_type) == (RewriteKind.REDUNDANT_DISTINCT, SuggestType.REWRITE)


# EXCEPT and INTERSECT over a duplicate-free input -----------------------------------


@pytest.mark.parametrize(("query", "message"), [
    ("SELECT id FROM users EXCEPT SELECT user_id FROM sessions", "left input are alike, NULL equal to NULL, so EXCEPT removes no duplicates: write EXCEPT ALL"),
    ("SELECT user_id FROM sessions INTERSECT SELECT id FROM users", "right input are alike, NULL equal to NULL, so INTERSECT removes no duplicates: write INTERSECT ALL"),
    # The left input of the second EXCEPT is the first one's distinct result.
    ("SELECT user_id FROM sessions EXCEPT SELECT id FROM users EXCEPT SELECT 3", "write EXCEPT ALL"),
])
def test_except_or_intersect_over_an_input_without_duplicates_can_keep_all(query: str, message: str):
    rewrite, = suggest(SCHEMA + query).rewrites
    assert rewrite.kind is RewriteKind.SET_OPERATION_ALL
    assert rewrite.message.endswith(message)


@pytest.mark.parametrize("query", [
    # EXCEPT removes duplicates of its left input only.
    "SELECT user_id FROM sessions EXCEPT SELECT id FROM users",
    # Set operations compare NULL equal to NULL: two NULL emails are duplicates.
    "SELECT email FROM users EXCEPT SELECT 'x'",
    "SELECT id FROM users EXCEPT ALL SELECT user_id FROM sessions",
    "SELECT id FROM users UNION SELECT user_id FROM sessions",
])
def test_except_or_intersect_that_may_remove_a_duplicate_is_kept(query: str):
    assert RewriteKind.SET_OPERATION_ALL not in rewrites(SCHEMA + query)


@pytest.mark.parametrize("dialect", ["sqlite", "bigquery", "snowflake", "oracle"])
def test_except_all_is_not_suggested_where_the_dialect_lacks_it(dialect: str):
    report = suggest(SCHEMA + "SELECT id FROM users EXCEPT SELECT user_id FROM sessions", dialect=dialect)
    assert RewriteKind.SET_OPERATION_ALL not in [rewrite.kind for rewrite in report.rewrites]


# LIMIT and ORDER BY on one row --------------------------------------------------


def test_limit_and_order_by_on_at_most_one_row_change_nothing():
    rewrite, = suggest(SCHEMA + "SELECT id, name FROM users WHERE id = 3 ORDER BY name LIMIT 1").rewrites
    assert rewrite.kind is RewriteKind.ONE_ROW
    assert rewrite.message.endswith("ORDER BY and LIMIT change nothing: drop them")


@pytest.mark.parametrize("query", [
    "SELECT id FROM users WHERE id = 3 LIMIT 1 OFFSET 1",
    "SELECT id FROM users WHERE id = 3 LIMIT 0",
    "SELECT id FROM users WHERE name = 'a' ORDER BY id LIMIT 1",
])
def test_limit_that_may_change_the_rows_is_kept(query: str):
    assert RewriteKind.ONE_ROW not in rewrites(SCHEMA + query)


# Self-joins on a key -------------------------------------------------------------


@pytest.mark.parametrize(("query", "rewritten"), [
    ("SELECT u.id, v.name FROM users u JOIN users v ON u.id = v.id", "SELECT u.id, u.name FROM users AS u"),
    ("SELECT u.id, v.name FROM users u JOIN users v ON u.id = v.id AND v.nick = 'x'", "SELECT u.id, u.name FROM users AS u WHERE u.nick = 'x'"),
    (
        "SELECT u.id, v.name FROM users u LEFT JOIN users v ON u.id = v.id AND v.nick = 'x'",
        "SELECT u.id, CASE WHEN u.nick = 'x' THEN u.name END AS name FROM users AS u",
    ),
    ("SELECT u.id, v.nick FROM users u JOIN users v ON u.id = v.id AND u.nick = v.nick", "SELECT u.id, u.nick FROM users AS u WHERE u.nick IS NOT NULL"),
    (
        "WITH c AS (SELECT id, name, random() AS r FROM users) SELECT a.id, b.name FROM c a JOIN c b ON a.id = b.id",
        "SELECT a.id, a.name FROM c AS a",
    ),
])
def test_a_self_join_on_a_key_reads_the_same_row(query: str, rewritten: str):
    rewrite = next(i for i in suggest(SCHEMA + query).rewrites if i.kind is RewriteKind.SELF_JOIN)
    assert rewrite.message.endswith(rewritten)


def test_a_self_join_that_only_filters_reads_the_same_row_instead_of_exists():
    query = "SELECT u.id FROM users u JOIN users v ON u.id = v.id AND u.nick = v.nick"
    kinds = rewrites(SCHEMA + query)
    assert RewriteKind.SELF_JOIN in kinds and RewriteKind.FILTER_JOIN not in kinds and RewriteKind.REDUNDANT_JOIN not in kinds


@pytest.mark.parametrize("query", [
    # Another row: the manager.
    "SELECT o.id, p.total FROM orders o JOIN orders p ON o.user_id = p.id",
    # A nullable unique key: a NULL email matches nothing.
    "SELECT u.id, v.name FROM users u JOIN users v ON u.email = v.email",
    # A volatile column read through the second copy.
    "WITH c AS (SELECT id, random() AS r FROM users) SELECT a.id, b.r FROM c a JOIN c b ON a.id = b.id",
    # A body read twice may give different rows.
    "WITH c AS (SELECT id, name FROM users LIMIT 3) SELECT a.id, b.name FROM c a JOIN c b ON a.id = b.id",
])
def test_a_join_that_may_read_another_row_is_not_a_self_join(query: str):
    assert RewriteKind.SELF_JOIN not in rewrites(SCHEMA + query)


# Conditions the others imply -----------------------------------------------------


@pytest.mark.parametrize(("query", "message"), [
    ("SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id AND o.user_id = o.id AND u.id = o.id", "delete it from ON"),
    ("SELECT id FROM orders WHERE id = 5 AND user_id = 5 AND id = user_id", "id = user_id is always TRUE here (both sides are 5): delete it from WHERE"),
    ("SELECT id FROM (SELECT * FROM users WHERE name = 'x') t WHERE name = 'x'", "delete it from WHERE"),
    ("SELECT id FROM (SELECT * FROM users WHERE nick = 'x') t WHERE nick IN ('x', 'y')", "delete it from WHERE"),
    ("SELECT u.id, v.nick FROM users u JOIN users v ON u.id = v.id AND u.nick = v.nick", "write u.nick IS NOT NULL in ON"),
    ("SELECT id FROM users WHERE name IS NOT NULL", "name IS NOT NULL is always TRUE here ('name' is never NULL): delete it from WHERE"),
])
def test_a_condition_the_others_imply_is_redundant(query: str, message: str):
    rewrites = [i for i in suggest(SCHEMA + query).rewrites if i.kind is RewriteKind.IMPLIED_CONDITION]
    assert len(rewrites) == 1
    assert rewrites[0].message.endswith(message)


@pytest.mark.parametrize("query", [
    "SELECT id FROM users WHERE name = 'x'",
    "SELECT id FROM (SELECT * FROM users WHERE nick IN ('x', 'y')) t WHERE nick = 'x'",
    "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id AND u.name = 'x'",
    "SELECT id FROM users WHERE nick IS NOT NULL",
    # Both pinned to 5, but text '05' = 5 may hold while '05' = '5' does not.
    "SELECT o.id FROM orders o JOIN users u ON u.id = o.user_id WHERE o.total = 5 AND u.nick = 5 AND o.total = u.nick",
])
def test_a_condition_that_may_remove_a_row_is_kept(query: str):
    assert RewriteKind.IMPLIED_CONDITION not in rewrites(SCHEMA + query)


# Unused CTEs ------------------------------------------------------------------


def test_a_cte_nothing_references_is_dead_code():
    sql = SCHEMA + """
    WITH unused AS (SELECT id FROM users),
    used AS (SELECT user_id FROM sessions)
    SELECT user_id FROM used
    """
    rewrite, = suggest(sql).rewrites
    assert (rewrite.kind, rewrite.site.label, rewrite.site.line) == (RewriteKind.UNUSED_CTE, "CTE unused", 6)


def test_a_cte_referenced_only_by_an_unused_cte_is_dead_too():
    sql = SCHEMA + """
    WITH a AS (SELECT id FROM users), b AS (SELECT id FROM a)
    SELECT user_id FROM sessions
    """
    assert sorted(label for _, label, _ in rewrite_sites(sql)) == ["CTE a", "CTE b"]


def test_a_cte_used_in_a_nested_query_is_alive():
    sql = SCHEMA + """
    WITH a AS (SELECT id FROM users)
    SELECT user_id FROM sessions WHERE user_id IN (SELECT id FROM a)
    """
    assert rewrites(sql) == []


# Output -----------------------------------------------------------------------


def test_the_cli_lists_rewrites_before_markers(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT u.id, s.ts FROM sessions s\nLEFT JOIN users u ON u.id = s.user_id\nWHERE u.name = 'a'")

    assert main(["suggest", str(path)]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split()[:6] == ["rewrite", "line", "6", "LEFT", "JOIN", "users"]
    assert "INNER JOIN" in lines[0]
    assert lines[1:9] == [
        f"    --- {path}",
        f"    +++ {path} (rewritten)",
        "    @@ -4,4 +4,4 @@",
        "     CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users (id), total INTEGER);",
        "     SELECT u.id, s.ts FROM sessions s",
        "    -LEFT JOIN users u ON u.id = s.user_id",
        "    +JOIN users u ON u.id = s.user_id",
        "     WHERE u.name = 'a'",
    ]
    assert all(line.startswith("marker") for line in lines[9:])
    assert len(lines) == 9 + len(suggest(path.read_text()).suggestions)


def test_the_cli_labels_a_rewrite_with_caveat(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT id, count(*) AS n FROM users GROUP BY id")

    assert main(["suggest", str(path)]) == 0

    line = capsys.readouterr().out.splitlines()[0]
    assert line.startswith("rewrite with caveat  line 5  Root Select  GROUP BY of one row per group  ")
    assert line.endswith("Caveat: 'n' changes from count's result type to an integer literal's type; cast it if later arithmetic needs the wider type")


# Rewrites preserve the result ---------------------------------------------------

# (original, rewritten, the rewrite that justifies the rewrite)
EQUIVALENT = [
    # Issue 33: padding traced through a later LEFT JOIN whose ON rejects the padded column.
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = u.id WHERE o.total = 1",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = u.id JOIN users v ON v.id = o.user_id",
        RewriteKind.INNER_JOIN,
    ),
    # A condition never TRUE on a padded row, through OR or coalesce (issue 45, item 9).
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id WHERE u.nick = 'a' OR u.email = 'b'",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id WHERE coalesce(u.nick, u.email) = 'a'",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = u.id WHERE o.total = 1 OR o.id = 2",
        RewriteKind.INNER_JOIN,
    ),
    # A parenthesized join: the ON or WHERE outside rejects its padding.
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s "
        "LEFT JOIN (users u LEFT JOIN orders o ON o.user_id = u.id) ON o.total = s.ts",
        RewriteKind.INNER_JOIN,
    ),
    # Both LEFT JOINs: each Rewrite is checked on its own.
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s "
        "LEFT JOIN (users u LEFT JOIN orders o ON o.user_id = u.id) ON u.id = s.user_id WHERE o.total = 1",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM (users u LEFT JOIN orders o ON o.user_id = u.id) "
        "RIGHT JOIN sessions s ON o.total = s.ts",
        RewriteKind.INNER_JOIN,
    ),
    # A WHERE EXISTS is a semi join, whose ON rejects NULL like a later inner join's.
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.user_id = u.id)",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.user_id = s.user_id) AND u.nick = 'x'",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT JOIN users u ON u.id = s.user_id WHERE u.nick = '1'",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "FULL JOIN orders o ON o.total = s.ts WHERE u.name IS NOT NULL",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.user_id = s.user_id AND o.total = 1 JOIN orders p ON p.id = o.id",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT o.total, u.name FROM orders o LEFT JOIN users u ON u.id = o.user_id",
        RewriteKind.INNER_JOIN,
    ),
    (
        SCHEMA + "SELECT s.ts, count(*) AS n FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "AND u.nick = '1' GROUP BY s.ts",
        RewriteKind.REDUNDANT_JOIN,
    ),
    (
        SCHEMA + "SELECT o.total FROM orders o JOIN users u ON u.id = o.user_id",
        RewriteKind.REDUNDANT_JOIN,
    ),
    (
        SCHEMA + "SELECT DISTINCT o.id, u.name FROM orders o LEFT JOIN users u ON u.id = o.user_id",
        RewriteKind.REDUNDANT_DISTINCT,
    ),
    (
        SCHEMA + "SELECT DISTINCT s.user_id, u.nick FROM (SELECT DISTINCT user_id FROM sessions) AS s "
        "LEFT JOIN users u ON u.id = s.user_id",
        RewriteKind.REDUNDANT_DISTINCT,
    ),
    (
        SCHEMA + "SELECT DISTINCT user_id, total FROM (SELECT DISTINCT user_id, total FROM orders) AS t",
        RewriteKind.REDUNDANT_DISTINCT,
    ),
    (
        SCHEMA + "WITH dead AS (SELECT id FROM users) SELECT user_id FROM sessions",
        RewriteKind.UNUSED_CTE,
    ),
    (
        SCHEMA + "SELECT id, nick FROM users EXCEPT SELECT user_id, CAST(ts AS VARCHAR) FROM sessions",
        RewriteKind.SET_OPERATION_ALL,
    ),
    (
        SCHEMA + "SELECT user_id FROM sessions INTERSECT SELECT id FROM users",
        RewriteKind.SET_OPERATION_ALL,
    ),
    (
        SCHEMA + "SELECT user_id FROM sessions EXCEPT SELECT id FROM users WHERE nick IS NULL EXCEPT SELECT ts FROM sessions",
        RewriteKind.SET_OPERATION_ALL,
    ),
    (
        SCHEMA + "SELECT id, name FROM users WHERE id = 3 ORDER BY name LIMIT 1",
        RewriteKind.ONE_ROW,
    ),
    # A WHERE subquery every row passes (Kossmann IND, Sec. 8.2).
    (
        SCHEMA + "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = o.user_id)",
        RewriteKind.REDUNDANT_FILTER,
    ),
    (
        SCHEMA + "SELECT o.id FROM orders o WHERE o.user_id IN (SELECT id FROM users) AND o.total = 1",
        RewriteKind.REDUNDANT_FILTER,
    ),
]


# Kinds whose rewrite has no edits yet: compared with a query written by hand.
EQUIVALENT_BY_HAND = [
    (
        SCHEMA + "SELECT id, name, count(*) AS n FROM users JOIN sessions ON sessions.user_id = users.id GROUP BY 1, 2",
        SCHEMA + "SELECT id, any_value(name) AS name, count(*) AS n FROM users JOIN sessions ON sessions.user_id = users.id GROUP BY 1",
        RewriteKind.REDUNDANT_GROUPING,
    ),
    (
        SCHEMA + "SELECT s.ts, o.total FROM sessions s JOIN users u ON u.id = s.user_id AND u.nick = '1' "
        "LEFT JOIN orders o ON o.total = s.ts",
        SCHEMA + "SELECT s.ts, o.total FROM sessions s LEFT JOIN orders o ON o.total = s.ts "
        "WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = s.user_id AND u.nick = '1')",
        RewriteKind.FILTER_JOIN,
    ),
    (
        SCHEMA + "SELECT u.id, u.name, count(s.ts) AS n FROM users u LEFT JOIN sessions s ON s.user_id = u.id "
        "GROUP BY u.id, u.name",
        SCHEMA + "SELECT u.id, any_value(u.name) AS name, count(s.ts) AS n FROM users u "
        "LEFT JOIN sessions s ON s.user_id = u.id GROUP BY u.id",
        RewriteKind.REDUNDANT_GROUPING,
    ),
    (
        SCHEMA + "SELECT s.user_id, u.nick, count(*) AS n FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "GROUP BY s.user_id, u.nick",
        SCHEMA + "SELECT s.user_id, any_value(u.nick) AS nick, count(*) AS n FROM sessions s "
        "LEFT JOIN users u ON u.id = s.user_id GROUP BY s.user_id",
        RewriteKind.REDUNDANT_GROUPING,
    ),
    (
        SCHEMA + "SELECT id, count(*) AS n, count(nick) AS c, max(name) AS m, sum(DISTINCT id) AS s, "
        "any_value(nick) AS a, avg(id) AS v FROM users WHERE nick IS NULL OR id > 1 GROUP BY id",
        SCHEMA + "SELECT id, 1 AS n, CASE WHEN nick IS NULL THEN 0 ELSE 1 END AS c, name AS m, id AS s, "
        "nick AS a, id AS v FROM users WHERE nick IS NULL OR id > 1",
        RewriteKind.GROUPING_PROJECTION,
    ),
    (
        SCHEMA + "SELECT o.id, max(u.nick) AS nick, count(u.nick) AS c FROM orders o LEFT JOIN users u ON u.id = o.user_id "
        "GROUP BY o.id",
        SCHEMA + "SELECT o.id, u.nick AS nick, CASE WHEN u.nick IS NULL THEN 0 ELSE 1 END AS c FROM orders o "
        "LEFT JOIN users u ON u.id = o.user_id",
        RewriteKind.GROUPING_PROJECTION,
    ),
    (
        SCHEMA + "SELECT u.id, v.name, v.email FROM users u LEFT JOIN users v ON u.id = v.id AND v.nick = 'x'",
        SCHEMA + "SELECT u.id, CASE WHEN u.nick = 'x' THEN u.name END AS name, CASE WHEN u.nick = 'x' THEN u.email END AS email FROM users AS u",
        RewriteKind.SELF_JOIN,
    ),
    (
        SCHEMA + "SELECT u.id, v.nick FROM users u JOIN users v ON u.id = v.id AND u.nick = v.nick AND v.name <> 'a'",
        SCHEMA + "SELECT u.id, u.nick FROM users AS u WHERE u.nick IS NOT NULL AND u.name <> 'a'",
        RewriteKind.SELF_JOIN,
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
        before = Counter(database.connection.execute(queries[0]).fetchall())
        try:
            after = Counter(database.connection.execute(queries[1]).fetchall())
        except duckdb.Error:
            return True
        return before != after

    return database.search(differs, max_examples=400)


def offered(original: str, kind: RewriteKind) -> list[str]:
    """The SQL of each `kind` Rewrite `suggest` offers for `original`, its edits applied."""
    report = suggest(original)
    found = [rewrite.rewritten(report.sql) for rewrite in report.rewrites if rewrite.kind is kind]
    assert found, f"no {kind.value} rewrite offered"
    return found


@pytest.mark.parametrize(("original", "rewrite"), EQUIVALENT)
def test_each_rewrite_preserves_the_result_on_random_instances(original: str, rewrite: RewriteKind):
    for rewritten in offered(original, rewrite):
        search = differing_instance(original, rewritten)
        assert search.status == "no-counterexample", (rewritten, search)
        assert search.valid >= 100


@pytest.mark.parametrize(("original", "rewritten", "rewrite"), EQUIVALENT_BY_HAND)
def test_each_rewrite_without_edits_preserves_the_result_on_random_instances(original: str, rewritten: str, rewrite: RewriteKind):
    assert rewrite in rewrites(original)

    search = differing_instance(original, rewritten)
    assert search.status == "no-counterexample", search
    assert search.valid >= 100


# One Rewrite of each kind, and the diff its edits make, written by hand.
EXPECTED_DIFFS = [
    (
        RewriteKind.INNER_JOIN,
        SCHEMA + "SELECT s.ts, u.name FROM sessions s\nLEFT OUTER JOIN users u ON u.id = s.user_id\nWHERE u.nick = 'x'",
        """\
@@ -6 +6 @@
-LEFT OUTER JOIN users u ON u.id = s.user_id
+JOIN users u ON u.id = s.user_id""",
    ),
    (
        RewriteKind.REDUNDANT_JOIN,
        SCHEMA + "SELECT s.ts FROM sessions s\nLEFT JOIN users u ON u.id = s.user_id\nWHERE s.ts = 1",
        """\
@@ -6 +5,0 @@
-LEFT JOIN users u ON u.id = s.user_id""",
    ),
    (
        RewriteKind.REDUNDANT_DISTINCT,
        SCHEMA + "SELECT DISTINCT id, nick FROM users",
        """\
@@ -5 +5 @@
-SELECT DISTINCT id, nick FROM users
+SELECT id, nick FROM users""",
    ),
    (
        RewriteKind.SET_OPERATION_ALL,
        SCHEMA + "SELECT id FROM users\nEXCEPT\nSELECT user_id FROM sessions",
        """\
@@ -6 +6 @@
-EXCEPT
+EXCEPT ALL""",
    ),
    (
        RewriteKind.ONE_ROW,
        SCHEMA + "SELECT id, name FROM users WHERE id = 3 ORDER BY name LIMIT 1",
        """\
@@ -5 +5 @@
-SELECT id, name FROM users WHERE id = 3 ORDER BY name LIMIT 1
+SELECT id, name FROM users WHERE id = 3""",
    ),
    (
        RewriteKind.UNUSED_CTE,
        SCHEMA + "WITH used AS (SELECT user_id FROM sessions),\ndead AS (SELECT id FROM users)\nSELECT user_id FROM used",
        """\
@@ -5,2 +5 @@
-WITH used AS (SELECT user_id FROM sessions),
-dead AS (SELECT id FROM users)
+WITH used AS (SELECT user_id FROM sessions)""",
    ),
    (
        RewriteKind.REDUNDANT_FILTER,
        SCHEMA + "SELECT o.id FROM orders o\nWHERE o.total BETWEEN 1 AND 2\n  AND o.user_id IN (SELECT id FROM users)",
        """\
@@ -7 +6,0 @@
-  AND o.user_id IN (SELECT id FROM users)""",
    ),
    (
        RewriteKind.REDUNDANT_SORT_KEY,
        SCHEMA + "SELECT id, name FROM users\nORDER BY id DESC, name NULLS FIRST\nLIMIT 2",
        """\
@@ -6 +6 @@
-ORDER BY id DESC, name NULLS FIRST
+ORDER BY id DESC""",
    ),
]


@pytest.mark.parametrize(("kind", "sql", "expected"), EXPECTED_DIFFS, ids=[kind.name for kind, _, _ in EXPECTED_DIFFS])
def test_each_kind_of_rewrite_makes_the_diff_written_here(kind: RewriteKind, sql: str, expected: str):
    report = suggest(sql)
    rewrite, = (item for item in report.rewrites if item.kind is kind)
    assert rewrite.diff(report.sql, context=0) == "--- original\n+++ original (rewritten)\n" + expected


def test_an_edit_keeps_the_comments_in_its_span():
    sql = SCHEMA + "SELECT s.ts, u.name FROM sessions s LEFT /* outer */ OUTER JOIN users u ON u.id = s.user_id WHERE u.nick = 'x'"
    report = suggest(sql)
    rewrite, = report.rewrites
    assert rewrite.rewritten(report.sql).endswith("FROM sessions s /* outer */ JOIN users u ON u.id = s.user_id WHERE u.nick = 'x'")


def test_the_equivalence_check_finds_a_rewrite_that_changes_the_result():
    # Guards the test above: dropping a join that may multiply rows is caught.
    search = differing_instance(
        SCHEMA + "SELECT u.name FROM users u LEFT JOIN sessions s ON s.user_id = u.id",
        SCHEMA + "SELECT u.name FROM users u",
    )
    assert search.status == "counterexample"
    assert search.instance is not None


def test_the_equivalence_check_finds_except_all_over_duplicates():
    search = differing_instance(
        SCHEMA + "SELECT user_id FROM sessions EXCEPT SELECT id FROM users",
        SCHEMA + "SELECT user_id FROM sessions EXCEPT ALL SELECT id FROM users",
    )
    assert search.status == "counterexample"


def test_a_later_left_join_on_a_column_that_is_not_padded_does_not_chain():
    sql = SCHEMA + (
        "SELECT s.ts, u.name, o.total FROM sessions s LEFT JOIN users u ON u.id = s.user_id "
        "LEFT JOIN orders o ON o.total = s.ts WHERE o.total = 1"
    )
    assert [label for rewrite, label, _ in rewrite_sites(sql) if rewrite is RewriteKind.INNER_JOIN] == ["LEFT JOIN orders AS o"]


@pytest.mark.parametrize(
    "sql",
    [
        # A nullable column: a NULL matches nothing.
        "SELECT s.ts FROM sessions s WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = s.user_id)",
        # Another condition may reject the matching row.
        "SELECT o.id FROM orders o WHERE EXISTS (SELECT 1 FROM users u WHERE u.id = o.user_id AND u.nick = 'x')",
        # NOT EXISTS keeps no row here; it is not redundant.
        "SELECT o.id FROM orders o WHERE NOT EXISTS (SELECT 1 FROM users u WHERE u.id = o.user_id)",
    ],
)
def test_a_subquery_filter_that_may_reject_a_row_is_kept(sql: str):
    assert RewriteKind.REDUNDANT_FILTER not in rewrites(SCHEMA + sql)


@pytest.mark.parametrize(
    ("sql", "message"),
    [
        ("SELECT id, name FROM t_sorted ORDER BY id, name", "('id') determine 'name'"),
        ("SELECT name FROM t_sorted ORDER BY score, score", "('score') determine 'score'"),
        ("SELECT score, count(*) AS n FROM t_sorted GROUP BY score ORDER BY score, n", "('score') determine 'n'"),
    ],
)
def test_an_order_by_key_the_earlier_keys_determine_is_redundant(sql: str, message: str):
    schema = "CREATE TABLE t_sorted (id INTEGER PRIMARY KEY, name VARCHAR, score INTEGER);\n"
    found = [item for item in suggest(schema + sql).rewrites if item.kind is RewriteKind.REDUNDANT_SORT_KEY]
    assert len(found) == 1
    assert message in found[0].message


@pytest.mark.parametrize(
    "sql",
    ["SELECT id, name FROM t_sorted ORDER BY name, id", "SELECT id FROM t_sorted ORDER BY lower(name), id"],
)
def test_an_order_by_key_that_breaks_ties_is_kept(sql: str):
    schema = "CREATE TABLE t_sorted (id INTEGER PRIMARY KEY, name VARCHAR, score INTEGER);\n"
    assert not any(item.kind is RewriteKind.REDUNDANT_SORT_KEY for item in suggest(schema + sql).rewrites)


@pytest.mark.parametrize(
    "sql",
    [
        # q.b is the input column, not the output alias b, so b DESC still breaks ties.
        "CREATE TABLE q (a INTEGER, b INTEGER);\nSELECT DISTINCT a AS b FROM q ORDER BY q.b, b DESC LIMIT 1",
        # DISTINCT ON's ORDER BY picks which row of each group survives.
        "CREATE TABLE d (id INTEGER, v INTEGER);\nSELECT DISTINCT ON (id) id, v FROM d ORDER BY id, v",
        # -0.0 = 0.0, so two rows tie on x: n orders them.
        "CREATE TABLE c (x DOUBLE PRIMARY KEY, n INTEGER);\nSELECT x, n FROM c ORDER BY x, n LIMIT 1",
    ],
    ids=["qualified key", "distinct on", "float key"],
)
def test_an_order_by_key_whose_earlier_keys_may_tie_different_rows_is_kept(sql: str):
    assert not any(item.kind is RewriteKind.REDUNDANT_SORT_KEY for item in suggest(sql).rewrites)


def test_the_equivalence_check_catches_errors_in_only_the_rewritten_query():
    ddl = "CREATE TABLE t(a INT PRIMARY KEY, b INT); "
    before = ddl + "SELECT a, sum(b)+1 AS s FROM t WHERE b<=2147483647 GROUP BY a"
    after = ddl + "SELECT a, b+1 AS s FROM t WHERE b<=2147483647"
    assert differing_instance(before, after).status == "counterexample"
