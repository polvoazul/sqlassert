# Calcite audit (issue 40)

sqlassert's inference compared with Apache Calcite at commit
[`180f167e`](https://github.com/apache/calcite/commit/180f167e65f28c160d4d6ffab35fa37303f0ebe0),
read from source in `vendor/calcite` (setup in [issue 40](issues/done/0040-audit-inference-against-calcite.md)).
Calcite was never executed: every Calcite behavior below is read from its
code, and every counterexample was run in DuckDB. sqlassert's side is tested:
each row names cases in `tests/calcite_audit*.sqltest`, all passing with
`pytest --soundness-oracle`. Expectations follow sqlassert's Property
contracts, never Calcite's answers.

Result: **five soundness bugs in sqlassert, all fixed** (below); **nine
soundness problems in Calcite** that sqlassert does not share; **24 missing
proofs**, filed as [issue 45](issues/0045-close-the-calcite-audit-missing-proofs.md)
(item 2, `GROUP BY ()`, since closed).

## Files

| File | Cases | What |
|---|---|---|
| `tests/calcite_audit.sqltest` | 11 | Regressions for the five soundness bugs, with the sound boundary next to each |
| `tests/calcite_audit_keys.sqltest` | 53 | FDs, unique keys, column uniqueness (`RelMetadataTest`, `MockCatalogReaderSimple`) |
| `tests/calcite_audit_nulls.sqltest` | 52 | Predicate inference, null rejection, expression nullability (`RexProgramTest`, `RelMetadataTest`) |
| `tests/calcite_audit_iq.sqltest` | 30 | Plans and NULL boundaries from the Quidem `.iq` files (scott schema) |

Each case names the upstream test or `.iq` query that inspired it.

## Contracts

Compare contracts before answers:

- **Uniqueness.** Calcite's `areColumnsUnique(rel, cols, ignoreNulls=true)`
  is our UNIQUE (a Unique Set: a row with a NULL in the set is exempt).
  `ignoreNulls=false`, the default, is a null-equal strict key; sqlassert has
  no marker for it. NOT NULL UNIQUE is stronger than both. Calcite's
  `TableScan` handlers assume declared keys hold no NULLs; a nullable
  UNIQUE is a lax key for us.
- **FDs.** Calcite's `ArrowSet` has no strict/lax flag, but its consumers
  (`AggregateRemoveDuplicateKeysRule`, `SortRemoveDuplicateKeysRule`) read
  FDs as strict (NULL equals NULL). Our DETERMINES is lax in its determinant,
  null-equal in its dependent.
- **Null rejection.** Calcite's `Strong` has `isNull` (strict: NULL when the
  inputs are) and `isNotTrue` (never TRUE when they are NULL). Ours is the
  former per column; the latter is missing (issue 45, item 12). Calcite
  rejects a whole padded side at once; we reject per column (item 9).
- **Comparison types.** Calcite's `EquivalenceFinder` and FD projection
  ignore types and collations. Ours require identity comparison
  (`equality._identity_comparison`, `uniqueness._identity_inputs`).
- **Dialects.** Calcite types MOD/DIVIDE as non-null and GREATEST as strict;
  DuckDB's `1 % 0` is NULL and its `greatest` skips NULLs. We model these per
  dialect (`convert._CHECKED_DIVISION_DIALECTS`, Builtins).

## Comparison

Calcite paths are under `core/src/main/java/org/apache/calcite/`: `FD` is
`rel/metadata/RelMdFunctionalDependency.java`, `UK` `RelMdUniqueKeys.java`,
`CU` `RelMdColumnUniqueness.java`, `PR` `RelMdPredicates.java`, `Strong`
`plan/Strong.java`. Our code is under `sqlassert/`. Verdicts: **agrees**,
**ours stronger** (we prove more, soundly), **Calcite unsound** (C-number
below), **different** (on purpose), **missing** (issue 45 item), **fixed**
(was unsound here; fixed in issue 40).

### Functional dependencies

