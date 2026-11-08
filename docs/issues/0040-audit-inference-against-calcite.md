# 40. Audit inference against Apache Calcite and adapt targeted regression cases

- **State:** open
- **Labels:** check, ready-for-agent
- **Created:** 2026-11-06
- **Related:** [11: SQL corpus](0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md), [27: soundness oracle](0027-soundness-oracle-against-duckdb.md), [36: corpus/test organization](0036-refactor-tests-for-corpus-and-oracle-scale.md)

## Why

Paulley's rules are our theoretical baseline. Calcite supplies an independent
implementation and regression corpus for overlapping problems. Use it to find
missing cases and qualify novelty claims. The initial
[source survey](../calcite-notes.md) pins commit
`180f167e65f28c160d4d6ffab35fa37303f0ebe0` and identifies implementation,
test and catalog-fixture entry points.

Keep this as a standalone audit issue, following
[35: the Paulley audit](done/0035-audit-fd-propagation-against-paulley.md).
It owns the semantic comparison and targeted regressions. Broad corpus import
belongs to issue 11, oracle infrastructure to issue 27, and test-harness changes
to issue 36. File implementation gaps discovered by the audit as follow-up issues.

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
   and expression null rejection. Use existing Python tests; no new harness
   or broad migration is needed for this audit. Reconstruct actual catalog
   assumptions in explicit DDL or Property Declarations.
4. Record upstream commit/path/method, transformations and attribution for
   each adapted case, including applicable LICENSE/NOTICE material. Derive
   sqlassert's expected outcomes from its Property contracts. Check executable
   PROVED cases with the oracle and track unsupported cases explicitly.
5. Reduce disagreements to named regressions or follow-up issues. Report
   soundness failures separately from missing proofs and semantic differences.
   Use `.iq` SQL and operator tests as additional corpus candidates for issue
   11; defer broad extraction to the existing corpus work.

This is an audit and targeted test adaptation, not a Calcite runtime
integration. A Java runner is unnecessary for the pilot; record upstream
expectations from source without claiming Calcite was executed locally.

## Acceptance criteria

- [ ] A versioned comparison table covers the five inference areas above.
- [ ] Pilot cases retain schema assumptions, provenance and attribution.
- [ ] Each covered area has positive and boundary/counterexample cases where applicable.
- [ ] Relevant tests pass with `pytest --soundness-oracle`; unsupported oracle cases are listed.
- [ ] Disagreements become reduced regressions or tracked issues.
- [ ] Source notes distinguish implemented coverage from unverified research claims.
