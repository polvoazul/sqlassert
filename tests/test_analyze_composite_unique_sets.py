"""Composite and nullable Unique Sets, constants, USING, and LEFT joins.

Covers the acceptance criteria of proving Unique Join Assertions from
composite and nullable Unique Sets supplied by Create Table declarations or
explicit Knowledge, per docs/mvp-scope.md.
"""

from __future__ import annotations

from sqlassert import (
    ColumnKnowledge,
    Knowledge,
    Outcome,
    RelationKnowledge,
    UniqueSetKnowledge,
    analyze,
)


def test_fully_covering_a_composite_unique_set_in_an_inner_join_is_proved():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, region INTEGER, name VARCHAR, PRIMARY KEY (id, region));
        CREATE TABLE sessions (user_id INTEGER, user_region INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND sessions.user_region = users.region
        """
    )

    assert report.proved
    assert report.assertions[0].outcome is Outcome.PROVED
    assert report.assertions[0].proving_unique_set == ("id", "region")


def test_covering_only_part_of_a_composite_unique_set_is_unknown_with_the_missing_member_as_evidence():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, region INTEGER, name VARCHAR, PRIMARY KEY (id, region));
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert not report.proved
    assertion = report.assertions[0]
    assert assertion.outcome is Outcome.UNKNOWN
    assert assertion.proving_unique_set == ()
    assert assertion.missing_columns == ("region",)


def test_a_right_side_key_member_equated_to_a_constant_completes_a_composite_unique_set():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, region INTEGER, name VARCHAR, PRIMARY KEY (id, region));
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND users.region = 1
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id", "region")


def test_a_range_predicate_does_not_cover_a_key_member_and_stays_unknown():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, region INTEGER, name VARCHAR, PRIMARY KEY (id, region));
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND users.region > 1
        """
    )

    assert not report.proved
    assertion = report.assertions[0]
    assert assertion.outcome is Outcome.UNKNOWN
    assert assertion.missing_columns == ("region",)


def test_a_nullable_unique_set_proves_ordinary_equality_uniqueness():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, email VARCHAR, UNIQUE (email));
        CREATE TABLE sessions (user_email VARCHAR, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_email = users.email
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("email",)
    assert not report.assertions[0].is_candidate_key


def test_a_candidate_key_also_proves_ordinary_equality_uniqueness_and_is_identified_as_one():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)
    assert report.assertions[0].is_candidate_key


def test_a_composite_candidate_key_is_identified_as_one_only_when_every_member_is_non_null():
    fully_non_null = analyze(
        """
        CREATE TABLE users (id INTEGER NOT NULL, region INTEGER NOT NULL, PRIMARY KEY (id, region));
        CREATE TABLE sessions (user_id INTEGER, user_region INTEGER);

        SELECT * FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND sessions.user_region = users.region
        """
    )
    assert fully_non_null.assertions[0].is_candidate_key

    partly_nullable = analyze(
        """
        CREATE TABLE users (id INTEGER NOT NULL, region INTEGER, UNIQUE (id, region));
        CREATE TABLE sessions (user_id INTEGER, user_region INTEGER);

        SELECT * FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND sessions.user_region = users.region
        """
    )
    assert partly_nullable.assertions[0].outcome is Outcome.PROVED
    assert not partly_nullable.assertions[0].is_candidate_key


def test_explicit_knowledge_can_supply_a_composite_nullable_unique_set_without_sql():
    report = analyze(
        """
        CREATE TABLE sessions (user_id INTEGER, user_region INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND sessions.user_region = users.region
        """,
        knowledge=Knowledge(
            (
                RelationKnowledge(
                    name="users",
                    columns=(
                        ColumnKnowledge("id", nullable=True),
                        ColumnKnowledge("region", nullable=True),
                    ),
                    unique_sets=(UniqueSetKnowledge(("id", "region")),),
                ),
            )
        ),
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id", "region")
    assert not report.assertions[0].is_candidate_key


def test_using_participates_in_the_same_key_coverage_reasoning_as_on():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE sessions (id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            USING (id)
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_left_joins_participate_in_the_same_key_coverage_reasoning_as_inner():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ LEFT JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_a_left_join_without_a_matching_unique_set_is_unknown():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, name VARCHAR);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ LEFT JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert not report.proved
    assert report.assertions[0].outcome is Outcome.UNKNOWN


def test_null_safe_equality_does_not_cover_a_key_member_and_stays_unknown():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id IS NOT DISTINCT FROM users.id
        """
    )

    assert not report.proved
    assert report.assertions[0].outcome is Outcome.UNKNOWN


def test_an_or_predicate_does_not_cover_a_key_member_and_stays_unknown():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY, other_id INTEGER, name VARCHAR);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id OR sessions.user_id = users.other_id
        """
    )

    assert not report.proved
    assert report.assertions[0].outcome is Outcome.UNKNOWN


def test_using_against_an_ambiguous_left_side_stays_unknown_rather_than_guessing():
    report = analyze(
        """
        CREATE TABLE a (id INTEGER);
        CREATE TABLE b (id INTEGER);
        CREATE TABLE c (id INTEGER PRIMARY KEY);

        SELECT * FROM a JOIN b USING (id) /**UNIQUE**/ JOIN c USING (id)
        """
    )

    assert not report.proved
    assert report.assertions[0].outcome is Outcome.UNKNOWN


def test_a_table_level_unique_constraint_does_not_imply_non_null():
    report = analyze(
        """
        CREATE TABLE users (id INTEGER, region INTEGER, UNIQUE (id, region));
        CREATE TABLE sessions (user_id INTEGER, user_region INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND sessions.user_region = users.region
        """
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id", "region")
