# TODO

## Task 1: Distinguish property assertions from property declarations

Define two kinds of property marker:

- An **Assertion Marker** asks the analyzer to prove a property. It produces an
  `AssertionReport` with a `PROVED` or `UNKNOWN` outcome.
- A **Property Declaration Marker** states a trusted property. It adds that
  property to the analysis without creating a proof obligation. External
  `Knowledge` is the API-level source of equivalent trusted facts.

Use **Property Marker** as the umbrella term for their SQL syntax. Use
`ASSERT` and `DECLARE` to make the distinction explicit:

```sql
select id from users /**ASSERT UNIQUE(id)**/;
select id from users /**DECLARE UNIQUE(id)**/;

select *
from sessions
/**ASSERT UNIQUE**/ join users on sessions.user_id = users.id;
```

The existing bare markers are replaced by the explicit assertion spelling:

- `/**UNIQUE**/` becomes `/**ASSERT UNIQUE**/`.
- `/**UNIQUE(id)**/` becomes `/**ASSERT UNIQUE(id)**/`.
- `/**PRIMARY KEY(id)**/` becomes `/**ASSERT PRIMARY KEY(id)**/`.

A proved assertion may continue to supply the proved property to downstream
statements. A declaration supplies its property immediately and is reported as
the source of any proof that depends on it. A declaration must never appear as
an assertion result.

Update `CONTEXT.md` so that **Declaration** on its own continues to mean a SQL
declaration such as `CREATE TABLE`; use the full term **Property Declaration**
for a trusted property marker. Document that declarations are assumptions, not
proofs, even though `DECLARE` is the user-facing syntax.

Verify:

- The parser accepts canonical `ASSERT` and `DECLARE` markers at every valid
  attachment site.
- Marker-shaped misspellings are diagnosed rather than ignored.
- Assertion markers produce reports; declaration markers do not.
- A declared property can prove a later assertion, and the explanation names
  the declaration as evidence.
- Migrate all existing tests and documentation to the explicit syntax.

## Task 2: Add `NOT NULL` assertions and declarations

Support trailing Select Expression property markers:

```sql
select order_id, customer_id
from orders
/**ASSERT NOT NULL(order_id, customer_id)**/;

select order_id, customer_id
from imported_orders
/**DECLARE NOT NULL(order_id, customer_id)**/;
```

`ASSERT NOT NULL(a, b)` is one assertion requiring every named output column to
be non-null. Its explanation must identify the columns whose non-nullness could
not be proved. `DECLARE NOT NULL(a, b)` adds independent non-null facts for the
Select Expression's output columns.

Prove non-nullness from:

- `NOT NULL` and `PRIMARY KEY` table knowledge.
- Property declarations and explicit `Knowledge`.
- Direct pass-through projection, renaming, filtering, aliases, CTEs, and views.
- Non-null literals and expressions with explicitly modeled non-null semantics.
- Join outputs according to join kind: an outer join must not preserve
  non-nullness for its null-extended side.

Do not guess the nullability of opaque expressions. Extend literal IR enough to
distinguish `NULL` from a non-null literal.

Verify root Selects, views, CTEs, subqueries, composite column lists, renamed
columns, unknown columns, nullable outer-join outputs, declarations feeding
later statements, and durable report explanations.

## Task 3: Add finite `ENUM` domains

Use **Enumerated Domain** as the semantic term and `ENUM` as the marker syntax:

```sql
select status
from orders
/**ASSERT ENUM(status, 'pending', 'paid', 'cancelled')**/;

select status
from imported_orders
/**DECLARE ENUM(status, 'pending', 'paid', 'cancelled')**/;
```

The property means that every non-null value of the named output column belongs
to the listed finite set. It does not mean that every listed value occurs.
Nullability is orthogonal: combine it with `ASSERT NOT NULL(status)` when null
must also be impossible.

Represent literal values with their SQL type and value so that, for example,
`1` and `'1'` are different domain members. The first slice should support
string, numeric, and boolean literals. Unsupported literal forms must produce a
diagnostic rather than being stringified into a fact. Reject `NULL`, an empty
value list, and duplicate values; nullability remains a separate property.

Add enumerated domains to `Knowledge`. Derive them through property-preserving
columns, filters, aliases, CTEs, views, and modeled literal expressions. An
established smaller domain proves an assertion containing a superset. Do not
infer that every value in a declared or asserted domain actually occurs.

Verify rejected empty and duplicate enumerations, typed-value distinctions,
null semantics, domain containment, renamed columns, propagation, declarations
feeding later assertions, and useful uncovered-possible-value explanations for
`UNKNOWN`.

