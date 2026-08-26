from __future__ import annotations

from sqlassert import (
    ColumnKnowledge,
    Knowledge,
    Outcome,
    RelationKnowledge,
    UniqueSetKnowledge,
    analyze,
)


PROVABLE_PROGRAM = """
    CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
    CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

    SELECT *
    FROM sessions
    /**UNIQUE**/ JOIN users
        ON sessions.user_id = users.id
"""

UNPROVABLE_PROGRAM = """
    CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
    CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

    SELECT *
    FROM users
    /**UNIQUE**/ JOIN sessions
        ON users.id = sessions.user_id
"""


def test_declared_unique_set_covered_by_the_join_predicate_is_proved():
    report = analyze(PROVABLE_PROGRAM)

    assert report.proved
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
    assert report.assertions[0].proving_unique_set == ("id",)
    assert report.assertions[0].explanation == "Proved: the join covers the right side's unique set (id)."


def test_join_without_a_matching_unique_set_is_unknown_rather_than_disproved():
    report = analyze(UNPROVABLE_PROGRAM)

    assert not report.proved
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
    assert report.assertions[0].proving_unique_set == ()
    assert report.assertions[0].explanation == "Unknown: no unique set is known for the right side of this join."
    assert not report.diagnostics


def test_assertion_provenance_locates_the_marked_join_in_the_source():
    report = analyze(PROVABLE_PROGRAM)

    origin = report.assertions[0].origin
    assert "users" in origin.detail
    assert origin.line == 7


def test_program_without_markers_declares_no_assertions():
    report = analyze("SELECT * FROM sessions JOIN users ON sessions.user_id = users.id")

    assert report.assertions == ()
    assert report.proved


def test_more_than_one_root_select_is_an_explicit_program_diagnostic():
    report = analyze("SELECT 1; SELECT 2")

    assert not report.proved
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["multiple-root-selects"]
    assert report.assertions == ()


def test_analysis_requires_exactly_one_stable_model():
    assert analyze(PROVABLE_PROGRAM).stable_model_count == 1
    assert analyze(UNPROVABLE_PROGRAM).stable_model_count == 1


def test_supplied_knowledge_proves_a_relation_the_program_does_not_declare():
    report = analyze(
        """
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """,
        knowledge=Knowledge(
            (
                RelationKnowledge(
                    name="users",
                    columns=(ColumnKnowledge("id", nullable=False),),
                    unique_sets=(UniqueSetKnowledge(("id",)),),
                ),
            )
        ),
    )

    assert report.proved
    assert report.assertions[0].proving_unique_set == ("id",)


def test_omitted_knowledge_behaves_as_empty_knowledge():
    report = analyze(
        """
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);

        SELECT *
        FROM sessions
        /**UNIQUE**/ JOIN users
            ON sessions.user_id = users.id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]
