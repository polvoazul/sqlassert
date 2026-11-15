# 72. Type computed select items from certified function result types

- **State:** done 2026-11-15
- **Theme:** lowering
- **Created:** 2026-11-15
- **Related:** [69: real-world SQL corpora](0069-real-world-sql-corpora.md)

A select item gets a comparison type only when it is a plain column, a small
integer literal, or a `CAST`. Every other function result is untyped, so an
equality on it binds nothing and its column is not compared by identity, even
where the engine fixes the result type: DuckDB's `md5(x)` is always
`VARCHAR`, `length(x)` always `BIGINT`. In one public dbt project with fully
typed sources, 103 of 303 model output columns were untyped, all of them
function results: surrogate keys (`md5`), text parsed with `substring` and
`strpos`, date parts.

## Scope

- [x] A dialect claims a result type for a function only where the engine
  returns that type for every argument type it accepts. A type that depends on
  an argument (`date_trunc` of a DATE or TIMESTAMP, `extract(epoch ...)`, a
  `strptime` format with `%z`) is not claimed.
- [x] Each claim is a dialect fact certified by `scripts/certify_dialects.py`,
  probed with the engine's own type function (`typeof` in DuckDB) over several
  argument types, and recorded in `docs/dialect-provenance.json`.
- [x] DuckDB first. Other dialects claim nothing until certified, so their
  function results stay untyped.
- [x] A select item whose expression is a function with a claimed result type
  gets that type's facts (comparison type, identity comparison, binary text,
  exact numeric), as a column declared with it would.

## Not in scope

- Operators whose result depends on operand types (`DATE + INTERVAL`).
- Joining across types (`TIMESTAMP = DATE`) and resolving a select alias in
  `QUALIFY PARTITION BY`: both block one public dbt join; separate issues.
- Nullability of `md5` and other unmodelled functions.

## Outcome

`DialectSemantics.result_types` lists, by SQLGlot node, the functions whose
result type the dialect fixes; DuckDB claims 17 (`md5`, `sha256`, `lower`,
`upper`, `trim`, `replace`, `left`, `right`, `substring`, `concat_ws`,
`dayname`, `monthname` as `VARCHAR`; `length`, `strpos`, `year`, `month`,
`day` as `BIGINT`), each certified on DuckDB 2.0.0 with `typeof` over several
argument types. `ir/convert` types a select item that is such a call as a
column declared with the type. Cases in `tests/computed_select_item_types.sqltest`.
On the public dbt project above, typed model columns went from 200 to 223 of
303; what is left is `try_strptime` (its type follows the format) and date
arithmetic.
