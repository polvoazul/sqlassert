# 27. Soundness oracle against DuckDB

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Split from:** [issue 11](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md)
  (its "Extension: oracle-based differential testing" and the `TODO.md` Task 12
  addendum)

Do this before [issue 24](0024-functional-dependency-reasoning.md): that rewrite
replaces unique-set propagation outright, and this oracle is the safety net
that catches an unsound `PROVED` it introduces.

## What to build

A soundness oracle that reuses the existing test corpus. Every `analyze()`
call in the suite already holds a runnable SQL Program (DDL plus a query whose
markers are comments). For each `PROVED` assertion, Hypothesis generates tiny
tables that satisfy the DDL, DuckDB runs the SQL unchanged, and the asserted
property is checked on the real result. A violation is a soundness bug in the
rules, and Hypothesis shrinks it to a minimal counterexample that becomes a
regression test. A real engine is the ground truth, so no second model of SQL
semantics has to be written or trusted.

Why tiny instances: for select-project-join queries, any uniqueness
counterexample fits in two rows per table (each violating output row draws one
row per table, and per-row filters keep those rows once the others are
deleted: each output row needs only the input rows it is built from; Paulley
2000, Thm 11, p.196, and Thm 12, p.207 for "at most one match"). Beyond that
fragment (outer joins, aggregates, `EXCEPT`) the check is a strong random
search, not a complete one. See
[`docs/literature-notes.md`](../literature-notes.md#testing-and-guarantees).

## Part 1 — judge existing `PROVED` outcomes

- [ ] Collect `(sql, report)` pairs from the existing tests (fixture or
  conftest hook around `analyze`) without rewriting the tests.
- [ ] Generate table contents from the parsed DDL: values from a tiny domain
  (e.g. `[1, 2, None]`) plus the query's literals, respecting primary keys,
  `UNIQUE`, and `NOT NULL`, so collisions and NULLs are frequent.
- [ ] First cut: Unique Set and Non-Null Unique Set assertions on the Root
  Select, checked with `GROUP BY <cols> HAVING count(*) > 1` over the query
  (and a NULL check for Non-Null Unique Sets).
- [ ] Unique Join assertions: the join's row count never exceeds its left
  input's. Needs the left input and the join extracted as SQL from the parsed
  node.
- [ ] Assertions on views (query the view), CTEs and FROM subqueries (extract
  the node's SQL with sqlglot, carrying any `WITH` clause it depends on).
- [ ] Property Declarations must hold in generated data, or they cause false
  alarms: table-level ones become extra constraints; declarations on query
  results are enforced by rejecting violating instances.
- [ ] Skip programs with tables that have no `CREATE` (no schema to generate).
- [ ] Opt-in suite (marker excluded from the default run) that fails on any
  `PROVED` with a counterexample, printing the instance.

## Part 2 — differential testing without markers

Hand-adding markers does not scale, and letting sqlassert both place markers
and judge them is circular. Generate candidate properties mechanically and
judge them with the oracle.

**Candidates:**

- Every JOIN: is it a Unique Join?
- Every Select Expression: is each output-column subset up to size k a Unique
  Set / Non-Null Unique Set? Is each output column non-null?
- `Report.relation_facts` already lists inferred Unique Sets per named
  relation without markers; start there.

**Comparison:**

| sqlassert | Oracle | Meaning |
|---|---|---|
| PROVED | counterexample | Soundness bug — test fails. |
| UNKNOWN | probably true | Missed proof — backlog, ranked by frequency. |
| PROVED | probably true | Agreement. |
| UNKNOWN | counterexample | Agreement. |

**Corpora with published PKs/FKs:** TPC-H (22 queries), TPC-DS, SSB, JOB
(IMDb, 113 join-heavy queries). Known facts to check against: Lindner et al.
2024, Table 2 and §7.4 (e.g. TPC-H Q10's seven grouping columns reduce to
`c_custkey`). Links in [literature-notes.md](../literature-notes.md).

- [ ] Candidate generator for joins, output-column subsets, and non-null
  columns over a parsed program. Shared with
  [issue 15](0015-suggest-provable-properties-for-unannotated-sql.md).
- [ ] Missed-proof report (UNKNOWN but probably true), grouped by cause, as a
  completeness metric to track across engine changes (e.g. FD support).
- [ ] TPC-H DDL and queries checked in as a corpus, with provenance.

## Deferred alternative — symbolic oracle with z3

Encode SQL semantics as SMT formulas over symbolic two-row tables: each
candidate output row is a `(guard, values)` pair, joins and filters rewrite
guards, and the property becomes "two distinct candidates with true guards
agree on the key". UNSAT is a proof for select-project-join queries; SAT
returns counterexample tables. It covers all databases at once, but the
encoding is a second model of SQL semantics to write and trust, and aggregates
and outer joins make it grow quickly. Prior art worth reading first: VeriEQL
(bounded SMT with NULL semantics), Cosette, SQLSolver, Qex, XData. Revisit
after the DuckDB oracle exists.

## Related

- [0011](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md) — crash
  fuzzing over a real-world corpus; that corpus can feed Part 2.
- [0015](0015-suggest-provable-properties-for-unannotated-sql.md) — `suggest`
  mode, a user-facing use of the same candidate generator.
