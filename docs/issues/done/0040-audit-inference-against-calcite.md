# 40. Audit inference against Apache Calcite and adapt targeted regression cases

- **State:** done
- **Labels:** check, ready-for-agent
- **Created:** 2026-11-06
- **Related:** [11: SQL corpus](../0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md), [27: soundness oracle](../0027-soundness-oracle-against-duckdb.md), [36: `.sqltest` cases](0036-refactor-tests-for-corpus-and-oracle-scale.md)

## Why

Paulley's rules are our theoretical baseline. Calcite supplies an independent
implementation and regression corpus for overlapping problems. Use it to find
missing cases and qualify novelty claims. The initial
[source survey](../../calcite-notes.md) pins commit
`180f167e65f28c160d4d6ffab35fa37303f0ebe0` and identifies implementation,
test and catalog-fixture entry points.

Keep this as a standalone audit issue, following
[35: the Paulley audit](0035-audit-fd-propagation-against-paulley.md).
It owns the semantic comparison and targeted regressions. Broad corpus import
belongs to issue 11, oracle infrastructure to issue 27, and test-harness changes
to issue 36. File implementation gaps discovered by the audit as follow-up issues.

## Setup

Read Calcite from a local shallow clone at the pinned commit, in the
gitignored `vendor/` folder:

```bash
mkdir -p vendor/calcite && cd vendor/calcite && git init -q && git remote add origin https://github.com/apache/calcite.git && git fetch --depth 1 origin 180f167e65f28c160d4d6ffab35fa37303f0ebe0 && git checkout FETCH_HEAD
```

Cite upstream code by path and method at that commit. Each adapted case gets
a one-line provenance comment naming the upstream test it was inspired by
(e.g. `-- inspired by RelMetadataTest.testJoinUniqueKeys`, or
`-- inspired by join.iq, "left join with key"`). No attribution or
LICENSE/NOTICE material is needed.

## What to do

1. Compare FD propagation, unique keys, column uniqueness, predicate inference
   and null rejection with our Analysis Functions. Produce a table naming the
   pinned Calcite method, its assumptions, our corresponding code and tests,
   and whether the behavior agrees, is intentionally different, or is missing.
2. Check semantic contracts before comparing answers: NULL-equal versus
   NULL-exempt uniqueness, strict/lax FDs, padding, comparison types and
   collations, volatile functions, and dialect-specific expression behavior.
   Investigate the FD handler's shared INNER/LEFT/RIGHT join branch with small
   counterexamples. Do not adopt upstream expectations as axioms.
3. Adapt a small pilot from `RelMetadataTest` and `RexProgramTest` covering
   projection, joins (including padding and self joins), grouping, uniqueness
   and expression null rejection. Write them as `.sqltest` cases; no new harness
   or broad migration is needed for this audit. Reconstruct actual catalog
   assumptions in explicit DDL or Property Declarations.
4. Derive sqlassert's expected outcomes from its Property contracts, not
   from Calcite's answers. Check executable PROVED cases with the oracle and
   track unsupported cases explicitly.
5. Reduce disagreements to named regressions or follow-up issues. Report
   soundness failures separately from missing proofs and semantic differences.
6. Assess the Quidem `.iq` tests (65 files, ~61k lines, ~3,850 `!ok` and
   ~390 `!plan` cases at the pinned commit) and decide, per file, whether
   anything is worth migrating to `.sqltest` or whether it duplicates our
   cases. They are execution tests (SQL plus expected rows), not metadata
   tests, so they carry no Property expectations. What to look for:
   - `!plan` outputs where Calcite removed an aggregate, DISTINCT, sort or
     join, or simplified a predicate: each encodes an inference (uniqueness,
     FD, null rejection) we can restate as an ASSERT. Most are in
     `sub-query.iq` (173 plans), `agg.iq` (45), `planner.iq` (24),
     `join.iq` (17), `hep.iq` and `new-decorr.iq`.
   - NULL-semantics cases (`blank.iq`, `conditions.iq`, `some.iq`,
     `outer.iq`, `row-equality.iq`, `set-op.iq`) as boundary and
     counterexample material for null rejection and padding.
   - Self-contained `VALUES`/`WITH` queries, which need no catalog.
   Most cases use the `scott` schema (HSQLDB, from the external
   `scott-data-hsqldb` artifact, not in the repo); reconstruct its DDL and
   keys explicitly. Skip dialect, function, cast, spatial, stream and server
   DDL files unless a case exercises inference. The output is a per-file
   verdict (migrate / duplicate / irrelevant) in the comparison notes, the
   migrated cases themselves, and a short list of whole files worth handing
   to issue 11 as corpus.

This is an audit and targeted test adaptation, not a Calcite runtime
integration. A Java runner is unnecessary for the pilot; record upstream
expectations from source without claiming Calcite was executed locally.

## Acceptance criteria

- [x] A versioned comparison table covers the five inference areas above.
- [x] Pilot cases state their schema assumptions in DDL or Property Declarations and carry a provenance comment.
- [x] Each covered area has positive and boundary/counterexample cases where applicable.
- [x] Relevant tests pass with `pytest --soundness-oracle`; unsupported oracle cases are listed.
- [x] Disagreements become reduced regressions or tracked issues.
- [x] Each `.iq` file has a migrate / duplicate / irrelevant verdict; migrated cases are `.sqltest` cases.
- [x] Source notes distinguish implemented coverage from unverified research claims.

## Done

The comparison is [`docs/calcite-audit.md`](../../calcite-audit.md): FDs,
unique keys, column uniqueness, predicate inference and null rejection, each
row naming the pinned Calcite method, its assumptions, our code and our
cases. 136 cases in `tests/calcite_audit*.sqltest`, each with a provenance
comment, all passing with `pytest --soundness-oracle` (0 counterexamples).

- **Five soundness bugs in sqlassert, fixed here** with regressions in
  `tests/calcite_audit.sqltest`: padding through CASE and non-strict
  operators, EXISTS over a scalar aggregate, FULL JOIN padding shared by both
  sides (it also reached the INNER JOIN suggestion), Oracle's `''`, and set
  operation chains parsed right-associatively (now left-associative, with
  INTERSECT bound first).
- **Nine soundness problems in Calcite** (C1-C9), among them the shared
  INNER/LEFT/RIGHT FD branch; sqlassert stays UNKNOWN on each.
- **24 missing proofs**, filed as [issue 45](0045-close-the-calcite-audit-missing-proofs.md).
- **`.iq` files**: mostly irrelevant or duplicate; 25 cases migrated from 10
  files, every file has a verdict, and the issue-11 corpus candidates are
  marked.

Unsupported by the oracle: `nullability_of_mod_by_zero_postgres` (the oracle
runs DuckDB only) and the Oracle-dialect cases; every other PROVED case is
judged.
