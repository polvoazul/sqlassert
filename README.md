# assql

`assql` is a Python library for adding safety checks to SQL before you run it.

The goal is to catch common query mistakes at test time or build time, using fast static and metadata-backed proofs instead of scanning production data. You can add `assql` to your test suite and validate important queries offline, making them more resilient independent of the current contents of your database.


```bash
pip install assql # assert + sql => assql
```

_Alpha warning: Today `assql` supports only one check: `/**UNIQUE**/` joins. It is also only tested on duckdb._

## Features

### Unique Join

Joins often accidentally multiply rows. A query may look correct against today’s data but silently break when the RHS relation later contains multiple matching rows.

`assql` lets you mark joins that are expected to be unique. That is, the result of the join must never 'grow' the number of rows with respect to the LHS.

```sql
select *
from sessions
/**UNIQUE**/ join users
  on sessions.user_id = users.id;
```

The marker is just a SQL comment. Your SQL remains valid SQL and can still run normally. `assql` reads the query separately and validates that the RHS is provably unique for the join keys.

## Usage

Run validation offline, before your application or analytics job executes the query:

```python
import duckdb
from assql import validate_unique_joins

con = duckdb.connect("warehouse.duckdb")

query = """
select *
from sessions
/**UNIQUE**/ join users
  on sessions.user_id = users.id
"""

result = validate_unique_joins(con, query)

assert result.valid, result.reason
```

For a test suite, keep your model/query SQL as strings or load them from files, then validate them against a db connection that has the relevant schema:

```python
def test_query_join_contract(con):
    query = load_query("models/session_enrichment.sql")
    result = validate_unique_joins(con, query)

    assert result.valid, result.reason
```

`result.checks` contains one check per marker:

```python
for check in result.checks:
    print(check.valid)
    print(check.reason)
    print(check.inferred_key_columns)
    print(check.constrained_key_columns)
```

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

`assql` does **not** validate by querying actual table data. It will not run `count(*)`, search for duplicates, or sample rows.

Instead, it proves uniqueness using fast information available from the SQL and database metadata. If uniqueness cannot be proven, validation fails with a reason that names the join and RHS column:

```text
in join: "INNER JOIN events ON sessions.event_id = events.id", we can't prove that RHS column id is unique
```

Supported uniqueness proofs today:

- RHS `PRIMARY KEY` and `UNIQUE` constraints from db metadata.
- RHS `GROUP BY` subqueries, when the join covers the grouping keys.
- RHS `SELECT DISTINCT` subqueries, when the join covers the selected distinct columns.
- RHS `QUALIFY row_number() over (partition by ...) = 1` subqueries, when the join covers the partition keys.

It also works across CTEs and views as expected.

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

Views do not currently inherit uniqueness from their underlying definitions. Arbitrary subquery semantics are not guessed. More compile-time SQL checks can be added under the same model: explicit syntax, fast validation, and clear reasons when a proof is missing.
