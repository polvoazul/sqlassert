# Property Engine MVP Scope

The MVP proves Unique Join Assertions and Unique Set Assertions from a SQL Program and IR-linked Knowledge. Unsupported semantics produce `UNKNOWN` or an explicit unsupported-program diagnostic; they are never silently approximated.

## Included

- `CREATE TABLE` and `CREATE VIEW` statements followed by at most one Root Select.
- Constraints added after a table's `CREATE TABLE`: `ALTER TABLE ... ADD PRIMARY KEY / UNIQUE / FOREIGN KEY`, `ALTER COLUMN ... SET NOT NULL`, and `CREATE UNIQUE INDEX`. A unique index over expressions makes the columns they read a Unique Set when each expression is a pure function of identity-compared columns, non-null when they are (`lower` is not modelled yet, so not known to be). One naming a table not created earlier is diagnosed; so is any other `ALTER TABLE` action, a partial unique index, and an expression unique index that states no Unique Set.
- Declaration collection followed by linker-style resolution, including forward references and cycle detection.
- CTEs, FROM subqueries, and recursively expanded view definitions.
- `NamedRelation`, `Alias`, `Project`, `Filter`, `Limit`, `Join`, `Aggregate`, `Distinct`, `SetOperation`, `QualifyByPartition`, and opaque/recursive relational operations.
- Grouping keys and aggregate expressions.
- `INNER` and `LEFT` Unique Join Assertions.
- `ON` predicates composed from simple equalities joined by `AND`, plus `USING`.
- Non-Null Unique Sets and nullable Unique Sets supplied by DDL or IR-linked Knowledge.
- Unique Set Assertions (`/**ASSERT UNIQUE(...)**/` and `/**ASSERT NOT NULL UNIQUE(...)**/`, alias `PRIMARY KEY`) written on any Select Expression -- a Root Select, a view, a CTE, or a subquery -- proved against Unique Sets already derivable for that Relation Expression's output columns, and fed forward as Knowledge-equivalent facts for later statements.
- A proven-unique join's own output earns every Unique Set of its left-hand input, so uniqueness composes through a join exactly as it does through Filter and Project.
- Uniqueness propagated through grouping, `DISTINCT`, projection, filtering, joining, and recognized `row_number() = 1` partition qualification.
- Not-null Assertions (`/**ASSERT NOT NULL(...)**/`) at the same attachment sites, proved from `NOT NULL`/`PRIMARY KEY` constraints, declarations, non-null literals, `count(...)`, `coalesce` with a non-null argument, and null-rejecting predicates, followed through pass-through columns and lost on the null-extended side of an outer join. `/**ASSERT NOT NULL UNIQUE(...)**/` is proved as a Unique Set whose every column is non-null.
- Property Declarations (`/**DECLARE UNIQUE(...)**/`, `/**DECLARE NOT NULL(...)**/`, `/**DECLARE NOT NULL UNIQUE(...)**/`) at the same attachment sites, trusted without proof and producing no assertion result.
- `PROVED` and `UNKNOWN` outcomes.
- Deterministic Python analysis with `Result` evidence.
- DuckDB catalog hydration (`analyze_database`): columns, nullability, primary keys, unique constraints, and recursively expanded view definitions of the Relations a Program reaches.

## Limitations and Future Features

- `RIGHT`, `FULL`, `SEMI`, `ANTI`, and `CROSS` join analysis.
- Non-equality, null-safe, and `OR` join predicates.
- `NATURAL JOIN` is rejected with a diagnostic; use explicit `ON` or `USING`.
- Scalar and correlated subqueries.
- Grouping sets, `ROLLUP`, and `CUBE`.
- General window-function property reasoning beyond recognized partition qualification.
- A `DISPROVED` outcome backed by genuine refutation evidence.
- Polished multi-level reporting for nested view-expansion provenance.

## Null Semantics

A Unique Set guarantees that fully non-null value combinations do not repeat. A Non-Null Unique Set is a Unique Set whose columns are all non-null. Nullable Unique Sets are sufficient for ordinary equality-join uniqueness because null values do not match; null-safe equality is outside the MVP.
