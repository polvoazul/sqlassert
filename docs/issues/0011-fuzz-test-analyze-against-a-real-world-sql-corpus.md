# 11. Fuzz-test analyze against a real-world SQL corpus

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-08-25
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/11

## Parent

#9

## What to build

Build a corpus-driven regression harness that stress-tests `analyze()`'s "trustworthy result or explicit diagnostic, never a crash" boundary against real-world SQL, rather than only hand-authored examples.

Curate a fixture corpus of realistic SELECT statements with joins (SQL text only, no expected results — e.g. a hand-picked subset drawn from an existing public SQL test suite such as DuckDB's `test/sql` logic tests, favoring join diversity: RIGHT/FULL/SEMI/ANTI/CROSS, correlated subqueries, set operations), checked in under `tests/corpus/*.sql`. For each corpus statement:

1. Run `SqlParser.parse` and `IrParser.parse` and confirm no exception escapes (as opposed to being turned into a `Diagnostic`, which `SqlParser` already does for known-bad input).
2. For each JOIN in the statement, produce one mutated variant with a single `/**ASSERT UNIQUE**/` marker inserted immediately before that JOIN's keyword (one marker per variant, isolating which join is under test), and run the mutated SQL through `analyze()` end-to-end, confirming no exception escapes.
3. For any join whose kind is RIGHT/FULL/SEMI/ANTI/CROSS, or whose condition contains OR, a non-equality operator, or null-safe equality, assert the resulting `AssertionReport.outcome` is not `PROVED` — this checks the exclusion list in `docs/mvp-scope.md` at corpus scale, not only in hand-written cases.
4. When a run surfaces an interesting failure or an unexpected `PROVED`, promote the reduced SQL into a named test in the relevant `tests/test_analyze_*.py` file with a comment noting what it exercises and where it came from, rather than keeping the whole corpus as the permanent regression surface.

Keep this suite opt-in / separate from the default fast run (a dedicated marker or a file excluded from the default `pytest` invocation), since running `analyze()` over a whole corpus is not free.

## Acceptance criteria

- [ ] A curated SQL corpus of realistic multi-join SELECT statements is checked into the repo as static fixtures, with its provenance documented (source, license, why each file was included).
- [ ] Every corpus statement parses through `SqlParser` and `IrParser` without an unhandled exception; any statement that cannot be handled produces a `Diagnostic` instead.
- [ ] Every corpus statement, mutated with one `/**ASSERT UNIQUE**/` marker per JOIN in turn, runs through `analyze()` without an unhandled exception.
- [ ] No mutated case whose marked join is RIGHT/FULL/SEMI/ANTI/CROSS, or whose join condition contains OR/non-equality/null-safe equality, reports `PROVED`.
- [ ] The corpus-fuzz suite is excluded from the default fast test run and documented as an opt-in suite with instructions to run it.
- [ ] At least one real finding from running the harness (a crash or an incorrect `PROVED`) is fixed or promoted into a first-class named test in `tests/test_analyze_*.py`, demonstrating the harness catches something the hand-written suite would miss.

## Extension: oracle-based differential testing

Hand-adding markers to a corpus does not scale, and letting sqlassert both
place markers and judge them is circular. Instead, generate candidate
properties mechanically and judge them with an oracle independent of the
engine. Background: [literature-notes.md](../literature-notes.md), "Testing and
guarantees".

**Candidates (no human markers):**

- Every JOIN: is it a Unique Join?
- Every Select Expression: is each output-column subset up to size k a Unique
  Set / Non-Null Unique Set? Is each output column non-null?
- `Report.relation_facts` already lists inferred Unique Sets per named
  relation without markers; start there.

**Oracle — tiny random instances:** for each candidate, generate many small
random tables satisfying the DDL (types, keys, NOT NULL; Hypothesis), run the
query on DuckDB, and check the property on the result. A counterexample means
*definitely false*; none across many instances means *probably true*. For
select-project-join queries, two rows per table suffice to find every
counterexample to uniqueness (Paulley 2000, Thm 11, p.196) and to "at most one
match" (Thm 12, p.207), so there "probably" becomes "certainly".

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

**Acceptance criteria:**

- [ ] Candidate generator for joins, output-column subsets, and non-null
  columns over a parsed program.
- [ ] Random-instance oracle on DuckDB that respects the program's DDL.
- [ ] Opt-in suite that fails on any PROVED-vs-counterexample case, with the
  counterexample instance printed.
- [ ] Missed-proof report (UNKNOWN but probably true), grouped by cause, as a
  completeness metric to track across engine changes (e.g. FD support).
- [ ] TPC-H DDL and queries checked in as a corpus, with provenance.

## Related

- [0015](0015-suggest-provable-properties-for-unannotated-sql.md) — `suggest`
  mode, a user-facing use of the same candidate generator.

## Blocked by

- #9

## Addendum — Fuzz PROVED outcomes against DuckDB

Migrated from `TODO.md` Task 12. A soundness oracle that complements the crash/diagnostic corpus above.

### What to build

 A soundness oracle that reuses the existing test corpus.
Every `analyze()` call in the suite already holds a runnable SQL Program (DDL
plus a query whose markers are comments). For each `PROVED` assertion,
Hypothesis generates tiny tables that satisfy the DDL, DuckDB runs the SQL
unchanged, and the asserted property is checked on the real result. A
violation is a soundness bug in the rules, and Hypothesis shrinks it to a
minimal counterexample that becomes a regression test. A real engine is the
ground truth, so no second model of SQL semantics has to be written or trusted.

Why tiny instances: for select-project-join queries, any uniqueness
counterexample fits in two rows per table (each violating output row draws one
row per table, and per-row filters keep those rows once the others are
deleted: each output row needs only the input rows it is built from). Beyond that fragment (outer joins, aggregates, `EXCEPT`) the check is
a strong random search, not a complete one. See
[`docs/literature-notes.md`](../literature-notes.md#testing-and-guarantees).

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
- [ ] Optionally run the oracle on `UNKNOWN` outcomes to find properties that
  hold but the rules cannot prove (completeness gaps, not bugs).

**Deferred alternative — symbolic oracle with z3.** Encode SQL semantics as SMT
formulas over symbolic two-row tables: each candidate output row is a
`(guard, values)` pair, joins and filters rewrite guards, and the property
becomes "two distinct candidates with true guards agree on the key". UNSAT is a
proof for select-project-join queries; SAT returns counterexample tables. It
covers all databases at once, but the encoding is a second model of SQL
semantics to write and trust, and aggregates and outer joins make it grow
quickly. Prior art worth reading first: VeriEQL (bounded SMT with NULL
semantics), Cosette, SQLSolver, Qex, XData. Revisit after the DuckDB oracle
exists.
