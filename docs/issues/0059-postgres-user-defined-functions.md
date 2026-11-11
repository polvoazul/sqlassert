# 59. User-defined functions and table functions (Postgres, Spark, Hive)

- **State:** not planned
- **Labels:** P4, soundness, lowering, discovery, postgres, spark, hive
- **Created:** 2026-11-10
- **Related:** [23. Close IR lowering gaps](0023-close-ir-lowering-gaps.md)
  (how unknown functions lower), [42. Lowering support v2](0042-lowering-support-v2.md),
  [56. dbt: read the live warehouse catalog](0056-dbt-live-connection.md)

## Problem

A call core does not know (a user-defined function, or a built-in missing from
its registry) is an `ir.OpaqueExpression`: its value is unknown and is not
even a function of its arguments, so a volatile function proves nothing wrong.
But the call is still taken to return one value per row. In Postgres that is a
guess: a function declared `RETURNS SETOF ...` or `RETURNS TABLE (...)`
returns rows, and in a select list it repeats each input row once per row it
returns, the same as `unnest`. Core handles the built-in set-returning
functions (`e798ffc`, `tests/set_returning_functions.sqltest`) but not a UDF:

```sql
CREATE TABLE t (id INTEGER PRIMARY KEY);
SELECT id, my_rows(id) AS r FROM t /**ASSERT UNIQUE(id)**/   -- PROVED, false if my_rows is SETOF
```

This breaks ADR 0005's "never guess". It also leaves proofs on the table: an
`IMMUTABLE` UDF is a function of its arguments and could carry functional
dependencies, as `upper(x)` does.

Spark and Hive have the same gap. A UDTF (a Hive `GenericUDTF`, registered
with `CREATE FUNCTION f AS 'class' USING JAR ...`) is called in a select list
like `explode`, and it repeats rows the same way. The SQL alone never says
whether a registered class is a UDTF.

DuckDB, Snowflake and BigQuery table functions are only allowed in FROM, where
they already lower as opaque sources. Databricks SQL and Python table
functions (`RETURNS TABLE`, `@udtf`) are too, so they're out of scope.

## What to do

1. **From the SQL alone.** Read `CREATE FUNCTION` in the program:
   - `RETURNS SETOF` / `RETURNS TABLE`: set-returning, lowered like the
     built-ins (a cross join with an opaque relation of its values).
   - `IMMUTABLE`: an `ir.OpaquePureFunction` of its arguments. `STABLE` and
     `VOLATILE` stay opaque.
   - `STRICT` (`RETURNS NULL ON NULL INPUT`): NULL when any argument is
     NULL. Whether it is ever NULL otherwise is unknown.
   - Overloads: resolve by name and argument count; if it is still
     ambiguous, take the weakest facts.
2. **With no definition.** An unknown function in a Postgres select list may
   return rows. Decide between:
   - (a) Sound: assume it may, so that SELECT keeps no key. Warn with what is
     missing (a `CREATE FUNCTION`, or a connection).
   - (b) Assume one row as today and warn.

   (a) follows ADR 0005, but it costs proofs on every query that calls a UDF
   in its select list. Built-ins in the registry are not affected.
3. **With a connection.** The Postgres fetcher (`sqlassert/discovery/postgres.py`)
   reads the same facts from `pg_proc` for each function the SQL calls:
   `proretset`, `provolatile`, `proisstrict`, the argument types and the
   namespace, following `search_path` as for relations. A connection only
   adds facts. Redshift's fetcher can share it for its Python and SQL UDFs.
4. **Spark and Hive.**
   - From the SQL: `CREATE FUNCTION ... USING JAR` names a class, not a kind.
     So the step 2 decision applies to every function that isn't a built-in.
     The one exception is a Spark SQL UDF (`CREATE FUNCTION f(...) RETURNS
     type RETURN expr`), which returns one value.
   - With a connection: `DESCRIBE FUNCTION EXTENDED f` gives the class (and
     the usage). The Databricks fetcher can read `information_schema.routines`.
     Whether a class is a UDTF can't be read from the catalog, so a
     connection settles only SQL UDFs. Allowing a configured list of
     functions known to return one value is another option.

## Tests

- A `.sqltest` per declaration form: `SETOF` loses keys, `RETURNS TABLE` too,
  `IMMUTABLE` carries a dependency, `VOLATILE` does not, `STRICT` keeps NULL
  in NULL out. Also an unknown function under the decision in step 2.
- A hydration test against Postgres (`tests/test_postgres_catalog.py`) with
  `CREATE FUNCTION` in the fixture database.
- Spark: `.sqltest` cases (`-- dialect: spark`) for a registered JAR function
  in a select list (under the step 2 decision) and for a SQL UDF.
- The oracle runs DuckDB only. A DuckDB `CREATE MACRO ... AS TABLE` cannot be
  called in a select list, so the set-returning cases are judged by the
  Postgres tests, not the oracle.
