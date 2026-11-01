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
  injectivity (`id + 0`, lossless casts). Function FDs done (Paulley §3.1.4):
  an opaque expression built only from built-in functions and operators over
  resolved columns records them as `inputs`, and closure reaches it from them
  when each compares by identity. Still open: injectivity (P4), and searched
  `CASE`, `coalesce` and simple `CASE` as FD sources.
- [x] `SetOperation` output columns refer to the left arm only; the right arm's
  columns are not tracked. (`ALL` vs distinct is now distinguished:
  `SetOperation.distinct`, issue 24.) Done: `SetOperation.arms(column)` pairs
  each output column with both inputs' columns by position (None for
  `UNION BY NAME` or inputs of different width), used by nullability
  (Paulley §3.2.5-3.2.6: UNION needs both inputs non-null, INTERSECT either,
  EXCEPT the left), Enumerated Domains (issue 31) and Foreign Keys. Not done:
  FDs of the right input through INTERSECT (sound only under identity
  comparison, like the INTERSECT domain rule).
- [x] Unique-join analysis models only `inner` and `left`; `right`, `full`,
  `semi`, `anti`, `cross`, and `LATERAL`/`UNNEST` sources are not modelled.
  Done for `right`, `full` and `cross` (Paulley §3.2.3, §3.2.10): Unique and
  Exactly One Joins hold by the left row's matches whatever the kind; a cross
  join is an ON with no conjuncts, unique when the right input has at most one
  row. A scalar aggregate (no `GROUP BY`) now lowers to an `Aggregate` grouped
  by nothing, so its one row gives the empty Unique Set. `POSITIONAL JOIN` was
  lowered as an inner join, which proved `NOT NULL` on columns it pads: it is
  now a full join on an opaque row-position condition. `semi` and `anti`
  joins are modelled as filters of their left input (see correlated
  subqueries below). Still open (P4): `LATERAL`/`UNNEST`.
