"""Not-null assertions and declarations, and the Non-Null Unique Sets they complete.

`/**ASSERT NOT NULL(...)**/` asks for one proof covering every listed column;
`/**DECLARE NOT NULL(...)**/` and `/**DECLARE PRIMARY KEY(...)**/` state trusted
facts and produce no assertion result of their own.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNKNOWN_ASSERTED_COLUMN

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL, nickname VARCHAR);
CREATE TABLE sessions (user_id INTEGER NOT NULL, ts TIMESTAMP);
"""


def outcomes(sql: str) -> list[Outcome]:
    report = analyze(f"{SCHEMA} {sql}")
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


PROVED_NOT_NULL = [
    # Set operations (Paulley §3.2.5-3.2.6), inputs paired by position.
    ("union", "WITH c AS (SELECT id FROM users UNION ALL SELECT id FROM users) SELECT id FROM c /**ASSERT NOT NULL(id)**/"),
    ("intersect, non-null on the right", "WITH c AS (SELECT nickname FROM users INTERSECT SELECT email FROM users) SELECT nickname FROM c /**ASSERT NOT NULL(nickname)**/"),
    ("except", "WITH c AS (SELECT email FROM users EXCEPT SELECT nickname FROM users) SELECT email FROM c /**ASSERT NOT NULL(email)**/"),
    ("primary key column", "SELECT id FROM users /**ASSERT NOT NULL(id)**/"),
    ("NOT NULL column", "SELECT email FROM users /**ASSERT NOT NULL(email)**/"),
    ("several columns", "SELECT id, email FROM users /**ASSERT NOT NULL(id, email)**/"),
    ("renamed column", "SELECT email AS address FROM users /**ASSERT NOT NULL(address)**/"),
    ("filtered", "SELECT id FROM users WHERE nickname = 'x' /**ASSERT NOT NULL(id)**/"),
    ("filtered by IN", "SELECT nickname FROM users WHERE nickname IN ('a', 'b') /**ASSERT NOT NULL(nickname)**/"),
    ("star", "SELECT * FROM users /**ASSERT NOT NULL(id)**/"),
    ("string literal", "SELECT 'x' AS tag FROM users /**ASSERT NOT NULL(tag)**/"),
    ("numeric literal", "SELECT 1 AS one FROM users /**ASSERT NOT NULL(one)**/"),
    ("view", "CREATE VIEW v AS SELECT id FROM users; SELECT id FROM v /**ASSERT NOT NULL(id)**/"),
    ("cte", "WITH c AS (SELECT id FROM users) SELECT id FROM c /**ASSERT NOT NULL(id)**/"),
    ("subquery", "SELECT id FROM (SELECT id FROM users) AS s /**ASSERT NOT NULL(id)**/"),
    ("group key", "SELECT user_id, count(*) AS n FROM sessions GROUP BY user_id /**ASSERT NOT NULL(user_id)**/"),
    ("inner join, both sides", "SELECT s.user_id, u.email FROM sessions s JOIN users u ON s.user_id = u.id /**ASSERT NOT NULL(user_id, email)**/"),
    ("left join, preserved side", "SELECT s.user_id FROM sessions s LEFT JOIN users u ON s.user_id = u.id /**ASSERT NOT NULL(user_id)**/"),
    ("coalesce, non-null last argument", "SELECT coalesce(nickname, email) AS name FROM users /**ASSERT NOT NULL(name)**/"),
    ("coalesce, non-null first argument", "SELECT coalesce(email, nickname) AS name FROM users /**ASSERT NOT NULL(name)**/"),
    ("coalesce, literal fallback", "SELECT coalesce(nickname, '') AS name FROM users /**ASSERT NOT NULL(name)**/"),
    ("ifnull", "SELECT ifnull(nickname, 'anon') AS name FROM users /**ASSERT NOT NULL(name)**/"),
    ("coalesce over a null-extended column", "SELECT coalesce(u.nickname, 'anon') AS name FROM sessions s LEFT JOIN users u ON s.user_id = u.id /**ASSERT NOT NULL(name)**/"),
    ("right join, preserved side", "SELECT u.email FROM sessions s RIGHT JOIN users u ON s.user_id = u.id /**ASSERT NOT NULL(email)**/"),
    ("count(*) per group", "SELECT user_id, count(*) AS n FROM sessions GROUP BY user_id /**ASSERT NOT NULL(n)**/"),
    ("count of a nullable column", "SELECT user_id, count(ts) AS n FROM sessions GROUP BY user_id /**ASSERT NOT NULL(n)**/"),
    ("count DISTINCT", "SELECT user_id, count(DISTINCT ts) AS n FROM sessions GROUP BY user_id /**ASSERT NOT NULL(n)**/"),
    ("count without GROUP BY", "SELECT count(*) AS n FROM sessions /**ASSERT NOT NULL(n)**/"),
    ("count over a padded side", "SELECT s.user_id, count(u.nickname) AS n FROM sessions s LEFT JOIN users u ON s.user_id = u.id GROUP BY s.user_id /**ASSERT NOT NULL(n)**/"),
    ("count through a CTE", "WITH c AS (SELECT user_id, count(*) AS n FROM sessions GROUP BY user_id) SELECT n FROM c /**ASSERT NOT NULL(n)**/"),
]