| Calcite | Behavior | Ours | Cases (keys) | Verdict |
|---|---|---|---|---|
| FD#getTableScanFD | Each key determines all columns | `ir/convert._table_knowledge`, `rules/uniqueness._functional_dependencies` | fd_inner_join, uk_nullable_unique_column | agrees; nullable UNIQUE is lax (different) |
| FD#getProjectionFD | Input FDs whose determinant is projected; computed column tied to single inputs | `uniqueness._row_preserving`, `_computed`, `_one_to_one` | fd_project_expression, fd_composite_key_expression, fd_complex_expressions | ours stronger (composite determinants, injective reverse) |
| FD#getProjectionFD | Identical expressions determine each other | `uniqueness._inverse`: an input column maps back to every output passing it | fd_identical_expressions | agrees (18) |
| FD#getProjectionFD | Columns determine literals | literal is `∅ -> c` | fd_constants | agrees, ours stronger |
| FD#getProjectionFD | Skips non-deterministic expressions | `convert._NOT_A_FUNCTION_OF_ITS_COLUMNS`; a key determines every column | fd_volatile_function | agrees; ours stronger on keys |
| FD#getProjectionFD | Any deterministic call is a function of its inputs, any type | identity comparison required | fd_double_cast_to_text | Calcite unsound (C2) |
| FD#getFilterFD | `col = col`, `IS NOT DISTINCT FROM` both ways | `uniqueness._equality_dependencies`, `equality._pins` | fd_equality_conditions, uk_filter_constant | agrees (11) |
| FD#getAggregateFD | Group FDs kept; group set determines aggregates | `_functional_dependencies`, `uniqueness._grouped` | fd_aggregate, fd_aggregate_double_pk | agrees; Calcite latently unsound (C8, C9) |
| FD#getJoinFD INNER | Both sides plus ON equalities | `uniqueness._join_dependencies` | fd_inner_join | agrees |
| FD#getJoinFD LEFT/RIGHT | Shared branch with INNER: ON equalities both ways | `_outer_on_dependencies`, `_join_side` (η, match status) | fd_left_join, fd_left_join_extra_conjunct, fd_right_join, fd_right_join_extra_conjunct | Calcite unsound (C1); extra-conjunct proof missing (20) |
| FD#getJoinFD FULL | No FDs | `_join_side` with η on each side | fd_full_join | ours stronger; keys missing (21) |
| FD#getFDs SetOp | No FDs | INTERSECT/EXCEPT keep the left input's, UNION all columns | fd_union_all | disjoint UNION ALL missing in both (22) |

### Unique keys and column uniqueness

| Calcite | Behavior | Ours | Cases (keys unless noted) | Verdict |
|---|---|---|---|---|
| UK/CU TableScan | Table keys, assumed non-null | `convert._table_knowledge` | uk_nullable_unique_column | different (nullable UNIQUE is lax) |
| UK#getUniqueKeys(Filter), CU#getConstantColumnSet | Constant columns drop out of keys; `IS NULL` counts as constant | `_equality_dependencies`; lax step needs a definite determinant | uk_filter_constant, uk_filter_constant_on_nullable_unique, uk_filter_is_null_on_nullable_unique, uk_filter_is_null_after_group_by, uk_constant_key, uk_aggregate_constant | agrees; Calcite unsound for IS NULL with ignoreNulls (C7) |
| UK#getProjectUniqueKeys, CU Project | Plain column refs, nullability-only casts | `_one_to_one`, `_row_preserving` | uk_projected_expression, uk_repeated_key_column | ours stronger (injective `+`, `-`) |
| UK Join, UK#getConstantJoinKeys | Keys through a side unique on the join keys; IS NOT DISTINCT FROM as an equi-key | `_join_dependencies`, `join_cardinality.many_to_one_join` | uk_join_constant_key_inner, uk_join_constant_key_left, uk_null_safe_join_on_key, uk_null_safe_join_on_nullable_unique, uk_join_non_key, uk_self_join_on_key, uk_inner_join_left_key_right_not_key | Calcite unsound (C3, C6); null-safe key agrees, only onto a non-null key (11) |
| CU Join | Unique when each side is; one-row INNER side; padded side not alone | same | uk_left_join_limit_1, uk_right_join_limit_1, uk_join_constant_columns, uk_join_singleton_aggregate, uk_full_join_one_row_inputs | agrees; ours stronger with ignoreNulls |
| UK/CU Aggregate, UK#getPassedThroughCols | Group set; MIN/MAX/ANY_VALUE pass a key through | `uniqueness._grouped` | uk_scalar_aggregate, uk_group_by_precise, uk_group_by_pass_through_aggregate, uk_composite_key_pass_through_aggregates, uk_grouping_sets | Calcite unsound for composite keys (C4); one-column pass-through missing (19) |
| UK/CU SetOp | UNION all columns; INTERSECT any input's key; EXCEPT the left's | `_functional_dependencies`, `_delegations` | uk_union_constant, uk_intersect_all, uk_intersect_all_right_key, uk_except_all | agrees on the left input; I2, U2 in issue 37 |
| UK/CU Sort | At most one row: empty key | Limit count ≤ 1 | uk_limit_1, uk_left_join_limit_1 | agrees |
| UK/CU Values | Literal distinctness | VALUES opaque | uk_values | missing (4) |
| UK/CU Correlate | UK: left keys always; CU: both sides | LATERAL opaque (issue 42) | uk_correlate_lateral, uk_correlate_lateral_many, uk_correlated_scalar_subquery, uk_uncorrelated_scalar_subquery | Calcite unsound in UK (C5); missing (7, issue 42) |

