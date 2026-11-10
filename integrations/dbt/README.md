# sqlassert-dbt

Prove your dbt tests from the SQL, and skip the ones that can't fail.

A `unique` or `not_null` test scans a table in the warehouse on every run, and
it only tells you about today's data. Many of those tests follow from the SQL
itself: `customer_id` is unique because the model groups by it, `status` is
never null because of a `coalesce`. [sqlassert](https://github.com/polvoazul/sqlassert)
proves them from the compiled model before anything runs, so dbt can leave them
out: faster CI, a smaller warehouse bill, and the same guarantee for *any*
data, not just the rows there today.

```bash
pip install sqlassert-dbt
```

_Alpha: the markers and dbt test mappings are stable; the CLI output may
change._

## Skip the tests the SQL proves

Run dbt through the plugin:

```bash
sqlassert-dbt build . -- --target prod    # or: sqlassert-dbt test
```

It runs `dbt compile`, checks every model, then runs `dbt build` (or
`dbt test`) with each proved test passed as `--exclude`. Arguments after `--`
go to dbt unchanged. The exit code is dbt's when dbt fails, otherwise the
check's.

The tests of the sources, seeds and models a model reads are trusted as
facts, since `dbt build` runs them first in the same invocation. If the
arguments after `--` select nodes (`--select`, `--exclude`, `--selector`, ...),
an upstream test may not run, so none is trusted.

## Check without dbt

`check` reads only `target/manifest.json`, so dbt does not need to be installed
where it runs (a CI step after `dbt compile`, say):

```bash
$ dbt compile
$ sqlassert-dbt check . --trust-upstream-tests
models/customers.sql:12: UNKNOWN /**ASSERT NOT NULL(first_name)**/ (customers)

Proved statically (a test here can be skipped):
  models/schema.yml  unique_stg_customers_customer_id  (test on stg_customers)

Not proved (the warehouse run still checks these):
  models/schema.yml  accepted_values_stg_orders_status__placed__shipped__returned  (test on stg_orders): its Property is not proved from the SQL

Proved but untested (schema.yml additions):
  models/schema.yml  customers.customer_id: not_null
```

Three lists come out:

- **Proved statically:** tests the SQL already guarantees. Skip them.
- **Not proved:** tests that still need the warehouse. Nothing is wrong; the
  SQL alone can't show the property.
- **Proved but untested:** properties sqlassert proved that no test states.
  Free `schema.yml` additions, which cost nothing to keep.

| Option | |
|---|---|
| `--trust-upstream-tests` | Treat passing tests of upstream nodes as facts (true under `dbt build`). Without it, a `dbt-untrusted-upstream-tests` warning lists them. |
| `--require-proved` | Fail when any test is not proved, not only when a marker is `UNKNOWN`. |
| `--explain` | Print each proof, and each failed proof. |
| `--dialect NAME` | Analyze under this dialect instead of the adapter's. |
| `--manifest PATH` | Read another manifest (default `PROJECT/target/manifest.json`). |

Exit codes: `0` every marker proved; `1` a marker is `UNKNOWN` or an error was
reported (unproved tests fail it only with `--require-proved`); `2` the
manifest cannot be read.

## What it understands

| dbt | sqlassert Property |
|---|---|
| `unique` | Unique Set |
| `not_null` | Not Null |
| `accepted_values` | Enum |
| `relationships` | Referential Coverage (Foreign Key) |
| `dbt_utils.unique_combination_of_columns` | Non-Null Unique Set (it counts NULL equal to NULL) |
| contract `primary_key`, `unique`, `not_null`, `foreign_key` | the same Properties |

A model's own tests are what gets proved. Upstream tests are evidence, used
only when they fail on any failing row: not a test with `severity: warn`, a
`where:` config, or `error_if`/`fail_calc` thresholds. A constraint counts only
on a contracted table whose adapter enforces it.

## Suggest from the models you're editing

```bash
$ sqlassert-dbt suggest models/customers.sql models/stg_orders.sql --trust-upstream-tests -- --target dev
customers:
  models/customers.sql:7   mid  CTE orders   Unique Set  /**ASSERT UNIQUE(customer_id)**/
  models/customers.sql:11  mid  Root Select  Not Null    /**ASSERT NOT NULL(customer_id, n_orders)**/

stg_orders:
  models/stg_orders.sql:2  mid  Root Select  Non-Null Unique Set  /**ASSERT NOT NULL UNIQUE(order_id)**/
```

It runs `dbt compile` on those models and what they read (arguments after
`--` go to dbt; a `--select` of your own replaces that selection), then lists
what [`sqlassert suggest`](https://github.com/polvoazul/sqlassert#readme)
finds in each model, with the relations it reads declared as `check` declares
them: `high` rewrites that keep the result, `mid` markers that will be
`PROVED` when pasted at the end of their line, `low` smells. `--explain`
prints each one's evidence. A line inside SQL that dbt inlined or that a tag
wrote is given in the compiled file instead. In an incremental model, a
rewrite or marker is listed only when both `is_incremental()` branches prove
it. dbt's own output goes to stderr.

## Markers in models

You can also write sqlassert markers straight into a model, and they are checked
and reported at their line in the model file:

```sql
-- models/customer_orders.sql
select c.customer_id, c.first_name, o.order_id
from {{ ref('stg_orders') }} o
/**ASSERT MANY TO ONE**/ join {{ ref('stg_customers') }} c
  on o.customer_id = c.customer_id
```

If someone later loads duplicate customers upstream, or drops the `unique`
test that made this provable, the marker turns `UNKNOWN` and CI fails before
the join fans out in production. See the
[sqlassert README](https://github.com/polvoazul/sqlassert#readme) for every
marker.

## Details

- **Manifest:** only the latest schema (v12, written by dbt 1.8 to 1.12).
- **Ephemeral models** are analyzed where dbt inlined them.
- **Incremental models** are analyzed in both `is_incremental()` branches. A
  test holds only if it holds in both, and a key only under a `merge` or
  `delete+insert` strategy on that `unique_key` with no
  `incremental_predicates`. The other branch is rendered without dbt only when
  the model uses `ref`, `source`, `this`, `config` and `is_incremental()`
  alone; otherwise it is reported unanalyzed.
- **Adapters:** analyzed when sqlassert models their dialect (DuckDB,
  PostgreSQL, MySQL, Snowflake, BigQuery, Databricks / Spark, Redshift, Trino).
- A model with markers but no compiled code is an error, not skipped.

## License

MIT
