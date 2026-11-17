# 73. Pending lowering and classification decisions

- **State:** open
- **Priority:** P3 (needs decisions): Each item loses a few proofs or a
  reporting detail; none is unsound. Each needs the owner's pick before work.
- **Theme:** lowering
- **Created:** 2026-11-16

Decisions left open by the 2026-11-16 round of Unknown-join attacks (stars,
QUALIFY, VALUES and computed types, row_number(), table functions, UNION BY
NAME, injective join keys, the Unproved Join Reason split). Each lists the
alternatives; **(rec)** marks the one recommended when it was written.
Numbers are the ones used when the decisions were discussed.

## Deferred: keep the status quo for now

- [ ] **7. Typing decimal arithmetic and products** in join keys.
  `ON a.amount = b.amount + 0.01`, `ON a.cents = b.amount * 100` bind nothing.
  A: leave untyped (today). B: type DECIMAL precision growth per dialect
  after probes (capped at 38 digits). C: integer products only (rec).
- [ ] **8. Outer, full and semi join `ON` dependencies through expressions.**
  `a LEFT JOIN b ON b.k = a.k + 1` gives no dependency; an inner join gets
  `a.k -> b.k`. A: leave. B: extend `_outer_on_dependencies`,
  `_full_on_dependencies` and `_matched_dependencies` with the same `_pins`
  gate (rec).
- [ ] **9. What a `no-unique-set (cascade)` names.** It names the nearest
  unsupported construct anywhere below the input, which may not be what
  blocks the key: in one audited project 19 joins name a recursive CTE.
  A: nearest anywhere (today). B: only constructs on the lineage of the
  bound key columns (rec). C: cascade only for an unproved join below, the
  construct secondary.

## QUALIFY and windows

- [ ] **16. A lone star that cannot be expanded under QUALIFY** makes the
  SELECT opaque: `SELECT * FROM undeclared QUALIFY row_number() OVER
  (PARTITION BY k ORDER BY ts) = 1`. A: opaque (today). B: the row-subset
  fallback the other QUALIFY forms use (rec).
- [ ] **17. A `row_number()` item whose partition key is not selected**
  states nothing: `SELECT id, row_number() OVER (PARTITION BY k) AS rn FROM t`.
  A: nothing (today). B: hidden output, as QUALIFY does (rec).
- [ ] **18. `(k, rn)` is stated as a lax Unique Set** though NULL keys share
  one partition, so a strict one holds. A: lax (today, rec). B: a strict
  Knowledge variant.
- [ ] **19. Warning text** "a window function (other than a row_number()
  select item or QUALIFY row_number() ... = 1)". A: keep (rec). B: a flat
  "other than row_number()".

## VALUES

- [ ] **20. MySQL `VALUES ROW('a', 1), ROW('b', 2)`** is not lowered at all
  (SQLGlot reads each row as an anonymous `ROW` call). A: leave. B: unwrap
  `ROW` and lower like the other dialects (rec).
- [ ] **21. Booleans, cast first rows and typed literals** in a VALUES column
  stay untyped: `(VALUES (true)), (VALUES (DATE '2024-01-01'))`. A: leave
  (rec). B: type per dialect after probes (MySQL types booleans BIGINT).
- [ ] **22. A decimal with a whole part of 0** (`VALUES (0.5)`) stays
  untyped: DuckDB and MySQL give DECIMAL(2,1), Trino and Spark DECIMAL(1,1).
  A: leave (rec). B: a per-dialect rule.

## Computed types

- [ ] **23. `NULLIF(a, b)` is typed as a CASE over both operands**, so
  `NULLIF(int_col, 1.5)` is untyped though DuckDB returns INTEGER.
  A: both (today, rec). B: first operand only.
- [ ] **24. `CASE ... THEN tinyint_col ELSE 0 END`** is labelled with the
  widest integer type of its results. A: widest (today, rec). B: identical
  labels only.

## Table functions

- [ ] **25. Columns of a table function have no type** (`json_each(...).key`
  is VARCHAR in DuckDB). A: untyped (today). B: type the fixed columns per
  dialect (rec).
- [ ] **26. `UNNEST(x) WITH ORDINALITY`** is not modelled. A: leave (rec).
  B: add the `ordinality` column after probes (DuckDB's alias rule differs).
- [ ] **27. A scalar subquery over a correlated table function**
  (`(SELECT sum(r.value) FROM json_each(t.x) AS r)`) has an opaque value.
  A: leave (rec). B: lower it as a join.
- [ ] **28. A bare name shared by a table and a known table-function schema**
  (`id` beside `json_each`'s `id`) goes opaque silently. A: silent (today).
  B: warn `ambiguous-column` (rec).

## Set operations and opaque relations

- [ ] **29. `UNION BY NAME` is gated by `dialect == "duckdb"`.**
  A: keep. B: a certified `DialectSemantics` flag (rec). C: B plus Snowflake,
  which documents BY NAME (needs a quoted-name rule).
- [ ] **30. `NATURAL JOIN`** has its parser diagnostic but records no
  construct, so an UNKNOWN assertion through it gets no
  `unsupported-construct`. A: keep. B: also record it (rec).
- [ ] **31. `FROM t TABLESAMPLE 10%`** is opaque. A: keep. B: a row subset,
  like `USING SAMPLE` (rec).

## Join keys

- [ ] **32. `ON a.k = COALESCE(b.k, 0)`** is `expression-key`: CASE,
  COALESCE and NULLIF results are typed only as select items. A: leave.
  B: carry their typing onto `ON` operands (rec).

## Unproved Join Reason

- [ ] **33. Should `partial-key` also require that the other direction has
  no key to declare?** A: no (today, rec). B: yes.
- [ ] **34. A scalar-level unsupported construct** (`rank()` in a QUALIFY)
  is invisible to `cascade`, so the join reads `undeclared-key`. A: leave.
  B: cascade sees scalar-level constructs (rec).
- [ ] **35. A lateral table-function input** (`a CROSS JOIN json_each(a.doc)`
  as a join input) reads `undeclared-key` (the project's), though nobody can
  declare a key there. A: keep. B: mark it ¹ (rec).
