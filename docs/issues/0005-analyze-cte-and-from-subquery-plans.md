# 5. Analyze CTE and FROM-subquery plans

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-24
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/5

## Parent

#1

## What to build

Lower CTEs and FROM subqueries recursively into relational subplans so their preserved Unique Sets can prove assertions in an outer query. Subquery syntax must not become a semantic operator of its own, and unsupported scalar or correlated forms must remain explicit and conservative.

## Acceptance criteria

- [ ] A CTE that preserves a Unique Set through supported operations can prove a Unique Join Assertion in the Root Select.
- [ ] A FROM subquery that preserves a Unique Set can prove a Unique Join Assertion in its outer query.
- [ ] Nested CTE and FROM-subquery combinations resolve bound columns and Relation Instances correctly.
- [ ] Projection aliases inside a CTE or FROM subquery map Unique Sets to the correct outer columns.
- [ ] A CTE or FROM subquery that drops or obscures a required key member returns UNKNOWN.
- [ ] Scalar and correlated subqueries are represented as unsupported expression semantics and do not produce guessed proofs.
- [ ] Provenance in the returned Report identifies the outer assertion and the relevant nested SQL origin on a best-effort basis.
- [ ] Automated tests through `analyze` include happy paths for CTE and FROM-subquery propagation and unhappy paths for lost keys and unsupported scalar/correlated subqueries.

## Blocked by

- #4

---

**Comment by polvoazul on 2026-08-24:**

Implemented on the \`clingo\` branch:
- 2ea2df6 Document Clingo variable-naming conventions in the rule files
- 557a001 Lower CTEs and FROM subqueries recursively into relational subplans (#5)

\`IrParser\` now lowers CTE bodies and FROM subqueries recursively (table → CTE → subquery, any depth), instead of always falling back to an opaque relation. Covers all the acceptance criteria: CTE/subquery propagation proving outer joins, nested CTE/subquery combinations, projection-alias mapping, dropped/obscured key members returning UNKNOWN, and scalar/correlated subqueries staying explicitly unsupported rather than guessed. A JOIN/GROUP BY/DISTINCT/QUALIFY/nested-WITH inside a CTE or subquery body remains out of scope and falls back to OpaqueRelation, with an asserted join inside such a body surfaced via the existing unanalyzed-assertion diagnostic rather than silently dropped. New tests in \`tests/test_analyze_cte_and_subquery.py\`; full suite (132 tests) passing.
