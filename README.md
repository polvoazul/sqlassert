# sqlassert

`sqlassert` is a Python library for adding safety checks to SQL before you run it.

The goal is to catch common query mistakes at test time or build time, using fast static and schema-backed proofs instead of scanning production data. You can add `sqlassert` to your test suite and validate important queries offline, making them more resilient independent of the current contents of your database.


```bash
pip install sqlassert
```

_Alpha warning: Today `sqlassert` supports Unique Join Assertions, Unique Set Assertions (including Non-Null Unique Sets), Not Null Assertions, direct Foreign Keys, and simple Exactly One Join Assertions. It is only tested with the DuckDB SQL dialect._

## Features

### Unique Join

Joins often accidentally multiply rows. A query may look correct against today’s data but silently break when the RHS relation later contains multiple matching rows.

`sqlassert` lets you mark joins that are expected to be unique. That is, the result of the join must never 'grow' the number of rows with respect to the LHS.

```sql
select *
from sessions
/**ASSERT UNIQUE**/ join users
  on sessions.user_id = users.id;
```

The marker is just a SQL comment. Your SQL remains valid SQL and can still run normally. `sqlassert` reads the query separately and validates that the RHS is provably unique for the join keys.

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

- At most one match: the right predicate column covers a Unique Set, as for `/**ASSERT UNIQUE**/`.
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

A value keeps its SQL type: numeric `1`, string `'1'`, and `TRUE` are three values, and none covers another. A number is identified by how it is written, because that is its SQL type: `007` is `7`, but `1.0` (a decimal) and `1e0` (a double) are not `1`, so asserting `ENUM(n IN (1))` of a column known to be `1.0` is `UNKNOWN`. Narrowing is more careful than identity: SQL compares a column with a literal after implicit casts, so one value can equal two different literals -- a `FLOAT` equals both `0.1` and `0.100000001`, a `VARCHAR` `'1.4'` equals both `1` and `1.4`, and an `INTEGER` equals both `1` and `'1'`. Domains therefore never lose a value that might equal one on the other side. Distinct strings, too, can equal one value -- an `INTEGER` equals `'01'` and `'1'`, a `VARCHAR COLLATE NOCASE` equals `'a'` and `'A'` -- so they narrow each other only on a column declared `VARCHAR` or `TEXT` with no `COLLATE`, in DuckDB or PostgreSQL (whose default collation is binary). A column of unknown type, including one read from a database catalog, is not narrowed that way.

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
/**ASSERT UNIQUE**/ join latest on orders.user_id = latest.user_id;
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
/**ASSERT UNIQUE**/ join users
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

The rules are in `rules/unique_join.py`, `rules/uniqueness.py`,
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
live database instead of `CREATE` statements, pass a catalog fetcher
(`pip install sqlassert[duckdb]`):

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

### Suggesting markers

You rarely start from a blank page. `suggest` reads unannotated SQL and lists what sqlassert can already prove, each with the marker to paste:

```bash
$ sqlassert suggest query.sql        # or: python -m sqlassert suggest query.sql
line 7   view active_users       Non-Null Unique Set  /**ASSERT NOT NULL UNIQUE(id)**/
line 7   view active_users       Unique Set           /**ASSERT UNIQUE(email)**/
line 7   view active_users       Not Null             /**ASSERT NOT NULL(name)**/
line 11  JOIN active_users AS u  Unique Join          /**ASSERT UNIQUE**/
```

A column marker goes at the end of the Select Expression on that line; a join marker goes right before the join. Pick the facts you want locked in: a later change that breaks one turns it `UNKNOWN`. `--explain` prints each proof.

From Python, `suggest(sql)` returns the same suggestions, each with its `site`, `property`, `marker`, and `evidence`; `report.annotated()` returns the SQL with every marker pasted in. Only proved properties are suggested, each Unique Set in its strongest form (`NOT NULL UNIQUE` over `UNIQUE`) and minimal (no superset of another suggestion), and each join as `EXACTLY ONE` when that holds, else `UNIQUE`. Unique Sets are searched exhaustively up to `max_size` columns (default 3); a wider one is still found when no smaller one exists. Properties the SQL already asserts or declares are not suggested again.

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
| `INNER JOIN` | A LEFT (or RIGHT) JOIN that never keeps a padded row: it matches exactly one row, or a later inner join's ON or the WHERE clause of the same Select rejects NULL in a column it pads. |
| Redundant Join | No column of the joined relation is used outside its own ON, and the join keeps every left row once: a LEFT JOIN with at most one match, or an Exactly One inner JOIN. |
| Filter-Only Join | The same for an inner JOIN with at most one match: it only filters, so it can be a `WHERE EXISTS`. Not suggested when a RIGHT or FULL JOIN follows it. |
| Redundant DISTINCT | No two rows agree on the selected columns, NULL equal to NULL. |
| Redundant GROUP BY | The remaining grouping columns determine this one, NULL equal to NULL. One grouping column always stays. |
| Unused CTE | Nothing in its WITH reaches it, directly or through another CTE. Its markers are never checked either. |

`report.improvements` holds them, each with its `site`, `rewrite`, `message`, and `evidence`; `--explain` prints the proofs. Padding is only followed within the join's own Select Expression: past it, a LIMIT, window, or aggregate may see the padded rows before a filter removes them.

`NATURAL JOIN` is unsupported: its presence anywhere in the program produces an `unsupported-natural-join` diagnostic, and `suggest` returns no suggestions. Write explicit `ON` or `USING` conditions instead.

## Details

### Unique Join Syntax

Place `/**ASSERT UNIQUE**/` immediately before the join that should be uniqueness-checked:

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
- RHS `GROUP BY` subqueries, when the join covers the grouping keys.
- RHS `SELECT DISTINCT` subqueries, when the join covers the selected distinct columns.
- RHS `QUALIFY row_number() over (partition by ...) = 1` subqueries, when the join covers the partition keys.
- Views, CTEs, and subqueries that preserve one of the proofs above, including through their own joins.

A view, CTE, or FROM subquery is lowered like the Root Select, joins and markers included, so its facts flow to every query that reads it. A marker in a CTE or view that nothing reads is reported as unanalyzed. Computed expressions and arbitrary subquery semantics are not guessed.

Examples:

```sql
-- Proved by primary key.
select *
from sessions
/**ASSERT UNIQUE**/ join users
  on sessions.user_id = users.id;
```

```sql
-- Proved by composite primary key plus RHS-only filter.
select *
from sessions
/**ASSERT UNIQUE**/ join orders
  on sessions.user_id = orders.user_id
 and orders.order_id = 1;
```

```sql
-- Proved by GROUP BY.
with latest_session as (
  select user_id, max(ts) as max_ts
  from sessions
  group by user_id
)
select *
from users
/**ASSERT UNIQUE**/ join latest_session
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
/**ASSERT UNIQUE**/ join sessions_ranked
  on users.id = sessions_ranked.user_id;
```

More compile-time SQL checks can be added under the same model: explicit syntax, fast validation, and clear reasons when a proof is missing.

## Roadmap

Open work is tracked as one file per issue in [`docs/issues/`](docs/issues/README.md).
