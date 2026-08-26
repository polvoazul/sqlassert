# sqlassert

`sqlassert` is a Python library for adding safety checks to SQL before you run it.

The goal is to catch common query mistakes at test time or build time, using fast static and metadata-backed proofs instead of scanning production data. You can add `sqlassert` to your test suite and validate important queries offline, making them more resilient independent of the current contents of your database.


```bash
pip install sqlassert
```

_Alpha warning: Today `sqlassert` supports only one check: `/**UNIQUE**/` joins. It is also only tested on duckdb._

## Features

### Unique Join

Joins often accidentally multiply rows. A query may look correct against today’s data but silently break when the RHS relation later contains multiple matching rows.

`sqlassert` lets you mark joins that are expected to be unique. That is, the result of the join must never 'grow' the number of rows with respect to the LHS.

```sql
select *
from sessions
/**UNIQUE**/ join users
  on sessions.user_id = users.id;
```

The marker is just a SQL comment. Your SQL remains valid SQL and can still run normally. `sqlassert` reads the query separately and validates that the RHS is provably unique for the join keys.

## Usage

Run validation offline, before your application or analytics job executes the query. `analyze` takes a whole SQL Program -- the `CREATE TABLE`/`CREATE VIEW` statements a query depends on, plus the query itself -- and proves each `/**UNIQUE**/` assertion from that declared schema. It never connects to a database.

```python
from sqlassert import analyze

program = """
create table users (id integer primary key, name varchar);
create table sessions (user_id integer);

select *
from sessions
/**UNIQUE**/ join users
  on sessions.user_id = users.id
"""

report = analyze(program)

assert report.proved, report.diagnostics
```

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
    print(assertion.proving_unique_set)
    print(assertion.is_candidate_key)
    print(assertion.missing_columns)  # best-effort, when UNKNOWN

for diagnostic in report.diagnostics:
    print(diagnostic.code, diagnostic.message)
```

If some relations aren't declared in the SQL Program itself -- for example, you'd rather introspect a live connection than duplicate its DDL -- pass their properties in as `Knowledge`:

```python
from sqlassert import Knowledge, RelationKnowledge, UniqueSetKnowledge

knowledge = Knowledge((
    RelationKnowledge("users", unique_sets=(UniqueSetKnowledge(("id",)),)),
))

report = analyze(query_only_sql, knowledge=knowledge)
```

> **Migrating from `validate_unique_joins`?** That DuckDB-connection-based checker has been superseded by `analyze` above -- same idea, proved from declared schema instead of a live connection, with broader join/predicate coverage and conservative diagnostics instead of a boolean `valid`/`reason`. It is no longer part of the public API; see [`deprecated/unique.py`](deprecated/unique.py) if you still need it.

## Details

### Unique Join Syntax

Place `/**UNIQUE**/` immediately before the join that should be uniqueness-checked:

```sql
select *
from lhs
/**UNIQUE**/ left join rhs
  on lhs.rhs_id = rhs.id;
```

`ON` and `USING` are both supported:

```sql
select *
from users
/**UNIQUE**/ join user_profiles
  using (id);
```

The marker applies to the next join after the comment.

## Proofs, Not Data Checks

`sqlassert` does **not** validate by querying actual table data. It will not run `count(*)`, search for duplicates, or sample rows.

Instead, it proves uniqueness using fast information available from the SQL and database metadata. If uniqueness cannot be proven, validation fails with a reason that names the join and RHS column:

```text
in join "INNER JOIN events ON sessions.event_id = events.id", we can't prove that RHS column id is unique
```

Supported uniqueness proofs today:

- RHS `PRIMARY KEY` and `UNIQUE` constraints from db metadata.
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
/**UNIQUE**/ join users
  on sessions.user_id = users.id;
```

```sql
-- Proved by composite primary key plus RHS-only filter.
select *
from sessions
/**UNIQUE**/ join orders
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
/**UNIQUE**/ join latest_session
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
/**UNIQUE**/ join sessions_ranked
  on users.id = sessions_ranked.user_id;
```

More compile-time SQL checks can be added under the same model: explicit syntax, fast validation, and clear reasons when a proof is missing.

## Ideas / Todo

- Exhaustive case statements that match all items of an enum / union data type.
- Document that a Select should have a specific unique col combo
- A `UNION`/`INTERSECT`/`EXCEPT` without `ALL` earns a Unique Set over all of its output columns, the same way `SELECT DISTINCT` does -- not modeled yet. Derive that property from the leftmost arm's output columns and lower set-operation bodies when they appear inside a view, CTE, or subquery.
- Column lineage, column unique status, column nullable status
- Functional-dependency reasoning (`X → Y`): represent and derive when equal values of one column set guarantee equal values of another, including dependencies created by keys, deterministic expressions, and aggregation.
- General window modeling: eventually represent window expressions and their filtering as separate `Window` and `Filter` operations, with the proof engine deriving uniqueness from recognized shapes. Until then, keep `QUALIFY row_number() over (partition by ...) = 1` as the explicit supported special case.
