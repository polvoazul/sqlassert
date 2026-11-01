# 36. Refactor semantic tests and prepare the corpus/oracle harness for scale

- **State:** pending human review
- **Labels:** needs-human-review, proposal
- **Created:** 2026-11-01
- **Related:** [11: SQL corpus and join mutations](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md), [27: soundness oracle and differential properties](0027-soundness-oracle-against-duckdb.md)

This is a design proposal. A human must review and approve the scope and test
format before implementation or a broad migration of existing tests.

## Why

The suite currently has 483 test functions across 38 files, expanded by pytest
parametrization into 1,125 cases. Seventy-eight parametrized functions account
for 720 cases; one marker-accounting function alone covers 201 combinations.
Hypothesis's generated database samples are additional checks inside those
cases, not additional pytest items. Reducing the displayed test count is not
the objective: preserve meaningful coverage while making cases easier to write,
review, identify, run selectively, and grow.

Issue 11 adds realistic SQL and one-marker-per-join mutations. Issue 27 Part 2
adds mechanically generated candidate Properties over those queries. The
number of queries, attachment sites, column subsets, and generated instances
can multiply rapidly. Writing one Python function per case or creating a pytest
item per candidate/sample would make that growth harder to manage.

## Current execution model to preserve

With `--soundness-oracle`, the test harness wraps `analyze()` before test-module
imports and records SQL, dialect, and the returned Report. It does not inspect
arbitrary test locals or infer the test's intent. Recording spans fixture setup,
the test body, and fixture teardown; calls during collection are excluded.

The oracle judges the recorded PROVED assertions after fixture finalizers have
restored mocks. SQL/dialect pairs are deduplicated within each test, while two
tests using the same SQL each get their own check. Counterexamples are teardown
errors on the originating pytest item. Counts and findings are combined through
pytest's reports, including under xdist.

The suite defaults to `-n auto`; `-n 0` runs serially. Each in-memory DuckDB uses
one execution thread. There is no separate oracle process pool or final
catch-all oracle test. Preserve normal node-ID and `-k` selection.

For context, single end-to-end runs on the same 16-CPU machine took 45.4 seconds
with the earlier separate pool and 30.7 seconds with per-test judging; the final
pre-commit verification took 28.3 seconds. These were not controlled repeated
benchmarks. The ordinary suite without the oracle took 5.7 seconds.

## Proposed division of tests

| Purpose | Representation | Scheduling unit |
|---|---|---|
| Curated semantic regressions | SQL-first named cases, potentially `.test` files | One named case |
| Issue 11: imported SQL and join mutations | Original SQL fixtures, real schema context, provenance, generated variants | One program or independently reproducible mutation |
| Issue 27: differential Property testing | Program plus mechanically generated candidates | One program, checking multiple candidates together |
| Engine internals, fixtures, mocks, detailed Evidence or provenance behavior | Python tests | Existing pytest functions |

Use one underlying analysis/oracle implementation across these inputs. The
representations can differ without introducing competing implementations of
SQL semantics or parallel worker pools.

## SQL-first case format to evaluate

