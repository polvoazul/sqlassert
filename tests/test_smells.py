"""Low-level suggestions: Smells (issue 15, from the Kossmann Table 1 triage)."""

from __future__ import annotations

import pytest

from sqlassert import suggest
from sqlassert.__main__ import main
from sqlassert.smells import Concern

SCHEMA = """
CREATE TABLE t (id INTEGER PRIMARY KEY, name VARCHAR, score INTEGER);
CREATE TABLE u (id INTEGER PRIMARY KEY, t_id INTEGER);
"""


@pytest.mark.parametrize(
    ("sql", "loose"),
    [
        ("SELECT id, name FROM t ORDER BY score LIMIT 3", "(id, name)"),
        ("SELECT id, name FROM t LIMIT 3", None),
        ("SELECT score, count(*) AS n FROM t GROUP BY score ORDER BY n DESC LIMIT 1", "(score)"),
        ("SELECT name FROM t ORDER BY score OFFSET 2", "(name)"),
        ("WITH top AS (SELECT id FROM t ORDER BY score LIMIT 3) SELECT id FROM top", "(id)"),
    ],
)
def test_a_limit_whose_order_leaves_different_rows_tied_is_a_smell(sql: str, loose: str | None):
    smells = suggest(SCHEMA + sql).smells

    assert [smell.concern for smell in smells] == [Concern.ARBITRARY_LIMIT]
    assert (loose or "with no ORDER BY") in smells[0].message


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT id, name FROM t ORDER BY id LIMIT 3",
        "SELECT id, name FROM t ORDER BY score, id LIMIT 3",
        "SELECT id, name FROM t ORDER BY 1 LIMIT 3",
        "SELECT name FROM t ORDER BY t.id LIMIT 3",
        # Rows tied on the keys are identical: either may come first.
        "SELECT name FROM t ORDER BY name LIMIT 3",
        # At most one row.
        "SELECT id, name FROM t WHERE id = 1 LIMIT 1",
        "SELECT count(*) AS n FROM t LIMIT 1",
        # No LIMIT: the order is the reader's business.
        "SELECT id, name FROM t ORDER BY score",
        # A key sqlassert cannot map to one column keeps it silent.
        "SELECT t.name FROM t JOIN u ON u.t_id = t.id ORDER BY id LIMIT 2",
        "SELECT id, name FROM t ORDER BY lower(name) LIMIT 3",
    ],
)
def test_a_determined_or_unreadable_limit_is_not_a_smell(sql: str):
    assert suggest(SCHEMA + sql).smells == ()


def test_the_cli_lists_smells_last_at_the_low_level(tmp_path, capsys):
    path = tmp_path / "query.sql"
    path.write_text(SCHEMA + "SELECT id, name FROM t ORDER BY score LIMIT 3")

    assert main(["suggest", str(path)]) == 0

    last = capsys.readouterr().out.splitlines()[-1]
    assert last.split()[:2] == ["low", "line"]
    assert "Arbitrary LIMIT" in last
