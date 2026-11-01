"""Exactly One Join Assertions: `/**ASSERT EXACTLY ONE**/` before a JOIN.

Every left row matches exactly one right row. The at-most-one half is a Unique
Join: the right-side predicate columns cover a Unique Set. The at-least-one half
needs an ON of cross-input column equalities only, and Referential Coverage of
the left columns as one tuple onto the exact right relation and columns; every
left column must be non-null. Any join kind but semi and anti: the left row's
matches decide it, whatever a right or full join pads on.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze
from sqlassert.diagnostics import UNANALYZED_ASSERTION, UNATTACHED_MARKER, UNRECOGNIZED_MARKER

SCHEMA = """
CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
CREATE TABLE accounts (user_id INTEGER PRIMARY KEY);
CREATE TABLE sessions (user_id INTEGER);
CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    buyer INTEGER REFERENCES users(id),
    plain INTEGER NOT NULL
);
CREATE TABLE members (user_id INTEGER NOT NULL REFERENCES accounts(user_id));
CREATE TABLE events (user_id INTEGER);
"""


def report_of(sql: str):
    return analyze(f"{SCHEMA} {sql}")


def outcomes(sql: str) -> list[Outcome]:
    report = report_of(sql)
    assert not report.diagnostics, report.diagnostics
    return [assertion.outcome for assertion in report.assertions]


def explanation(sql: str) -> str:
    report = report_of(sql)
    assert not report.diagnostics, report.diagnostics
    evidence = report.assertions[0].evidence
    assert evidence is not None
    return evidence.explain()


PROVED = [
    ("inner join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id"),
    ("left join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ LEFT JOIN users ON orders.user_id = users.id"),
    ("operands reversed", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON users.id = orders.user_id"),
    ("aliased tables", "SELECT * FROM orders AS o /**ASSERT EXACTLY ONE**/ JOIN users AS u ON o.user_id = u.id"),
    ("using", "SELECT * FROM members /**ASSERT EXACTLY ONE**/ JOIN accounts USING (user_id)"),
    # Every left row matched once; the right rows nothing matched are padded on.
    ("right join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ RIGHT JOIN users ON orders.user_id = users.id"),
    ("full join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ FULL JOIN users ON orders.user_id = users.id"),
]


@pytest.mark.parametrize(("label", "sql"), PROVED)
def test_a_simple_exactly_one_join_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


UNKNOWN = [
    ("nullable left column", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.buyer = users.id"),
    ("no foreign key", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.plain = users.id"),
    ("right column not unique", "SELECT * FROM users /**ASSERT EXACTLY ONE**/ JOIN orders ON users.id = orders.user_id"),
    (
        "additional predicate",
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id AND users.name = 'x'",
    ),
    (
        "additional equality",
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id AND orders.id = users.id",
    ),
    ("equality to a constant", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON users.id = 1"),
    ("non-equality predicate", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id < users.id"),
    ("right join without coverage", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ RIGHT JOIN users ON orders.buyer = users.id"),
    ("cross join", "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ CROSS JOIN (SELECT count(*) AS n FROM users) AS c"),
    (
        "foreign key onto a narrowed relation",
        "WITH active AS (SELECT * FROM users WHERE name = 'x') "
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN active ON orders.user_id = active.id",
    ),
    (
        "foreign key onto another relation",
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN accounts ON orders.user_id = accounts.user_id",
    ),
]


@pytest.mark.parametrize(("label", "sql"), UNKNOWN)
def test_an_unproved_exactly_one_join_is_unknown(label: str, sql: str):
    assert outcomes(sql) == [Outcome.UNKNOWN], label


@pytest.mark.parametrize("body", ["SELECT id FROM users LIMIT 0", "SELECT id FROM users LIMIT 1", "SELECT id FROM users UNION SELECT id FROM users LIMIT 1"])
def test_a_limited_right_input_does_not_guarantee_a_match(body: str):
    assert outcomes(f"SELECT * FROM orders o /**ASSERT EXACTLY ONE**/ JOIN ({body}) u ON o.user_id = u.id") == [Outcome.UNKNOWN]


def test_a_nullable_left_column_is_reported_as_the_only_missing_evidence():
    explained = explanation(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.buyer = users.id"
    )

    assert "PROVED: At most one match" in explained
    assert "PROVED: At least one match" in explained
    assert "UNKNOWN: Non-null left column" in explained


def test_a_missing_foreign_key_is_reported_as_missing_guaranteed_matching():
    explained = explanation(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.plain = users.id"
    )

    assert "PROVED: At most one match" in explained
    assert "UNKNOWN: At least one match" in explained
    assert "PROVED: Non-null left column" in explained


def test_every_missing_half_is_reported_separately():
    explained = explanation(
        "SELECT * FROM events /**ASSERT EXACTLY ONE**/ JOIN sessions ON events.user_id = sessions.user_id"
    )

    assert "UNKNOWN: At most one match" in explained
    assert "UNKNOWN: At least one match" in explained
    assert "UNKNOWN: Non-null left column" in explained


def test_an_additional_predicate_is_named_in_the_explanation():
    explained = explanation(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id AND users.name = 'x'"
    )

    assert "column-to-column equalities" in explained


def test_referential_coverage_onto_a_non_unique_column_still_needs_a_unique_right_column():
    # Coverage alone guarantees a match, not that the match is unique.
    sql = """
        CREATE TABLE visits (user_id INTEGER NOT NULL REFERENCES sessions(user_id));
        SELECT * FROM visits /**ASSERT EXACTLY ONE**/ JOIN sessions ON visits.user_id = sessions.user_id
    """
    explained = explanation(sql)

    assert "UNKNOWN: At most one match" in explained
    assert "PROVED: At least one match" in explained
    assert "PROVED: Non-null left column" in explained


def test_the_assertion_locates_its_join():
    report = report_of(
        "SELECT * FROM orders\n/**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id"
    )

    origin = report.assertions[0].origin
    assert origin.line == SCHEMA.count("\n") + 2
    assert "JOIN users" in origin.detail


def test_exactly_one_and_unique_markers_mark_separate_joins():
    assert outcomes(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id "
        "/**ASSERT UNIQUE**/ JOIN accounts ON orders.plain = accounts.user_id"
    ) == [Outcome.PROVED, Outcome.PROVED]


def test_an_exactly_one_marker_in_an_unreferenced_cte_is_a_diagnostic():
    report = report_of(
        "WITH enriched AS (SELECT orders.id FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id) "
        "SELECT * FROM orders"
    )

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNANALYZED_ASSERTION]
    assert "exactly one join assertion" in report.diagnostics[0].message


def test_an_exactly_one_marker_not_before_a_join_is_unattached():
    report = report_of("SELECT * FROM orders /**ASSERT EXACTLY ONE**/ WHERE id = 1")

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNATTACHED_MARKER]
    assert "exactly one join marker" in report.diagnostics[0].message


def test_a_mistyped_exactly_one_marker_is_reported_with_the_marker_list():
    report = report_of("SELECT * FROM orders /**ASSERT EXACTLY 1**/ JOIN users ON orders.user_id = users.id")

    assert [diagnostic.code for diagnostic in report.diagnostics] == [UNRECOGNIZED_MARKER]
    assert "/**ASSERT EXACTLY ONE**/" in report.diagnostics[0].message


def test_a_proved_exactly_one_left_join_pads_no_right_column():
    # Every left row has its match, so the LEFT JOIN never null-extends.
    proved = """
        SELECT o.id, u.id AS matched_id FROM orders AS o
        /**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.user_id
        /**ASSERT NOT NULL(matched_id)**/
    """
    # A nullable buyer may match nothing, so the right side may be padded.
    padded = """
        SELECT o.id, u.id AS matched_id FROM orders AS o
        /**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.buyer
        /**ASSERT NOT NULL(matched_id)**/
    """

    assert outcomes(proved) == [Outcome.PROVED, Outcome.PROVED]
    assert outcomes(padded) == [Outcome.UNKNOWN, Outcome.UNKNOWN]


def test_a_proved_exactly_one_join_keeps_the_left_inputs_key():
    assert outcomes("""
        SELECT o.id, u.name FROM orders AS o
        /**ASSERT EXACTLY ONE**/ JOIN users AS u ON u.id = o.user_id
        /**ASSERT NOT NULL UNIQUE(id)**/
    """) == [Outcome.PROVED, Outcome.PROVED]


COMPOSITE = """
CREATE TABLE regions (country VARCHAR, code VARCHAR, label VARCHAR, PRIMARY KEY (country, code));
CREATE TABLE shops (
    id INTEGER PRIMARY KEY,
    country VARCHAR NOT NULL,
    code VARCHAR NOT NULL,
    other VARCHAR NOT NULL,
    maybe VARCHAR,
    FOREIGN KEY (country, code) REFERENCES regions(country, code),
    FOREIGN KEY (country, maybe) REFERENCES regions(country, code)
);
CREATE TABLE loose (country VARCHAR NOT NULL, code VARCHAR NOT NULL);
"""

COMPOSITE_PROVED = [
    (
        "composite key",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country AND s.code = r.code",
    ),
    (
        "composite pairs in another order",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ LEFT JOIN regions AS r ON r.code = s.code AND r.country = s.country",
    ),
    (
        "renamed pass-through columns",
        "WITH s AS (SELECT id, country AS c, code AS k FROM shops), "
        "r AS (SELECT country AS rc, code AS rk, label FROM regions) "
        "SELECT * FROM s /**ASSERT EXACTLY ONE**/ JOIN r ON s.c = r.rc AND s.k = r.rk",
    ),
    (
        "filtered left input",
        "SELECT * FROM (SELECT * FROM shops WHERE id > 3) AS s "
        "/**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country AND s.code = r.code",
    ),
    (
        "nullable foreign key column made non-null by the left input",
        "SELECT * FROM (SELECT * FROM shops WHERE maybe IS NOT NULL) AS s "
        "/**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country AND s.maybe = r.code",
    ),
]


@pytest.mark.parametrize(("label", "sql"), COMPOSITE_PROVED)
def test_a_composite_or_derived_exactly_one_join_is_proved(label: str, sql: str):
    assert outcomes(COMPOSITE + sql) == [Outcome.PROVED], label


DERIVED_PROVED = [
    (
        "cte on the left",
        "WITH o AS (SELECT id, user_id FROM orders) SELECT * FROM o /**ASSERT EXACTLY ONE**/ JOIN users ON o.user_id = users.id",
    ),
    (
        "renamed column on the left",
        "SELECT * FROM (SELECT user_id AS uid FROM orders) AS o /**ASSERT EXACTLY ONE**/ JOIN users AS u ON o.uid = u.id",
    ),
    (
        "projection of the right relation",
        "WITH u AS (SELECT id AS key, name FROM users) SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN u ON orders.user_id = u.key",
    ),
    (
        "view on the right",
        "CREATE VIEW people AS SELECT id, name FROM users; "
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN people ON orders.user_id = people.id",
    ),
    (
        "join inside the left input",
        "WITH o AS (SELECT orders.id, orders.user_id FROM orders JOIN accounts ON orders.plain = accounts.user_id) "
        "SELECT * FROM o /**ASSERT EXACTLY ONE**/ JOIN users ON o.user_id = users.id",
    ),
    (
        "left column equal to the referencing column",
        "SELECT * FROM members JOIN orders ON orders.plain = members.user_id "
        "/**ASSERT EXACTLY ONE**/ JOIN accounts ON accounts.user_id = orders.plain",
    ),
]


@pytest.mark.parametrize(("label", "sql"), DERIVED_PROVED)
def test_an_exactly_one_join_over_derived_relations_is_proved(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED], label


COMPOSITE_UNKNOWN = [
    (
        "only one column of the composite key",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country",
    ),
    (
        "coverage of each column is not coverage of the pair",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country AND s.other = r.code",
    ),
    (
        "pairs swapped across the key",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.code AND s.code = r.country",
    ),
    (
        "nullable referencing component",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country AND s.maybe = r.code",
    ),
    (
        "no foreign key",
        "SELECT * FROM loose AS l /**ASSERT EXACTLY ONE**/ JOIN regions AS r ON l.country = r.country AND l.code = r.code",
    ),
    (
        "right side filter in ON",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r "
        "ON s.country = r.country AND s.code = r.code AND r.label = 'x'",
    ),
    (
        "left side filter in ON",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r "
        "ON s.country = r.country AND s.code = r.code AND s.other = s.country",
    ),
    (
        "narrowed right relation",
        "WITH r AS (SELECT * FROM regions WHERE label = 'x') "
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN r ON s.country = r.country AND s.code = r.code",
    ),
    (
        "right relation narrowed by a join",
        "WITH r AS (SELECT regions.* FROM regions JOIN loose ON regions.code = loose.code) "
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN r ON s.country = r.country AND s.code = r.code",
    ),
    (
        "right relation duplicated by a join",
        "WITH r AS (SELECT regions.* FROM regions LEFT JOIN loose ON regions.code = loose.code) "
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN r ON s.country = r.country AND s.code = r.code",
    ),
    (
        "computed left column",
        "SELECT * FROM (SELECT upper(country) AS country, code FROM shops) AS s "
        "/**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country AND s.code = r.code",
    ),
    (
        "semi join",
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ SEMI JOIN regions AS r ON s.country = r.country AND s.code = r.code",
    ),
]


@pytest.mark.parametrize(("label", "sql"), COMPOSITE_UNKNOWN)
def test_an_unproved_composite_or_derived_exactly_one_join_is_unknown(label: str, sql: str):
    assert outcomes(COMPOSITE + sql) == [Outcome.UNKNOWN], label


def test_every_missing_half_of_a_composite_join_is_named():
    explained = explanation(
        COMPOSITE
        + "SELECT * FROM loose AS l /**ASSERT EXACTLY ONE**/ JOIN shops AS s ON l.country = s.country AND l.code = s.maybe"
    )

    assert "UNKNOWN: At most one match" in explained
    assert "UNKNOWN: At least one match" in explained
    assert "No known Foreign Key from loose(country, code)" in explained
    assert "PROVED: Non-null left columns" in explained


def test_each_nullable_left_column_is_named():
    explained = explanation(
        COMPOSITE
        + "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN regions AS r ON s.country = r.country AND s.maybe = r.code"
    )

    assert "PROVED: At most one match" in explained
    assert "PROVED: At least one match" in explained
    assert "UNKNOWN: Non-null left columns" in explained
    assert "maybe may be NULL" in explained


def test_a_narrowed_right_relation_is_named_in_the_explanation():
    explained = explanation(
        COMPOSITE
        + "WITH r AS (SELECT * FROM regions WHERE label = 'x') "
        "SELECT * FROM shops AS s /**ASSERT EXACTLY ONE**/ JOIN r ON s.country = r.country AND s.code = r.code"
    )

    assert "PROVED: At most one match" in explained
    assert "UNKNOWN: At least one match" in explained
    assert "may drop rows of its input" in explained


def test_an_extra_on_conjunct_leaves_only_matching_unknown():
    # A further conjunct only removes matches: at most one still holds.
    explained = explanation(
        "SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id AND users.name = 'x'"
    )

    assert "PROVED: At most one match" in explained
    assert "UNKNOWN: At least one match" in explained
    assert "PROVED: Non-null left columns" in explained


def test_a_proved_composite_exactly_one_left_join_pads_no_right_column():
    assert outcomes(COMPOSITE + """
        SELECT s.id, r.code AS matched FROM shops AS s
        /**ASSERT EXACTLY ONE**/ LEFT JOIN regions AS r ON r.country = s.country AND r.code = s.code
        /**ASSERT NOT NULL(matched)**/
    """) == [Outcome.PROVED, Outcome.PROVED]


PROPAGATED = [
    (
        "unique set through a left join",
        "SELECT o.id, u.name FROM orders AS o /**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.user_id "
        "/**ASSERT NOT NULL UNIQUE(id)**/",
    ),
    (
        "non-null left column",
        "SELECT o.user_id, u.name FROM orders AS o /**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.user_id "
        "/**ASSERT NOT NULL(user_id)**/",
    ),
    (
        "enumerated domain of a left column",
        "SELECT o.kind, u.name FROM (SELECT user_id, 'a' AS kind FROM orders) AS o "
        "/**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.user_id /**ASSERT ENUM(kind IN ('a'))**/",
    ),
    (
        "foreign key of a left column",
        "SELECT o.user_id, u.name FROM orders AS o /**ASSERT EXACTLY ONE**/ LEFT JOIN users AS u ON u.id = o.user_id "
        "/**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/",
    ),
]


@pytest.mark.parametrize(("label", "sql"), PROPAGATED)
def test_a_proved_exactly_one_join_keeps_the_left_inputs_properties(label: str, sql: str):
    assert outcomes(sql) == [Outcome.PROVED, Outcome.PROVED], label


def test_coverage_onto_an_exactly_one_join_carries_over_from_its_left_input():
    # The join keeps every orders row, so a Foreign Key onto orders covers it.
    sql = """
        CREATE TABLE refunds (order_id INTEGER NOT NULL REFERENCES orders(id));
        WITH enriched AS (
            SELECT o.id, u.name FROM orders AS o /**ASSERT EXACTLY ONE**/ JOIN users AS u ON u.id = o.user_id
        )
        SELECT * FROM refunds /**ASSERT EXACTLY ONE**/ JOIN enriched ON refunds.order_id = enriched.id
    """
    assert outcomes(sql) == [Outcome.PROVED, Outcome.PROVED]


def test_coverage_onto_an_unproved_inner_join_does_not_carry_over():
    sql = """
        CREATE TABLE refunds (order_id INTEGER NOT NULL REFERENCES orders(id));
        WITH enriched AS (
            SELECT o.id, u.name FROM orders AS o /**ASSERT EXACTLY ONE**/ JOIN users AS u ON u.id = o.buyer
        )
        SELECT * FROM refunds /**ASSERT EXACTLY ONE**/ JOIN enriched ON refunds.order_id = enriched.id
    """
    assert outcomes(sql) == [Outcome.UNKNOWN, Outcome.UNKNOWN]
