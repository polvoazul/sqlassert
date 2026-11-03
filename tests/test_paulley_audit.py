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
    # Restriction (§3.2.4).
    "R1": ("SELECT id, a FROM t WHERE b = 1 /**ASSERT UNIQUE(id)**/", PROVED),
    "R2": ("WITH s AS (SELECT * FROM t /**DECLARE DETERMINES(a -> b)**/ /**DECLARE DETERMINES(b -> c)**/) SELECT a, b, c FROM s WHERE b IS NOT NULL /**ASSERT DETERMINES(a -> c)**/", PROVED),
    "R5": ("SELECT a, b FROM t WHERE a = b /**ASSERT DETERMINES(a -> b)**/", PROVED),
    "R7": ("SELECT a FROM t WHERE a IS NOT NULL /**ASSERT NOT NULL(a)**/", PROVED),
    "R8": ("SELECT a, b FROM t WHERE a IS NULL /**ASSERT DETERMINES(b -> a)**/", PROVED),
    "R11": ("SELECT a, b, c FROM t WHERE a = b AND c + 1 > 0 /**ASSERT NOT NULL(a, b, c)**/", PROVED),
    "R12": ("SELECT a FROM t WHERE EXISTS (SELECT 1 FROM v WHERE v.w = t.a) /**ASSERT NOT NULL(a)**/", PROVED),
    "R13-": ("SELECT a FROM t WHERE a = 1 OR a = 2 /**ASSERT ENUM(a IN (1, 2))**/", UNKNOWN),
    # Set operations (§3.2.5–3.2.7), asserted on a CTE: a trailing marker would mark the last input.
    "I1": ("WITH q AS (SELECT id, a FROM t INTERSECT ALL SELECT id, a FROM t WHERE a = 1) SELECT id, a FROM q /**ASSERT DETERMINES(id -> a)**/", PROVED),
    "I2": ("WITH q AS (SELECT id FROM t INTERSECT ALL SELECT a FROM t) SELECT id FROM q /**ASSERT UNIQUE(id)**/", PROVED),
    "I3": ("WITH q AS (SELECT x FROM u INTERSECT ALL SELECT d FROM t) SELECT x FROM q /**ASSERT NOT NULL UNIQUE(x)**/", PROVED),
    "U1-": ("WITH q AS (SELECT id, a FROM t UNION ALL SELECT id, b FROM t) SELECT id, a FROM q /**ASSERT DETERMINES(id -> a)**/", UNKNOWN),
    "U3": ("WITH q AS (SELECT a, b FROM t UNION SELECT c, d FROM t) SELECT a, b FROM q /**ASSERT UNIQUE(a, b)**/", PROVED),
    "D1": ("WITH q AS (SELECT id, a FROM t EXCEPT ALL SELECT id, a FROM t WHERE a = 1) SELECT id, a FROM q /**ASSERT UNIQUE(id)**/", PROVED),
    # Grouping and aggregation (§3.2.8).
    "G1": ("SELECT id, a, count(*) AS n FROM t GROUP BY id, a /**ASSERT UNIQUE(id)**/", PROVED),
    "G2": ("SELECT a, a + 1 AS k, count(*) AS n FROM t GROUP BY a, a + 1 /**ASSERT UNIQUE(a)**/", PROVED),
    "G3": ("SELECT a, b, count(*) AS n FROM t GROUP BY a, b /**ASSERT UNIQUE(a, b)**/", PROVED),
    "G3e": ("SELECT count(*) AS n, max(a) AS m FROM t /**ASSERT UNIQUE(m)**/", PROVED),
    "G4": ("SELECT a, count(*) AS n, sum(b) AS s FROM t GROUP BY a /**ASSERT DETERMINES(a -> n, s)**/", PROVED),
    # Left outer join (§3.2.9).
    "L1": ("SELECT t.id, t.a, u.z FROM t LEFT JOIN u ON u.z = t.a /**ASSERT DETERMINES(id -> a)**/", PROVED),
    "L2": ("SELECT t.a, u.k, u.y FROM t LEFT JOIN u ON u.k = t.a /**ASSERT DETERMINES(a -> y)**/", PROVED),
    "L3": ("SELECT t.b, r.w, r.q FROM t LEFT JOIN (SELECT * FROM v WHERE q = w) AS r ON r.w = t.a /**ASSERT DETERMINES(w -> q)**/", PROVED),
    "L4": ("SELECT t.a, u.x, u.k FROM t LEFT JOIN u ON u.x = t.a /**ASSERT DETERMINES(x -> k)**/", PROVED),
    "L5": ("SELECT u.x, u.z FROM t LEFT JOIN u ON u.z = t.a AND u.x = u.z /**ASSERT DETERMINES(x -> z)**/", PROVED),
    "L6": ("SELECT t.a, u.x FROM t LEFT JOIN u ON u.z = t.a AND u.x = 5 /**ASSERT DETERMINES(a -> x)**/", PROVED),
    "L6-": ("SELECT t.b, u.x FROM t LEFT JOIN u ON u.z = t.a AND u.x = 5 /**ASSERT DETERMINES(b -> x)**/", UNKNOWN),
    "L7": ("SELECT t.id, u.k FROM t LEFT JOIN u ON u.z = t.a /**ASSERT UNIQUE(id, k)**/", PROVED),
    "L7-": ("SELECT t.id, u.k FROM t LEFT JOIN u ON u.z = t.a /**ASSERT UNIQUE(id)**/", UNKNOWN),
    "L8": ("SELECT t.a, u.x, u.k FROM t LEFT JOIN u ON u.z = t.a /**ASSERT DETERMINES(x -> k)**/", PROVED),
    # A strict constant of the padded side (q = 1) is NULL on padded rows.
    "L9-": ("SELECT t.b, r.q FROM t LEFT JOIN (SELECT * FROM v WHERE q = 1) AS r ON r.w = t.a /**ASSERT DETERMINES(b -> q)**/", UNKNOWN),
    "L10-": ("SELECT t.a, u.z FROM t LEFT JOIN u ON u.z = t.a /**ASSERT NOT NULL(z)**/", UNKNOWN),
    "L11-": ("SELECT t.b FROM t LEFT JOIN u ON u.z = t.a AND t.b = 1 /**ASSERT ENUM(b IN (1))**/", UNKNOWN),
    "L15": ("SELECT u.y FROM t LEFT JOIN u ON u.z = t.a WHERE u.k IS NOT NULL /**ASSERT NOT NULL(y)**/", PROVED),
    "L15-": ("SELECT u.y FROM t LEFT JOIN u ON u.z = t.a /**ASSERT NOT NULL(y)**/", UNKNOWN),
    # Full outer join (§3.2.10).
    "F1": ("SELECT t.id, t.a, u.k FROM t FULL JOIN u ON u.k = t.a /**ASSERT DETERMINES(id -> a)**/", PROVED),
    "F1-": ("SELECT t.b, r.q FROM t FULL JOIN (SELECT * FROM v WHERE q = 1) AS r ON r.w = t.a /**ASSERT DETERMINES(b -> q)**/", UNKNOWN),
    "F1-left": ("SELECT s.b, u.k FROM (SELECT * FROM t WHERE b = 1) AS s FULL JOIN u ON u.k = s.a /**ASSERT DETERMINES(k -> b)**/", UNKNOWN),
    "F3": ("SELECT t.id, u.k FROM t FULL JOIN u ON u.k = t.a /**ASSERT UNIQUE(id, k)**/", PROVED),
    "F3-": ("SELECT t.id, u.k FROM t FULL JOIN u ON u.k = t.a /**ASSERT UNIQUE(id)**/", UNKNOWN),
    "F5-": ("SELECT t.b, u.k FROM t FULL JOIN u ON u.k = t.a AND t.b = 1 /**ASSERT ENUM(b IN (1))**/", UNKNOWN),
    "F6": ("SELECT t.b, r.w, r.q FROM t FULL JOIN (SELECT * FROM v WHERE q = w) AS r ON r.w = t.a /**ASSERT DETERMINES(w -> q)**/", PROVED),
    "F8": ("SELECT t.d FROM t FULL JOIN u ON u.k = t.a WHERE t.id IS NOT NULL /**ASSERT NOT NULL(d)**/", PROVED),
    "F8-": ("SELECT t.d FROM t FULL JOIN u ON u.k = t.a /**ASSERT NOT NULL(d)**/", UNKNOWN),
}


@pytest.mark.parametrize("rule", sorted(CASES))
def test_paulley_rule(rule: str):
    sql, outcome = CASES[rule]
    report = analyze(SCHEMA + sql)

    assert not report.diagnostics, report.diagnostics
    assert [assertion.outcome for assertion in report.assertions] == [outcome], rule
