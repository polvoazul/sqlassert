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
- [ ] Scalar expressions are column, literal, `NULL`, `coalesce`, simple
  `CASE` (`ir.SimpleCase`, issue 18), `count(...)` (`ir.Count`), or opaque.
  Searched `CASE`, `CAST`, and other function calls are opaque, so no
  injectivity (`id + 0`, lossless casts).
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
- [x] Only `CREATE TABLE` and `CREATE VIEW` are lowered; indexes, `ALTER TABLE`
  constraints, and other DDL are ignored. Done: `ALTER TABLE ... ADD PRIMARY
  KEY / UNIQUE / FOREIGN KEY` (named or not), `ALTER COLUMN ... SET NOT NULL`,
  and `CREATE UNIQUE INDEX` over plain columns become the Knowledge their
  `CREATE TABLE` forms state (and a named `CONSTRAINT` in `CREATE TABLE`,
  previously ignored, now counts too). A statement naming a table not created
  earlier in the program is a `constraint-without-table` Diagnostic. Any other
  `ALTER TABLE` action (ADD/DROP COLUMN, DROP NOT NULL, RENAME, CHECK, ...)
  makes the whole statement `unsupported-statement`: it could invalidate
  what is known. A partial or expression unique index is
  `unsupported-create-statement` rather than silent: it is uniqueness the
  author wrote and sqlassert drops, the same reasoning as for markers. A
  plain index is accepted silently: it states nothing. Still open:
  `ADD COLUMN`; a unique expression index over deterministic expressions does
  imply its columns are unique, which is not used.
- [ ] Tables without a `CREATE` in the program have an incomplete schema, so an
  unqualified column cannot be resolved when several sources are in scope.
  Today this is silently UNKNOWN. Per
  [ADR 0005](../adr/0005-analyze-sql-without-a-connection.md):
  - **Without a connection:** emit a warning Diagnostic naming the column, the
    candidate sources, and the fix (qualify it, or `CREATE` the tables), and
    treat facts that depend on that column as unknown.
  - **With a connection** (`analyze_database`): give `IrParser` the catalog's
    column lists, so the column resolves to its one real source and is
    analyzed normally.
  - Tests: the same SQL warns offline and proves with an in-memory DuckDB
    catalog; adding the connection never turns PROVED into UNKNOWN.
- [x] No functional dependencies (`X → Y`). Done in issue 24, except
  deterministic and injective expressions (still opaque). Keys, equality predicates,
  deterministic expressions, and `GROUP BY` all create them; without them,
  uniqueness only flows through columns that pass through unchanged. Theory
  and planned opportunities: [`docs/functional-dependencies.md`](../functional-dependencies.md).

## Folded in from `TODO.md` Later Ideas and the README Ideas list

- [x] Non-nullness from predicates and expressions: `WHERE x IS NOT NULL`,
  `WHERE x = 5`, and inner-join equalities imply non-null `x`; `count(...)` is
  never null. Predicates done in issue 24 (null sources); `count(...)` lowers
  to `ir.Count`, which has no Null Source, with or without `GROUP BY`. Only a
  bare `count(...)` counts: one under a window, `FILTER`, or another function
  stays opaque.
- [x] A `UNION`/`INTERSECT`/`EXCEPT` without `ALL` earns a Unique Set over all
  of its output columns, the same way `SELECT DISTINCT` does. Derive it from
  the leftmost arm's output columns, and lower set-operation bodies when they
  appear inside a view, CTE, or subquery. The Unique Set is done in issue 24;
  issue 28 lowers every view, CTE, and FROM-subquery body the way the Root
  Select is lowered, set operations and joins included.
- [ ] Column lineage, column unique status.
- [ ] General window modeling: represent window expressions and their filtering
  as separate `Window` and `Filter` operations, with the engine deriving
  uniqueness from recognized shapes. Until then, keep
  `QUALIFY row_number() over (partition by ...) = 1` as the explicit special case.

## Found while building issue 28

- [x] A column marker naming a column that is not visible in a relation with
  an incomplete schema is dropped silently: no assertion and no diagnostic.
  Example, with `a` and `b` undeclared:
  `SELECT * FROM a JOIN b ON a.id = b.aid /**ASSERT UNIQUE(name)**/`.
  Report it instead (a dropped marker reads to its author as a proof).
  Done: an `unresolved-asserted-column` Diagnostic. Not an UNKNOWN assertion
  over an invented opaque column: the column may not exist at all, and a
  made-up output column would leak into every consumer of the relation
  (`suggest`, relation facts); the Diagnostic also tells the author the fix
  (declare the table). Markers are now accounted for by identity, not line,
  so two markers on one line no longer cover for each other;
  `tests/test_analyze_marker_accounting.py` checks every marker kind in every
  context for an Assertion, a Declaration, or a Diagnostic on its line.

