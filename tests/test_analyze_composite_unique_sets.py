"""Composite and nullable Unique Sets, constants, USING, and LEFT joins.

Covers the acceptance criteria of proving Unique Join Assertions from
composite and nullable Unique Sets supplied by Create Table declarations,
per docs/mvp-scope.md.
"""

from __future__ import annotations

from sqlassert import Outcome, analyze


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


def test_covering_only_part_of_a_composite_unique_set_is_unknown():
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


def test_a_candidate_key_also_proves_ordinary_equality_uniqueness():
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


def test_composite_keys_support_unique_joins_with_or_without_nullable_members():
    fully_non_null = analyze(
        """
        CREATE TABLE users (id INTEGER NOT NULL, region INTEGER NOT NULL, PRIMARY KEY (id, region));
        CREATE TABLE sessions (user_id INTEGER, user_region INTEGER);

        SELECT * FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id AND sessions.user_region = users.region
        """
    )

    assert fully_non_null.assertions[0].outcome is Outcome.PROVED

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
