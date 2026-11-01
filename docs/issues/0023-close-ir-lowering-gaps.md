# 23. Close IR lowering gaps

- **State:** open
- **Labels:** epic
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md` (IR Limitations), README Ideas

What lowering ([`ir/convert.py`](../../sqlassert/ir/convert.py)) loses today. Each item
caps what the rules can prove, however good the rules get. Feature-level gaps
live in [`docs/mvp-scope.md`](../mvp-scope.md#limitations-and-future-features).

- [ ] `Filter` carries no predicate. `WHERE k = 5` cannot pin `k` (so `(k, id)`
  cannot reduce to `(id)`), and `WHERE x IS NOT NULL` cannot establish non-null `x`.
- [ ] Join predicates keep only top-level `=` conjuncts. `OR`, ranges,
  `IS NOT DISTINCT FROM`, and equalities over expressions are dropped.
- [ ] Scalar expressions are column, literal, `NULL`, or opaque. `CASE`,
  `COALESCE`, `CAST`, and function calls are opaque, so no injectivity
  (`id + 0`, lossless casts) and no non-nullness (`coalesce(x, 0)`, `count(*)`).
- [ ] `SetOperation` output columns refer to the left arm only; the right arm's
  columns are not tracked and `ALL` vs distinct is not distinguished.
- [ ] Unique-join analysis models only `inner` and `left`; `right`, `full`,
  `semi`, `anti`, `cross`, and `LATERAL`/`UNNEST` sources are not modelled.
- [ ] Windows: only `QUALIFY row_number() over (partition by ...) = 1` is
  recognized; other window functions are opaque. Planned rules:
  [`docs/functional-dependencies.md`](../functional-dependencies.md#window-functions).
- [ ] Recursive CTEs and views become `RecursiveRelation` (opaque).
- [ ] Only `CREATE TABLE` and `CREATE VIEW` are lowered; indexes, `ALTER TABLE`
  constraints, and other DDL are ignored.
- [ ] Tables without a `CREATE` in the program have an incomplete schema, so an
  unqualified column cannot be resolved when several sources are in scope.
  Needs catalog input to `IrParser`, not just knowledge for the engine.
- [ ] No functional dependencies (`X → Y`). Keys, equality predicates,
  deterministic expressions, and `GROUP BY` all create them; without them,
  uniqueness only flows through columns that pass through unchanged. Theory
  and planned opportunities: [`docs/functional-dependencies.md`](../functional-dependencies.md).

## Folded in from `TODO.md` Later Ideas and the README Ideas list

- [ ] Non-nullness from predicates and expressions: `WHERE x IS NOT NULL`,
  `WHERE x = 5`, and inner-join equalities imply non-null `x`; `count(...)` is
  never null. Needs `Filter` to carry its conjuncts (first item above).
- [ ] A `UNION`/`INTERSECT`/`EXCEPT` without `ALL` earns a Unique Set over all
  of its output columns, the same way `SELECT DISTINCT` does. Derive it from
  the leftmost arm's output columns, and lower set-operation bodies when they
  appear inside a view, CTE, or subquery.
- [ ] Column lineage, column unique status.
- [ ] General window modeling: represent window expressions and their filtering
  as separate `Window` and `Filter` operations, with the engine deriving
  uniqueness from recognized shapes. Until then, keep
  `QUALIFY row_number() over (partition by ...) = 1` as the explicit special case.
