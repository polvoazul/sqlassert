# Dialects

`sqlassert/dialects.py` holds one `DialectSemantics` per SQLGlot dialect name:
what that dialect means by the constructs the rules depend on. `ir/convert.py`
reads it while lowering; the rules never see a dialect, only the IR it
produced (Builtin flags, column comparison types, Knowledge and Property
Declarations). This page argues each entry and says how soundness is judged
for dialects the oracle does not run.

## The rule for every flag

A flag may only claim what holds under *all* of the dialect's settings,
because the SQL alone does not say which are in force (ADR 0005). Where a
setting decides (MySQL's `sql_mode`, Spark's ANSI mode), the flag takes the
weaker reading: the analysis loses a proof, never gains a wrong one. The one
exception is the default collation: text without a `COLLATE` is taken to
compare under the engine's shipped default, and a setting that changes it
(DuckDB's `default_collation`, Snowflake's `DEFAULT_DDL_COLLATION`, Oracle's
`NLS_COMP`) is out of scope, like MySQL's `foreign_key_checks = 0`. A dialect not listed gets `DialectSemantics()`:
cautious arithmetic, collation and `||`, and enforced keys (the behaviour
before dialects were listed; see "Not done").

## Semantics by dialect

| | DuckDB | Postgres | MySQL | Snowflake | BigQuery | Databricks / Spark | Redshift | Trino |
|---|---|---|---|---|---|---|---|---|
| `+ - * CAST` never NULL (`checked_arithmetic`) | yes | yes | no | yes | yes | no (ANSI off) | yes | yes |
| `/ // %` never NULL (`checked_division`) | no (`n % 0` is NULL) | yes | no | yes | yes | no | yes | yes |
| comparisons never NULL (`casts_raise`) | yes | yes | no | yes | yes | no (`1 < 'x'` is NULL) | yes | yes (no implicit casts) |
| text without `COLLATE` compared by identity (`text_is_binary`) | yes | yes | no (`_ci`) | no (not yet certified) | no (not yet certified) | no (not yet certified) | no (`CASE_INSENSITIVE` databases) | yes (no collations) |
| `\|\|` | strict | lenient (`ARRAY[1] \|\| NULL` is `{1}`) | ambiguous | strict | strict | strict | strict | strict (arrays too) |
| `concat()` | skips NULL | skips NULL | strict | strict | strict | strict | not modelled | strict |
| `concat_ws()` | skips NULL | skips NULL | skips NULL | strict | none | skips NULL | none | skips NULL |
| primary / unique keys | enforced | enforced | enforced | `RELY` | unenforced | `RELY` | unenforced | none (unenforced) |
| foreign keys | enforced | enforced | enforced (InnoDB, NDB; not inline) | `RELY` | unenforced | `RELY` | unenforced | none (unenforced) |

Also: SQLite foreign keys are unenforced (`PRAGMA foreign_keys` is off by
default) and its primary keys may be NULL (only an `INTEGER PRIMARY KEY` may
not, and SQLGlot reads `INT` and `INTEGER` alike, so none is taken to be
non-null). A ClickHouse primary key never rejects a duplicate, and its
`concat_ws` is NULL for a NULL value, as Snowflake's. Hive has neither checked
arithmetic nor enforced keys, and its `concat_ws` is not modelled.
`concat_ws()` is modelled only where it is listed: a NULL separator makes it
NULL everywhere, but whether a NULL value is skipped differs.

