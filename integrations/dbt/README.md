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

A `profiles.yml` kept in the project directory is found without
`--profiles-dir` (by `build`, `test` and `suggest`), unless the working
directory has one or `DBT_PROFILES_DIR` is set.

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
| `--catalog PATH` | Read column lists and types from this `catalog.json` instead of `PROJECT/target/catalog.json`, which is read when present (also on `build`, `test` and `suggest`). See below. |
| `--no-catalog` | Read no `catalog.json`: the manifest's columns only. |
| `--connect` | `build`, `test` and `suggest`: read the tables models read from the warehouse, through dbt's own connection. See below. |
| `--warning-limit N` | Print at most N warnings of each code, then say the rest are suppressed (default 10; 0 prints all). |

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
  models/customers.sql:11  mid  Root Select  Not Null    /**ASSERT NOT NULL(customer_id)**/

stg_orders:
  models/stg_orders.sql:2  mid  Root Select  Not Null  /**ASSERT NOT NULL(order_id)**/
```

Pass model files, a directory of models, or the project directory for every
model (`sqlassert-dbt suggest .`). It runs `dbt compile` on those models and
what they read (arguments after `--` go to dbt; a `--select` of your own
replaces that selection), then lists
what [`sqlassert suggest`](https://github.com/polvoazul/sqlassert#readme)
finds in each model, with the relations it reads declared as `check` declares
them by type: `rewrite`s that keep the result, `rewrite with caveat`s that
keep its rows but may change a column's type (the message says which),
`marker`s that will be
`PROVED` when pasted at the end of their line, `hazard`s. `--explain`
prints each one's evidence. A marker on the model's own output that its
tests or contract already state is left out, or cut to the part none states
(a `unique` test leaves `NOT NULL` to suggest). A line inside SQL that dbt inlined or that a tag
wrote is given in the compiled file instead. In an incremental model, a
rewrite or marker is listed only when both `is_incremental()` branches prove
it. dbt's own output goes to stderr, and its `target/` and `logs/` go to a
temporary directory, leaving the project's alone (`--dbt-compile-inplace`
compiles there instead, keeping dbt's partial-parse cache; a `--target-path`
or `--log-path` after `--` is kept). Each model's suggestions print as soon as
it is analyzed.

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

## Columns from `catalog.json`

The manifest lists no columns for sources, seeds, snapshots or uncontracted
incremental models, so `SELECT *` over them cannot be expanded, an unqualified
column may be ambiguous, and their types are unknown (a `dbt-unknown-columns`
warning). `dbt docs generate` writes `target/catalog.json` with every
relation's columns and types, as the warehouse had them:

```bash
$ dbt docs generate
$ sqlassert-dbt check .    # reads target/catalog.json
```

It is read whenever it is there (`--catalog PATH` names another) and trusted like a connection: it can prove more, never less. Without one, a `dbt-unknown-columns` warning says how to get it.
It has no keys and no nullability, so those still come from tests and
contracts. A catalog is a snapshot, so an entry is not used where the project
shows it stale:

- **Another target** (`dbt-catalog-other-relation`): the catalog names the
  relation in another database or schema than the manifest does.
- **A changed seed** (`dbt-catalog-stale`): the seed's CSV header lists other
  columns.
- **An enforced contract** (`dbt-catalog-stale`): a contract that lists other
  columns wins.

A source whose warehouse table changed after `docs generate` cannot be told
apart; regenerate the catalog when sources change, or connect.

## Columns, keys and NOT NULLs from the warehouse

`--connect` on `build`, `test` and `suggest` reads every table the models read
(sources, seeds, snapshots, tables) from the warehouse, current: columns,
types, nullability, primary and unique keys, foreign keys. It opens the
connection dbt opens, from the same profile and target (`-- --target prod`
picks another), and closes it before dbt runs again. No `catalog.json` is
read then. An enforced contract's table is still declared from the contract,
since the build may not have made it yet; every other table comes from the
warehouse.
As with `analyze_database`, a connection only adds proofs. A key the
warehouse does not enforce is trusted as a Property Declaration, as the SQL's
own are.

```bash
$ sqlassert-dbt build --connect .
```

Adapters: DuckDB (tested end to end), Postgres, Redshift, Snowflake, MySQL
and Trino. It uses dbt's adapter API, which is internal and changes across dbt
versions; when it fails, or for BigQuery and Databricks, pass a fetcher over
your own connection from Python:

```python
import duckdb
from sqlassert.discovery.duckdb import DuckDBKnowledgeFetcher
from sqlassert_dbt import analyze_project

report = analyze_project(project, fetcher=DuckDBKnowledgeFetcher(duckdb.connect("warehouse.duckdb")))
```

`suggest_models(..., fetcher=...)` takes one too. `check` stays
connectionless: it runs where dbt is not installed.

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
