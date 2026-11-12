# 42. Lowering support v2

- **State:** not planned
- **Priority:** P4 (not planned): Not in Paulley. The rules would be our own
  design, with no literature to test against.
- **Theme:** lowering
- **Labels:** epic, P4
- **Created:** 2026-11-08
- **Split from:** [23](done/0023-close-ir-lowering-gaps.md)'s "Not planned" list

Lowering gaps that are deliberately out of scope for now: Paulley does not
cover them, or no proof needs them. Each stays opaque, which is sound: nothing
is proved through it, and a marker inside it is reported
`unanalyzed-assertion`. Revisit when there is demand.

- [ ] **Window functions.** Only `QUALIFY row_number() over (partition by
  ...) = 1` is recognized. General modelling would add `Window` and `Filter`
  operations, with the engine deriving uniqueness from recognized shapes.
  Planned rules:
  [`docs/functional-dependencies.md`](../functional-dependencies.md#window-functions).
  Would also allow the INNER JOIN suggestion across CTEs (issue 33's
  out-of-scope part).
- [ ] **Recursive CTEs and views** are `RecursiveRelation` (opaque): a
  recursive relation is a genuine fixpoint, which the memoized engine and its
  strata cannot compute.
- [ ] **`LATERAL` and `UNNEST` sources** are opaque.
- [ ] **Column lineage** and per-column unique status as a first-class output.
- [ ] **`ALTER TABLE ... ADD COLUMN`** is `unsupported-statement`: only
  migration-style programs use it, and the statement's diagnostic already
  says so.
- [ ] **Scalar subqueries in the select list of a grouped, `DISTINCT` or
  `QUALIFY` Select** stay `OpaqueExpression`. Issue 38 lowers them only in
  WHERE and in a plain select list: in a grouped Select the subquery is
  evaluated per group, after grouping, so it would join onto the Aggregate's
  output rather than its input.
- [ ] **Scalar subqueries in a join's `ON`** stay `OpaqueExpression`: the
  conjunct only filters and binds no column. Lowering one would join the
  subquery's body before the join it sits in.
- [ ] **`GROUPING SETS`, `ROLLUP` and `CUBE`** are opaque (from issue 45,
  item 8). Their subtotal rows hold NULL in the rolled-up columns, so the
  grouping columns keep their Enumerated Domains (NULL is outside one), and
  nested sets such as `((ename), (ename, job))` keep `(ename, job)` a Unique
  Set: a subtotal row's `job` is NULL and a data row's never is. Mostly
  reporting SQL; an assertion that needs one says it is not supported yet.
- [x] **Collated text**: won't do. sqlassert supports each dialect's
  default collation only; a `COLLATE` is opaque and warns
  ([49](0049-collation-aware-properties.md), not planned).
