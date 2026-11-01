"""Direct Foreign Keys: `REFERENCES` constraints in `CREATE TABLE`,
`/**DECLARE FOREIGN KEY(...) REFERENCES t(...)**/`, and
`/**ASSERT FOREIGN KEY(...) REFERENCES t(...)**/`.

A Foreign Key is Referential Coverage onto a Unique Set of the referenced
relation. Constraints and declarations supply the coverage only; the Unique Set
is proved separately every time. Under MATCH SIMPLE a row with a null
referencing component is exempt, so nullable referencing columns never weaken
a Foreign Key.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import (
    INVALID_FOREIGN_KEY_CONSTRAINT,
    UNATTACHED_MARKER,
    UNKNOWN_ASSERTED_COLUMN,
    UNKNOWN_REFERENCED_COLUMN,
)

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, org INTEGER, name VARCHAR, UNIQUE (org, name));
CREATE TABLE plain (id INTEGER);
CREATE TABLE orders (
    id INTEGER,
    user_id INTEGER REFERENCES users(id),
    buyer INTEGER REFERENCES users,
    org INTEGER,
    owner VARCHAR,
    FOREIGN KEY (org, owner) REFERENCES users(org, name)
);
CREATE TABLE events (user_id INTEGER, ts TIMESTAMP);
"""


def report_of(sql: str):
    return analyze(f"{SCHEMA} {sql}")


