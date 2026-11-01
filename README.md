# sqlassert

`sqlassert` is a Python library for adding safety checks to SQL before you run it.

The goal is to catch common query mistakes at test time or build time, using fast static and schema-backed proofs instead of scanning production data. You can add `sqlassert` to your test suite and validate important queries offline, making them more resilient independent of the current contents of your database.


```bash
pip install sqlassert
```

_Alpha warning: Today `sqlassert` supports Unique Join Assertions and Unique Set Assertions, including Non-Null Unique Sets. It is only tested with the DuckDB SQL dialect._

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

`PRIMARY KEY` is accepted as a convenience alias for `NOT NULL UNIQUE` (`/**ASSERT PRIMARY KEY(id)**/`, `/**DECLARE PRIMARY KEY(id)**/`). Unlike SQL's `PRIMARY KEY`, it does not mean one key per relation: a relation may have several Non-Null Unique Sets, and nothing checks that only one is marked.

### Not Null

`/**ASSERT NOT NULL(col, ...)**/` proves that every listed output column is non-null. One assertion covers the whole list, and an `UNKNOWN` explanation names each column that could not be proved. Non-nullness comes from `NOT NULL` and `PRIMARY KEY` constraints, declarations, non-null literals, and `coalesce`/`ifnull`/`nvl` with at least one non-null argument. It follows pass-through and renamed columns through filters, projections, grouping keys, views, CTEs, and subqueries. It is lost on the null-extended side of a `LEFT`, `RIGHT`, or `FULL` join, and is never guessed for other computed expressions or for set operations.

Every assertion is written with an explicit `ASSERT`. A bare `/**UNIQUE**/` is rejected with a diagnostic that names the explicit spelling.

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

A declaration is an assumption, not a proof. It produces no assertion result, and it is trusted evidence for later proofs; explanations show it as `Declared UniqueSet`. Every column marker has a `DECLARE` form -- `DECLARE UNIQUE`, `DECLARE NOT NULL`, and `DECLARE NOT NULL UNIQUE` -- attached wherever its `ASSERT` form can be. `DECLARE NOT NULL UNIQUE(a, b)` declares the Unique Set and also each column non-null, so `a` stays non-null after `b` is projected away.

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

The rules are in `rules/unique_join.py`, `rules/uniqueness.py`, and
`rules/nullability.py`; shared proof composition, declaration lookup, and
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

From Python, `suggest(sql)` returns the same suggestions, each with its `site`, `property`, `marker`, and `evidence`; `report.annotated()` returns the SQL with every marker pasted in. Only proved properties are suggested, each Unique Set in its strongest form (`NOT NULL UNIQUE` over `UNIQUE`) and minimal (no superset of another suggestion). Unique Sets are searched exhaustively up to `max_size` columns (default 3); a wider one is still found when no smaller one exists. Properties the SQL already asserts or declares are not suggested again.

Database knowledge gatherers bind facts to this Program's IR nodes before it
reaches the engine; `analyze` itself accepts one SQL Program. This work is
tracked in [#14](https://github.com/polvoazul/sqlassert/issues/14).

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
- Simple projection views and subqueries that preserve one of the proofs above.

Views can inherit uniqueness when they are simple projections over a source relation with a supported proof. Filters preserve uniqueness; computed expressions, joins inside views, unions, and arbitrary subquery semantics are not guessed.

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
