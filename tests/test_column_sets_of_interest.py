"""Superset inference is property logic, restricted to sets under analysis."""

from sqlassert import Outcome, analyze


def test_a_superset_of_a_known_unique_set_is_proved_and_fed_forward():
    report = analyze(
        """
        CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE VIEW v AS SELECT id, name FROM t /**ASSERT UNIQUE(id, name)**/;
        SELECT id, name FROM v
        """
    )

    assert report.proved
    assert set(report.facts.unique_sets("v")) == {("id",), ("id", "name")}


def test_unrequested_supersets_are_not_generated():
    report = analyze(
        """
        CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE VIEW v AS SELECT id, name FROM t;
        SELECT id, name FROM v
        """
    )

    assert report.facts.unique_sets("v") == (("id",),)


def test_sets_of_interest_cannot_prove_themselves_or_each_other():
    report = analyze(
        """
        CREATE TABLE t (id INTEGER, name VARCHAR);
        SELECT id, name FROM t /**ASSERT UNIQUE(id)**/ /**ASSERT UNIQUE(id, name)**/
        """
    )

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN, Outcome.UNKNOWN]


def test_a_subset_of_a_known_composite_unique_set_is_not_proved_unique():
    report = analyze(
        """
        CREATE TABLE t (id INTEGER, name VARCHAR, region VARCHAR, UNIQUE (id, region));
        SELECT id, name FROM t /**ASSERT UNIQUE(id, name)**/
        """
    )

    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]
