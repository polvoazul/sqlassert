"""Issue 35: one case per rule of the Paulley audit table.

`docs/functional-dependencies.md`, section "Paulley audit", lists every rule
of Paulley 2000 §2.5.2 and §3.1–3.2 with its status. Each implemented row
names a case here by its ID; a rule sqlassert must not apply has a case that
stays UNKNOWN (suffix `-`). The soundness oracle judges every PROVED case.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze

SCHEMA = """
CREATE TABLE t (id INTEGER PRIMARY KEY, a INTEGER, b INTEGER, c INTEGER, d INTEGER NOT NULL, e INTEGER NOT NULL);
CREATE TABLE u (k INTEGER PRIMARY KEY, x INTEGER, y INTEGER NOT NULL, z INTEGER, UNIQUE (x));
CREATE TABLE v (w INTEGER, q INTEGER);
"""

PROVED, UNKNOWN = Outcome.PROVED, Outcome.UNKNOWN

CASES = {
    # Axioms (§2.5.2). Determinants of DETERMINES are taken as non-null.
    "A1": ("SELECT a, b FROM t /**ASSERT DETERMINES(a, b -> a)**/", PROVED),
    "A2": ("SELECT id, a, b FROM t /**ASSERT DETERMINES(id, a -> b)**/", PROVED),
    "A3": ("SELECT x, z, k FROM u /**ASSERT DETERMINES(x, z -> k)**/", PROVED),
    "A4": ("SELECT id, a, b FROM t /**ASSERT DETERMINES(id -> a, b)**/", PROVED),
    "A5": ("SELECT x, k, z FROM u /**ASSERT DETERMINES(x -> k, z)**/", PROVED),
    "A6": ("WITH s AS (SELECT * FROM t /**DECLARE DETERMINES(d -> a, b)**/) SELECT d, a, b FROM s /**ASSERT DETERMINES(d -> b)**/", PROVED),
    # Our lax dependencies are lax in the determinant only, so decomposition needs no definite dependent.
    "A7": ("WITH s AS (SELECT * FROM t /**DECLARE DETERMINES(a -> b, c)**/) SELECT a, b, c FROM s /**ASSERT DETERMINES(a -> c)**/", PROVED),
    "A8": ("SELECT id, a FROM t /**ASSERT DETERMINES(id -> a)**/", PROVED),
    "A9": ("SELECT x, z FROM u WHERE x IS NOT NULL /**ASSERT NOT NULL UNIQUE(x)**/", PROVED),
    "A10": ("SELECT id, a, b FROM t WHERE a = id /**ASSERT DETERMINES(a -> b)**/", PROVED),
    "A11-": ("WITH s AS (SELECT * FROM t /**DECLARE DETERMINES(a -> b)**/ /**DECLARE DETERMINES(b -> c)**/) SELECT a, b, c FROM s /**ASSERT DETERMINES(a -> c)**/", UNKNOWN),
    "A11": ("WITH s AS (SELECT * FROM t /**DECLARE DETERMINES(a -> d)**/ /**DECLARE DETERMINES(d -> c)**/) SELECT a, d, c FROM s /**ASSERT DETERMINES(a -> c)**/", PROVED),
    # Equivalence constraints (§3.1.3).
    "E1": ("SELECT a, c FROM t WHERE a = b AND b = c /**ASSERT DETERMINES(a -> c)**/", PROVED),
    "E2": ("SELECT a, b FROM t WHERE a = b /**ASSERT DETERMINES(b -> a)**/", PROVED),
    "E5": ("SELECT t.a, u.z FROM t LEFT JOIN u ON u.z = t.a /**ASSERT DETERMINES(z -> a)**/", PROVED),
    "E6": ("SELECT t.b, u.z FROM t LEFT JOIN u ON u.z = t.a WHERE t.a = t.b /**ASSERT DETERMINES(z -> b)**/", PROVED),
    "E6-": ("SELECT t.a, v.w FROM t LEFT JOIN u ON u.z = t.a LEFT JOIN v ON v.w = u.z /**ASSERT DETERMINES(w -> a)**/", UNKNOWN),
    # Sources (§3.1–3.2.1).
    "S1": ("SELECT id, a FROM t /**ASSERT NOT NULL UNIQUE(id)**/", PROVED),
    # A unique index, like UNIQUE, repeats NULLs in SQL: a lax key, not Paulley's strict one.
    "S1-": ("CREATE TABLE w (m INTEGER); CREATE UNIQUE INDEX wm ON w (m); SELECT m FROM w /**ASSERT NOT NULL UNIQUE(m)**/", UNKNOWN),
    "S2": ("SELECT x, z FROM u /**ASSERT UNIQUE(x)**/", PROVED),
    "S2-": ("SELECT x, z FROM u /**ASSERT NOT NULL UNIQUE(x)**/", UNKNOWN),
    "S3": ("SELECT a, b FROM t WHERE a = 5 AND b = 6 /**ASSERT DETERMINES(a -> b)**/", PROVED),
    "S4": ("SELECT a, a + 1 AS k FROM t /**ASSERT DETERMINES(a -> k)**/", PROVED),
    # Projection (§3.2.2).
    "P1": ("SELECT a, b FROM t WHERE a = id /**ASSERT UNIQUE(a)**/", PROVED),
    "P2": ("SELECT id, a * 2 AS k FROM t /**ASSERT DETERMINES(id -> k)**/", PROVED),
    "P3": ("SELECT DISTINCT a, b FROM t /**ASSERT UNIQUE(a, b)**/", PROVED),
    # Cartesian product (§3.2.3).
    "C1": ("SELECT t.id, t.a, v.w FROM t CROSS JOIN v /**ASSERT DETERMINES(id -> a)**/", PROVED),
    "C2": ("SELECT t.id, u.k FROM t CROSS JOIN u /**ASSERT UNIQUE(id, k)**/", PROVED),
    "C2-": ("SELECT t.id, u.k FROM t CROSS JOIN u /**ASSERT UNIQUE(id)**/", UNKNOWN),
    "C3": ("SELECT t.id FROM t CROSS JOIN (SELECT count(*) AS n FROM v) AS c /**ASSERT UNIQUE(id)**/", PROVED),
}


@pytest.mark.parametrize("rule", sorted(CASES))
def test_paulley_rule(rule: str):
    sql, outcome = CASES[rule]
    report = analyze(SCHEMA + sql)

    assert not report.diagnostics, report.diagnostics
    assert [assertion.outcome for assertion in report.assertions] == [outcome], rule
