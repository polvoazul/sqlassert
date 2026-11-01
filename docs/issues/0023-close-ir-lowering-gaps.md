# 23. Close IR lowering gaps

- **State:** open
- **Labels:** epic
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md` (IR Limitations), README Ideas

What lowering ([`ir/convert.py`](../../sqlassert/ir/convert.py)) loses today. Each item
caps what the rules can prove, however good the rules get. Feature-level gaps
live in [`docs/mvp-scope.md`](../mvp-scope.md#limitations-and-future-features).

- [x] `Filter` carries no predicate. `WHERE k = 5` cannot pin `k` (so `(k, id)`
  cannot reduce to `(id)`), and `WHERE x IS NOT NULL` cannot establish non-null `x`.
  Done in issue 24: `Filter.conjuncts` recognizes `=`, `IS NULL`, `IS NOT NULL`.
- [x] Join predicates keep only top-level `=` conjuncts. `OR`, ranges,
  `IS NOT DISTINCT FROM`, and equalities over expressions are dropped.
  Done in issue 24: `Join.conjuncts` holds the whole condition, recognized as
  in `Filter`; the rest are opaque conjuncts the rules read only as "may
  depend on anything". Ranges, `LIKE`, and `OR` are still not reasoned about.
- [ ] Scalar expressions are column, literal, `NULL`, or opaque. `CASE`,
  `COALESCE`, `CAST`, and function calls are opaque, so no injectivity
  (`id + 0`, lossless casts) and no non-nullness (`coalesce(x, 0)`, `count(*)`).
- [ ] `SetOperation` output columns refer to the left arm only; the right arm's
  columns are not tracked. (`ALL` vs distinct is now distinguished:
  `SetOperation.distinct`, issue 24.)
- [ ] Unique-join analysis models only `inner` and `left`; `right`, `full`,
  `semi`, `anti`, `cross`, and `LATERAL`/`UNNEST` sources are not modelled.
- [ ] Windows: only `QUALIFY row_number() over (partition by ...) = 1` is
  recognized; other window functions are opaque. Planned rules:
  [`docs/functional-dependencies.md`](../functional-dependencies.md#window-functions).
- [ ] Recursive CTEs and views become `RecursiveRelation` (opaque). Caveat:
  a recursive relation is a genuine fixpoint, which the memoized engine and its
  strata cannot compute; supporting it needs bespoke fixpoint machinery.
- [ ] Correlated subqueries (`EXISTS (… WHERE i.x = o.y)`) are not lowered.
  When they are, lower them as a dependent join (apply) over the outer input,
  so the IR stays a DAG; a subquery that refers to its outer relation directly
  would create a cycle across relations that the strata cannot order
  ([Strata](../functional-dependencies.md#strata)).
- [ ] Only `CREATE TABLE` and `CREATE VIEW` are lowered; indexes, `ALTER TABLE`
  constraints, and other DDL are ignored.
- [ ] Tables without a `CREATE` in the program have an incomplete schema, so an
  unqualified column cannot be resolved when several sources are in scope.
  Needs catalog input to `IrParser`, not just knowledge for the engine.
- [x] No functional dependencies (`X → Y`). Done in issue 24, except
  deterministic and injective expressions (still opaque). Keys, equality predicates,
  deterministic expressions, and `GROUP BY` all create them; without them,
  uniqueness only flows through columns that pass through unchanged. Theory
  and planned opportunities: [`docs/functional-dependencies.md`](../functional-dependencies.md).

## Folded in from `TODO.md` Later Ideas and the README Ideas list

- [ ] Non-nullness from predicates and expressions: `WHERE x IS NOT NULL`,
  `WHERE x = 5`, and inner-join equalities imply non-null `x`; `count(...)` is
  never null. Predicates done in issue 24 (null sources); `count(...)` remains.
- [ ] A `UNION`/`INTERSECT`/`EXCEPT` without `ALL` earns a Unique Set over all
  of its output columns, the same way `SELECT DISTINCT` does. Derive it from
  the leftmost arm's output columns, and lower set-operation bodies when they
  appear inside a view, CTE, or subquery. The Unique Set is done in issue 24
  (view and CTE bodies already lower); a set operation inside a FROM subquery
  is still opaque.
- [ ] Column lineage, column unique status.
- [ ] General window modeling: represent window expressions and their filtering
  as separate `Window` and `Filter` operations, with the engine deriving
  uniqueness from recognized shapes. Until then, keep
  `QUALIFY row_number() over (partition by ...) = 1` as the explicit special case.
