# 45. Close the Calcite audit's missing proofs

- **State:** open
- **Priority:** P2 (ready): With #37: what the #40 audit found missing. None is
  unsound. Its lowering items lose proofs on common SQL, so do those first.
- **Theme:** soundness
- **Labels:** ready-for-agent
- **Created:** 2026-11-08
- **Related:** [40: Calcite audit](done/0040-audit-inference-against-calcite.md), [37: Paulley audit gaps](0037-close-the-paulley-audit-missing-rules.md), [42: lowering support v2](0042-lowering-support-v2.md)

## Why

The [Calcite audit](../calcite-audit.md) found Properties that hold on every
instance but that sqlassert leaves UNKNOWN. None is unsound. Each has a case
in `tests/calcite_audit_*.sqltest` annotated UNKNOWN with a "missing proof"
comment. Turn that annotation into PROVED and keep the boundary cases next to
it UNKNOWN. Gaps already filed elsewhere are not repeated: U2 and I2 (set
operations), E4 (outer-join ON domains) and F2/F4 (full join ON dependencies)
are in issue 37. LATERAL is in issue 42.

## What to do

Roughly in this order: the lowering gaps lose proofs on common SQL.

### Lowering

1. ~~**GROUP BY with HAVING** is not lowered,~~ Done: HAVING is a Filter of the groups; a part written as a select item reads its column, any other conjunct is opaque (`convert._lower_having`). Was: so the grouped query loses its key
   and its domains. Lower HAVING as a Filter above the Aggregate.
   `iq: having_keeps_the_grouping_key`, `nulls: pullup_aggregate_having_on_group_key`.
2. ~~**`GROUP BY ()`** is one row, like a scalar aggregate.~~ Done: the
   empty grouping set is dropped before lowering (`sql_parse.grouping`).
   `iq: group_by_empty_set_is_one_row`.
3. ~~**An unqualified USING column** is not resolved:~~ Done: after its join a USING column is one column, the left input's (INNER, LEFT), the right's (RIGHT) or their coalesce (FULL), which an unqualified name and a later USING read (`convert._merge_using`). Was: in a RIGHT or FULL join
   it is `coalesce(left, right)`, and even an INNER join loses its domain.
   `iq: right_join_using_column_is_the_preserved_side_s`.
4. ~~**VALUES in FROM** lowers opaque, silently.~~ Done: its literals are Knowledge of its columns (non-null, Enumerated Domain up to 100 rows, lax Unique Sets by `_may_coincide`; `convert._lower_values`). Was: Its literals give domains,
   nullability and (null-equal) uniqueness.
   `keys: uk_values`, `nulls: pullup_values`.
5. ~~**QUALIFY over GROUP BY** is not recognized as QualifyByPartition.~~ Done: `row_number() OVER (PARTITION BY k...) = 1` over the groups (after HAVING) is a QualifyByPartition when every key is a selected item and no other item is aliased to a bare key; any other QUALIFY over groups or DISTINCT, which was ignored (unsound: a join onto it was EXACTLY ONE), is an opaque Filter (`convert._qualified`).
   `iq: qualify_over_group_by_keeps_one_row_per_partition`.
6. ~~**A parenthesized join as a join's right input** gives
   `unanalyzed-assertion`:~~ Done: an unaliased parenthesized join, anywhere in FROM, lowers as a join input whose sources keep their names; an aliased one stays opaque (DuckDB renames its repeated names) (`convert._nested_join`). A lax step's determinant is definite also when it is NULL only on rows a join pads that also pads a requested column (`uniqueness._padded_with`), which the outer UNIQUE(empno) needs. Was: `dept d LEFT JOIN (emp e /**ASSERT MANY TO ONE**/ LEFT JOIN emp c USING (empno)) ON ...`.
7. ~~**A correlated scalar subquery whose body aggregates** stays opaque:~~ Done for one aggregate call correlated by same-typed equalities: grouped by the key and left joined, count as `coalesce(count, 0)` (`convert._aggregate_subquery_join`). Was:
   `count(*)` of it is never NULL, and `x = (SELECT max(...) ... WHERE corr)`
   pins one row per correlation key.
   `iq: correlated_scalar_count_is_never_null`, `keys: uk_correlated_scalar_subquery`.
8. ~~ROLLUP and GROUPING SETS lower opaque (low priority):~~ Moved to issue 42 (P4). Was: their grouping
   columns keep their domains (NULL is outside a domain).
   `keys: uk_grouping_sets`, `nulls: pullup_aggregate_rollup`.

