# Apache Calcite as an audit source

Calcite supplies an independent implementation of dependency, uniqueness and
null-rejection analysis, plus regression tests. Use it to find missing rules,
challenge our semantics, and ground comparisons with existing systems.

Source survey: 2026-11-06, commit
[`180f167e65f28c160d4d6ffab35fa37303f0ebe0`](https://github.com/apache/calcite/commit/180f167e65f28c160d4d6ffab35fa37303f0ebe0).
This records source entry points, not a completed correctness or coverage audit.
It adds no runtime dependency. [Issue 40](issues/0040-audit-inference-against-calcite.md)
tracks the audit and first adapted regressions.

## Implementation entry points

Paths below are relative to the Calcite repository at that commit. The
[metadata API](https://calcite.apache.org/javadocAggregate/org/apache/calcite/rel/metadata/RelMetadataQuery.html)
is useful for navigation; use pinned source for reproducible comparisons.

| Source | What to compare |
|---|---|
| `core/src/main/java/org/apache/calcite/rel/metadata/RelMdFunctionalDependency.java` | FD introduction and propagation, closure queries, projections, joins and aggregates |
| `core/src/main/java/org/apache/calcite/rel/metadata/RelMdColumnUniqueness.java` | Uniqueness queries, including the `ignoreNulls` distinction |
| `core/src/main/java/org/apache/calcite/rel/metadata/RelMdUniqueKeys.java` | Deriving sets of keys through relational operators |
| `core/src/main/java/org/apache/calcite/rel/metadata/RelMdPredicates.java` | Predicate inference through operators |
| `core/src/main/java/org/apache/calcite/plan/Strong.java` | Null propagation and predicates that cannot be true when selected inputs are NULL |

For example, the surveyed FD handler combines both inputs' dependencies and
ON equalities in a shared INNER/LEFT/RIGHT join branch. Audit that behavior
against our strict/lax definitions and padded-row counterexamples; do not infer
equivalence between the two systems from API names.

## Test corpus entry points

- [`RelMetadataTest.java`](https://github.com/apache/calcite/blob/180f167e65f28c160d4d6ffab35fa37303f0ebe0/core/src/test/java/org/apache/calcite/test/RelMetadataTest.java):
  `testFunctionalDependency*`, `testJoinUniqueKeys`,
  `testColumnUniquenessForJoin`, and `testFullOuterJoinUniqueness1` are
  starting points for FD, key and NULL-sensitive join cases.
- [`RexProgramTest.java`](https://github.com/apache/calcite/blob/180f167e65f28c160d4d6ffab35fa37303f0ebe0/core/src/test/java/org/apache/calcite/rex/RexProgramTest.java):
  `testStrong`, `testItemStrong` and `testSimplifyFunctionWithStrongPolicy`
  exercise expression-level null reasoning. These build expressions in Java;
  they need translation into SQL cases rather than string extraction.
- [`SqlOperatorTest.java`](https://github.com/apache/calcite/blob/180f167e65f28c160d4d6ffab35fa37303f0ebe0/testkit/src/main/java/org/apache/calcite/test/SqlOperatorTest.java):
  expression results, return types and nullability, including dialect-specific
  operators. Confirm target-dialect behavior before adopting expectations.
- [`core/src/test/resources/sql/join.iq`](https://github.com/apache/calcite/blob/180f167e65f28c160d4d6ffab35fa37303f0ebe0/core/src/test/resources/sql/join.iq):
  SQL with expected results and script directives. Some cases are self-contained
  `WITH ... VALUES` queries; others depend on a configured connection/schema.

Metadata SQL often relies on
[`MockCatalogReaderSimple.java`](https://github.com/apache/calcite/blob/180f167e65f28c160d4d6ffab35fa37303f0ebe0/testkit/src/main/java/org/apache/calcite/test/catalog/MockCatalogReaderSimple.java)
and its supporting fixtures for types, nullability and keys. Reconstruct those
facts explicitly in DDL or Property Declarations. Record any assumptions that
cannot be represented; never invent constraints to obtain a Proof.

## Reuse and interpretation

Read the source from a shallow clone at the pinned commit in the gitignored
`vendor/calcite` (setup in issue 40). Start with a small curated selection of
`.sqltest` cases, stating fixture assumptions explicitly. Cases are inspired by
upstream tests, not copied; each carries a one-line comment naming the upstream
test it was inspired by, and needs no attribution.

Translate the expected Property explicitly: uniqueness with NULLs ignored is
different from a null-equal strict key; an inferred predicate is different from
an FD. Calcite's negative metadata answer is not automatically a sqlassert
Disproof. Judge adapted PROVED cases with the DuckDB oracle where executable;
record unsupported cases separately. Agreement is comparison evidence, and
UNKNOWN without a counterexample remains Unknown.

Broader extraction belongs to [issue 11](issues/0011-fuzz-test-analyze-against-a-real-world-sql-corpus.md);
targeted regressions are `.sqltest` cases ([issue 36](issues/done/0036-refactor-tests-for-corpus-and-oracle-scale.md)).
Keep large corpus runs opt-in and preserve their schema context and provenance.