Borrow the SQL/expected-result block structure from DuckDB's
[sqllogictest format](https://www.duckdb.org/docs/preview/dev/sqllogictest/intro).
The following is a proposal for sqlassert tests, not existing syntax or a claim
of compatibility with DuckDB's runner:

```text
# case: right_key_prevents_row_multiplication
analyze
CREATE TABLE users (id INTEGER PRIMARY KEY);
CREATE TABLE events (user_id INTEGER);

SELECT *
FROM events
/**ASSERT UNIQUE**/ JOIN users
    ON events.user_id = users.id;
----
assertions: PROVED
diagnostics: none
```

A collector would expose each case as a normal pytest item with a stable,
readable name and source location. The runner would call `analyze()` and compare
assertion outcomes in written order and expected Diagnostic codes. The existing
per-test oracle integration could observe that call.

Keep the first format small: named cases, SQL, assertion outcomes, and
Diagnostic codes. Start with self-contained setup. Shared schemas, includes,
matrices, and other directives need demonstrated corpus use cases before being
added. The Property Markers and their meaning remain unchanged; test metadata
must not become new user-facing SQL marker syntax.

Keep tests in Python when they need detailed Evidence structure, exact origins,
mocking, or behavior that the small case format cannot express naturally.
Avoid snapshotting whole Reports, proof text, or unstable generated identifiers.

## Scaling requirements from issues 11 and 27

- Preserve a corpus query's actual DDL and trusted constraints. Extracting a
  SELECT from a DuckDB test while discarding its setup may retain crash
  coverage but makes a soundness check impossible. Do not invent constraints
  to make imported queries provable. Report missing schema or unsupported
  execution explicitly and retain source/license information.
- For issue 11, retain the original SQL and stable mutation identities. Insert
  one join marker per variant so a failure identifies the join under test.
  Unsupported predicates supply no proof facts, but extra filters do not
  invalidate a proof established by supported conjuncts.
- Keep collection lightweight. Do not download corpora, run analyses, generate
  database instances, or eagerly expand every Property/sample combination at
  collection time. Materialize expensive checks within a worker's program job.
- For issue 27, investigate loading each generated database instance once and
  checking multiple candidate Properties against it, reusing the program's
  schema and database. Shrink a failing candidate separately. Measure this
  against the current assertion-by-assertion search before adopting it.
- Keep candidate/sample identities in failure output without necessarily
  assigning every candidate or generated sample its own pytest item. Program
  jobs should remain small enough for useful xdist load balancing and reruns.
- Distinguish soundness failures from completeness observations. PROVED plus a
  counterexample is a failure. UNKNOWN with no counterexample found is not a
  Proof; it is a possible missed proof to report and investigate.
- Keep the large corpus/differential suites opt-in, as issue 11 requires.
  Promote reduced findings into small, named semantic regressions in the fast
  suite, with their provenance. Curated regressions must not depend on a
  corpus download to run.

## Human review decisions

- Approve SQL-first cases, or prefer a smaller Python case helper initially?
- Approve the minimum expected-outcome/Diagnostic syntax and case naming.
- Choose the pilot cases and which detailed assertions remain in Python.
- Agree on scheduling granularity, corpus ownership, and opt-in execution.
- Agree on how exact Diagnostic origins are tested when SQL moves into files.
- Decide whether shared generated instances are a separate optimization after
  the format/collection refactor, rather than part of its initial scope.

## Suggested implementation after approval

1. Pilot representative PROVED, UNKNOWN, and Diagnostic cases, plus a case
   that must remain in Python for Evidence/provenance assertions.
2. Verify case selection, stable IDs and locations, fixture cleanup, failure
   attribution, and identical behavior in serial and parallel execution.
3. Compare outcomes, Diagnostic codes, and oracle coverage before and after;
   do not replace semantic assertions with "does not crash" checks.
4. Measure collection time, wall time, and memory on a representative corpus,
   including xdist's repeated collection across workers. Record search budgets
   and judged/skipped counts so a speedup cannot hide lost oracle coverage.
5. Migrate related groups only after the pilot is reviewed. Delete superseded
   helpers and tests in the same change rather than keeping two paths for old
   and new behavior.

## Acceptance criteria

- [ ] Human review approves or revises the proposed scope and format.
- [ ] The pilot preserves expected outcomes, Diagnostics, and oracle coverage.
- [ ] Cases have stable, readable pytest IDs and useful source locations.
- [ ] Individual cases and mutations can be selected and rerun independently.
- [ ] Parallelism is owned by pytest; no second oracle worker pool is added.
- [ ] Corpus inputs retain real schema context, provenance, and licensing.
- [ ] Large corpus/differential runs remain separate from the default fast run.
- [ ] Measured collection/runtime/memory costs and coverage counts are recorded.
- [ ] Reduced corpus findings become fast, named regression cases.
