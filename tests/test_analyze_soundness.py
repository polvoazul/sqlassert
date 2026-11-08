"""A proof is only ever as good as the relation it was proved about.

Every case here shares a bare relation name with a declared table without
sharing its Unique Sets. Each must be UNKNOWN: an unsound PROVED is the worst
defect this engine can have.
"""

from __future__ import annotations

import pytest

from sqlassert import analyze


MISPLACED_MARKERS = [
    ("before a comma", "SELECT * FROM a /**ASSERT MANY TO ONE**/ , b", "unattached-marker"),
    ("trailing with no join", "SELECT * FROM a /**ASSERT MANY TO ONE**/", "unattached-marker"),
    ("in the select list", "SELECT /**ASSERT MANY TO ONE**/ 1", "unattached-marker"),
    ("before a create statement", "/**ASSERT MANY TO ONE**/ CREATE TABLE t (id INTEGER);", "sql-parse-failed"),
    ("before a select", "/**ASSERT MANY TO ONE**/ SELECT * FROM t", "sql-parse-failed"),
    ("after the join keyword", "SELECT * FROM a JOIN /**ASSERT MANY TO ONE**/ b ON a.x = b.x", "sql-parse-failed"),
    (
        "with the join in a later statement",
        "/**ASSERT MANY TO ONE**/ CREATE TABLE users (id INTEGER PRIMARY KEY);\n"
        "SELECT * FROM sessions JOIN users ON sessions.user_id = users.id",
        "sql-parse-failed",
    ),
]


@pytest.mark.parametrize(("label", "sql", "code"), MISPLACED_MARKERS)
def test_a_marker_that_does_not_mark_a_join_is_reported(label: str, sql: str, code: str):
    """However a marker is misplaced, it is named and nothing is proved.

    The grammar requires a marker to sit immediately before its join. Some
    misplacements are caught by the join rule and some break parsing outright,
    so the code differs — but an assertion is never silently dropped, and the
    diagnostic always points at the marker's own line.
    """
    report = analyze(sql)

    assert report.assertions == (), label
    assert not report.proved, label
    assert [diagnostic.code for diagnostic in report.diagnostics] == [code], label
    lines = []
    for diagnostic in report.diagnostics:
        assert diagnostic.origin is not None, label
        lines.append(diagnostic.origin.line)
    assert lines == [1], label
    assert "marker" in report.diagnostics[0].message, label


@pytest.mark.parametrize(
    ("bare", "explicit"),
    [
        ("/**UNIQUE**/", "/**ASSERT UNIQUE**/"),
        ("/**UNIQUE(id)**/", "/**ASSERT UNIQUE(id)**/"),
        ("/**PRIMARY KEY(id)**/", "/**ASSERT PRIMARY KEY(id)**/"),
    ],
)
def test_a_bare_marker_is_rejected_and_points_at_its_explicit_spelling(bare: str, explicit: str):
    """Assertions need an explicit ASSERT. A bare marker was once valid, so its
    author believes in it: report it, and say how to write it now."""
    report = analyze(
        f"""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);

        SELECT * FROM sessions {bare} JOIN users ON sessions.user_id = users.id
        """
    )

    assert report.assertions == ()
    assert not report.proved
    assert [diagnostic.code for diagnostic in report.diagnostics] == ["unrecognized-marker"]
    assert repr(explicit) in report.diagnostics[0].message