def outcomes(sql: str) -> list[Outcome]:
    report = report_of(sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


def explanation(sql: str) -> str:
    report = report_of(sql)
    evidence = report.assertions[0].evidence
    assert evidence is not None
    return evidence.explain()


PROVED = [
    ("column-level constraint", "SELECT * FROM orders /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/"),
    ("constraint naming no columns uses the primary key", "SELECT * FROM orders /**ASSERT FOREIGN KEY(buyer) REFERENCES users(id)**/"),
    ("table-level composite constraint", "SELECT * FROM orders /**ASSERT FOREIGN KEY(org, owner) REFERENCES users(org, name)**/"),
    ("composite pairs listed in another order", "SELECT * FROM orders /**ASSERT FOREIGN KEY(owner, org) REFERENCES users(name, org)**/"),
    ("aliased table", "SELECT * FROM orders AS o /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/"),
]


@pytest.mark.parametrize(("label", "sql"), PROVED)
def test_a_direct_foreign_key_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


def test_nullable_referencing_columns_do_not_invalidate_a_foreign_key():
    # `user_id` is nullable: under MATCH SIMPLE its null rows are exempt.
    assert outcomes(
        "SELECT * FROM orders /**ASSERT NOT NULL(user_id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/"
    ) == [Outcome.UNKNOWN, Outcome.PROVED]


def test_assertions_are_reported_in_written_order():
    assert outcomes(
        "SELECT * FROM orders /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/ /**ASSERT NOT NULL(user_id)**/"
    ) == [Outcome.PROVED, Outcome.UNKNOWN]


def test_composite_columns_pair_by_position():
    assert outcomes("SELECT * FROM orders /**ASSERT FOREIGN KEY(org, owner) REFERENCES users(name, org)**/") == [
        Outcome.UNKNOWN
    ]


def test_a_partial_composite_key_is_not_a_foreign_key():
    # Under MATCH SIMPLE a row with a null `owner` need not match on `org`.
    assert outcomes("SELECT * FROM orders /**ASSERT FOREIGN KEY(org) REFERENCES users(org)**/") == [Outcome.UNKNOWN]


def test_qualified_and_named_table_level_constraints():
    report = analyze(
        """
        CREATE TABLE app.users (id INTEGER PRIMARY KEY);
        CREATE TABLE app.orders (user_id INTEGER, CONSTRAINT fk_user FOREIGN KEY (user_id) REFERENCES app.users(id) MATCH FULL);
        SELECT * FROM app.orders /**ASSERT FOREIGN KEY(user_id) REFERENCES app.users(id)**/
        """
    )

    assert not report.diagnostics
    assert [a.outcome for a in report.assertions] == [Outcome.PROVED]


def test_a_self_referencing_table():
    report = analyze(
        """
        CREATE TABLE employees (id INTEGER PRIMARY KEY, manager INTEGER REFERENCES employees(id));
        SELECT * FROM employees /**ASSERT FOREIGN KEY(manager) REFERENCES employees(id)**/
        """
    )

    assert report.proved


def test_without_a_constraint_the_relationship_is_unknown_and_explained():
    sql = "SELECT * FROM events /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/"

    assert outcomes(sql) == [Outcome.UNKNOWN]
    assert "No known Foreign Key from (user_id) to users(id)" in explanation(sql)


def test_an_unknown_relationship_names_the_known_ones_from_the_same_columns():
    sql = "SELECT * FROM orders /**ASSERT FOREIGN KEY(user_id) REFERENCES plain(id)**/"

    assert outcomes(sql) == [Outcome.UNKNOWN]
    assert "these columns reference users(id)" in explanation(sql)


def test_foreign_keys_are_not_yet_propagated_through_filters():
    sql = "SELECT * FROM orders WHERE id > 0 /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/"

    assert outcomes(sql) == [Outcome.UNKNOWN]
    assert "not propagated" in explanation(sql)


# Declarations ---------------------------------------------------------------

DECLARATION_SITES = [
    ("root select", "SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/"),
    ("view", "CREATE VIEW v AS SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/; SELECT * FROM v"),
    ("cte", "WITH c AS (SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/) SELECT * FROM c"),
    ("from subquery", "SELECT * FROM (SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/) AS s"),
]


@pytest.mark.parametrize(("label", "sql"), DECLARATION_SITES)
def test_a_declaration_proves_an_assertion_at_every_attachment_site(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


def test_a_declaration_produces_no_assertion_result():
    report = report_of("SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/")

    assert report.assertions == ()
    assert report.proved


def test_the_explanation_identifies_the_declaration_as_trusted():
    sql = "SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/"

    assert "Declared ReferentialCoverage" in explanation(sql)


def test_a_declaration_still_needs_a_unique_set_on_the_referenced_columns():
    sql = "SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES plain(id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES plain(id)**/"

    assert outcomes(sql) == [Outcome.UNKNOWN]
    text = explanation(sql)
    assert "Declared ReferentialCoverage" in text
    assert "referenced columns plain(id) must form a Unique Set" in text


def test_a_declared_unique_set_on_the_referenced_relation_suffices():
    sql = """
    CREATE VIEW people AS SELECT id FROM plain /**DECLARE UNIQUE(id)**/;
    SELECT user_id FROM events
    /**DECLARE FOREIGN KEY(user_id) REFERENCES people(id)**/
    /**ASSERT FOREIGN KEY(user_id) REFERENCES people(id)**/
    """

    assert outcomes(sql) == [Outcome.PROVED]


def test_a_cte_can_be_referenced():
    sql = """
    WITH ids AS (SELECT DISTINCT id FROM plain)
    SELECT user_id FROM events
    /**DECLARE FOREIGN KEY(user_id) REFERENCES ids(id)**/
    /**ASSERT FOREIGN KEY(user_id) REFERENCES ids(id)**/
    """

    assert outcomes(sql) == [Outcome.PROVED]


def test_a_view_can_reference_itself():
    sql = """
    CREATE VIEW tree AS SELECT id, parent FROM plain_tree
    /**DECLARE UNIQUE(id)**/
    /**DECLARE FOREIGN KEY(parent) REFERENCES tree(id)**/
    /**ASSERT FOREIGN KEY(parent) REFERENCES tree(id)**/;
    SELECT * FROM tree
    """

    assert outcomes(sql) == [Outcome.PROVED]


def test_an_undeclared_referenced_relation_is_explained():
    sql = "SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES accounts(id)**/ /**ASSERT FOREIGN KEY(user_id) REFERENCES accounts(id)**/"

    assert outcomes(sql) == [Outcome.UNKNOWN]
    assert "accounts has no complete declared schema" in explanation(sql)


# Diagnostics ----------------------------------------------------------------


def test_an_unknown_referencing_column_is_reported():
    report = report_of("SELECT user_id FROM events /**ASSERT FOREIGN KEY(missing) REFERENCES users(id)**/")

    assert [d.code for d in report.diagnostics] == [UNKNOWN_ASSERTED_COLUMN]
    assert "foreign key assertion" in report.diagnostics[0].message


def test_an_unknown_referenced_column_is_reported():
    report = report_of("SELECT user_id FROM events /**DECLARE FOREIGN KEY(user_id) REFERENCES users(missing)**/")

    assert [d.code for d in report.diagnostics] == [UNKNOWN_REFERENCED_COLUMN]
    assert "users" in report.diagnostics[0].message and "missing" in report.diagnostics[0].message


@pytest.mark.parametrize(
    ("label", "marker"),
    [
        ("mismatched column counts", "/**ASSERT FOREIGN KEY(user_id) REFERENCES users(org, name)**/"),
        ("no REFERENCES", "/**ASSERT FOREIGN KEY(user_id)**/"),
        ("no referenced columns", "/**ASSERT FOREIGN KEY(user_id) REFERENCES users**/"),
        ("repeated referenced column", "/**ASSERT FOREIGN KEY(user_id, ts) REFERENCES users(id, id)**/"),
    ],
)
def test_a_malformed_marker_is_reported(label: str, marker: str):
    report = report_of(f"SELECT user_id, ts FROM events {marker}")

    assert [d.code for d in report.diagnostics] == [UNATTACHED_MARKER], label
    assert report.assertions == ()


def test_a_constraint_naming_an_unknown_referenced_column_is_reported():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (user_id INTEGER REFERENCES users(missing));
        SELECT * FROM orders
        """
    )

    assert [d.code for d in report.diagnostics] == [UNKNOWN_REFERENCED_COLUMN]


def test_a_constraint_without_columns_needs_a_primary_key():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER);
        CREATE TABLE orders (user_id INTEGER REFERENCES users);
        SELECT * FROM orders
        """
    )

    assert [d.code for d in report.diagnostics] == [UNKNOWN_REFERENCED_COLUMN]
    assert "primary key" in report.diagnostics[0].message


@pytest.mark.parametrize(
    ("label", "constraint"),
    [
        ("unknown referencing column", "FOREIGN KEY (missing) REFERENCES users(id)"),
        ("mismatched column counts", "FOREIGN KEY (user_id) REFERENCES users(id, org)"),
    ],
)
def test_an_invalid_table_level_constraint_is_reported(label: str, constraint: str):
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY, org INTEGER);
        CREATE TABLE orders (user_id INTEGER, {constraint});
        SELECT * FROM orders
        """
    )

    assert [d.code for d in report.diagnostics] == [INVALID_FOREIGN_KEY_CONSTRAINT], label


def test_a_constraint_to_a_relation_outside_the_program_is_ignored():
    report = analyze("CREATE TABLE orders (user_id INTEGER REFERENCES users(id)); SELECT * FROM orders")

    assert report.proved