- [ ] Windows: only `QUALIFY row_number() over (partition by ...) = 1` is
  recognized; other window functions are opaque. Planned rules:
  [`docs/functional-dependencies.md`](../functional-dependencies.md#window-functions).
- [ ] Recursive CTEs and views become `RecursiveRelation` (opaque). Caveat:
  a recursive relation is a genuine fixpoint, which the memoized engine and its
  strata cannot compute; supporting it needs bespoke fixpoint machinery.
- [x] Correlated subqueries (`EXISTS (… WHERE i.x = o.y)`) are not lowered.
  When they are, lower them as a dependent join (apply) over the outer input,
  so the IR stays a DAG; a subquery that refers to its outer relation directly
  would create a cycle across relations that the strata cannot order
  ([Strata](../functional-dependencies.md#strata)).
  Done (Paulley §4.4.2): a top-level WHERE conjunct `EXISTS (SELECT … FROM r
  [JOIN …] WHERE p)` is a semi join of the outer input with r on `p` (lowered
  with the outer and inner bindings together), `NOT EXISTS` an anti join, and
  `x IN (SELECT a …)` a semi join on `x = a`. A semi or anti join outputs
  only its left columns and keeps whole left rows; a semi join's ON holds on
  each kept row (non-null, Equality Groups, domains, and Referential Coverage
  onto the right columns ON equates). The explicit `SEMI`/`ANTI JOIN` syntax
  lowers the same way (`RIGHT SEMI`/`RIGHT ANTI` with inputs swapped). Not
  lowered: `NOT IN` (NULL semantics), tuple `IN`, an EXISTS body with
  `GROUP BY`, `HAVING`, `DISTINCT`, `LIMIT` or `WITH`, subqueries in the
  select list, and `ANY`/`ALL` comparisons.
  Semi and anti joins take the ordinary Unique Join proof (a left row is kept
  or dropped whole); Exactly One applies to semi joins, not anti joins, which
  drop every row they match.
- [x] A Select with no FROM was opaque. Done: it lowers over one row (an opaque
  relation of no columns under `LIMIT 1`), so the empty set is its key and its
  literals keep their values.
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
- [x] Tables without a `CREATE` in the program have an incomplete schema, so an
  unqualified column cannot be resolved when several sources are in scope.
  Today this is silently UNKNOWN. Per
  [ADR 0005](../adr/0005-analyze-sql-without-a-connection.md):
  - **Without a connection:** emit a warning Diagnostic naming the column, the
    candidate sources, and the fix (qualify it, or `CREATE` the tables), and
    treat facts that depend on that column as unknown.
  - **More broadly, warn whenever a connection would have helped**: every
    table with no `CREATE` (once per table: its keys, nullability and types
    are unknown), and every comparison whose result depends on an unknown
    column type (issue 29's conservative string handling). Not emitted by
    `analyze_database`, where the catalog supplies the facts.
  - **With a connection** (`analyze_database`): give `IrParser` the catalog's
    column lists, so the column resolves to its one real source and is
    analyzed normally.
  - Tests: the same SQL warns offline and proves with an in-memory DuckDB
    catalog; adding the connection never turns PROVED into UNKNOWN.
  Done: `Report.warnings` (and `SuggestionReport.warnings`, printed by the
  CLI as `warning: ...` without failing) carries `undeclared-table`, once per
  table, and `ambiguous-column`, once per column and candidate sources. Unknown
  column types need no warning of their own: a column whose type a catalog
  could supply belongs to an undeclared table, which already warns. With a
  connection, hydration already declares each catalog table's columns, so the
  column resolves; catalog column types and collations are passed too (issue 30).
  Tests: `tests/test_connection_warnings.py`.
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


## Found by the Paulley audit (issue 35)

Rows marked *missing* in the audit table (`docs/functional-dependencies.md`,
"Paulley audit"):

- [ ] **E4** (eq7): a lax equivalence whose columns a later filter makes
  definite does not become an Equality Group: after `LEFT JOIN r ON r.z = l.a
  WHERE r.z IS NOT NULL`, z and a are not merged, so Enumerated Domains and
  Foreign Key stand-ins do not cross between them. (The FD closure already
  strengthens the lax FD.)
- [ ] **S5** (§3.2.1): CHECK constraints are ignored without a diagnostic.
  `CHECK (m = 1)` or `CHECK (m IN ('a', 'b'))` states a constant or an
  Enumerated Domain of m's non-null values (CHECK is true-interpreted: NULL
  passes), which is exactly what ENUM means.
- [ ] **R11** (§3.2.4, footnote 9): only `=`, `IN (...)` and `IS NOT NULL`
  reject NULL. Paulley counts every false-interpreted comparison as
  null-intolerant: `<`, `>`, `<=`, `>=`, `<>`, `BETWEEN`, `LIKE` with column
  operands. `WHERE total > 0` should make `total` non-null (and enable the
  INNER JOIN suggestion). Cheapest and most common of these rows.
- [ ] **R4** (Claim 20): `WHERE b = a + 1` should give `a → b` when the
  expression is deterministic and its inputs compare by identity (the same
  rule as a computed column, `uniqueness._computed`).
- [ ] **R10** (§3.2.4): `WHERE a = (uncorrelated scalar subquery)` makes `a`
  constant (`∅ → a`).
- [ ] **R9** (§3.2.4): `LIKE 'x'` with no wildcard is `= 'x'`.
- [ ] **I1, I2** (Lemma 6, Corollary 1): the right input's FDs and keys
  through INTERSECT. Sound only when the paired columns compare by identity:
  an INTERSECT row carries the left value, equal to the right one under the
  left column's comparison (`identity_comparison`).
- [ ] **U2** (§3.2.6, Example 15): equivalences and constants that hold in
  both UNION ALL inputs at the same positions hold in the output.
