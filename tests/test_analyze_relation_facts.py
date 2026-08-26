"""`Report.facts`: Unique Sets proved for named relations, queryable directly.

Unlike `report.assertions`, which only covers `/**UNIQUE**/`-marked joins,
`report.facts` answers "is this column set unique on this relation" for any
named relation -- a table or a view -- that the analysis actually reasoned
about. It is read from the same `unique_set`/`unique_set_member` atoms the
Reporter already captures for assertion evidence; nothing new is derived.
"""

from __future__ import annotations

from sqlassert import analyze


def test_a_declared_primary_key_is_a_unique_set_on_its_table():
    report = analyze("CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR); SELECT * FROM users")

    assert report.facts.unique_sets("users") == (("id",),)
    assert report.facts.is_unique("users", ["id"])
    assert report.facts.is_unique("users", ["id", "email"])
    assert not report.facts.is_unique("users", ["email"])


def test_relation_names_are_looked_up_case_insensitively():
    report = analyze("CREATE TABLE Users (id INTEGER PRIMARY KEY); SELECT * FROM Users")

    assert report.facts.unique_sets("USERS") == (("id",),)
    assert report.facts.is_unique("users", ["ID"])


def test_a_composite_primary_key_requires_every_member_to_be_covered():
    report = analyze(
        """
        CREATE TABLE order_items (order_id INTEGER, line_no INTEGER, PRIMARY KEY (order_id, line_no));
        SELECT * FROM order_items
        """
    )

    assert report.facts.unique_sets("order_items") == (("order_id", "line_no"),)
    assert not report.facts.is_unique("order_items", ["order_id"])
    assert report.facts.is_unique("order_items", ["order_id", "line_no"])


def test_a_view_actually_used_in_the_program_reports_its_derived_unique_set():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE VIEW active_users AS SELECT id FROM users WHERE id > 0;

        SELECT * FROM active_users
        """
    )

    assert report.facts.unique_sets("active_users") == (("id",),)
    assert report.facts.is_unique("active_users", ["id"])


def test_a_view_never_referenced_reports_no_facts():
    """The engine only proves properties about relations it actually expands
    -- a declared-but-unused view was never lowered, so it has no facts."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE VIEW active_users AS SELECT id FROM users WHERE id > 0;

        SELECT * FROM users
        """
    )

    assert report.facts.unique_sets("active_users") == ()


def test_an_unknown_relation_reports_no_facts():
    report = analyze("CREATE TABLE users (id INTEGER PRIMARY KEY); SELECT * FROM users")

    assert report.facts.unique_sets("nonexistent") == ()
    assert not report.facts.is_unique("nonexistent", ["id"])


def test_a_cte_with_proved_output_properties_is_queryable_by_a_prefixed_name():
    """A CTE is a distinct Named Relation, safe to label with its declared
    name under a `CTE_` prefix so it cannot collide with table or view
    Knowledge."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        WITH active_users AS (SELECT id FROM users WHERE id > 0)
        SELECT * FROM active_users
        """
    )

    assert report.facts.unique_sets("CTE_active_users") == (("id",),)
    assert report.facts.is_unique("CTE_active_users", ["id"])
    assert report.facts.unique_sets("active_users") == ()


def test_a_bare_passthrough_cte_still_earns_its_own_name():
    """`SELECT * FROM users` ends in a Filter -- Filter owns a fresh Relation
    Definition just like Project, Aggregate, and Distinct do, and inherits
    its input's Unique Sets through `propagation.lp` rather than by sharing
    identity with it, so it is nameable exactly like any other CTE shape.
    The real `users` table's own facts stay reported separately."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        WITH active_users AS (SELECT * FROM users WHERE id > 0)
        SELECT * FROM active_users
        """
    )

    assert report.facts.unique_sets("CTE_active_users") == (("id",),)
    assert report.facts.unique_sets("users") == (("id",),)


def test_a_from_subquery_has_no_name_to_ask_about():
    """Unlike a CTE, a FROM subquery has no declared name in the SQL at all --
    only this slice's own naming machinery ever sees its alias."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        SELECT * FROM (SELECT id FROM users WHERE id > 0) AS active_users
        """
    )

    assert report.facts.unique_sets("active_users") == ()
    assert report.facts.unique_sets("CTE_active_users") == ()


def test_a_cte_shadowing_a_real_table_name_never_borrows_its_knowledge():
    """A CTE shadows a same-named table for name *resolution* (the query
    reads from `other`, not the real `active_users`), but the CTE's own
    Named Relation must never borrow the real table's declared Knowledge just
    because its report label echoes the name: `other.x` has no
    Unique Set, so the CTE must report none, regardless of what the real,
    unrelated `active_users` table declares."""
    report = analyze(
        """
        CREATE TABLE active_users (id INTEGER PRIMARY KEY, plan VARCHAR);
        CREATE TABLE other (x INTEGER);

        WITH active_users AS (SELECT x AS id FROM other)
        SELECT * FROM active_users
        """
    )

    assert report.facts.unique_sets("CTE_active_users") == ()
