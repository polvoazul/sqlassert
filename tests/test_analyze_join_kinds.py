"""A Unique Join Assertion through every join kind.

A left row appears once per matching right row, or once padded, whatever the
kind: RIGHT and FULL joins pad right rows on but never repeat a left row more
than its matches do, so the right side's Unique Set proves them as it proves
INNER and LEFT joins. A CROSS join is an ON with no conjuncts: unique only when
the right input has at most one row. SEMI and ANTI joins keep or drop each
left row whole and output no right row: no cardinality marker applies to them.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze


PADDED_CONSTANT = """
CREATE TABLE t (id INTEGER PRIMARY KEY);
CREATE TABLE u (tid INTEGER);
SELECT t.id, v.k FROM t {join} (SELECT tid, 'a' AS k FROM u) AS v ON v.tid = t.id /**ASSERT ENUM(k IN ('a'))**/
"""


def test_an_outer_join_pads_with_null_which_is_outside_every_domain():
    assert [assertion.outcome for assertion in analyze(PADDED_CONSTANT.format(join="LEFT JOIN")).assertions] == [Outcome.PROVED]


@pytest.mark.parametrize("join", ["LEFT JOIN", "RIGHT JOIN", "FULL JOIN", "ANY LEFT JOIN", "SEMI LEFT JOIN", "ANTI LEFT JOIN"])
def test_a_clickhouse_join_other_than_inner_or_cross_is_opaque(join: str):
    # ClickHouse pads with the column type's default ('' here), not NULL,
    # unless join_use_nulls is set; its SEMI and ANTI joins keep right columns.
    report = analyze(PADDED_CONSTANT.format(join=join), dialect="clickhouse")
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN], join


def test_a_clickhouse_inner_join_is_an_inner_join():
    report = analyze(PADDED_CONSTANT.format(join="JOIN"), dialect="clickhouse")
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]