@pytest.mark.parametrize(("label", "sql"), PROVED_NOT_NULL)
def test_not_null_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


UNKNOWN_NOT_NULL = [
    ("nullable column", "SELECT nickname FROM users /**ASSERT NOT NULL(nickname)**/"),
    ("null literal", "SELECT NULL AS nothing FROM users /**ASSERT NOT NULL(nothing)**/"),
    ("opaque expression", "SELECT upper(email) AS name FROM users /**ASSERT NOT NULL(name)**/"),
    ("coalesce of nullables", "SELECT coalesce(nickname, NULL) AS name FROM users /**ASSERT NOT NULL(name)**/"),
    ("left join, null-extended side", "SELECT u.email FROM sessions s LEFT JOIN users u ON s.user_id = u.id /**ASSERT NOT NULL(email)**/"),
    ("right join, null-extended side", "SELECT s.user_id FROM sessions s RIGHT JOIN users u ON s.user_id = u.id /**ASSERT NOT NULL(user_id)**/"),
    ("full join", "SELECT s.user_id FROM sessions s FULL JOIN users u ON s.user_id = u.id /**ASSERT NOT NULL(user_id)**/"),
    ("union, nullable in one input", "WITH c AS (SELECT id FROM users UNION ALL SELECT nickname FROM users) SELECT id FROM c /**ASSERT NOT NULL(id)**/"),
    ("union by name", "WITH c AS (SELECT id FROM users UNION BY NAME SELECT id FROM users) SELECT id FROM c /**ASSERT NOT NULL(id)**/"),
    ("intersect, nullable in both", "WITH c AS (SELECT nickname FROM users INTERSECT SELECT nickname FROM users) SELECT nickname FROM c /**ASSERT NOT NULL(nickname)**/"),
    ("except, nullable on the left", "WITH c AS (SELECT nickname FROM users EXCEPT SELECT email FROM users) SELECT nickname FROM c /**ASSERT NOT NULL(nickname)**/"),
    ("count padded by a later outer join", "WITH c AS (SELECT user_id, count(*) AS n FROM sessions GROUP BY user_id) SELECT c.n FROM users u LEFT JOIN c ON c.user_id = u.id /**ASSERT NOT NULL(n)**/"),
    ("count inside another expression", "SELECT user_id, nullif(count(*), 0) AS n FROM sessions GROUP BY user_id /**ASSERT NOT NULL(n)**/"),
    ("sum", "SELECT user_id, sum(user_id) AS n FROM sessions GROUP BY user_id /**ASSERT NOT NULL(n)**/"),
]


@pytest.mark.parametrize(("label", "sql"), UNKNOWN_NOT_NULL)
def test_not_null_is_not_guessed(label: str, sql: str):
    report = analyze(f"{SCHEMA} {sql}")
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN], label


