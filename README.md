# sqlassert

**sqlassert adds asserts and static proofs to your SQL, without touching your data, to catch common mistakes before they ship.**

Mark what a query must guarantee (this join never multiplies rows, this column is a key, this `CASE` covers every status) and sqlassert proves it from the SQL and schema alone. A proof holds for *any* data, not just the rows in the table today, and it runs in milliseconds in your test suite or CI, with no warehouse and no connection.

```bash
pip install sqlassert
sqlassert suggest my_query.sql    # what can already be proved, and what looks wrong
```

Using dbt? [`sqlassert-dbt`](https://github.com/polvoazul/sqlassert/tree/main/integrations/dbt#readme) proves your `unique`, `not_null`, `accepted_values` and `relationships` tests from the compiled SQL and **skips the ones that can't fail**, so `dbt build` runs fewer warehouse queries.

_Alpha: the markers (`/**ASSERT ...**/`, `/**DECLARE ...**/`) are the stable contract; the Python API may change. Tested with DuckDB, PostgreSQL, MySQL, Snowflake, BigQuery, Databricks / Spark, Redshift and Trino SQL (see [Dialects](#dialects)); only DuckDB programs are checked by the soundness oracle._

## Pitfalls it catches

### A join that silently multiplies rows

It works today because every user has one email row. Then someone adds a second one, and every downstream count doubles. Say what the join must do:

```sql
create table users (id integer primary key, email varchar);
create table user_emails (user_id integer, email varchar);

select u.id, e.email
from users u
/**ASSERT MANY TO ONE**/ join user_emails e on e.user_id = u.id;
```

```console
$ sqlassert check join.sql
join.sql:6:1: error unproved-many-to-one-join: /**ASSERT MANY TO ONE**/ is not proved (UNKNOWN)
```

Nothing makes `user_emails.user_id` unique, so the join may fan out, and `--explain` says exactly that: "No known unique set on this relation — user_emails". Add the key (or a `GROUP BY`, `DISTINCT`, or `QUALIFY row_number() = 1`) and it is `PROVED`. From then on, a change that breaks it fails CI rather than corrupting a dashboard.

### Fan traps: sums counted twice

Two one-to-many joins from the same row, then a `sum`: each order is counted once per payment.

```sql
select c.id, sum(o.amount) as ordered, sum(p.amount) as paid
from customers c
join orders o on o.customer_id = c.id
join payments p on p.customer_id = c.id
group by c.id;
```

```console
$ sqlassert suggest revenue.sql
...
low  line 9  Root Select  Fan trap  SUM(o.amount) reads rows that JOIN payments AS p may repeat: the join is not proved to repeat rows of only one input (fan trap), so a value may be counted more than once
low  line 9  Root Select  Fan trap  SUM(p.amount) reads rows that JOIN payments AS p may repeat: ...
```

Chasm traps (`sum(c.credit)` over `customers JOIN orders`), scalar subqueries that can return two rows, and implicit casts in join keys are flagged the same way.

### Joins that do nothing, and LIMITs that pick random rows

```sql
select o.id, u.name
from orders o
left join users u on u.id = o.user_id         -- user_id is NOT NULL REFERENCES users(id)
left join coupons c on c.id = o.coupon_id     -- nothing from coupons is used
order by u.name
limit 10;
```

```console
$ sqlassert suggest orders.sql
high  line 7   LEFT JOIN users AS u    INNER JOIN       every left row matches exactly one right row, so the LEFT JOIN pads nothing: write JOIN
high  line 8   LEFT JOIN coupons AS c  Redundant Join   at most one match per row and no column of coupons AS c is used outside its ON: delete the join
mid   line 7   LEFT JOIN users AS u    Exactly One Join /**ASSERT EXACTLY ONE**/
...
low   line 10  Root Select             Arbitrary LIMIT  the ORDER BY does not determine (id), so rows tied on it come in an arbitrary order and LIMIT may keep different rows on each run: order by a key as well
```

Every `high` rewrite is backed by a proof that it leaves the result unchanged, row for row. `--annotate` pastes the suggested markers into the file.

### A CASE that forgets the new status

```sql
with o as (
  select * from orders
  /**DECLARE ENUM(status IN ('new', 'paid', 'void', 'refunded'))**/
)
select id,
  /**ASSERT EXHAUSTIVE**/ case status when 'new' then 'open' when 'paid' then 'closed' when 'void' then 'closed' end as state
from o;
```

```console
$ sqlassert check orders.sql --explain
orders.sql:7:3: error unproved-exhaustive: /**ASSERT EXHAUSTIVE**/ is not proved (UNKNOWN)
    #1 UNKNOWN: exhaustive_case — CASE status WHEN 'new' THEN 'open' ...
      #2 UNKNOWN: status may be 'refunded', which no WHEN arm lists
```

Without the marker, `refunded` orders would quietly get a NULL `state`.

### dbt tests that scan the warehouse for nothing

```console
$ sqlassert-dbt build . -- --target prod
Proved statically (a test here can be skipped):
  models/schema.yml  unique_stg_customers_customer_id  (test on stg_customers)
...
```

Proved tests are passed to `dbt build` as `--exclude`, so they never run. See [sqlassert-dbt](https://github.com/polvoazul/sqlassert/tree/main/integrations/dbt#readme).

## What it proves

| Assertion | Marker | Guards against | `DECLARE` form |
|---|---|---|---|
| [Join cardinality](#join-cardinality) | `UNIQUE`, `MANY TO ONE`, `ONE TO MANY`, `ONE TO ONE` before a join | row multiplication | – |
| [Exactly One Join](#exactly-one-join) | `EXACTLY ONE` before a join | rows multiplied *or dropped*; a LEFT JOIN that pads | – |
| [Unique Set](#unique-set-and-non-null-unique-set) | `UNIQUE(cols)`, `NOT NULL UNIQUE(cols)`, `PRIMARY KEY(cols)` | duplicate keys, wrong grain | yes |
| [Not Null](#not-null) | `NOT NULL(cols)` | NULLs from outer joins and nullable sources | yes |
| [Not Null Elements](#not-null-elements) | `NOT NULL ELEMENTS(cols)` | NULLs inside arrays, rows and maps | yes |
| [Foreign Key](#foreign-key) | `FOREIGN KEY(cols) REFERENCES rel(cols)` | orphaned rows | yes |
| [Functional Dependency](#functional-dependency) | `DETERMINES(a -> b)` | a value that should follow from a key but doesn't | yes |
| [Enum](#enum) | `ENUM(col IN (...))` | unexpected values | yes |
| [Exhaustive CASE](#exhaustive-case) | `EXHAUSTIVE` before a `CASE` | a value no `WHEN` arm handles | – |

Every marker is a SQL comment (`/**ASSERT ...**/`), so annotated SQL still runs unchanged on your database.

| Tool | What it does |
|---|---|
| [`analyze(sql)`](#usage) | Python API: prove a SQL Program's markers from its `CREATE` statements, in your test suite. |
| [`analyze_database`](#usage) | The same, with tables, keys and views read from a live catalog (metadata only, never rows). |
| [`sqlassert check`](#checking-in-ci) | CLI for CI: text, JSON, SARIF and GitHub annotation output, `pyproject.toml` config. |
| [`sqlassert suggest`](#suggesting-markers) | Lists provable markers, proved rewrites, and smells (fan traps, chasm traps, arbitrary LIMIT, ...). |
| [`sqlassert-dbt`](https://github.com/polvoazul/sqlassert/tree/main/integrations/dbt#readme) | Checks a dbt project from its manifest and skips the tests the SQL proves. |

| Dialect | Key constraints | Catalog fetcher | Dialect facts certified on a real engine |
|---|---|---|---|
| DuckDB | enforced | yes | yes, plus the soundness oracle |
| PostgreSQL | enforced | yes | yes |
| MySQL 8.4+ (strict `sql_mode`) | enforced | yes | yes |
| Snowflake | unenforced, `RELY` | yes | – |
| BigQuery | unenforced | yes | – |
| Databricks / Spark | unenforced, `RELY` | yes | – |
| Redshift | unenforced | yes | – |
| Trino | none | yes (NOT NULL only) | – |

Unenforced keys are trusted as declarations by default; `trust_unenforced_keys=False` keeps only those marked `RELY` (see [Dialects](#dialects)).

## Assertions

### Join cardinality

Joins often accidentally multiply rows. A query may look correct against today’s data but silently break when a relation later contains multiple matching rows.

`sqlassert` lets you mark the cardinality a join is expected to have. The words read in FROM order: the left input is everything before the join, the right its joined source.

| Marker | Proved when | Rows the join may repeat |
|---|---|---|
| `/**ASSERT UNIQUE**/` (or `NOT MANY TO MANY`) | either input is unique on the join key | one input's at most |
| `/**ASSERT MANY TO ONE**/` | the right input is unique on the join key | the right input's only |
| `/**ASSERT ONE TO MANY**/` | the left input is unique on the join key | the left input's only |
| `/**ASSERT ONE TO ONE**/` | both are | none |

```sql
select *
from customers
/**ASSERT ONE TO MANY**/ join orders
  on orders.customer_id = customers.id;
```

`UNIQUE` catches the fan trap, a many-to-many join that multiplies both sides. A direction says which side is safe to aggregate: after `customers JOIN orders`, `sum(orders.amount)` is right but `sum(customers.credit)` counts each customer once per order. When `UNIQUE` is proved, its explanation names the input that was unique.

The marker is just a SQL comment. Your SQL remains valid SQL and can still run normally. `sqlassert` reads the query separately and validates that the input is provably unique for the join keys. A SEMI or ANTI join outputs no right rows, so no cardinality marker applies to it (`cardinality-on-filtering-join`).

### Unique Set and Non-Null Unique Set

A Select Expression can assert that specific output columns form a Unique Set. Use `UNIQUE` when the columns may be null, or `NOT NULL UNIQUE` when every column must also be non-null:

```sql
create table users (
  id integer primary key,
  email varchar unique
);

create view active_users as
select id, email
from users
where id > 0
/**ASSERT NOT NULL UNIQUE(id)**/;

select * from active_users;
```

The trailing marker applies to the relation produced by that Select Expression. `/**ASSERT NOT NULL UNIQUE(id)**/` asserts that `(id)` is a specific non-null Unique Set of `active_users`; `/**ASSERT UNIQUE(email)**/` would assert nullable-tolerant uniqueness instead. The assertion is proved from the SQL Program, not trusted as a declaration. If it cannot be proved, its outcome is `UNKNOWN`.

Unique Set Assertions can be attached to a Root Select, view, CTE, or FROM subquery. A proved assertion on a named relation is also available to later statements in the same SQL Program.

A `NOT NULL UNIQUE` assertion is proved when the columns are a Unique Set and each one is non-null.

Uniqueness is proved through functional dependencies, so a key does not have to pass through unchanged. A column pinned to a constant drops out of a key (`select id from items where tenant_id = 5` is unique on `id` when `(tenant_id, id)` is), a column equal to a key column stands in for it (an inner join `on o.user_id = u.id` lets `u.id` replace `o.user_id`), and grouping columns another grouping column determines are not needed (`group by u.id, u.name` is unique on `id`).

`PRIMARY KEY` is accepted as a convenience alias for `NOT NULL UNIQUE` (`/**ASSERT PRIMARY KEY(id)**/`, `/**DECLARE PRIMARY KEY(id)**/`). Unlike SQL's `PRIMARY KEY`, it does not mean one key per relation: a relation may have several Non-Null Unique Sets, and nothing checks that only one is marked.

### Not Null

`/**ASSERT NOT NULL(col, ...)**/` proves that every listed output column is non-null. One assertion covers the whole list, and an `UNKNOWN` explanation names each column that could not be proved. Non-nullness comes from `NOT NULL` and `PRIMARY KEY` constraints, declarations, non-null literals, `coalesce`/`ifnull`/`nvl` with at least one non-null argument, and null-rejecting conditions: a `WHERE` or inner-join `ON` conjunct `col = ...` or `col IS NOT NULL`. It follows pass-through and renamed columns through filters, projections, grouping keys, views, CTEs, and subqueries. The null-extended side of a `LEFT`, `RIGHT`, or `FULL` join is padded with NULLs, and a null-rejecting `WHERE` conjunct on any padded column (`where u.id = 5`) proves that join matched, so every column it padded is non-null again unless it is nullable for its own reasons. An `UNKNOWN` explanation names each source, such as "nickname is nullable in users" and "nickname is padded by the LEFT JOIN". Non-nullness is never guessed for other computed expressions or for set operations.

### Not Null Elements

`/**ASSERT NOT NULL ELEMENTS(col, ...)**/` proves that no listed column's value (an ARRAY, ROW or MAP) holds a NULL element, nested ones included; the value itself may still be NULL. No catalog states it, so it comes only from `/**DECLARE NOT NULL ELEMENTS(col, ...)**/`, followed through pass-through columns, `coalesce` and `CASE`. It matters for comparisons: `ARRAY[1, NULL] <> ARRAY[1, NULL]` and `ROW(1, NULL) = ROW(1, NULL)` are NULL in Trino, so a comparison of composites is non-null only when no element is NULL either. A comparison of a composite column or `CAST` warns (`composite-comparison`) unless its elements are declared. To declare them for a table, select it in a CTE or view that carries the declaration:

```sql
create table posts (id integer, tags array(varchar) not null, old_tags array(varchar) not null);
with tagged as (select id, tags, old_tags from posts /**DECLARE NOT NULL ELEMENTS(tags, old_tags)**/)
select id, tags <> old_tags as retagged from tagged /**ASSERT NOT NULL(retagged)**/
```

A column of unknown type is taken to be a scalar, not a composite. A
constructor (`ARRAY[...]`, `ROW(...)`) is not modelled, so a comparison with
one is never proved non-null.

### Foreign Key

`/**ASSERT FOREIGN KEY(col, ...) REFERENCES relation(col, ...)**/` proves that the listed output columns reference a Unique Set of a named table, view, or CTE. Columns pair by position, so `FOREIGN KEY(org, owner) REFERENCES users(org, name)` pairs `org` with `org` and `owner` with `name`; listing the pairs in another order means the same Foreign Key.

```sql
create table users (id integer primary key);
create table orders (
  id integer,
  user_id integer references users(id)
);

select * from orders
/**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/;
```

A Foreign Key needs two things, and an `UNKNOWN` explanation names whichever is missing:

- Referential coverage: every row whose referencing columns are all non-null has a matching referenced row. It comes from column-level `REFERENCES` and table-level `FOREIGN KEY` constraints in `CREATE TABLE` (a constraint without columns references the primary key), from enforced catalog foreign keys, and from `DECLARE FOREIGN KEY`. It holds on the table itself and on a direct `select * from table`; it is not yet carried through filters, projections, joins, or views.
- A Unique Set on the referenced columns, proved or declared like any other.

Nulls follow SQL's `MATCH SIMPLE`: a row with a null in any referencing column is exempt, so nullable referencing columns do not weaken a Foreign Key. Assert `NOT NULL` separately when every row must match.

`LIMIT` preserves facts about the rows it retains, including outgoing Foreign Keys, uniqueness, non-nullness, and Enumerated Domains. It may remove a required referenced row, so coverage onto its input does not establish coverage onto its result. The same boundary applies to `OFFSET`. A hard limit of at most one row also proves uniqueness without an input key.

Equality Groups merge columns only when their declared comparison types are known and match, without collation. Mixed or unknown types leave value propagation unproved, even when SQL would compare them using implicit casts.

`/**DECLARE FOREIGN KEY(...) REFERENCES relation(...)**/` states the coverage as trusted evidence (`Declared ReferentialCoverage` in explanations); it never vouches for the referenced Unique Set, which is still proved separately. A marker naming a column the referenced relation does not have is reported as `unknown-referenced-column`.

### Exactly One Join

`/**ASSERT EXACTLY ONE**/` before a join proves that every left row matches exactly one right row: the join neither multiplies nor drops left rows, and a `LEFT` join null-extends none of them.

```sql
create table users (id integer primary key);
create table orders (
  id integer,
  user_id integer not null references users(id)
);

select * from orders
/**ASSERT EXACTLY ONE**/ join users
  on orders.user_id = users.id;
```

It is supported on `INNER` and `LEFT` joins whose predicate is a single column-to-column equality (`ON` or a one-column `USING`). It needs three things, and an `UNKNOWN` explanation names each one that is missing:

- At most one match: the right predicate column covers a Unique Set, as for `/**ASSERT MANY TO ONE**/`.
- At least one match: Referential Coverage from the left column onto that exact right column, as for a Foreign Key. Coverage onto a table is not coverage onto a filtered view or CTE of it.
- A non-null left column, since a null matches nothing.

Any additional conjunct in the predicate, even another equality, makes the assertion `UNKNOWN`: it could remove the guaranteed match. Composite keys and joins over derived relations are not supported yet.

Every assertion is written with an explicit `ASSERT`. A bare `/**UNIQUE**/` is rejected with a diagnostic that names the explicit spelling.

### Functional Dependency

`/**ASSERT DETERMINES(a, b -> c, d)**/` proves that rows agreeing on `a` and `b` agree on `c` and `d`. Like a Unique Set it is lax: rows with a null in `a` or `b` are exempt; agreement on `c` and `d` counts NULL as equal to NULL.

```sql
select p.part_id, s.rating
from part p
left join supply s on p.part_id = s.part_id and s.rating = 'A'
/**ASSERT DETERMINES(part_id -> rating)**/;
```

Dependencies come from keys, equalities and constants in `WHERE` and `ON`, grouping, and `DISTINCT`. On an outer join, the preserved side's `ON` columns determine the null-supplying columns the condition binds; a dependency of the null-supplying side survives only when the join cannot pad a row that agrees with a matched one.

`/**DECLARE DETERMINES(zip -> city)**/` states a business rule no schema shows. Closure uses it like any other dependency, so `(zip, street)` is unique when `(city, street)` is and `city` is non-null.

### Enum

`/**ASSERT ENUM(status IN ('new', 'paid'))**/` proves that every non-null value of `status` is one of the listed values. NULL is never in the set: `NOT NULL` governs it separately. One marker names one column, and lists distinct, non-null string, numeric, and boolean literals, which may be mixed: `/**ASSERT ENUM(priority IN (1, 2, 3))**/`, `/**DECLARE ENUM(active IN (TRUE))**/`. Anything else -- `NULL`, an empty list, a repeated value, `DATE '2020-01-01'`, a column, an expression -- is reported as a malformed marker.

```sql
with o as (
  select * from orders
  /**DECLARE ENUM(status IN ('new', 'paid', 'void'))**/
)
select status from o where status <> 'void' and status in ('new', 'paid')
/**ASSERT ENUM(status IN ('new', 'paid'))**/;
```

Domains come from `DECLARE ENUM` and external Knowledge, literals, `coalesce` of enumerated arguments, and simple `CASE` expressions (see below), and follow pass-through and renamed columns through filters, joins, aliases, grouping keys, `DISTINCT`, views, CTEs, and subqueries. A `WHERE` or inner-join `ON` conjunct `status = 'paid'` or `status IN ('new', 'paid')` narrows it, as does an outer join's `ON` on the side it pads. A domain crosses equalities: after `WHERE a = b` (or an inner-join `ON a = b`), `b`'s domain is `a`'s intersected with its own. An asserted set larger than the known domain is proved; an `UNKNOWN` explanation lists each possible value the assertion does not cover, such as "status may be 'void', which the assertion does not cover". A declaration does not claim that every listed value occurs. `UNION` columns, aggregates other than grouping keys, and other computed expressions are not enumerated.

A value keeps its SQL type: numeric `1`, string `'1'`, and `TRUE` are three values, and none covers another. A number is identified by how it is written, because that is its SQL type: `007` is `7`, but `1.0` (a decimal) and `1e0` (a double) are not `1`, so asserting `ENUM(n IN (1))` of a column known to be `1.0` is `UNKNOWN`. Narrowing is more careful than identity: SQL compares a column with a literal after implicit casts, so one value can equal two different literals -- a `FLOAT` equals both `0.1` and `0.100000001`, a `VARCHAR` `'1.4'` equals both `1` and `1.4`, and an `INTEGER` equals both `1` and `'1'`. Domains therefore never lose a value that might equal one on the other side. Distinct strings, too, can equal one value -- an `INTEGER` equals `'01'` and `'1'`, a `VARCHAR COLLATE NOCASE` equals `'a'` and `'A'` -- so they narrow each other only on a column declared `VARCHAR` or `TEXT` compared by identity: with no `COLLATE`, in DuckDB or PostgreSQL (whose default collation is binary). A column of unknown type is not narrowed that way. A MySQL `ENUM` column's domain is its members without trailing spaces, as MySQL stores them (under its default strict `sql_mode`).

### Exhaustive CASE

`/**ASSERT EXHAUSTIVE**/`, written immediately before a simple `CASE`, proves that every value in the operand's known Enumerated Domain has a `WHEN` arm of its own:

```sql
with o as (
  select * from orders
  /**DECLARE ENUM(status IN ('new', 'paid', 'void'))**/
)
select id,
  /**ASSERT EXHAUSTIVE**/ case status when 'new' then 'open' when 'paid' then 'closed' when 'void' then 'closed' end as state
from o;
```

`ELSE` never stands in for an arm: add a status to the declaration without a matching `WHEN`, and the assertion is `UNKNOWN` with "status may be 'refunded', which no WHEN arm lists". Arms cover values by identity, as for `ENUM`: `WHEN 1` does not cover `'1'` or `1.0`. A NULL operand matches no arm; that is `NOT NULL`'s business, asserted separately. With no known domain for the operand, the assertion is `UNKNOWN`.

The marker may mark a `CASE` anywhere it is lowered: a select list, `WHERE`, or a join's `ON`. A `CASE` nested inside a function call or an aggregate is reported as unanalyzed. A searched `CASE WHEN ...`, a `WHEN` value that is not a string, numeric, or boolean literal (including `NULL`), a repeated `WHEN` value, and a marker not immediately followed by `CASE` are reported as malformed markers. Exhaustiveness can only be asserted: `/**DECLARE EXHAUSTIVE**/` is reported.

A simple `CASE` takes one of its `THEN` values or its `ELSE` value (NULL when it has none), so its result has an Enumerated Domain when they all do. When the `CASE` is exhaustive and its operand `NOT NULL`, `ELSE` is unreachable and drops out: above, with `status` also declared `NOT NULL`, `state` is proved `ENUM(state IN ('open', 'closed'))` even if the `CASE` had `ELSE 'unknown'`.

### Declaring what you know

When you know a fact the SQL cannot show -- say, an upstream job guarantees one row per `user_id` -- state it with `DECLARE` instead of `ASSERT`:

```sql
with latest as (
  select user_id, max(ts) as ts from events group by all
  /**DECLARE UNIQUE(user_id)**/
)
select * from orders
/**ASSERT MANY TO ONE**/ join latest on orders.user_id = latest.user_id;
```

A declaration is an assumption, not a proof. It produces no assertion result, and it is trusted evidence for later proofs; explanations show it as `Declared UniqueSet`. Every column marker has a `DECLARE` form -- `DECLARE UNIQUE`, `DECLARE NOT NULL`, `DECLARE NOT NULL UNIQUE`, `DECLARE DETERMINES`, and `DECLARE ENUM` -- attached wherever its `ASSERT` form can be. `DECLARE NOT NULL UNIQUE(a, b)` declares the Unique Set and also each column non-null, so `a` stays non-null after `b` is projected away.

## Usage

Run validation offline, before your application or analytics job executes the query. `analyze` takes a whole SQL Program -- the `CREATE TABLE`/`CREATE VIEW` statements a query depends on, plus the query itself -- and proves its Unique Join and Unique Set Assertions from that declared schema. It never connects to a database.

```python
from sqlassert import analyze

program = """
create table users (id integer primary key, name varchar);
create table sessions (user_id integer);

select *
from sessions
/**ASSERT MANY TO ONE**/ join users
  on sessions.user_id = users.id
"""

report = analyze(program)

assert report.proved, report.diagnostics
```

The default Python engine evaluates the rules directly on the IR:

```python
report = analyze(program)
for assertion in report.assertions:
    print(assertion.evidence.explain())
```

The rules are in `rules/join_cardinality.py`, `rules/uniqueness.py`,
`rules/nullability.py`, and `rules/foreign_key.py`; shared proof composition, declaration lookup, and
per-analysis caching live in `python_engine.py`.

For a test suite, keep your model/query SQL as strings or load them from files, and include (or generate) the `CREATE` statements for whatever schema the query assumes:

```python
def test_query_join_contract():
    program = schema_ddl() + load_query("models/session_enrichment.sql")
    report = analyze(program)

    assert report.proved, report.diagnostics
```

`report.assertions` contains one result per marker, and `report.diagnostics` explains anything the program did that this analysis could not model:

```python
for assertion in report.assertions:
    print(assertion.outcome)          # Outcome.PROVED or Outcome.UNKNOWN
    print(assertion.origin)           # SQL source location

for diagnostic in report.diagnostics:
    print(diagnostic.code, diagnostic.message)
```

`analyze` reads only the SQL Program. To take tables, keys, and views from a
live database instead of `CREATE` statements, pass a catalog fetcher. Fetchers
take a connection from any driver you already use; sqlassert imports none:

```python
import duckdb
from sqlassert import analyze_database
from sqlassert.discovery.duckdb import DuckDBKnowledgeFetcher

report = analyze_database(sql, DuckDBKnowledgeFetcher(duckdb.connect("warehouse.db")))
```

The fetcher reads catalog metadata only (columns, nullability, primary keys,
unique constraints, foreign keys, view definitions), never table rows. A
catalog foreign key is used when the SQL reaches both of its tables. `CREATE` statements in
the SQL take precedence over catalog objects of the same name.

PostgreSQL, MySQL, Snowflake, BigQuery, Databricks, Redshift and Trino have
fetchers too, in `sqlassert.discovery.<dialect>`. They take any DB-API
connection (psycopg, PyMySQL, snowflake-connector-python, ...), so sqlassert
depends on no driver:

```python
import psycopg
from sqlassert.discovery.postgres import PostgresKnowledgeFetcher

report = analyze_database(sql, PostgresKnowledgeFetcher(psycopg.connect(dsn)), dialect="postgres")
```

Warehouse keys are not enforced: catalog keys there are trusted as Property
Declarations by default, and `trust_unenforced_keys=False` ignores them (see
Dialects below). `BigQueryKnowledgeFetcher`
also takes the project and default dataset. Trino's catalog has no keys, so
`TrinoKnowledgeFetcher` reports NOT NULLs only.

### Dialects

Pass `dialect=` (a SQLGlot dialect name) to `analyze`, `analyze_database` and
`suggest`. What the analysis assumes of each dialect is in
`sqlassert/dialects.py`, argued in [docs/dialects.md](https://github.com/polvoazul/sqlassert/blob/main/docs/dialects.md):

- **Keys.** DuckDB, PostgreSQL and MySQL enforce primary, unique and foreign
  keys. Snowflake, BigQuery, Databricks / Spark, Redshift and Trino (which has
  no key constraints) do not, so a key
  there is not Knowledge: it becomes a Property Declaration, since
  `trust_unenforced_keys` is on by default. With `trust_unenforced_keys=False`
  only one marked `RELY` (Snowflake, Databricks / Spark) does, and any other is
  ignored with an `unenforced-constraint` warning. The same holds for a PostgreSQL foreign key added `NOT VALID`, any constraint
  declared `NOT ENFORCED` or `DEFERRABLE`, a MySQL column-level `REFERENCES`
  (ignored before MySQL 9.0), a foreign key of a MySQL table whose `ENGINE`
  does not enforce them, and SQLite foreign keys.
- **Text.** `VARCHAR` and `TEXT` with no `COLLATE` are taken to compare by
  identity in DuckDB, PostgreSQL and Trino, whose shipped default collation is
  binary (Trino has no collations); a setting that changes it (DuckDB's
  `SET default_collation`) is out of scope. MySQL's default collations ignore case, and the warehouses are
  not yet certified, so their text is not. Text with any `COLLATE` is never
  taken to compare by identity.
- **Zoned timestamps.** A TIMESTAMP WITH TIME ZONE compares by identity where
  it holds an instant only (DuckDB, PostgreSQL, MySQL, BigQuery, Spark,
  Redshift). Trino and Snowflake keep the zone it was given, so equal instants
  cast to different text: there it does not, and comparing a `CAST` of one
  warns (`zoned-timestamp-cast`).
- **NULL.** Arithmetic and CAST are taken never to return NULL for non-null
  operands only where they raise instead (DuckDB, PostgreSQL, Snowflake,
  BigQuery, Redshift, Trino); division likewise, except in DuckDB. Spark
  (outside ANSI mode) may return NULL even from a comparison. MySQL is modelled
  under its default strict `sql_mode`, in 8.4 and later: a comparison of
  non-null operands is non-null, arithmetic is not taken as checked. A CAST from JSON may be NULL everywhere (JSON `null`). `||` and
  `concat()` follow each dialect's NULL handling.
- **MySQL `||`** is OR, or concatenation under `sql_mode` `PIPES_AS_CONCAT`,
  which also groups its operands differently, so it is an unknown function of
  its operands, with an `ambiguous-operator` warning: write `OR` or `CONCAT()`.

### Checking in CI

`check` analyzes each SQL file as one SQL Program, from its SQL alone, and fails when any assertion is `UNKNOWN` or a Diagnostic is reported. Connection Warnings (a table with no `CREATE`, say) are printed but never fail it.

```bash
$ sqlassert check models/ 'queries/**/*.sql'
queries/sessions.sql:4:3: error unproved-many-to-one-join: /**ASSERT MANY TO ONE**/ is not proved (UNKNOWN)
models/events.sql: warning undeclared-table: table events has no CREATE TABLE in the program, ...
5 files checked: 9 assertions (8 PROVED, 1 UNKNOWN), 1 error, 1 warning
```

A path is a file, a directory (every `*.sql` under it), or a glob (`**` crosses directories); `-` reads stdin. A path that matches nothing is a usage error rather than an empty pass. Each Finding is placed at the marker itself, line and column.

| Exit status | Meaning |
|---|---|
| `0` | Every assertion PROVED and no Diagnostic (warnings allowed). |
| `1` | An assertion is `UNKNOWN`, or a Diagnostic was reported. |
| `2` | Bad arguments or configuration, or a file that could not be read. Nothing was checked. |

Options:

- `--format text|json|sarif|github`: `text` (default) is `path:line:column: severity code: message` plus a summary; `json` lists every assertion's outcome and every Finding with its span; `sarif` is a SARIF 2.1.0 log for GitHub code scanning; `github` prints `::error`/`::warning` workflow commands, which GitHub Actions shows as annotations on the marker.
- `--explain`: print each `UNKNOWN` assertion's evidence (in `json` and `sarif` too), as `suggest --explain` does.
- `--ignore CODE`: also drop Findings with this code, on top of the configured `ignore` (repeatable, or comma-separated). The codes are the Diagnostic codes plus one per marker for an assertion that is not proved: `unproved-unique`, `unproved-not-null-unique` (also `PRIMARY KEY`), `unproved-not-null`, `unproved-not-null-elements`, `unproved-foreign-key`, `unproved-determines`, `unproved-enum`, `unproved-exhaustive`, `unproved-unique-join`, `unproved-many-to-one-join`, `unproved-one-to-many-join`, `unproved-one-to-one-join`, `unproved-exactly-one-join`.
- `--only-ignore CODE`: drop Findings with these codes instead of the configured `ignore`; `--only-ignore ''` ignores nothing.
- `--dialect NAME`: the SQLGlot dialect (default `duckdb`).

Defaults live in `pyproject.toml`; `check` reads the nearest one with a `[tool.sqlassert]` table, searching up from the working directory. Each flag, when given, replaces its setting (`--ignore` adds to `ignore`; `--only-ignore` replaces it), and paths given on the command line replace `paths`:

```toml
[tool.sqlassert]
dialect = "duckdb"
paths = ["models", "queries/**/*.sql"]  # relative to this pyproject.toml
ignore = ["undeclared-table"]
```

In GitHub Actions, annotate the pull request directly, or upload SARIF to code scanning:

```yaml
- run: sqlassert check --format github
# or
- run: sqlassert check --format sarif > sqlassert.sarif
- uses: github/codeql-action/upload-sarif@v3
  if: always()
  with:
    sarif_file: sqlassert.sarif
```

### Suggesting markers

You rarely start from a blank page. `suggest` reads unannotated SQL and lists what sqlassert can already prove, each with the marker to paste:

```bash
$ sqlassert suggest query.sql        # or: python -m sqlassert suggest query.sql
line 7   view active_users       Non-Null Unique Set  /**ASSERT NOT NULL UNIQUE(id)**/
line 7   view active_users       Unique Set           /**ASSERT UNIQUE(email)**/
line 7   view active_users       Not Null             /**ASSERT NOT NULL(name)**/
line 11  JOIN active_users AS u  Many To One Join     /**ASSERT MANY TO ONE**/
```

A column marker goes at the end of the Select Expression on that line; a join marker goes right before the join. Pick the facts you want locked in: a later change that breaks one turns it `UNKNOWN`. `--explain` prints each proof; `--annotate` pastes every suggested marker into the file (for `-`, it prints the annotated SQL).

From Python, `suggest(sql)` returns the same suggestions, each with its `site`, `property`, `marker`, and `evidence`; `report.annotated()` returns the SQL with every marker pasted in. Only proved properties are suggested, each Unique Set in its strongest form (`NOT NULL UNIQUE` over `UNIQUE`) and minimal (no superset of another suggestion), and each join in its most specific proved form: `EXACTLY ONE`, then `ONE TO ONE`, then `MANY TO ONE` or `ONE TO MANY`. Unique Sets are searched exhaustively up to `max_size` columns (default 3); a wider one is still found when no smaller one exists. Properties the SQL already asserts or declares are not suggested again.

#### Improvements

Before any marker, `suggest` lists improvements to the SQL itself (level `high`; markers are `mid`). Each is a rewrite a proof shows leaves the result unchanged, row for row:

```bash
$ sqlassert suggest orders.sql
high  line 4  CTE big                 Unused CTE          CTE big is never referenced, so it never runs and its markers go unchecked: delete it
high  line 7  LEFT JOIN users AS u    INNER JOIN          every left row matches exactly one right row, so the LEFT JOIN pads nothing: write JOIN
high  line 8  LEFT JOIN coupons AS c  Redundant Join      at most one match per row and no column of coupons AS c is used outside its ON: delete the join
high  line 9  Root Select             Redundant GROUP BY  (o.id, u.name) determine u.id, NULL equal to NULL: drop u.id from GROUP BY and select any_value(u.id) AS user_id
mid   line 7  LEFT JOIN users AS u    Exactly One Join    /**ASSERT EXACTLY ONE**/
...
```

| Improvement | When |
|---|---|
| `INNER JOIN` | A LEFT (or RIGHT) JOIN that never keeps a padded row: it matches exactly one row, or a later inner join's ON, a `WHERE EXISTS`/`IN` subquery, or the WHERE clause of the same Select rejects NULL in a column it pads, directly or through a later LEFT JOIN whose ON rejects that column (so it pads the same rows). |
| Redundant Join | No column of the joined relation is used outside its own ON, and the join keeps every left row once: a LEFT JOIN with at most one match, or an Exactly One inner JOIN. |
| Filter-Only Join | The same for an inner JOIN with at most one match: it only filters, so it can be a `WHERE EXISTS`. Not suggested when a RIGHT or FULL JOIN follows it. |
| Redundant DISTINCT | No two rows agree on the selected columns, NULL equal to NULL. |
| Redundant ORDER BY key | The ORDER BY keys before it determine it, NULL equal to NULL: it never breaks a tie. Silent when a key is an expression or a name sqlassert cannot map to one column. |
| Redundant GROUP BY | The remaining grouping columns determine this one, NULL equal to NULL. One grouping column always stays. |
| Redundant EXISTS | A `WHERE EXISTS`, `IN` subquery or SEMI JOIN every row passes: each row matches exactly one row of it (a non-null Foreign Key onto a key, and an ON of column equalities only). |
| Unused CTE | Nothing in its WITH reaches it, directly or through another CTE. Its markers are never checked either. |

`report.improvements` holds them, each with its `site`, `rewrite`, `message`, and `evidence`; `--explain` prints the proofs. Padding is only followed within the join's own Select Expression: past it, a LIMIT, window, or aggregate may see the padded rows before a filter removes them.

Low-level smells come last: places whose result the SQL leaves unspecified. `report.smells` holds them, each with its `site`, `concern`, `message`, and the failed proof as `evidence`.

| Smell | When |
|---|---|
| Arbitrary LIMIT | A `LIMIT` or `OFFSET` whose `ORDER BY` (if any) does not determine every selected column, NULL equal to NULL: rows tied on the sort keys may come in any order, so which rows are kept can change between runs. Silent when an `ORDER BY` key is an expression or a name sqlassert cannot map to one column. |
| Scalar subquery | A scalar subquery not proved to return at most one row, a runtime error when it returns more: uncorrelated, its body is not proved to have at most one row; correlated, the columns its WHERE binds to the outer row are not proved a Unique Set. |
| Implicit Cast | A join equality between a text column and a column of another declared type: the text is cast, so different strings may match one value (`'01'` and `'1'` both equal 1). No cardinality is proved across it either: cast explicitly, and the proof stays `UNKNOWN`, which is correct. |
| Fan Trap | An aggregate over a join that is not proved to repeat rows of only one input, when its argument (or, for `count(*)`, either input) comes from rows the join may repeat: `sum(p.amount)` over `orders JOIN payments USING (customer_id)`. |
| Chasm Trap | An aggregate over a one-to-many join whose argument is on the side with one row per key: `sum(c.credit)` over `customers JOIN orders`. Every aggregate counts, `min` and `max` included. Silent when the repeated input has no known Unique Set, or the argument reads both inputs. |

`NATURAL JOIN` is unsupported: its presence anywhere in the program produces an `unsupported-natural-join` diagnostic, and `suggest` returns no suggestions. Write explicit `ON` or `USING` conditions instead.

### dbt

The `sqlassert-dbt` plugin, a separate package in this repository (`integrations/dbt/`), checks a compiled dbt project from its `target/manifest.json` (run `dbt compile` or `dbt build` first). It reads only the manifest, so dbt itself is not needed where it runs, and only the latest manifest schema (v12, written by dbt 1.8 through 1.12): compile with a current dbt.

```bash
$ pip install sqlassert-dbt   # depends on sqlassert; sqlassert does not depend on it
$ sqlassert-dbt check . --trust-upstream-tests
models/customers.sql:12: UNKNOWN /**ASSERT NOT NULL(first_name)**/ (customers)

Proved statically (a test here can be skipped):
  models/schema.yml  unique_stg_customers_customer_id  (test on stg_customers)

Not proved (the warehouse run still checks these):
  models/schema.yml  accepted_values_stg_orders_status__placed__shipped__returned  (test on stg_orders): its Property is not proved from the SQL

Proved but untested (schema.yml additions):
  models/schema.yml  customers.customer_id: not_null
```

Each model is analyzed from its compiled SQL. Its own tests are assertions: `unique` is a Unique Set, `not_null` Not Null, `accepted_values` an Enum (a declaration only of a column whose type compares it with its values by identity: text with text, an integer with integers), `relationships` Referential Coverage, `dbt_utils.unique_combination_of_columns` a `NOT NULL UNIQUE` (it counts NULL equal to NULL), and model contract `primary_key`, `unique`, `not_null` and `foreign_key` constraints likewise. The tests of the sources, seeds, snapshots and models it reads are declarations when they passed before it was built, as under `dbt build`: say so with `--trust-upstream-tests` (otherwise a `dbt-untrusted-upstream-tests` warning counts them) -- and only tests that fail on any failing row: not one with `severity: warn`, a `where:` config, or `error_if`/`fail_calc` thresholds; and a constraint only on a contracted table whose adapter enforces it. A test is never a declaration for its own model.

Markers written in model files are checked too, and reported at their line in the model file. An ephemeral model is analyzed where dbt inlined it; an incremental model in both `is_incremental()` branches, and a test of one holds only in both, and for a key only under a `merge` or `delete+insert` strategy on that `unique_key` with no `incremental_predicates`. The other branch is rendered without dbt only when the model uses `ref`, `source`, `this`, `config` and `is_incremental()` alone; otherwise it is reported unanalyzed. A model with markers but no compiled code is an error, not skipped. The exit code is 1 when a marker is `UNKNOWN` or an error is reported (tests not proved do not fail it, unless `--require-proved`), 2 when the manifest cannot be read. Only adapters whose dialect sqlassert models are analyzed; `--dialect` names one explicitly.

To check the markers as part of a dbt run, and skip the tests the SQL proves, run dbt through the plugin:

```bash
$ sqlassert-dbt build . -- --target prod   # or: sqlassert-dbt test
```

It runs `dbt compile`, prints the check, then runs `dbt build` (or `dbt test`) with every proved test passed as `--exclude`. dbt runs the upstream tests in that same invocation, so they are trusted, unless the arguments after `--` select nodes (`--select`, `--exclude`, `--selector`, ...), which may leave one out. The exit code is dbt's when dbt fails, else that of the check.

To see what sqlassert can prove about the models you are editing, as `sqlassert suggest` would on plain SQL:

```bash
$ sqlassert-dbt suggest models/customers.sql models/stg_orders.sql --explain -- --target dev
```

It compiles those models and what they read, then lists the rewrites, markers and smells for each model at its file's lines, with what the model reads declared as `check` declares it. In an incremental model, a rewrite or marker is listed only when both `is_incremental()` branches prove it.

## Details

### Join Cardinality Syntax

Place a cardinality marker (`UNIQUE`, `NOT MANY TO MANY`, `MANY TO ONE`, `ONE TO MANY`, `ONE TO ONE`) immediately before the join it checks:

```sql
select *
from lhs
/**ASSERT UNIQUE**/ left join rhs
  on lhs.rhs_id = rhs.id;
```

`ON` and `USING` are both supported:

```sql
select *
from users
/**ASSERT UNIQUE**/ join user_profiles
  using (id);
```

The marker applies to the next join after the comment.

## Proofs, Not Data Checks

`sqlassert` does **not** validate by querying actual table data. It will not run `count(*)`, search for duplicates, or sample rows.

Instead, it proves uniqueness using information available from the SQL Program and IR-linked Knowledge. If uniqueness cannot be proven, the assertion outcome is `UNKNOWN` and `report.proved` is false. Python assertion results retain structural evidence and expose it through `assertion.evidence.explain()`.

Supported uniqueness proofs today:

- `PRIMARY KEY` and `UNIQUE` constraints from `CREATE TABLE` declarations or IR-linked Knowledge.
- `GROUP BY` subqueries, when the join covers the grouping keys.
- `SELECT DISTINCT` subqueries, when the join covers the selected distinct columns.
- `QUALIFY row_number() over (partition by ...) = 1` subqueries, when the join covers the partition keys.
- Views, CTEs, and subqueries that preserve one of the proofs above, including through their own joins.

A view, CTE, or FROM subquery is lowered like the Root Select, joins and markers included, so its facts flow to every query that reads it. Every view is also a query of its own, checked whether or not anything reads it, so a file of views (a project's models, say) needs no Root Select. A marker in a CTE that nothing reads is reported as unanalyzed. Computed expressions and arbitrary subquery semantics are not guessed.

Examples:

```sql
-- Proved by primary key.
select *
from sessions
/**ASSERT MANY TO ONE**/ join users
  on sessions.user_id = users.id;
```

```sql
-- Proved by composite primary key plus RHS-only filter.
select *
from sessions
/**ASSERT MANY TO ONE**/ join orders
  on sessions.user_id = orders.user_id
 and orders.order_id = 1;
```

```sql
-- Proved by GROUP BY on one side and the primary key on the other.
with latest_session as (
  select user_id, max(ts) as max_ts
  from sessions
  group by user_id
)
select *
from users
/**ASSERT ONE TO ONE**/ join latest_session
  on users.id = latest_session.user_id;
```

```sql
-- Proved by QUALIFY row_number() = 1.
with sessions_ranked as (
    select user_id, *
    from sessions
    qualify row_number() over (partition by user_id order by ts) = 1
)
select *
from users
/**ASSERT ONE TO ONE**/ join sessions_ranked
  on users.id = sessions_ranked.user_id;
```

More compile-time SQL checks can be added under the same model: explicit syntax, fast validation, and clear reasons when a proof is missing.

## Roadmap

Open work is tracked as one file per issue in [`docs/issues/`](https://github.com/polvoazul/sqlassert/blob/main/docs/issues/README.md).
