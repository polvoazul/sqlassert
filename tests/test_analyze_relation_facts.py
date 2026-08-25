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


def test_a_cte_that_earns_its_own_relation_definition_is_queryable_by_a_prefixed_name():
    """A CTE ending in a Project, Aggregate, or Distinct always earns a fresh,
    exclusively-owned Relation Definition -- safe to label with the CTE's own
    declared name, under a `CTE_` prefix so it can never collide with a real
    table or view Knowledge is looked up by."""
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


def test_a_bare_passthrough_cte_shares_its_inputs_definition_and_has_no_name_of_its_own():
    """`SELECT * FROM users` ends in a Filter, which deliberately reuses its
    *input's* Relation Definition rather than earning its own -- relabelling
    a shared definition could misname whatever real relation it belongs to,
    so this CTE is left unnamed rather than mislabeled."""
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY);

        WITH active_users AS (SELECT * FROM users WHERE id > 0)
        SELECT * FROM active_users
        """
    )

    assert report.facts.unique_sets("CTE_active_users") == ()
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
    Relation Definition must never borrow the real table's declared Knowledge
    just because `report_name` happens to echo its name: `other.x` has no
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
