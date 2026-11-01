# 27. Soundness oracle against DuckDB

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Split from:** [issue 11](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md)
  (its "Extension: oracle-based differential testing" and the `TODO.md` Task 12
  addendum)

Do this before issue 24 (done; see [functional-dependencies.md](../functional-dependencies.md)): that rewrite
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

Done: `tests/soundness_oracle.py`, run with `python -m pytest --soundness-oracle`.
`tests/conftest.py` wraps `analyze` to record each test's programs and judge
its `PROVED` assertions after fixture teardown. A counterexample is reported
as a teardown error on the originating test. Part 2 and the z3 alternative
remain open.

DDL Foreign Keys are removed from the oracle's physical tables and checked
with coverage queries after the complete generated instance is loaded. This
allows arbitrary loading/deletion order, forward references, and cycles while
still rejecting rows that violate the declared Foreign Keys (`MATCH SIMPLE`).
Other constraints remain enforced by DuckDB.

Each search returns its outcome (`counterexample`, `no-counterexample`, or
`insufficient-data`), its counterexample instance when found, and attempted,
valid, and rejected predicate evaluation counts (including shrinking).
`Judgement.searches` associates those results with assertion IDs. An
insufficient-data result is reported as skipped, not counted as judged.

Pytest owns parallelism for the entire suite through `pytest-xdist`, with
`-n auto` configured by default. Use `python -m pytest --soundness-oracle`
for the parallel oracle run, `-n 0` for serial execution, or `-n 24` to
choose a worker count. A node ID or `-k` selection checks only those tests.

Recording spans each test's fixture setup, body, and fixture teardown.
Repeated SQL/dialect pairs are deduplicated within that test; another test
using the same SQL gets its own oracle check. Calls made during collection
are excluded. Checks run after fixture finalizers restore mocks, so the
oracle uses the real DuckDB implementation. Each program has its own
in-memory DuckDB with one execution thread, and its assertions run serially
inside the same pytest worker as the originating test.

Per-test counts and skipped/known findings travel with pytest's teardown
reports. The controller combines them into the terminal summary. There is
no final catch-all test or separate oracle worker pool.

Measured on 2026-11-01 with 16 CPUs, Python 3.13.7, DuckDB 1.5.2,
Hypothesis 6.170.14, and pytest-xdist 3.8.0: the full oracle run passed
1,125 tests in 30.71 seconds, compared with 45.42 seconds for the prior
separate-pool implementation. Per-test recording checked 697 program
analyses and judged 473 assertions, with no counterexamples and seven
skipped checks. The ordinary suite without the oracle passed in 5.72
seconds. These are single end-to-end runs, including worker startup.

- [x] Collect `(sql, report)` pairs from the existing tests (fixture or
  conftest hook around `analyze`) without rewriting the tests.
- [x] Generate table contents from the parsed DDL: values from a tiny domain
  (e.g. `[1, 2, None]`) plus the query's literals, respecting primary keys,
  `UNIQUE`, and `NOT NULL`, so collisions and NULLs are frequent.
- [x] First cut: Unique Set and Non-Null Unique Set assertions on the Root
  Select, checked with `GROUP BY <cols> HAVING count(*) > 1` over the query
  (and a NULL check for Non-Null Unique Sets).
- [x] Unique Join assertions: the join's row count never exceeds its left
  input's. Needs the left input and the join extracted as SQL from the parsed
  node. Built as a stronger check: the join is rewritten to `LEFT`, so the
  counts differ exactly when some left row matches more than once.
- [x] Assertions on views (query the view), CTEs and FROM subqueries (extract
  the node's SQL with sqlglot, carrying any `WITH` clause it depends on).
- [x] Property Declarations must hold in generated data, or they cause false
  alarms: table-level ones become extra constraints; declarations on query
  results are enforced by rejecting violating instances. (Every `DECLARE`
  marker sits on a Select Expression today, so all are enforced by rejection;
  DDL constraints are enforced by DuckDB and by the generator.)
- [x] Skip programs with tables that have no `CREATE` (no schema to generate).
- [x] Opt-in checks attached to each test that fail on any `PROVED` with a
  counterexample, printing the instance.

### Findings

First run (260 programs, 139 `PROVED` assertions, 200 examples of up to 3
rows per table; also clean at 1500 examples of up to 4 rows): no
counterexamples. Skipped, because DuckDB rejects the program itself:

- `test_a_parenthesized_aggregate_view_self_joined_on_its_grouping_key_is_proved`
  and `test_a_positional_grouping_key_maps_to_its_selected_output` self-join
  a view without aliases (`FROM per_user JOIN per_user USING (user_id)`),
  which DuckDB reports as an ambiguous table reference.

A seeded rule bug (every join accepted as unique) is caught, with two-row
counterexamples.

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

- [x] Candidate generator for joins, output-column subsets, and non-null
  columns over a parsed program: `sqlassert.suggest.candidates`, shared with
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