### Null rejection and expression nullability

| Calcite | Behavior | Ours | Cases (nulls unless noted) | Verdict |
|---|---|---|---|---|
| Strong#createPolicyMap, #isNull | ANY policy: comparisons, LIKE, NOT, arithmetic, CAST, TRIM, FLOOR, ITEM, GREATEST | `ir/model.strict_inputs`, `ir/convert._builtins` | strong_right_join_through_arithmetic, strong_nondeterministic_conjunct_still_rejects | agrees for modelled Builtins; TRIM, FLOOR, ITEM, NULLIF missing (13) |
| Strong#isNotTrue | AND: any part; OR: every part | `rules/nullability._null_rejected` | strong_and_rejects_padded_side_through_one_part, strong_or_with_a_preserved_part, strong_not_is_not_null_keeps_padding | agrees per column |
| RelOptUtil#simplifyJoin | Whole padded side NULL at once | per column | strong_or_of_two_padded_columns, strong_or_of_is_not_null_on_padded_columns, strong_coalesce_of_padded_columns | missing (9) |
| RelOptUtil#simplifyJoin | FULL becomes LEFT/RIGHT one side at a time | `nullability.Padding` keyed by join and side | strong_full_join_where_rejects_its_own_side; `calcite_audit` full_join_* | fixed (bug 3) |
| Strong#isNull CASE | NULL only if every value is | `nullability._case_sources`: padding kept only when every arm has it | `calcite_audit` case_* | fixed (bug 1) |
| Strong policy map | BETWEEN, non-strict operators | `_expression_sources`: padding kept only through strict arguments | `calcite_audit` between_*, strict_argument_* | fixed (bug 1) |
| Strong SEARCH | `x IN (1, NULL)` strict in x | `InList` | strong_search_with_null_value | agrees, ours stronger (ENUM) |
| Strong SOME/ALL; IS TRUE | SOME not strong; ALL true over no rows | quantified comparisons opaque | strong_any_subquery_is_never_true_on_null, strong_all_subquery_is_true_over_no_rows, strong_is_true_rejects | ALL agrees; ANY, IS TRUE missing in both (12) |
| PR#getPredicates(Filter) | Drops volatile conjuncts | keeps their null rejection | strong_nondeterministic_conjunct_still_rejects | ours stronger |
| Strong NOT_NULL; RelOptPredicateList#isEffectivelyNotNull | IS NULL, IS DISTINCT FROM, EXISTS never NULL | `nullability._expression_sources`: the IS family never NULL, `=` total where casts raise (`Equality.total`) | nullability_of_comparisons_and_is_family, nullability_of_an_equality_with_a_nullable_side, nullability_of_an_equality_without_ansi | agrees (10) |
| ReturnTypes ARG0_NULLABLE_IF_EMPTY, AGG_SUM | Grouped MIN/MAX/SUM non-null | MIN/MAX an `AnyAggregate`, SUM an `Accumulation` (where arithmetic is checked), `empty` when grouped by nothing | nullability_of_grouped_max_and_sum, nullability_of_scalar_max | agrees (14) |
| SqlStdOperatorTable#CONCAT; GREATEST_PG | `\|\|` AS_IS; PG greatest skips NULLs | opaque (dialects differ) | nullability_of_concat_operator, nullability_of_greatest_duckdb | agrees on rejection; nullability missing (13) |
| MOD/DIVIDE typing | Non-null | `convert._CHECKED_DIVISION_DIALECTS` | nullability_of_mod_by_zero_duckdb, nullability_of_mod_by_zero_postgres | different: Calcite's typing is wrong for DuckDB |
| (none) | Oracle `''` is NULL | `convert._EMPTY_STRING_IS_NULL_DIALECTS` | `calcite_audit` oracle_empty_string_is_null | fixed (bug 4) |