Trino has no key constraints: its `CREATE TABLE` rejects `PRIMARY KEY`,
`UNIQUE` and `REFERENCES`, so a key in SQL read as Trino's describes a table
held elsewhere, and is unenforced (trusted only under
`trust_unenforced_keys`); `NOT NULL` is enforced where a connector accepts
it. Its VARCHAR compares by code point and it has no collations; its
connectors keep that by not pushing text predicates down where the source
compares differently (MySQL: none; SQL Server: only on case-sensitive CHAR,
otherwise re-checked in Trino; PostgreSQL: `=` and `<>`, not ranges, unless
the experimental `enable_string_pushdown_with_collate` is on, which is out
of scope like DuckDB's `default_collation`). `CHAR` pads, as everywhere. Trino
spells no integer division (`//`, `DIV`), `ILIKE` or shift operator, and
SQLGlot reads `bitwise_left_shift()` as an opaque function: those Builtins
never occur in Trino SQL. `DOUBLE` division by zero is `Infinity` and `%`
is `NaN`, both non-null; `NaN = NaN` is false (DuckDB and PostgreSQL: true).

PostgreSQL `NUMERIC` with no scale (`unscaled_decimal`) keeps each value's own
scale: `1.0 = 1.00`, but their text differs, so such a column is neither
compared by identity nor exact; `NUMERIC(10, 2)` is both. DuckDB, MySQL,
Snowflake, BigQuery and Redshift give a bare `DECIMAL` a fixed scale.

MySQL ENUMs (`enum_error_value`): MySQL stores `''` for a value outside the
members outside strict mode and under `INSERT IGNORE` in any mode, and strips
trailing spaces from members, so an ENUM column's domain is its members
without trailing spaces, plus `''`. Its comparisons follow the column's
collation, so `e = 'A'` keeps the member `'a'`; ENUM narrowing never treats
distinct strings as distinct values there.

MySQL `||` is OR unless `sql_mode` has `PIPES_AS_CONCAT`, under which it is
concatenation *and binds tighter than any comparison*: `a = 1 AND b || c` is
`(a = 1 AND b) OR c`, or `a = 1 AND (b || c)`. sqlassert parses it as OR (as
SQLGlot does), marks the node (`sql_parse.is_ambiguous_pipes`), and lowers it
as an `OpaquePureFunction` of its operands, with an `ambiguous-operator`
warning. That is sound for both readings: as OR, `||` has the lowest
precedence, so its node's span is delimited by parentheses, a function's
arguments or a clause; under either reading the expression over that span is
a pure function of the columns in it. An OR around it, at the same level,
splits identically in both readings.

## Unenforced keys (decision)

An unenforced key is never Knowledge (`Accepted`), so it never proves anything
the SQL alone does not. It becomes a Property Declaration (`Declared`, as a
`DECLARE` marker does) when:

- it is marked `RELY` in a dialect that has `RELY` (Snowflake, Databricks /
  Spark): the author's statement that the data satisfies it, and the
  warehouse's own optimizer trusts it on the same terms; or
- the caller passes `trust_unenforced_keys=True` to `analyze`,
  `analyze_database` or `suggest`: the opt-in for BigQuery and Redshift, which
  have no `RELY`, and for any other constraint the database does not check.

Otherwise it is ignored, and an `unenforced-constraint` warning says why and
how to trust it. A trusted primary key declares its columns non-null too.

Alternatives not taken: declaring every unenforced key (unsound for exactly
the data that violates them, and silent about it), or ignoring `RELY` and
trusting only opt-in (`RELY` is already the warehouse-native statement of
trust). Redshift's planner trusts declared keys, but that is a reason they
must be right, not evidence that they are.

Constraints that are written but not enforced on every row are treated the
same way, in every dialect: `NOT ENFORCED` (BigQuery, PostgreSQL 18),
`DEFERRABLE` (a transaction may see one violated), a PostgreSQL foreign key
added by `ALTER TABLE ... NOT VALID`, a MySQL column-level `REFERENCES`
(parsed and ignored before MySQL 9.0), a foreign key of a MySQL table whose
`ENGINE` is not InnoDB or NDB.

## Catalog fetchers

`discovery/information_schema.py` is one fetcher over any DB-API connection
(no driver is a dependency) that reads the standard `INFORMATION_SCHEMA`
views: `TABLES`, `COLUMNS`, `TABLE_CONSTRAINTS`, `KEY_COLUMN_USAGE`,
`REFERENTIAL_CONSTRAINTS`, `VIEWS`. A subclass per dialect overrides only
the hooks whose standard view is missing or incomplete there:

- Postgres: every hook reads `pg_catalog`, since `information_schema` shows a
  constraint only to a role with a privilege on its table other than SELECT,
  and a view's text only to its owner. It keeps only validated, enforced
  (PostgreSQL 18), non-deferrable constraints, unique indexes over plain key
  columns (not `INCLUDE` ones, no `WHERE`, no expression), no foreign key
  onto a table with row-level security (it may hide the referenced row). A
  table others inherit from returns their rows, which its keys and NOT NULLs
  do not cover, so it is columns only; so is a foreign table. A NOT NULL
  added `NOT VALID` does not count. A nondeterministic column collation is
  reported, since the default compares by identity and it does not.
- MySQL: `COLUMN_TYPE` (an ENUM's members), unique indexes from `STATISTICS`
  with no functional key part, foreign keys from `KEY_COLUMN_USAGE`'s
  `REFERENCED_*` columns (only enforcing engines keep them).
- Redshift: `current_schema()` only, and view text from `pg_get_viewdef`.
  Redshift has no indexes.
- Snowflake: key columns from `SHOW PRIMARY / UNIQUE / IMPORTED KEYS`, as
  Snowflake has no `KEY_COLUMN_USAGE`; `VIEW_DEFINITION` is a whole
  `CREATE VIEW`.
- BigQuery: each dataset's own `project.dataset.INFORMATION_SCHEMA`; the
  fetcher is given the project and default dataset. Foreign keys come from
  `CONSTRAINT_COLUMN_USAGE`, which does not order columns, so only
  single-column ones are read.
- Databricks: the standard views, with `?` parameters.
- Trino: each catalog's own `catalog.information_schema`, with `?`
  parameters; a name may be qualified by any catalog. It has no constraint
  views, and does not surface keys a connector's source enforces, so it
  reports NOT NULLs and no keys.

Collation is the dialect's default: per-table collations are not read, and
of per-column ones only DuckDB's declared `COLLATE`s and Postgres's
nondeterministic collations. A column reported with any collation is not
compared by identity. Text with a `COLLATE` is never taken to compare by
identity, binary or not: collation-aware Properties are issue 49.

Keys are reported as declared: there is no per-constraint trust field, and
`RELY` in a warehouse catalog is not read. Whether a catalog key is
Knowledge is the dialect's `unique_keys` / `foreign_keys` flag, applied by
`DatabaseKnowledgeFetcher.bind` as `ir/convert.py` applies it to keys
written in SQL: Knowledge where enforced, a Property Declaration under
`trust_unenforced_keys`, otherwise an `unenforced-constraint` warning. So a
warehouse key proves something only when the caller trusts every unenforced
key; if one is violated, proofs that depend on it are wrong.

## Judging soundness without DuckDB

The soundness oracle runs DuckDB only. For the other dialects the choice is a
written mapping, plus a live PostgreSQL run of the catalog fetcher
(`tests/test_postgres_catalog.py`, with `SQLASSERT_PSQL` naming a psql
command, e.g. `docker exec -i pg psql -U postgres`).

The mapping: every rule is dialect-independent and already judged by the
DuckDB oracle. A dialect changes only the inputs the rules read, through the
flags above, so a dialect's analysis is sound when (a) the rules are, which
the oracle checks, and (b) each flag it sets is true of that dialect. (b) is
certified fact by fact (next section): on the engine where one runs locally,
from vendor documentation otherwise (Snowflake, BigQuery and Redshift
`checked_arithmetic`/`checked_division`: overflow and division by zero raise;
`TRY_`, `SAFE_` and `DIV0` variants are separate functions SQLGlot parses
apart).

Where a flag is *weaker* than DuckDB's (MySQL, Spark, warehouse text,
keys), the dialect proves a subset of what DuckDB would, so the DuckDB
oracle's judgement of the same rules covers it. The `.sqltest` cases in each dialect
(`tests/dialect_*.sqltest`) pin each flag's effect.

## Certifying facts

Every claim a dialect's flags make, and every Builtin fact
(`strict(op)`, `total(op)`, `injective(op)`) `ir/convert.dialect_builtins`
derives from them, has a provenance in `docs/dialect-provenance.json`:
`{"engine": [versions]}` when probed on a real engine, `{"doc": url, "read":
version or date}` otherwise. A weak setting (unchecked arithmetic, text not
compared by identity, unenforced keys) claims nothing and has none.
`tests/test_dialect_certification.py` checks that every fact has one, that
every fact of a local engine (DuckDB, SQLite, PostgreSQL, MySQL, ClickHouse, Trino)
is certified on an engine, and reruns the DuckDB and SQLite probes.

`scripts/certify_dialects.py` (not part of the package) generates one probe
per fact from the fact itself: the Builtin called with a typed NULL (strict),
with overflowing, zero-dividing and badly cast arguments (total, injective),
or tables whose constraint violations it attempts (keys, text identity, ENUMs).
SQL a dialect writes differently comes from SQLGlot, and is kept only when
sqlassert parses it back to the same node; a construct the engine rejects is
VACUOUS for that engine (`^` in SQLite, an ENUM column in PostgreSQL). Each
fact runs under the engine's defaults and again after the dialect's
permissive settings (`PERMISSIVE`: MySQL `sql_mode = ''`, Spark ANSI off; a setting that
changes the default collation is out of scope). It prints PASS, FAIL (with
the weaker setting to apply by hand), ERROR (the probe could not run) or
VACUOUS per fact, and STRONGER for a weak setting the engine would also
satisfy (never acted on). Unless `--dry-run`, it adds the engine version to
each passing fact and removes it from each failing one; it never adds a fact
or strengthens one. `--list` prints the probes without a connection.

Certification is one-off: rerun it when adding a dialect, changing a fact, or
supporting a new major version. The engines it was run on:

```sh
python scripts/certify_dialects.py duckdb       # in process
python scripts/certify_dialects.py sqlite       # in process
docker run -d --rm --name sqlassert-pg18 -e POSTGRES_PASSWORD=probe -p 55432:5432 postgres:18
python scripts/certify_dialects.py postgres --driver psycopg \
    --connect 'host=127.0.0.1 port=55432 user=postgres password=probe'
docker run -d --rm --name sqlassert-mysql84 -e MYSQL_ROOT_PASSWORD=probe -e MYSQL_DATABASE=probe -p 53306:3306 mysql:8.4
python scripts/certify_dialects.py mysql --driver pymysql \
    --connect '{"host": "127.0.0.1", "port": 53306, "user": "root", "password": "probe", "database": "probe"}'
docker run -d --rm --name sqlassert-clickhouse -e CLICKHOUSE_PASSWORD=probe -p 58123:8123 clickhouse/clickhouse-server
python scripts/certify_dialects.py clickhouse --driver clickhouse_connect.dbapi \
    --connect '{"host": "127.0.0.1", "port": 58123, "username": "default", "password": "probe"}'
docker run -d --rm --name sqlassert-trino -p 58080:8080 trinodb/trino
python scripts/certify_dialects.py trino --driver trino.dbapi \
    --connect '{"host": "127.0.0.1", "port": 58080, "user": "probe", "catalog": "memory", "schema": "default"}'
```

`tests/test_trino_catalog.py` runs the Trino fetcher against the same
server when `SQLASSERT_TRINO` holds its connect arguments
(`{"host": "127.0.0.1", "port": 58080, "user": "probe"}`).

The drivers (`psycopg[binary]`, `pymysql`, `clickhouse-connect`, `trino`) are not
dependencies; install them where the script runs (MySQL is reached at its
port, so MySQL server 8.0, 8.4 and 9 images all work the same way). Wait for a
container to accept connections before probing. Not certified on an engine:
Spark and Databricks (a local Spark needs Java and a DB-API bridge), Hive,
Oracle, MySQL's NDB engine, and the cloud warehouses, whose probes exist for
when credentials do.

## Not done

- A dialect not listed keeps enforced keys and total comparisons, as before
  (T-SQL, Oracle, Presto, Athena, ...). Oracle's `||` reads NULL as `''`, and `''` is
  NULL there, so it is left unmodelled.
- MySQL compares a number with a string as DOUBLE, so an integer column above
  2^53 (or a DECIMAL with more than 15 significant digits) is not pinned by a
  string; neither, in any dialect, by a DOUBLE. `_pins` does not yet look at
  the other side's type.
- MySQL `ANSI_QUOTES` makes `"x"` an identifier rather than a string, and
  `NO_BACKSLASH_ESCAPES` changes string literals; both are read as the default.
- SQLite's column types are affinities, not types: an `INTEGER` column may hold
  text. Its columns are still compared as declared.