def test_one_assertion_covers_its_whole_column_list():
    assert outcomes("SELECT id, nickname FROM users /**ASSERT NOT NULL(id, nickname)**/") == [Outcome.UNKNOWN]


def test_an_unknown_assertion_names_every_column_it_could_not_prove():
    report = analyze(
        f"""{SCHEMA}
        SELECT id, nickname, NULL AS nothing FROM users /**ASSERT NOT NULL(id, nickname, nothing)**/
        """
    )

    evidence = report.assertions[0].evidence
    assert evidence is not None
    explanation = evidence.explain()
    assert "nickname" in explanation
    assert "NULL literal" in explanation


def test_a_not_null_assertion_naming_an_unknown_column_is_reported():
    report = analyze(f"{SCHEMA} SELECT id FROM users /**ASSERT NOT NULL(missing)**/")

    assert [d.code for d in report.diagnostics] == [UNKNOWN_ASSERTED_COLUMN]
    assert "not null assertion" in report.diagnostics[0].message


# Declarations ----------------------------------------------------------------


def test_a_not_null_declaration_produces_no_assertion_result():
    report = analyze(f"{SCHEMA} SELECT nickname FROM users /**DECLARE NOT NULL(nickname)**/")

    assert report.assertions == ()
    assert report.diagnostics == ()


def test_a_not_null_declaration_proves_downstream_and_is_labelled_declared():
    report = analyze(
        f"""{SCHEMA}
        WITH c AS (SELECT nickname FROM users /**DECLARE NOT NULL(nickname)**/)
        SELECT nickname FROM c /**ASSERT NOT NULL(nickname)**/
        """
    )

    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]
    evidence = report.assertions[0].evidence
    assert evidence is not None
    assert "Declared NonNullColumn" in evidence.explain()


def test_a_not_null_declaration_does_not_survive_null_extension():
    assert outcomes(
        """
        WITH c AS (SELECT id, nickname FROM users /**DECLARE NOT NULL(nickname)**/)
        SELECT c.nickname FROM sessions s LEFT JOIN c ON s.user_id = c.id /**ASSERT NOT NULL(nickname)**/
        """
    ) == [Outcome.UNKNOWN]


# Non-Null Unique Sets --------------------------------------------------------------


def test_a_primary_key_assertion_needs_uniqueness_and_non_nullness():
    assert outcomes("SELECT id, email FROM users /**ASSERT PRIMARY KEY(id)**/") == [Outcome.PROVED]
    assert outcomes("SELECT DISTINCT nickname FROM users /**ASSERT PRIMARY KEY(nickname)**/") == [Outcome.UNKNOWN]
    assert outcomes("SELECT DISTINCT email FROM users /**ASSERT PRIMARY KEY(email)**/") == [Outcome.PROVED]


def test_a_primary_key_survives_a_unique_join_on_its_preserved_side():
    distinct_sessions = "(SELECT DISTINCT user_id FROM sessions) s ON s.user_id = u.id"
    assert outcomes(
        f"SELECT u.id FROM users u /**ASSERT UNIQUE**/ JOIN {distinct_sessions} /**ASSERT PRIMARY KEY(id)**/"
    ) == [Outcome.PROVED, Outcome.PROVED]
    assert outcomes(
        f"SELECT u.id FROM users u LEFT JOIN {distinct_sessions} /**ASSERT PRIMARY KEY(id)**/"
    ) == [Outcome.PROVED]


PRIMARY_KEY_DECLARATION_SITES = [
    ("root select", "SELECT nickname, email FROM users /**DECLARE PRIMARY KEY(nickname)**/ /**ASSERT PRIMARY KEY(nickname, email)**/"),
    ("view", "CREATE VIEW v AS SELECT nickname FROM users /**DECLARE PRIMARY KEY(nickname)**/; SELECT nickname FROM v /**ASSERT PRIMARY KEY(nickname)**/"),
    ("cte", "WITH c AS (SELECT nickname FROM users /**DECLARE PRIMARY KEY(nickname)**/) SELECT nickname FROM c /**ASSERT PRIMARY KEY(nickname)**/"),
    ("subquery", "SELECT nickname FROM (SELECT nickname FROM users /**DECLARE PRIMARY KEY(nickname)**/) AS s /**ASSERT PRIMARY KEY(nickname)**/"),
]


