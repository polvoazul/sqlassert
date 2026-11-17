# Dialects

`sqlassert/dialects.py` holds one `DialectSemantics` per SQLGlot dialect name:
what that dialect means by the constructs the rules depend on. `ir/convert.py`
reads it while lowering; the rules never see a dialect, only the IR it
produced (Builtin flags, column comparison types, Knowledge and Property
Declarations). This page argues each entry and says how soundness is judged
for dialects the oracle does not run.

## The rule for every flag

A flag claims what the dialect's latest release does under its default
settings, with strict modes on ([ADR 0006](adr/0006-model-only-current-strict-engines.md)).
Where a setting decides a fact, the default decides it: Spark and Databricks
under ANSI mode, MySQL (8.4 and later) under its default strict `sql_mode`,
so `||` is OR. Older versions and settings that loosen what the engine checks
or accepts are out of scope: a looser `sql_mode`, `PIPES_AS_CONCAT`,
`ONLY_FULL_GROUP_BY` off, `INSERT IGNORE`, Spark with ANSI mode off, Spark 2,
MySQL's `foreign_key_checks = 0`. So is a setting that changes the default
collation (DuckDB's `default_collation`, Snowflake's `DEFAULT_DDL_COLLATION`,
Oracle's `NLS_COMP`): text compares under the engine's shipped default.
sqlassert supports no other collation: a `COLLATE`, in an expression or a
column definition, is opaque and warns `unsupported-construct`, and a column
declared with one is untyped, so nothing about its values is proved through
equality or domains (its nullability and keys still hold). On a legacy version or a permissive setting a result may not
be sound. SQL that standard SQL rejects is not given a meaning because a
flavor accepts it (SQLite's column neither grouped nor inside an aggregate):
it is opaque and warns `unsupported-construct`. A flag stays weak where the
default behaviour is not established (not certified on the engine, nor stated
in its docs): the analysis then loses a proof, never gains a wrong one. A
dialect not listed gets `DialectSemantics()`:
cautious arithmetic, collation and `||`, and enforced keys (the behaviour
before dialects were listed; see "Not done").

## Semantics by dialect

| | DuckDB | Postgres | MySQL | Snowflake | BigQuery | Databricks / Spark | Redshift | Trino |
|---|---|---|---|---|---|---|---|---|
| `+ - * CAST` never NULL (`checked_arithmetic`) | yes | yes | no (`-x` of the least BIGINT is an exact DECIMAL, not an error) | no (overflow not documented to raise) | yes | yes (ANSI mode) | yes | yes |
| `/ // %` never NULL (`checked_division`) | no (`n % 0` is NULL) | yes | no | yes | yes | Databricks yes; Spark no (its docs: `div` by zero is NULL) | yes | yes |
| `sum`, `avg` of non-null values never NULL (`checked_sum`) | yes | yes | yes (an integer sum is an exact DECIMAL; no infinity or NaN) | no (not documented) | yes | Spark yes; Databricks no (NaN not documented) | no (not documented) | yes |
| comparisons never NULL (`casts_raise`) | yes | yes | yes (strict `sql_mode`) | yes | yes | Databricks yes; Spark no (not documented) | yes | yes (no implicit casts) |
| text without `COLLATE` compared by identity (`text_is_binary`) | yes | yes | no (`_ci`) | no (not yet certified) | no (not yet certified) | no (not yet certified) | no (`CASE_INSENSITIVE` databases) | yes (no collations) |
| zoned timestamp compared by identity (`zoned_timestamp_is_instant`) | yes | yes | yes (`TIMESTAMP`) | no (`TIMESTAMP_TZ` keeps the zone) | yes (`TIMESTAMP`) | yes (`TIMESTAMP`) | yes | no (keeps the zone) |
| `\|\|` | strict | lenient (`ARRAY[1] \|\| NULL` is `{1}`) | OR | strict | strict | strict | strict | strict (arrays too) |
| `concat()` | skips NULL | skips NULL | strict | strict | strict | strict | not modelled | strict |
| `concat_ws()` | skips NULL | skips NULL | skips NULL | strict | none | skips NULL | none | not modelled (array overload) |
| `greatest()`, `least()` | skip NULL | skip NULL | strict | not modelled | not modelled | Spark skip NULL; Databricks not modelled | not modelled | strict |
| `xs[i]` NULL for a NULL `xs` (`subscript_strict`) | yes | yes | no (no subscripts) | no | no | Spark yes; Databricks no | no | yes |
| primary / unique keys | enforced | enforced | enforced | `RELY` | unenforced | `RELY` | unenforced | none (unenforced) |
| foreign keys | enforced | enforced | enforced (InnoDB, NDB; not inline) | `RELY` | unenforced | `RELY` | unenforced | none (unenforced) |
| `EXCEPT ALL`, `INTERSECT ALL` (`set_operations_all`) | yes | yes | yes (8.0.31) | no | no | yes | no | yes |
| VALUES column of literals (`values_types`) | string, integer, bigint, decimal | string, integer, bigint (no decimal: an unscaled NUMERIC) | string, bigint for every integer, decimal (`ROW(...)` rows not lowered) | none (not certified) | none (no VALUES) | Spark string, int, bigint, decimal (engine); Databricks the same from memory | none (no column list) | string, integer, bigint, decimal |

A VALUES column whose non-NULL values are literals of one kind is typed as
the engine types it (`DialectSemantics.values_types`, probed as
`values_type(KIND)=TYPE`), and then equality binds it as it binds a declared
column: text by identity only where `text_is_binary` holds, an integer to any
other signed integer, a decimal to a decimal of the same `DECIMAL(p, s)`. An
integer is INTEGER within its range (BIGINT in MySQL) and BIGINT up to BIGINT's
range; a decimal is typed only when written `d.f` with a whole part not
starting with 0 (`0.5` is DECIMAL(2, 1) in DuckDB and MySQL, DECIMAL(1, 1) in
Trino and Spark) and at most 18 digits either side of the point. Mixed kinds
(the engine casts or raises), NULL alone, a boolean, an exponent (DOUBLE), an
integer beyond BIGINT (HUGEINT, DECIMAL, NUMERIC or BIGINT UNSIGNED by engine)
and any expression stay untyped.

Also: SQLite foreign keys are unenforced (`PRAGMA foreign_keys` is off by
default) and its primary keys may be NULL (only an `INTEGER PRIMARY KEY` may
not, and SQLGlot reads `INT` and `INTEGER` alike, so none is taken to be
non-null). A ClickHouse primary key never rejects a duplicate, and its
`concat_ws` is NULL for a NULL value, as Snowflake's. Hive has neither checked
arithmetic nor enforced keys, and its `concat_ws` is not modelled.
`lower`, `upper` and `length` are strict and never NULL for a non-null
argument in every dialect; so is `trim`, except where `''` is NULL (Oracle:
`trim(' ')` is NULL). `abs`, `floor` and `ceil` are strict, and never NULL
for a non-null argument only where `checked_arithmetic` holds: they read text
as a number, which SQLite turns into NULL (`floor('x')`) although its
comparisons raise. `nullif(a, b)` is strict in `a` only. A call with more
arguments (`trim(x, chars)`, `floor(x, 2)`) is not modelled.
`set_operations_all` only gates suggesting `EXCEPT ALL` and `INTERSECT ALL`
(`suggest`): a wrong value suggests SQL the engine rejects, never a wrong
proof, so it is no fact and is not certified. It is off for SQLite, Oracle
(before 21c), ClickHouse and Hive, and any dialect not listed.
A CAST from JSON (or a VARIANT) may be NULL in every dialect: JSON `null` is
a value, and `CAST(JSON 'null' AS BIGINT)` is NULL (DuckDB, Trino, Snowflake's
`PARSE_JSON('null')::INT`). So a CAST is never taken to be non-null when its
operand is known to be JSON: a column declared `JSON`, `JSONB`, `VARIANT` or
`OBJECT`, a CAST to one, a JSON function, or a `COALESCE` or `CASE` of one. A
column of unknown type may be JSON, so the CAST remains potentially nullable.
A comparison (`<>`, `<`, `BETWEEN`, ...) of composites, ARRAYs, ROWs or MAPs,
is taken to be non-null only when no element of either operand is NULL
either, in every dialect: Trino's `ARRAY[1, NULL] <> ARRAY[1, NULL]` and
PostgreSQL's `ROW(1, NULL) = ROW(1, NULL)` are NULL (DuckDB's lists and
PostgreSQL's arrays compare NULL elements as equal, but are not singled out).
An operand is composite when declared or cast so, or built by `ARRAY[...]`,
`ROW(...)` or `MAP(...)` (whose elements are not modelled). Unknown column
types may be composite.
`NOT NULL ELEMENTS` declares its elements non-null, and a comparison of a
composite column or CAST without one warns (`composite-comparison`).
`greatest()` and `least()` skip NULLs in DuckDB, PostgreSQL and Spark: NULL only
when every argument is, so non-null when any argument is (`skips_null`). In
MySQL and Trino they are strict: NULL when any argument is (`greatest =
"strict"`); they may be NULL for non-null arguments there too, which is not
modelled. Elsewhere they are not modelled: Snowflake's and BigQuery's are
documented NULL for any NULL argument, and neither is certified on an engine
yet; Databricks is Spark, but only Apache Spark is certified. A subscript
(`xs[i]`, a list, array, map or struct element) is strict in its container
in DuckDB, PostgreSQL, Trino and Spark,
and may be NULL for a non-null one (an index out of range, a NULL element;
Trino raises out of range); MySQL has no subscript, and it is not modelled
elsewhere (ClickHouse returns a default value out of range, and its arrays
are never NULL). Both facts are certified on each engine they are claimed for.

`sum()` and `avg()` of non-null values are non-null (`Accumulation`) only
where `checked_sum` holds: an overflow raises or widens the type rather than
returning NULL, and NaN or an infinity is a value. DuckDB, PostgreSQL, MySQL
Trino and Spark are certified; BigQuery's docs say so. SQLite's sum of `9e999` and
`-9e999` is NULL (its NaN is NULL), so it is off there; Snowflake's and
Redshift's docs do not say, and Databricks's do not mention NaN.
MySQL's DOUBLE sum that overflows returns 0, not an error: wrong, but not NULL.
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

A TIMESTAMP WITH TIME ZONE (`zoned_timestamp_is_instant`; SQLGlot reads
MySQL's, BigQuery's and Spark's `TIMESTAMP` as one) holds an instant in
DuckDB, PostgreSQL, MySQL, BigQuery, Spark and Redshift: the zone it was
given only converts it, so equal values are identical and their text, in the
session's zone, is equal too. Trino, Snowflake's `TIMESTAMP_TZ` and Oracle
keep the zone: `TIMESTAMP '2024-01-01 01:00 +01:00'` equals
`TIMESTAMP '2024-01-01 00:00 UTC'`, yet the two cast to different `VARCHAR`,
`DATE` or local `TIMESTAMP` values. There a zoned timestamp is not compared by
identity, and a comparison with a `CAST` of a zoned column warns
(`zoned-timestamp-cast`): its result depends on the zone each row was given.
A dialect not listed is taken to keep the zone.

MySQL ENUMs (`enum_strips_trailing_spaces`): strict mode rejects a value
outside the members (the `''` MySQL stores for one outside strict mode or
under `INSERT IGNORE` is out of scope), and MySQL strips trailing spaces from
members, so an ENUM column's domain is its members without trailing spaces.
Its comparisons follow the column's
collation, so `e = 'A'` keeps the member `'a'`; ENUM narrowing never treats
distinct strings as distinct values there.

MySQL `||` is OR, as SQLGlot parses it: the default `sql_mode` has no
`PIPES_AS_CONCAT` (ADR 0006), so `a = 1 AND b || c` is `(a = 1 AND b) OR c`.
MySQL 8.4 deprecates `||` as OR; a deprecation is no reason to warn.

## Unenforced keys (decision)

An unenforced key is never Knowledge (`Accepted`), so it never proves anything
the SQL alone does not. It becomes a Property Declaration (`Declared`, as a
`DECLARE` marker does) when:

- it is marked `RELY` in a dialect that has `RELY` (Snowflake, Databricks /
  Spark): the author's statement that the data satisfies it, and the
  warehouse's own optimizer trusts it on the same terms; or
- `trust_unenforced_keys` is on, the default of `analyze`,
  `analyze_database` and `suggest`: every constraint the database does not
  check, in BigQuery and Redshift (which have no `RELY`) and everywhere else.

With `trust_unenforced_keys=False` it is ignored, and an
`unenforced-constraint` warning says why and how to trust it. A trusted primary key declares its columns non-null too.

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
`ENGINE` is not InnoDB or NDB, or that names no `ENGINE` (the server's
`default_storage_engine` decides, and may be MyISAM).

CHECK constraints follow the same rule (`check_constraints`). DuckDB,
PostgreSQL, SQLite, MySQL 8.4, Oracle and ClickHouse check them. Snowflake,
BigQuery, Redshift and Trino have none, so one in the SQL describes a table
another engine holds; Hive's and Spark's are not taken as checked (Databricks
checks them only on Delta tables). A CHECK has no `RELY`. A PostgreSQL CHECK
added `NOT VALID` is not Knowledge either.

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
  added `NOT VALID` does not count. A column under a nondeterministic
  collation is reported untyped, since the default compares by identity and
  it does not.
  Names that differ only in case are distinct relations, and the analysis
  does not keep a name's quoting, so a name denotes a relation spelled as
  written or folded to lower case, and none when relations spelled both ways
  are on its search path (`t` and `"T"`: whether it was quoted decides).
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

Collation is the dialect's default, the only one sqlassert supports:
per-table and per-column collations are not read, except that a column
declared with a `COLLATE` in DuckDB, or under a nondeterministic collation in
Postgres, is reported untyped, so it is never taken to compare by identity.

Keys are reported as declared: there is no per-constraint trust field, and
`RELY` in a warehouse catalog is not read. Whether a catalog key is
Knowledge is the dialect's `unique_keys` / `foreign_keys` flag, applied by
`DatabaseKnowledgeFetcher.bind` as `ir/convert.py` applies it to keys
written in SQL: Knowledge where enforced, a Property Declaration under
`trust_unenforced_keys` (the default), otherwise an `unenforced-constraint`
warning. So a warehouse key proves what it states unless the caller opts out;
if one is violated, proofs that depend on it are wrong.

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
from vendor documentation otherwise (BigQuery, Redshift, Spark and Databricks
`checked_arithmetic`, and Snowflake, BigQuery, Redshift and Databricks
`checked_division`: overflow and division by zero raise, in Spark and
Databricks under ANSI mode, their default;
`TRY_`, `SAFE_` and `DIV0` variants are separate functions SQLGlot parses
apart).

Where a flag is *weaker* than DuckDB's (MySQL, Spark, warehouse text,
keys), the dialect proves a subset of what DuckDB would, so the DuckDB
oracle's judgement of the same rules covers it. The `.sqltest` cases in each dialect
(`tests/dialect_*.sqltest`) pin each flag's effect.

## Types of computed expressions

An expression has the type of a column declared with it when it is known
(`rules/value_typing.py`, `scalar_typing`): a select item (projected, a
grouping key, a DISTINCT or QUALIFY item) has its expression's, and so does an
operand of any equality. The type says how values compare, never that the
expression is injective. Where a claim has no flag it holds under every
setting of every dialect, or the construct stays untyped. Provenance: DuckDB 2.0.0, probed with `typeof` (the
cases are in `tests/computed_select_item_types.sqltest`); every other engine
"from memory". None is in `docs/dialect-provenance.json`: no flag is
certified by `scripts/certify_dialects.py`.

- `CAST(x AS T)` and `TRY_CAST` (a failure is NULL, which equals nothing) are of
  type `T`, as a column declared `T` is: identity comparison, binary text and
  exact numeric follow `DialectSemantics` (`text_is_binary`, `unscaled_decimal`,
  `zoned_timestamp_is_instant`), so a cast to TIMESTAMP WITH TIME ZONE is
  identical only where it is an instant (not Trino, Snowflake, Oracle). To
  text, only the cast of a number of a declared type: a cast of text may keep
  the operand's collation (Postgres), which is not modelled. Standard SQL in
  every dialect; SQLite has no column types (a declared one is an affinity, a
  cast to `DATE` is `NUMERIC`), where a declared column is typed by the same
  rule, so the claim is no weaker there than for columns.
- `CASE`, `IF`, `COALESCE` (`NVL`, `IFNULL`) and `NULLIF` whose non-NULL
  results (a missing ELSE and a NULL literal are none) are all of one type
  are of that type: the standard's union type of identical types, in every
  dialect. Their results are typed the same way, so a nested CASE, or a
  literal of a type the dialect claims (below), is a typed result. Results
  that are signed integer types of different widths, or integer literals (within INT) beside them, are an
  integer type, labelled by the widest (DuckDB: `CASE WHEN c THEN smallint_col
  ELSE 0 END` is SMALLINT and `COALESCE(int_col, bigint_col)` BIGINT, both an
  integer type; engines differ in which, none in being exact). A string literal
  beside text compared by identity is text: DuckDB VARCHAR (probed); Postgres
  resolves the literal as the column's type and Trino as `varchar(n)`, from
  memory, and no other dialect is `text_is_binary`. Results of different
  types and a cast to text stay untyped: the hazard `Mixed CASE types` is for
  the differing ones.
  `NULLIF(a, b)` is read as a CASE over `a` and `b`: DuckDB returns the type of
  `a` (`NULLIF(int, 1.5)` is INTEGER), but another engine may compare in the
  common type.
- `row_number() OVER (...)`, written exactly so (its Unique Set with the
  partition keys needs that), is an integer type that is never NULL, typed
  as `BIGINT` wherever it is written (an
  integer, not claimed to be that one: DuckDB `typeof` gives BIGINT; SQLite
  `typeof` gives `integer`; from memory, Postgres, Trino and Redshift BIGINT,
  MySQL BIGINT UNSIGNED, Spark, Databricks and Hive INT, BigQuery INT64,
  Snowflake NUMBER, ClickHouse UInt64). Its Unique Set with the partition keys
  needs no flag: the numbers are 1..n within each partition, whatever the
  engine (NULL keys form one partition, DuckDB probed). RANK, DENSE_RANK and
  NTILE repeat and claim nothing.

- A column of a recursive CTE (or view) has the type of the base case's column
  (`RecursiveRelation.typed_by`): the recursive arm reads it typed, and so does
  every reference to the CTE. DuckDB casts the recursive arm to the base case's
  type (probed on DuckDB 2.0.0: base `1::INTEGER`, recursive arm `n * 1.5`, gives
  an INTEGER column with the values 2, 3, 5). Every other modelled dialect is
  read the same way, from memory and not probed: Postgres rejects an arm of
  another type, MySQL infers the columns' types from the base case alone, and
  for Trino, BigQuery, Snowflake, Spark, Databricks, Redshift and Hive it is
  the assumption that they do one or the other. Typing only: the base
  case's values, NULLs, keys, domains and lineage are not shared, since the
  recursive rows differ. The CTE's own column (the set operation's) is typed
  where both its arms are, so a passed-through column is, and `n + 1` over an
  INTEGER `n` is; an arm that computes another type leaves it untyped, though
  the engine would cast it. With several base arms (`a UNION b UNION
  recursive`) the base case is the set operation of them, typed where they share a
  type. A column alias list (`r(a, b)`) names the base case's columns by
  position; a base case whose columns are not all known, or whose name appears
  twice, types nothing.

## Expressions an ON equality binds

For one row of a join's other input, an expression of that row's columns is
one value, so an ON equality `m.c = f(o.x)` fixes the matched column `c` for
each `o` row, if the comparison loses nothing (`rules/join_cardinality.py`,
`_bound_columns`). `f` is any deterministic expression (`ir.determinants`:
no volatile or user-defined function, aggregate, window or subquery); it
need not be injective, nor `x` compared by identity, since it is one row.
`-m.c = o.y` or `m.c + 1 = o.y` fixes `c` too, only because the matched side
is injective in one exact column (`Builtin.injective`, the
`checked_arithmetic` dialects: DuckDB, PostgreSQL, Trino, BigQuery, Redshift,
Spark and Databricks; `injective(op)` in `docs/dialect-provenance.json`).
A CAST is not injective, so `CAST(m.ts AS DATE) = o.day` binds nothing of `m`
while `m.day = CAST(o.ts AS DATE)` binds `day`.

Losing nothing is `exact_comparison` of the two operands
(`rules/value_typing.py`), the one question every equality asks (what it pins,
binds or merges, the dependencies it gives, and the Hazard `Lossy
comparison`): both operands have a type, and either both are text compared by
identity, or both have one type whose equal values are identical, or both are
signed integers (of any width), or one is an integer or decimal literal and
the other an integer or decimal. A float binds nothing in either direction
(`-0.0 = 0.0`, and a BIGINT compared with a DOUBLE rounds: 2^53 and 2^53 + 1
are equal there), and neither does anything of unknown type.

An operand has a type where `scalar_typing` finds one: a column's declared
type (or its expression's); a CAST's (`ir.Call.typing`, the claim of `Types of
computed expressions` above); a literal's (below); `count(...)`, an integer;
arithmetic: `+`, `-`, `*` and negation of integers is an integer where the
dialect's arithmetic is checked (`Builtin.checked`: DuckDB, PostgreSQL,
Trino, BigQuery, Redshift, Spark and Databricks), the negation of a decimal has
its type, and a decimal with an integer or decimal literal added is a decimal
whose precision is not claimed (compared only with a literal, since
DECIMAL(10,2) + 1 is DECIMAL(13,2)); and any arithmetic with a float operand is
a float, in every dialect. A quotient, a remainder, any other call, and
anything of an operand with no type, is untyped. Probed on DuckDB 2.0.0 with
`typeof`: `-x` is `x`'s type for TINYINT through HUGEINT, DECIMAL(10,2) and
DOUBLE; `x + 1`, `10 - x` and `i * 2` keep an INTEGER and a BIGINT;
`DECIMAL(10,2) + 1` is DECIMAL(13,2) and `+ 0.5` DECIMAL(11,2); `x / 2` of
integers is a DOUBLE; `1e0 + 1` is a DOUBLE; `-i`, `i + 1`, `10 - i` and
`i * 2` raise on the least or greatest INTEGER instead of wrapping. Every other
engine, from memory: negation keeps the operand's integer or decimal type
(PostgreSQL's NUMERIC drops the precision, a value compare either way), and
integer arithmetic is an integer type. In a dialect that is not
`checked_arithmetic` (MySQL, Snowflake, SQLite, ClickHouse, Hive, Oracle) no
Builtin is injective or checked, so integer arithmetic is untyped and binds
nothing; a float operand still makes a float.

A literal has a type from its kind (`IrParser._literal_typing`): a string is
text compared by identity where the dialect's is (DuckDB `typeof`: VARCHAR);
a boolean a BOOLEAN; an integer an INT where it fits, else a BIGINT (DuckDB:
INTEGER, BIGINT, then HUGEINT, which here is a decimal); a number of more than
38 digits none (DuckDB turns it into a DOUBLE). A number written with a point
(`0.1`) or an exponent (`1e0`) has the type `DialectSemantics.literal_types`
claims for its kind, and none where it claims none (SQLite, Oracle): DuckDB
probed (`0.1` is DECIMAL(2,1), `1e0` and `1.5e3` DOUBLE, `123456789012345678901234567890123456789.5`
DOUBLE); from memory, the vendor documentation for the rest: a decimal and an
exponent literal are exact and a DOUBLE in MySQL, Spark, Databricks, Trino and
Snowflake (`1e0` FLOAT); PostgreSQL's NUMERIC constants take a point or an
exponent (an unscaled NUMERIC, so neither identical nor exact in the sense of a
column); Redshift's decimal is NUMERIC; BigQuery, ClickHouse and Hive read both
as floating point. Not certified by `scripts/certify_dialects.py`.

IS NOT DISTINCT FROM also matches NULL to NULL, so it fixes a column non-null
only where the other operand is never NULL: `ir.total` of it, over columns
proved non-null. `=` never matches a NULL operand, so an expression that is
NULL for a value (NULLIF, TRY_CAST, a CASE without ELSE) only removes
matches and does not change `definite`; those are untyped anyway.

## Stars and names

Two facts about the names a SELECT's output has decide whether a column can be
bound by name or position. DuckDB and SQLite are probed
(`tests/test_using_star_probe.py`, DuckDB 2.0.0 and SQLite 3.50.4); every other
engine is **from memory**, not probed, and the standard is assumed (an owner decision).

**Star over a USING join.** `*` over `a JOIN b USING (c1, c2)` has each USING
column once, as the merged column (the left input's for INNER and LEFT, the
right's for RIGHT, `COALESCE(a.c, b.c)` for FULL), and then the inputs' other
columns; `a.*` keeps all of a's columns, `a.k` included. Where the merged
column sits is the engines' difference:

| | DuckDB, SQLite | every other engine modelled |
|---|---|---|
| `*` over `a(x, k, m) JOIN b(y, m, k, z) USING (k)` | `x, k, m, y, m, z`: the column stays at its left input's position and the right input's is dropped (probed; `USING (m, k)` is `x, k, m, y, z`, the order of the inputs, not of the list; a nested `JOIN c USING (k)` merges the same `k`) | `k, x, m, y, m, z`: the USING columns first, in USING-list order, then the left input's others, then the right's (SQL standard; from memory) |
| `DialectSemantics.using_column_stays_in_place` | `True` | `False` |

MySQL's manual orders the coalesced columns by their order in the first table,
which differs from the standard when the USING list is in another order (from
memory; one more case where the two orders disagree, and only then). A USING
name that is not one column of each input (or is a name an engine may have
renamed, below) leaves the join's columns unknown: a star over it stays
unexpanded.

**Duplicate output names of a derived table.** A subquery, CTE, view or
aliased join whose outputs repeat a name:

| | |
|---|---|
| DuckDB | renames the later ones `k_1`, `k_2`, ... (case-insensitively: `k`, `K` gives `K_1`), and a real `k_1` in the way becomes `k_1_1` (probed: `SELECT 1 AS k, 2 AS k, 3 AS k_1` is `k, k_1, k_1_1`; `k, k_1, k` is `k, k_1, k_2`; a view and a CTE the same) |
| SQLite | renames the later ones `k:1` (probed) |
| Postgres, MySQL, Redshift, Snowflake, BigQuery, Databricks / Spark, Trino | from memory: reject a reference to the repeated name as ambiguous (MySQL rejects the derived table itself); none is known to rename |

So which column `s.k_1` is depends on the engine, and the real `k_1` is not
`s.k_1` in DuckDB. sqlassert treats every dialect like DuckDB where that is
sound for the others too: the first column of a name keeps it, each repeat
has an internal name no reference writes (`__duplicate_column_k_1`), and
any reference to `<repeated>_<digits>` (or `<repeated>:<digits>`) in that derived table binds
nothing and warns `ambiguous-duplicate-column`. The warning is a new code,
not `ambiguous-column`, whose message is about columns a connection would
supply; this one is about the SQL, which a connection cannot answer, so
`analyze_database` keeps it. A reference that is an error in an engine
needs no meaning there.

## Table functions

A table function or UNNEST in FROM has the columns the dialect fixes for it
(`DialectSemantics.table_functions`, probed as `table_function(NAME)=COLUMNS`),
and its rows nothing is known of: an opaque relation, no key, no dependency,
no type. `SELECT *` over the call returns exactly those columns whatever its
arguments; an alias column list renames the leading ones (a longer list is
ignored), and an alias with no list names the one column of a function in
`DialectSemantics.alias_named_table_functions` (DuckDB: `range(3) AS r` has
column `r`, though `r.range` also resolves, which is not modelled; not Spark's
`range(3) AS r`, whose column stays `id`). A function not listed, an UNNEST of
several arguments or `WITH ORDINALITY`, and any function of another dialect keep
columns unknown: an incomplete schema, so an unqualified name beside it is an
`ambiguous-column` warning, and the call is an `unsupported-construct`.

| | DuckDB 2.0.0 | SQLite 3.50.4 | BigQuery | Redshift | Spark, Databricks |
| --- | --- | --- | --- | --- | --- |
| `json_each`, `json_tree` | `key`, `value`, `type`, `atom`, `id`, `parent`, `fullkey`, `path` (probed, also with a path argument) | the same (probed with `pragma_table_info`; no alias column list exists) | | | |
| `range`, `generate_series` | one column, named like the function (`range`, `generate_series`) | none | | | `range`: the one column `id` whatever the alias (from memory) |
| `unnest` of one argument | one column `unnest`; DuckDB's FROM-clause UNNEST takes a list only | none | `UNNEST(xs) AS x`: the one column `x` (from memory) | `FROM t, t.xs AS x`, a SUPER array: the one column `x` (from memory) | |

SQLGlot reads BigQuery's `UNNEST(xs) AS x` and Redshift's `t.xs AS x` as an
UNNEST whose alias is a column list, so the column is named by it; unaliased,
the engine's column has no name, and the one named `unnest` here is never
read. Both engines spread an array of structs (an element that is an object,
in Redshift) over the struct's fields in a `SELECT *`, where this is one
column: a field is then an unresolved name (opaque, as any name the schema
does not have), never a wrong one, and a star's width differs, so a set
operation pairs its columns only where the widths agree, and those are opaque.
Provenance of every claim in those columns:
from memory, `tentative` in `docs/dialect-provenance.json`. No engine is
probed (decision: claims beyond what is already probed are made from memory).

Not claimed, each from memory, and why:

- Postgres `generate_series`, `unnest`, `regexp_split_to_table` (one column,
  named by the alias if written, else by the function: `chooseScalarFunctionAlias`),
  `jsonb_array_elements`, `json_array_elements` and their `_text` forms (one
  column `value`, whatever the alias, because the function has a named OUT
  parameter), `jsonb_each`, `json_each` and their `_text` forms (`key`,
  `value`). Postgres runs locally, so every fact of it must be probed on an
  engine (`tests/test_dialect_certification.py`: a local engine's fact may not
  be `tentative` or `doc`, and `DOCUMENTED_ONLY` lists only what no local engine
  can run), and `scripts/certify_dialects.py` has no Postgres probe of table
  functions. Postgres's `unnest` of an array of a composite type also spreads
  the type's fields over columns.
- Trino `UNNEST`: a local engine again, and its columns have no name unless an
  alias column list gives them (an alias alone names none, so `unnest` would
  stay out of `alias_named_table_functions`).
- Spark and Databricks `explode`, `posexplode`, `inline`, `stack`: `col`, or
  `key` and `value` of a map, by the argument's type, which the SQL alone does
  not give. `LATERAL VIEW explode(...)`, Spark's usual form, is a `Lateral`
  that sqlassert does not lower (LATERAL is unsupported).
- Snowflake `FLATTEN` (`SEQ`, `KEY`, `PATH`, `INDEX`, `VALUE`, `THIS`) is
  written `LATERAL FLATTEN(input => ...)`, which SQLGlot parses as a `Lateral`
  (it fills the six column names in as the alias list), or `TABLE(FLATTEN(...))`,
  a `TableFromRows`. Neither is a table function source, so LATERAL blocks it
  until LATERAL is lowered (issue 42). A bare `FLATTEN(...)` in FROM, which
  SQLGlot does parse as a call, is not claimed: it is not a form Snowflake's
  docs show, from memory.
- ClickHouse `numbers(n)` (`number`; local engine, no probe) and MySQL's
  `JSON_TABLE` (its columns are written in the call).

Whatever the columns, a call whose arguments read a column of the join's left
input is evaluated once per left row (an implicit LATERAL: DuckDB, probed;
from memory Postgres, SQLite, Trino's `CROSS JOIN UNNEST`, BigQuery's
`, UNNEST(t.arr)`, Redshift's `, t.arr AS x`; Spark and Databricks need the
`LATERAL` keyword, which is not lowered, so their `range` is never taken to read
the left input), so each of
its rows belongs to one left row: the join is One To Many (`Join.lateral`),
for INNER, LEFT and CROSS joins; DuckDB rejects a RIGHT or FULL join that
reads the left input. A call whose argument holds a subquery or a lambda is
never taken to read it, since either may name a column of its own. That rows of
the left input are repeated, none or several times (a LEFT JOIN: at least once),
is what no key survives.

## UNION BY NAME

DuckDB's `UNION [ALL] BY NAME` pairs the inputs' columns by name, not position.
`IrParser._align_by_name` lowers it to the positional UNION it means: each input
read as the output's columns in order, a column the input lacks as a NULL
literal (no type, nullable). Probed on DuckDB 2.0.0: the output is the left
input's columns in its order, then the right's that the left lacks in its
order; names match without regard to case (`SELECT 1 AS X UNION ALL BY NAME
SELECT 3 AS x` is `X`, the left's spelling); a column one input lacks is NULL
there; matched columns of different types widen (`INTEGER` and `VARCHAR` give
`VARCHAR`), which the pairing's comparison-type check already refuses to type.
DuckDB rejects, probed, `INTERSECT BY NAME`, `EXCEPT BY NAME`, `CORRESPONDING`,
`BY NAME ON (...)`, a `FULL` or `INNER` mode, and a column name written twice
in an input; all of these lower opaque and report `unsupported-construct`.

| | UNION BY NAME |
| --- | --- |
| DuckDB 2.0.0 | modelled, as above (probed) |
| Snowflake | documented: matched by identifier (unquoted names without regard to case, quoted ones exactly), output in order of first appearance left to right, a column in one input only filled with NULL (docs.snowflake.com, query operators). Not modelled: its quoted-name rule differs from the case-insensitive match used here; reported `unsupported-construct` |
| BigQuery | from memory: `BY NAME` and `CORRESPONDING` with `INNER`, `FULL`, `LEFT` modes and a `BY (columns)` list. Not modelled, reported |
| Databricks | the set operator reference lists no `BY NAME` (read on docs.databricks.com); not modelled, reported |
| Postgres, MySQL, Redshift, Trino | from memory: no `BY NAME`; not probed. Not modelled, reported |

SQLGlot parses `BY NAME` and `CORRESPONDING` for every dialect, so the dialect
is checked in `_align_by_name`, not by the parser. A flag in `DialectSemantics`
would replace that check if another dialect is modelled.

## Certifying facts

Every claim a dialect's flags make, every Builtin fact
(`strict(op)`, `total(op)`, `injective(op)`, `skips_null(op)`) `ir/convert.dialect_builtins`
derives from them, and every function result type (`result_type(Node)=TYPE`,
from `DialectSemantics.result_types`, `values_type(KIND)=TYPE`, from `values_types`, and `table_function(NAME)=COLUMNS`, from `table_functions`) has a provenance in `docs/dialect-provenance.json`:
`{"engine": [versions]}` when probed on a real engine, `{"doc": url, "read":
version or date}` when read in the engine's documentation, and, only for a
dialect with no local engine, `{"tentative": why}` when stated from memory,
neither probed nor read: a claim to verify, recorded rather than left out.
A probe that passes replaces a `doc` or `tentative` provenance. A weak setting (unchecked arithmetic, text not
compared by identity, unenforced keys) claims nothing and has none.
`tests/test_dialect_certification.py` checks that every fact has one, that
every fact of a local engine (DuckDB, SQLite, PostgreSQL, MySQL, ClickHouse, Trino)
is certified on an engine, which Spark facts are (the rest of Spark's are
`doc`), and reruns the DuckDB and SQLite probes.

`scripts/certify_dialects.py` (not part of the package) generates one probe
per fact from the fact itself: the Builtin called with a typed NULL (strict),
with overflowing, zero-dividing and badly cast arguments (total, injective),
with every argument NULL but one (skips_null), `sum` and `avg` of
overflowing BIGINTs and DOUBLEs, infinities and NaN (`checked_sum`; vacuous
where a DOUBLE holds no infinity or NaN),
or tables whose constraint violations it attempts (keys, text identity, ENUMs).
SQL a dialect writes differently comes from SQLGlot, and is kept only when
sqlassert parses it back to the same node; a construct the engine rejects is
VACUOUS for that engine (`^` in SQLite, an ENUM column in PostgreSQL). Each
fact runs under the engine's default settings (ADR 0006). It prints PASS,
FAIL (with the weaker setting to apply by hand), ERROR (the probe could not
run) or VACUOUS per fact, and STRONGER for a weak setting the engine would
also satisfy, to strengthen by hand when the engine is the latest release. Unless `--dry-run`, it files the engine version
under each passing fact's `engine` versions and each vacuous fact's
`undefined` ones, and removes it from each failing one; it never adds a fact
or strengthens one. An undefined fact claims nothing on that version, so
when a later version supports the construct its probe runs there and
decides. `--list` prints the probes without a connection; `--only` probes
and records the facts it names, leaving the others' provenance as it is. A probe that expects an error
(overflow, a failed cast, division by zero) declares the message it accepts as
a regex; any other error fails, and so does a returned value that is not the
exact result. An `injective` probe passes on the declared overflow error or on
the exact result as text (DuckDB 2.0 widens `-x` of the least BIGINT to a
HUGEINT); a wrapped, saturated, NULL or floating-point result fails.

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
docker run -d --rm --name sqlassert-spark -p 50000:10000 -e SPARK_NO_DAEMONIZE=true apache/spark \
    /opt/spark/sbin/start-thriftserver.sh --master 'local[2]' --hiveconf hive.server2.authentication=NOSASL \
    --conf spark.sql.warehouse.dir=/tmp/wh --conf spark.driver.extraJavaOptions=-Dderby.system.home=/tmp/derby
python scripts/certify_dialects.py spark --driver pyhive.hive \
    --only 'checked_sum,skips_null(greatest),skips_null(least),strict([])' \
    --connect '{"host": "127.0.0.1", "port": 50000, "auth": "NOSASL", "username": "probe"}'
```

`tests/test_trino_catalog.py` runs the Trino fetcher against the same
server when `SQLASSERT_TRINO` holds its connect arguments
(`{"host": "127.0.0.1", "port": 58080, "user": "probe"}`).

The drivers (`psycopg[binary]`, `pymysql`, `clickhouse-connect`, `trino`,
`pyhive[hive_pure_sasl]`) are not
dependencies; install them where the script runs (MySQL is reached at its
port, so MySQL server 8.4 and 9 images work the same way; 8.0 is not supported). Wait for a
container to accept connections before probing. Spark (4.3, ANSI mode on by
default) runs through its Thrift server and PyHive; only the facts named
are certified there, the rest of Spark's stay `doc` (a full run reaches
most, but cannot spell a CAST that SQLGlot parses back as one, and the zoned
timestamp probe's INSERT of text into a TIMESTAMP is rejected under ANSI
store assignment). Not certified on an engine: Databricks, Hive,
Oracle, MySQL's NDB engine, and the cloud warehouses, whose probes exist for
when credentials do.

## Not done

- A dialect not listed keeps enforced keys and total comparisons, as before
  (T-SQL, Oracle, Presto, Athena, ...). Oracle's `||` reads NULL as `''`, and `''` is
  NULL there, so it is left unmodelled.
- MySQL compares a number with a string as DOUBLE, so an integer column above
  2^53 (or a DECIMAL with more than 15 significant digits) is not pinned by a
  string; neither, in any dialect, by a DOUBLE. A string literal beside a
  number is not an `exact_comparison`, so nothing is pinned by it.
- MySQL `ANSI_QUOTES` makes `"x"` an identifier rather than a string, and
  `NO_BACKSLASH_ESCAPES` changes string literals; both are read as the default.
- SQLite's column types are affinities, not types: an `INTEGER` column may hold
  text. Its columns are still compared as declared.