### Nullability and null rejection

9. ~~**A whole padded side through OR or COALESCE**~~ Done: a conjunct rejects each padding on whose rows it is never TRUE: NULL there through strict arguments, coalesce or CASE (`_null_with`), `IS NOT NULL` of such an expression, an OR when every part rejects it; a later ON only on the side it drops unmatched rows of (`nullability._rejected_paddings`, `_on_rejected_paddings`). The INNER JOIN rewrite reads the same (`rewrites._inner_join`). Was: `WHERE e.sal > 0 OR
   e.comm > 0`, or `coalesce(e.sal, e.comm) > 0`, over two columns the same
   join pads, proves that join matched. Compute rejection per padding, not
   only per column. `nulls: strong_or_of_two_padded_columns`,
   `strong_or_of_is_not_null_on_padded_columns`, `strong_coalesce_of_padded_columns`.
10. ~~**Comparisons and the IS family in a select list**:~~ Done: the IS family (`IS NOT DISTINCT FROM` included) is never NULL, `IS DISTINCT FROM` and `EXISTS` are never-null Calls, and `a = b` is non-null when its sides are where a failed cast raises and neither side is a composite (`nullability._expression_sources`, `Equality.total`, `convert._checked_comparison`). Was: `x IS NULL`,
    `x IS DISTINCT FROM y` and `EXISTS (...)` are never NULL; `a = b` is
    non-null when `a` and `b` are. `nulls: nullability_of_comparisons_and_is_family`.
11. ~~**IS NOT DISTINCT FROM**~~ Done: an `ir.NullSafeEquality`, which bounds a domain and pins a constant like `=` (`domain._conjunct_bound`, `uniqueness._equality_dependencies`), merges Equality Groups and gives dependencies both ways between columns, rejects one side when the other is non-null where a failed cast raises (`nullability._null_rejected`), and binds join columns that are definite only where proved non-null (`join_cardinality._bound_columns`). Was: against a literal it rejects NULL and pins a
    constant; between two columns it is a strict equality both ways (an
    Equality Group, FDs); as a join key it reaches a Non-Null Unique Set, and
    never a nullable one. `nulls: strong_is_not_distinct_from_a_literal`,
    `keys: fd_equality_conditions`, `uk_null_safe_join_on_key`,
    `uk_null_safe_join_on_nullable_unique` (boundary);
    `not_null_rejection: a_null_safe_equality_to_a_literal_is_not_inferred`.
12. ~~**Never TRUE on NULL, without being strict**:~~ Done: `x op ANY (...)`, `IS TRUE` and `IS FALSE` are Builtins whose operand is `rejecting` (a NULL there makes the value never TRUE, not NULL), so a conjunct rejects that operand's strict columns and paddings (`nullability._null_rejected`, `_conjunct_paddings`); the value stays nullable for ANY and never NULL for the IS forms. ALL and `IS NOT FALSE` reject nothing. Was: `x > ANY (subquery)` and
    `(x > 0) IS TRUE` reject x. This is Calcite's `Strong.isNotTrue`, weaker
    than strictness. `nulls: strong_any_subquery_is_never_true_on_null`, `strong_is_true_rejects`.
