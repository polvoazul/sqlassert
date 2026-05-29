# Handoff: dbt Unique Join Assertion Package

## Next Session Focus

Start a brand new package that lets dbt users mark joins as uniqueness assertions without changing production SQL. The marker should compile to a SQL comment, and an external parser/test runner should inspect compiled SQL, find marked joins, generate assertion queries, and execute them against DuckDB.

## Conversation Summary

The user is working on a DuckDB `UNIQUE JOIN` idea. In core DuckDB, the current branch already has a core implementation shape involving:

- `JoinRef::is_unique`
- libpg_query/Bison grammar support for `UNIQUE JOIN`
- binder checks that prove RHS uniqueness
- tests under `/Users/fred/src/duckdb/test/sql/join/unique/test_unique_join.test`

We investigated whether DuckDB's new experimental PEG parser could make this distributable as an extension. The conclusion was no, not cleanly. The local DuckDB checkout has PEG parser code in `extension/autocomplete`, including `enable_peg_parser`, but it registers a full parser override rather than exposing a small public API for extensions to inject one join modifier and continue through normal binding. Parser extensibility alone also does not provide the binder semantics needed for `UNIQUE JOIN`.

The user rejected distributing a DuckDB fork because adoption would be poor. We pivoted to a dbt-package strategy.

## Agreed Direction

Use dbt templating only as a marker mechanism. The marker should always render a valid SQL comment, not special SQL syntax.

Example model SQL:

```sql
from sessions s
{{ u }} left join users u
  on s.user_id = u.id
```

Compiled SQL:

```sql
/* unique_join */
left join users u
  on s.user_id = u.id
```

Then a separate custom parser/test runner:

1. Reads dbt compiled SQL from `target/compiled`.
2. Finds `/* unique_join */` comments.
3. Parses the following valid DuckDB join.
4. Infers the RHS and key columns from the join condition.
5. Generates assertion queries, e.g. group by RHS key and fail if duplicates exist.
6. Runs those queries against DuckDB.
7. Reports failures back as package/test output.

Important: DuckDB itself should never see custom syntax in the package path. The SQL remains valid DuckDB because the marker is only a comment.

## Design Preferences From User

- Keep `u` literally just a marker.
- Do not make dbt parse or solve anything.
- Avoid `u()` or `u(...)` unless there is a compelling reason; the user explicitly pushed back on extra macro metadata.
- Heavy work belongs in the external parser/test runner.
- The package should be adoptable and not require a DuckDB fork or custom DuckDB extension.

## Parser Recommendation

Start with SQLGlot in Python:

- It has a DuckDB dialect.
- It exposes an AST.
- It is easier to distribute in a dbt/Python package context.

Alternatives discussed:

- `sqlparser-rs`: viable in Rust, has `DuckDbDialect`, but may be more work for source-span/comment-adjacent logic.
- SQLFluff: supports DuckDB and templating workflows, but likely too heavy and lint-oriented for the semantic rewrite engine.
- pglast: not recommended because DuckDB syntax differs enough from PostgreSQL.

## Suggested V1 Scope

Be strict:

- Only support joins immediately preceded by the marker comment.
- Only support equality predicates in `ON`, joined by `AND`.
- Only infer RHS key columns from simple predicates like `lhs.col = rhs.col`.
- Fail loudly on `OR`, non-equality joins, functions, arbitrary expressions, anti/semi joins, or ambiguous aliases.
- Generate simple duplicate checks against the RHS relation/subquery.

For example:

```sql
select id, count(*) as n
from users
group by id
having count(*) > 1
limit 1
```

For RHS subqueries, wrap the RHS in a CTE before checking duplicates.

## Files/Refs Mentioned

- DuckDB branch/workspace: `/Users/fred/src/duckdb`
- Current branch: `ujoin`
- User test file with current local changes: `/Users/fred/src/duckdb/test/sql/join/unique/test_unique_join.test`
- Core AST flag observed at: `/Users/fred/src/duckdb/src/include/duckdb/parser/tableref/joinref.hpp`
- DuckDB binder uniqueness checks observed at: `/Users/fred/src/duckdb/src/planner/binder/tableref/bind_joinref.cpp`
- DuckDB PEG/autocomplete parser override observed at: `/Users/fred/src/duckdb/extension/autocomplete/autocomplete_extension.cpp`
- SQLGlot docs: https://sqlglot.com/sqlglot.html
- SQLGlot DuckDB dialect docs: https://sqlglot.com/sqlglot/dialects/duckdb.html

## Suggested Skills

- `prototype`: use if the next session wants to quickly build a runnable parser/test-runner proof of concept before committing to package structure.
- `design-an-interface`: use if the next session wants to compare package APIs, CLI shapes, or marker/comment conventions.
- `spreadsheets`/`documents` are not relevant unless the user asks for package docs or planning artifacts.

## Open Questions For Next Session

- Package language: Python/dbt package only, or Python CLI plus dbt macros?
- Marker spelling: `/* unique_join */`, `/* ujoin */`, or something namespaced like `/* dbt_unique_join */`.
- How the tool discovers database connection settings: reuse dbt profiles, accept a DuckDB path, or run through dbt invocation.
- Whether V1 should support CTE/RHS subqueries or only base relations.
- How failures should be surfaced: stdout only, dbt-compatible test result artifacts, or generated dbt generic tests.
