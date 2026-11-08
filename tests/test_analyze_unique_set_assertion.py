"""Unique Set Assertions: `/**ASSERT UNIQUE(...)**/` and `/**ASSERT NOT NULL UNIQUE(...)**/`
written on a Select Expression -- a Root Select, a view, a CTE, or a
subquery -- asserting that the named output columns form a Unique Set or,
for `NOT NULL UNIQUE` (alias `PRIMARY KEY`), the stricter Non-Null Unique Set.

Proof is defined purely in terms of a Relation Expression and its Output Columns:
nothing here should behave differently depending on whether the Select
Expression is a Root Select, a view, or a CTE (see
`test_the_same_assertion_proves_identically_regardless_of_attachment_site`).
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze


NON_NULL_UNIQUE_SPELLINGS = pytest.mark.parametrize("spelling", ["NOT NULL UNIQUE", "PRIMARY KEY"])


@NON_NULL_UNIQUE_SPELLINGS
def test_a_non_null_unique_assertion_is_proved_under_either_spelling(spelling):
    """`PRIMARY KEY` is a convenience alias for the canonical `NOT NULL UNIQUE`."""
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY, email VARCHAR);

        SELECT id, email FROM users /**ASSERT {spelling}(id)**/
        """
    )

    assert report.proved


@NON_NULL_UNIQUE_SPELLINGS
def test_a_non_null_unique_assertion_on_a_nullable_column_is_unknown(spelling):
    """Unique alone is not enough: the column must also be independently
    known non-null."""
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER, email VARCHAR UNIQUE);

        SELECT email FROM users /**ASSERT {spelling}(email)**/
        """
    )

    assert not report.proved
    assert [a.outcome for a in report.assertions] == [Outcome.UNKNOWN]


def test_report_facts_includes_unique_sets_earned_through_a_proved_assertion():
    report = analyze(
        """
        CREATE TABLE raw (id INTEGER, region INTEGER);
        CREATE VIEW dedup AS SELECT id FROM raw GROUP BY id /**ASSERT UNIQUE(id)**/;

        SELECT * FROM dedup
        """
    )

    assert report.facts.is_unique("dedup", ["id"])