### Predicate inference

| Calcite | Behavior | Ours | Cases (nulls) | Verdict |
|---|---|---|---|---|
| PR#getPredicates(Project), #eqConstant | Literal columns are constants | `domain._expression_domain` | pullup_project_constants, pullup_project_is_null_of_constrained_column | agrees; casts of literals and `IS NULL` under `=` missing (16, 17) |
| RexUtil#predicateConstants | `IS NOT DISTINCT FROM c`, `IS NULL` are constants | `NullSafeEquality`: a domain bound, a constant, and rejection of the other side of a non-null one | strong_is_not_distinct_from_a_literal, strong_is_not_distinct_from_a_non_null_column, strong_is_not_distinct_from_a_nullable_column, strong_is_not_distinct_from_a_literal_without_ansi | agrees (11) |
| JoinConditionBasedPredicateInference INNER | Equivalence closure; rewrites any predicate across it, any type | `equality._equality_groups`, `_same_type`; `domain._meet` | pullup_inner_join_transitive_constant, pullup_inner_join_on_nullable_column, pullup_equivalence_under_a_collation | agrees; Calcite ignores collation (different) |
| inferPredicates SEMI | Left predicates plus inferred from the right | `domain._narrowings` | pullup_semi_join_from_in_subquery, pullup_semi_join_from_exists_on_constant, pullup_semi_join_equates_two_left_columns | agrees for IN; EXISTS constant and two-column equality missing (23) |
| inferPredicates ANTI/LEFT/RIGHT | Preserved side only; never padded-side or ON predicates | `equality._strict_conjuncts`, `domain._narrowings` | pullup_anti_join_keeps_left_only, pullup_left_join_preserved_on_conjunct, pullup_left_join_padded_on_constant, pullup_left_join_preserved_domain_below_the_join, pullup_left_join_preserved_domain_above_the_join | agrees; ours stronger (padded-side domains); E4 in issue 37 |
| inferPredicates FULL | Nothing | input domains survive | pullup_full_join_input_predicates | ours stronger |
| PR#getPredicates(Aggregate) | Group-key predicates; dropped under ROLLUP | grouping columns pass through | pullup_aggregate_rollup, pullup_aggregate_having_on_group_key | agrees on keys; ROLLUP, HAVING missing (1, 8) |
| PR#getPredicates(Union) | Common predicates plus OR of residuals | `domain._set_operation_domain` | pullup_union_common_and_disjunctive, pullup_union_with_null_literal, pullup_union_with_cast_null | agrees on domains; U2 in issue 37 |
| PR#getPredicates(Intersect, Minus) | All inputs; the left input | domains intersect; left input | pullup_intersect_takes_both_inputs, pullup_minus_takes_the_left_input | agrees; Calcite ignores collation |
| PR#getPredicates(Values) | Per-column `=` or Sarg | VALUES opaque | pullup_values | missing (4) |

## Soundness bugs found in sqlassert (fixed)

Each was PROVED with a DuckDB counterexample before issue 40; each has a
regression in `tests/calcite_audit.sqltest` (and the suggest one in
`tests/test_suggest_rewrites.py`).

