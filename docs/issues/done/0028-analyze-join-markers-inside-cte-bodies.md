# 28. Analyze join markers inside CTE bodies

- **State:** done
- **Labels:** ready-for-agent
- **Created:** 2026-11-01

## Problem

A join marker inside a CTE body is never analyzed, even when the CTE is used:

```sql
CREATE TABLE u (id INT PRIMARY KEY);
CREATE TABLE o (uid INT);
WITH c AS (SELECT * FROM o /**ASSERT UNIQUE**/ JOIN u ON u.id = o.uid)
SELECT * FROM c
```

The report has no assertion, only an `unanalyzed-assertion` diagnostic.
Column markers on a CTE work, and so does a join in the Root Select that reads
a CTE. CTEs are where people put their joins, so this is a large gap. The
soundness oracle cannot see these assertions either.

The same holds for view bodies and FROM subqueries: only the Root Select
lowers its joins; every other body with a join becomes opaque (found while
building issue 26). So a view `CREATE VIEW v AS SELECT ... FROM a JOIN b ...`
loses every fact through its join, not only its markers.

## What to build

Lower CTE, view, and FROM-subquery bodies so the joins in them become `ir.Join` nodes with their
assertions, the same way the Root Select's joins are handled.

## Acceptance criteria

- [ ] `ASSERT UNIQUE` and `ASSERT EXACTLY ONE` on a join inside a referenced
  CTE body produce an assertion with a PROVED or UNKNOWN outcome; the same for view bodies and FROM subqueries.
- [ ] Facts flow through a joined view: e.g. a view joining orders to users by
  primary key keeps the orders key.
- [ ] Nested CTEs and a CTE referenced twice work; the second reference does
  not duplicate the assertion.
- [ ] A marker in a CTE that nothing references is still reported (keep the
  diagnostic, or lower the CTE anyway). Issue 15 suggests removing such CTEs.
- [ ] The soundness oracle judges the new assertions; 0 counterexamples.