## Task 4: Add exhaustive simple `CASE` assertions

Support an assertion marker immediately before a simple `CASE` expression:

```sql
select
  /**ASSERT EXHAUSTIVE**/
  case status
    when 'pending' then 'open'
    when 'paid' then 'closed'
    when 'cancelled' then 'closed'
  end as status_group
from orders;
```

For the first slice, support only simple `CASE <expression> WHEN <literal>`.
The assertion is proved when the case operand has a known Enumerated Domain and
every member is covered by an explicit `WHEN`. An `ELSE` arm does not prove
exhaustiveness: the purpose is to detect newly introduced domain members that
would silently fall through. Null is outside the Enumerated Domain and remains
governed by `NOT NULL`.

Model simple `CASE` in the scalar IR. Its result expression should also earn an
Enumerated Domain when every result arm is a supported literal and the case is
proved exhaustive, enabling downstream `ENUM` proofs.

`EXHAUSTIVE` is assertion-only because it is a local property of an expression,
not reusable relation Knowledge. Diagnose `DECLARE EXHAUSTIVE`, searched cases,
non-literal `WHEN` values, duplicate arms, and assertions attached anywhere
other than a supported `CASE`.

Verify exact coverage, missing domain members, explicit `ELSE`, nullable
operands, typed literals, multiple cases in one Select, and propagation of the
result domain.

## Task 5: Add `FOREIGN KEY` knowledge, assertions, and declarations

Use standard SQL-like syntax on a Select Expression:

```sql
select customer_id, amount
from orders
/**ASSERT FOREIGN KEY(customer_id) REFERENCES customers(id)**/;

select customer_id, amount
from imported_orders
/**DECLARE FOREIGN KEY(customer_id) REFERENCES customers(id)**/;
```

Support composite keys with positional column pairing. A Foreign Key means that
every referencing tuple whose columns are all non-null has a matching tuple in
the referenced relation, and that the referenced columns form a Unique Set.
This follows SQL `MATCH SIMPLE` null semantics; combine the Foreign Key with
`NOT NULL` to guarantee a match for every referencing row.

Add `ForeignKeyKnowledge` to the public Knowledge model. Collect Foreign Keys
from `CREATE TABLE` column and table constraints. Resolve qualified relation
names with the same rules used by existing relation Knowledge.

Derive Foreign Keys through direct pass-through projection, renaming, filters
of the referencing relation, aliases, CTEs, and views. An inner join may
establish a Foreign Key for output columns that are structurally restricted to
matching referenced rows. Do not preserve a relationship when the referenced
relation has been filtered or transformed in a way that can remove target rows.

An assertion must remain `UNKNOWN` unless both referential coverage and target
uniqueness are proved. Its report should distinguish an unknown relationship,
an unknown target relation, and target columns that are not known unique.
Nullable referencing columns do not by themselves invalidate a Foreign Key
assertion.

Verify DDL and explicit Knowledge, composite and qualified keys, null semantics,
renaming and propagation, filtered referenced relations, unknown columns and
relations, declarations feeding downstream assertions, and proof provenance.

## Task 6: Add `EXACTLY ONE` join assertions

Support:

```sql
select *
from orders
/**ASSERT EXACTLY ONE**/ join customers
  on orders.customer_id = customers.id;
```

Define the assertion from the left input's perspective: every left row matches
exactly one right row. Internally this is the conjunction of:

- **Unique Join**: the predicate covers a Unique Set of the right input, so a
  left row matches at most one right row.
- **Matched Join**: the left join columns are non-null and have a proved Foreign
  Key to the exact right relation and columns used by the predicate, so every
  left row matches at least one right row.

For an inner join, a proved assertion means the join neither multiplies nor
removes left rows. For a left join, it additionally proves that no left row is
null-extended because of a missing match. Keep row-preservation and Matched Join
as internal properties; do not add public markers for them in this slice.

RHS filters and additional predicates can invalidate the Foreign Key coverage
and must make the assertion `UNKNOWN` unless their safety is separately proved.
Do not treat a Foreign Key to a base table as coverage of a filtered view of
that table.

Report the two proof halves independently so an unknown result says whether it
lacks RHS uniqueness, guaranteed matching, non-null referencing columns, or
more than one of these.

Verify inner and left joins, simple and composite predicates, nullable and
non-null Foreign Keys, filtered RHS relations, extra predicates, aliases,
renamed columns, declarations and DDL as evidence, and downstream preservation
of the left input's established properties.