1. **Padding through CASE and non-strict operators.** A `Padding` null
   source promises NULL on *every* padded row, so rejecting the column proves
   the join matched. `_case_sources` took the union of its arms' sources and
   `Call` passed padding through every total argument, so
   `CASE l.k WHEN 1 THEN 5 ELSE r.x END > 0` and `l.a BETWEEN r.x AND 10`
   "proved" a LEFT JOIN matched. Now padding survives a CASE only when every
   reachable arm has it, and a Call only through a strict argument; otherwise
   it becomes organic. Found from `Strong.isNull`. The oracle missed the
   BETWEEN form: it needs `l.a > 10`.
2. **EXISTS over a scalar aggregate** lowered as a semi join on the body's
   WHERE, though the body always returns one row, so `WHERE EXISTS (SELECT
   count(*) FROM u WHERE u.id = e.user_id)` "proved" `e.user_id` non-null
   and a foreign key. `convert._subquery_join` now refuses it (from
   `sub-query.iq`).
3. **FULL JOIN padding shared by both sides.** `Padding` was keyed by the
   join only, so a WHERE rejecting one side's column removed the other
   side's padding too. It also reached `suggest`: a WHERE on the left of a
   FULL JOIN chained after a LEFT JOIN suggested rewriting the LEFT JOIN as
   JOIN, which drops rows. `Padding` now carries the side; the INNER JOIN
   chain is keyed by join and side (from `outer.iq`, `RelOptUtil.simplifyJoin`).
4. **Oracle's `''`.** Oracle reads a zero-length string as NULL; `''` lowered
   as a non-null Constant in every dialect. It now lowers as NULL in Oracle.
5. **Set operation chains.** `sql_parse` retried set operations after a
   Select's trailing markers even for a set operation's right operand, so
   `t UNION u UNION ALL v` parsed as `t UNION (u UNION ALL v)` and proved
   UNIQUE falsely. The retry is now skipped for a right operand, and chains
   are rebalanced so INTERSECT binds tighter than UNION and EXCEPT, as in
   standard SQL and DuckDB (Oracle reads them left to right). From
   `set-op.iq` (CALCITE-7901).

Not found: any unsound FD, key or uniqueness rule; the shared
INNER/LEFT/RIGHT FD branch that worried the [source survey](calcite-notes.md)
is unsound in Calcite (C1), not here.

## Calcite answers we do not adopt

Read from source; each counterexample run in DuckDB 1.5.2.

- **C1. Outer-join ON equalities as FDs both ways** (`FD#getJoinFD`, the
  shared INNER/LEFT/RIGHT branch). With `ON e.deptno = d.deptno AND e.sal >
  1000`, rows (10, 10) and (10, NULL) both appear: `e.deptno -> d.deptno`
  fails, and `AggregateRemoveDuplicateKeysRule` on `GROUP BY e.deptno,
  d.deptno` would merge them. Padded to preserved is lax only: `GROUP BY
  d.deptno, e.deptno` would merge (NULL, 50) and (NULL, 60).
- **C2. Comparison types ignored.** `d -> CAST(d AS VARCHAR)` for DOUBLE:
  -0.0 = 0.0, but their texts differ.
- **C3. `UK#getConstantJoinKeys` on the preserved side** of LEFT/RIGHT
  joins: `ck(k1, k2) LEFT JOIN (SELECT 5 AS c) ON k1 = c` is claimed unique
  on k2 over rows (1, 1), (5, 1). CALCITE-6727 fixed column uniqueness, not
  unique keys.
- **C4. Pass-through aggregates over a composite key**: `(min(key1),
  max(key2)) GROUP BY value1` repeats (a, z). Upstream
  `testCompositeKeysAggregationUniqueKeys` asserts the unsound answer.
- **C5. `UK#getUniqueKeys(Correlate)`** returns the left keys whatever the
  lateral side returns; a LATERAL with several rows repeats them.
- **C6. IS NOT DISTINCT FROM join keys with `ignoreNulls=true`** onto a
  ROLLUP's NULL group repeat the left key.
- **C7. `IS NULL` as a constant with `ignoreNulls=true`**: after `GROUPING
  SETS ((a, b), (a))`, `WHERE b IS NULL` keeps two rows with the same a.
- **C8. Aggregate handlers assume the group set is {0..k-1}**
  (`groupSet.length()`); safe for SQL-converted plans, unsound after
  `AggregateProjectMergeRule` reorders it.