@pytest.mark.parametrize(("label", "sql"), PRIMARY_KEY_DECLARATION_SITES)
def test_a_primary_key_declaration_proves_a_downstream_primary_key(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


def test_a_composite_primary_key_declaration_also_declares_each_column_non_null():
    assert outcomes(
        """
        WITH c AS (SELECT nickname, email FROM users /**DECLARE PRIMARY KEY(nickname, email)**/)
        SELECT nickname FROM c /**ASSERT NOT NULL(nickname)**/ /**ASSERT UNIQUE(nickname)**/
        """
    ) == [Outcome.PROVED, Outcome.UNKNOWN]


def test_a_primary_key_declaration_proves_a_unique_join():
    assert outcomes(
        """
        WITH c AS (SELECT nickname FROM users /**DECLARE PRIMARY KEY(nickname)**/)
        SELECT * FROM sessions s /**ASSERT UNIQUE**/ JOIN c ON s.ts = c.nickname
        """
    ) == [Outcome.PROVED]


# Null sources: organic NULLs and outer-join padding ---------------------------

PADDING_SCHEMA = """
CREATE TABLE orders (id INTEGER PRIMARY KEY, user_id INTEGER);
CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR NOT NULL, nickname VARCHAR);
CREATE TABLE payments (id INTEGER PRIMARY KEY, order_id INTEGER, amount INTEGER NOT NULL);
"""


def padded_outcomes(sql: str) -> list[Outcome]:
    report = analyze(PADDING_SCHEMA + sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


def test_a_null_rejecting_where_conjunct_undoes_a_left_join_s_padding():
    """`u.id = 5` holds on no padded row, so every row left is matched and the
    right side's NOT NULL columns are non-null again; its keys are keys again."""
    assert padded_outcomes(
        """
        SELECT u.id, u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id = 5 /**ASSERT NOT NULL(id, email)**/
        """
    ) == [Outcome.PROVED]
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id IS NOT NULL /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.PROVED]


def test_without_a_null_rejecting_conjunct_padding_remains():
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE o.id = 5 /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.UNKNOWN]
    assert padded_outcomes(
        """
        SELECT u.email FROM orders o LEFT JOIN users u ON o.user_id = u.id
        WHERE u.id IS NULL /**ASSERT NOT NULL(email)**/
        """
    ) == [Outcome.UNKNOWN]


def test_each_outer_join_pads_its_own_columns():
    nested = """
        SELECT u.email, p.amount FROM orders o
        LEFT JOIN users u ON o.user_id = u.id
        LEFT JOIN payments p ON p.order_id = o.id
        WHERE {condition} /**ASSERT NOT NULL(amount)**/ /**ASSERT NOT NULL(email)**/
    """
    assert padded_outcomes(nested.format(condition="p.id IS NOT NULL")) == [Outcome.PROVED, Outcome.UNKNOWN]
    assert padded_outcomes(nested.format(condition="u.id IS NOT NULL")) == [Outcome.UNKNOWN, Outcome.PROVED]


def test_a_column_both_nullable_and_padded_needs_its_own_rejection():
    joined = "SELECT u.nickname FROM orders o LEFT JOIN users u ON o.user_id = u.id WHERE {condition} /**ASSERT NOT NULL(nickname)**/"
    assert padded_outcomes(joined.format(condition="u.id = 5")) == [Outcome.UNKNOWN]
    assert padded_outcomes(joined.format(condition="u.nickname IS NOT NULL")) == [Outcome.PROVED]


def test_an_unknown_names_each_null_source():
    report = analyze(
        PADDING_SCHEMA
        + "SELECT u.nickname FROM orders o LEFT JOIN users u ON o.user_id = u.id /**ASSERT NOT NULL(nickname)**/"
    )
    evidence = report.assertions[0].evidence
    assert evidence is not None
    explanation = evidence.explain()
    assert "nullable in users" in explanation
    assert "padded by the LEFT JOIN" in explanation