13. ~~More strict Builtins~~ Done: `lower`, `upper`, `length`, `trim` (not total where '' is NULL), `abs`, `floor`, `ceil` (total where CAST is checked) and `nullif` (strict in its first argument) are Builtins, certified per dialect; a call with more arguments (`trim(x, chars)`, `floor(x, 2)`) stays opaque. `||` as each dialect treats NULL. A subscript `xs[i]` is strict in `xs` (`subscript_strict`) and `greatest`/`least` non-null when any argument is (`skips_null`), in DuckDB and PostgreSQL only, where certified; other dialects leave them opaque (MySQL's strict greatest is not certified). Was: **More strict Builtins**: NULLIF (first argument), trim, floor/ceil,
    upper/lower, abs, length, list indexing. Per dialect: `||` is non-null
    when its operands are in DuckDB and Postgres (not Oracle, not MySQL), and
    greatest/least are non-null when any argument is in DuckDB and Postgres.
    `nulls: strong_nullif_first_argument`, `strong_trim_and_floor`,
    `strong_item_of_a_list`, `nullability_of_concat_operator`, `nullability_of_greatest_duckdb`.
14. ~~**Grouped MIN/MAX/SUM of a non-null argument**~~ Done for a select item that is the bare call: MIN/MAX are an `AnyAggregate` of their argument, SUM an `ir.Accumulation` where arithmetic is checked, both `empty` (NULL over no rows) when grouped by nothing; the scalar bare column is one too (`convert._aggregate_output_columns`). Was: are non-null (no FILTER);
    MIN/MAX keep the argument's domain. A scalar one stays nullable.
    `nulls: nullability_of_grouped_max_and_sum`, `nullability_of_scalar_max` (boundary),
    `iq: grouped_sum_of_a_non_null_column_is_non_null`.
15. ~~**Rejection back through a computed column**:~~ Done: a rejected column rejects every column of its own row it is strict in, down through Projects, Aliases, Filters and joins, and through views and CTEs per occurrence, never an aggregate or set operation (`nullability._row_lineage`). Was: `WHERE q.ms > 0` with
    `ms = mgr + sal` rejects NULL in mgr. `nulls: strong_rejected_computed_column`.
16. ~~**Domains of strict Calls over literals**:~~ Done: a strict Call with an always-NULL strict argument has the empty domain (`domain._expression_domain`), and a CAST of a plain non-negative integer literal (number or string, no sign, leading zero or space) to a signed integer type within its range is that constant (`convert._cast_integer_literal`); `CAST('1.5' AS INTEGER)` differs by dialect and stays opaque. Was: `CAST(NULL AS INTEGER)` has
    the empty domain, `CAST('1' AS INTEGER)` is `{1}`.
    `nulls: pullup_union_with_cast_null`, `pullup_project_constants`.
17. ~~`comm IS NULL` under `comm = 2` is FALSE, a constant.~~ Done: `IS [NOT] NULL` of a non-null operand, or of an always-NULL one, has a one-value domain (`domain._expression_domain`).
    `nulls: pullup_project_is_null_of_constrained_column`.

### Dependencies and keys

18. ~~**One input column projected twice**~~ Done: an input attribute maps back to every output carrying it (`uniqueness._inverse`). Was: `SELECT deptno, deptno AS deptno2`
    proves `deptno2 -> deptno`. `_row_preserving` and `_grouped` keep one
    output per input column (`back`). `keys: fd_identical_expressions`.
19. **MIN/MAX/ANY_VALUE of a one-column Unique Set** grouped by anything is
    unique, groups being disjoint. Never for a composite key: Calcite's
    version of this is unsound (see the audit, C4).
    `keys: uk_group_by_pass_through_aggregate`, `uk_composite_key_pass_through_aggregates` (boundary).
20. **Pure conjuncts on the preserved side of an outer join's ON** fix the
    match status by the columns they read (Paulley L6), not the whole row:
    `ON e.deptno = d.deptno AND e.sal > 1000` proves `e_deptno, sal -> d_deptno`.
    `keys: fd_left_join_extra_conjunct`, `fd_right_join_extra_conjunct`.
21. Moved to issue 37 (with F2/F4). Was: **A FULL join that is Many To One** keeps the left side's key laxly: the
    left Row Identity determines the right one. Pair with F2/F4 (issue 37).
    `keys: fd_full_join`.
22. Moved to issue 37 (with U2). Was: **UNION ALL with disjoint branches** (`WHERE deptno = 10` / `= 20`) keeps
    each branch's FDs. Pair with U2 (issue 37). `keys: fd_union_all`.
23. **Semi joins**: a constant in an EXISTS ON pins the left column
    (`d.deptno = e.mgr AND d.deptno = 10`); two left columns equal to one
    right column are equal. `nulls: pullup_semi_join_from_exists_on_constant`,
    `pullup_semi_join_equates_two_left_columns`.
24. ~~**EXACTLY ONE onto a scalar aggregate** with ON TRUE or CROSS JOIN.~~ Done: an ON always TRUE matches a right input that always has a row, an aggregate grouped by nothing through Aliases, Projects and DISTINCT, not HAVING (`exactly_one._has_a_row`).
    `iq: left_join_on_true_to_a_scalar_aggregate_matches_exactly_once`.

Consider a warning Diagnostic when a construct lowers opaque silently
(VALUES, GROUPING SETS, LATERAL, correlated aggregate subqueries), so a user
knows why an assertion stays UNKNOWN.

## Acceptance criteria

- [ ] Each item's cases are PROVED, its boundary cases stay UNKNOWN.
- [ ] `pytest --soundness-oracle` reports 0 counterexamples.
- [ ] `docs/calcite-audit.md` rows move from "missing" to "agrees".