- **C9. Group set determines aggregates under grouping sets** holds only
  laxly once ROLLUP rows put NULL in group columns.
- **Weaker answers**: `empno -> rand()` false, FULL join `empno -> deptno`
  false, no constants or injective reverses, no set-operation FDs. Not
  adopted; we prove these.
- **Type-blind rewriting** across `=` under a NOCASE collation, MOD/DIVIDE
  typed non-null, GREATEST as strict, volatile conjuncts dropped: see
  Contracts.

## The `.iq` files

Calcite's 65 Quidem files are execution tests: SQL and expected rows,
sometimes a plan; they state no Properties. Verdict: **mostly irrelevant or
duplicate**. About 50 files test dialects, functions, types or DDL. Of
~390 `!plan` cases, most are decorrelation and rule-firing regressions; the
few key- or nullability-driven removals are covered by our tests and
`calcite_audit_keys.sqltest`. Their value was boundary probing: they found
bugs 2, 3 and 5 and several missing proofs. 25 cases migrated from 10 files.

Fixture: `scott` (EMP, DEPT, BONUS, SALGRADE) is a CloneSchema over the
external `scott-data-hsqldb` jar. `ArrayTable.getStatistic` also makes every
all-distinct column a key (ENAME, DNAME, LOC), from data; plans relying on
those (e.g. `sub-query.iq:8892`) were not used. `post` and `blank`
(`CalciteAssert.java:957-1046`) declare no keys.

| File (core unless noted) | !ok/!plan | Verdict | Why | Issue 11 corpus |
|---|---|---|---|---|
| agg.iq | 270/45 | migrate | GROUP BY (), grouping sets, ROLLUP nullability, grouped SUM, HAVING | y |
| join.iq | 64/17 | migrate | one-row derived tables, inner ON removing padding, RIGHT JOIN USING | y |
| misc.iq | 187/26 | migrate (2) | LEFT JOIN ON TRUE to a scalar aggregate; DISTINCT/GROUP BY boundary | y |
| new-decorr.iq | 21/8 | migrate (1) | EXISTS over a RIGHT JOIN of a correlated table | y |
| outer.iq | 21/1 | migrate | RIGHT/FULL JOIN with WHERE; bug 3 | y |
| planner.iq | 27/24 | migrate (1) | LEFT JOIN … IS NULL as an anti join | y |
| qualify.iq | 6/0 | migrate (1) | QUALIFY over GROUP BY | y |
| scalar.iq | 23/4 | migrate (1) | correlated count(*) never NULL | y |
| set-op.iq | 19/2 | migrate | chain associativity; bug 5 | y |
| sub-query.iq | 465/173 | duplicate | decorrelation plans; UNIQUE-predicate cases covered; bug 2 came from here | y |
| blank.iq | 16/5 | duplicate | NOT IN with NULL, correlated EXISTS | y |
| conditions.iq | 33/6 | duplicate | AND/OR/NOT null rejection | y |
| hep.iq | 11/10 | duplicate | UNION/EXCEPT keys, EXISTS | y |
| interpreter.iq | 8/2 | duplicate | VALUES set operations (VALUES not lowered) | y |
| join-agg-transpose.iq | 3/2 | duplicate | aggregate joined on its key | y |
| some.iq | 51/3 | duplicate | ANY/ALL opaque; empty-subquery NULL cases covered | y |
| sort.iq | 34/5 | irrelevant | NULL collation, sort removal by collation | y |
| agg-reduce, aliasing, asof, cast, lateral, recursive_queries, row-equality, winagg | — | irrelevant | functions, alias resolution, unsupported constructs (issue 42) | y |
| babel: big-query, postgresql, redshift, select, spark | — | irrelevant | dialect functions and syntax | y |
| cast-with-format, dual-table-query, dummy, fetch, functions, lambda, map-equality, match, measure, measure-paper, offset, operator, pipe, pivot, sequence, spatial, stream, struct, tablesample, unnest, unsigned, variant, within-distinct; babel: dummy, within-group-over; plus: all four; server: all six | — | irrelevant | casts, functions, types, streams, DDL | n |
