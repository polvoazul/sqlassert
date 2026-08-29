# TODO

## Task 1 — Replace bare markers with explicit `ASSERT`

**What to build:** Explicit `ASSERT` becomes the only assertion syntax. Unique
Join Assertions, Unique Set Assertions, and Candidate Key assertions retain
their current behavior and reporting, while old bare markers are rejected.

**Blocked by:** None — can start immediately.

**Status:** ready-for-agent

- [ ] Explicit `ASSERT` syntax works for Unique Join Assertions, Unique Set
  Assertions, and Candidate Key assertions at every currently supported
  attachment site.
- [ ] Every repository-owned assertion marker in tests, examples, and
  documentation is migrated to explicit `ASSERT` syntax.
- [ ] Explicit assertions retain the existing proof outcomes, downstream
  facts, diagnostics, and explanations.
- [ ] Bare Unique Join, Unique Set, and Candidate Key markers are removed from
  the accepted grammar and produce clear unrecognized-marker diagnostics.
- [ ] Marker-shaped misspellings and unattached explicit markers are diagnosed
  rather than ignored.
- [ ] The domain glossary defines Property Marker as the umbrella term and
  Assertion Marker as a proof obligation.
- [ ] The complete test suite passes with no remaining use of bare assertion
  syntax.

## Task 2 — Declare trusted Unique Sets and Candidate Keys

**What to build:** Authors can use Property Declaration Markers to state
trusted Unique Sets and Candidate Keys on Select Expressions. Declarations
become evidence for downstream proofs without creating assertion results of
their own.

**Blocked by:** Task 1 — Replace bare markers with explicit `ASSERT`.

**Status:** ready-for-agent

- [ ] `DECLARE UNIQUE` and `DECLARE PRIMARY KEY` work on Root Selects, views,
  CTEs, and FROM subqueries.
- [ ] A declaration contributes its property without first requiring a proof.
- [ ] A declaration never appears as an `AssertionReport`.
- [ ] A declared property can prove a downstream assertion, including through
  supported property-preserving operations.
- [ ] Proof explanations identify a Property Declaration as trusted evidence
  rather than presenting it as a derived proof.
- [ ] Candidate Key declarations contribute both uniqueness and independent
  non-nullness for their declared columns.
- [ ] The domain glossary distinguishes Property Declarations from SQL Create
  Statements and explicitly describes declarations as assumptions, not proofs.

## Task 3 — Assert and declare `NOT NULL`

**What to build:** Authors can prove or declare that selected output columns
are non-null. Non-nullness flows through supported relational operations and
respects expression and join null semantics.

**Blocked by:** Task 2 — Declare trusted Unique Sets and Candidate Keys.

**Status:** ready-for-agent

- [ ] `ASSERT NOT NULL` produces one assertion result for its complete column
  list, and `DECLARE NOT NULL` contributes trusted non-null facts without an
  assertion result.
- [ ] Non-nullness can be established from table constraints, explicit
  Knowledge, Property Declarations, pass-through columns, renaming, filters,
  aliases, CTEs, views, and supported non-null literals.
- [ ] The semantic model distinguishes a null literal from a non-null literal
  and does not guess the nullability of opaque expressions.
- [ ] Join outputs preserve or lose non-nullness according to the join kind and
  null-extended side.
- [ ] Unknown columns and unsupported attachment sites produce diagnostics.
- [ ] An unknown assertion identifies every column whose non-nullness could not
  be proved.
- [ ] Proved and declared non-nullness is available to downstream Candidate Key
  and Foreign Key reasoning.

## Task 4 — Assert and declare string `ENUM` domains

**What to build:** Authors can constrain a column's non-null values to a finite
set of strings. Enumerated Domains can come from assertions, declarations, or
external Knowledge and can prove compatible downstream domain assertions.

**Blocked by:** Task 2 — Declare trusted Unique Sets and Candidate Keys.

**Status:** ready-for-agent

- [ ] `ASSERT ENUM` proves that every possible non-null value belongs to the
  asserted string set.
- [ ] `DECLARE ENUM` and external Knowledge introduce trusted Enumerated Domain
  facts without claiming that every listed value occurs.
- [ ] A known smaller domain proves an assertion containing a superset, while a
  known domain containing uncovered possibilities leaves the assertion
  `UNKNOWN`.
- [ ] Enumerated Domains propagate through pass-through columns, renaming,
  filters, aliases, CTEs, and views.
- [ ] Null remains outside the Enumerated Domain and is governed independently
  by `NOT NULL`.
- [ ] Unknown reports identify possible values that the assertion does not
  cover.
- [ ] The domain glossary defines Enumerated Domain and distinguishes it from
  occurrence, nullability, and a database enum type.

## Task 5 — Complete typed `ENUM` support

**What to build:** Enumerated Domains support string, numeric, and boolean SQL
literals without conflating values of different SQL types, and malformed
enumerations fail explicitly.

**Blocked by:** Task 4 — Assert and declare string `ENUM` domains.

**Status:** ready-for-agent

- [ ] Numeric and boolean domain members work in assertions, declarations,
  Knowledge, propagation, containment proofs, and reports.
- [ ] Literal identity retains SQL type and value, so values such as numeric one
  and string one remain distinct.
- [ ] Empty value lists, null members, and duplicate typed values are rejected
  with clear diagnostics.
- [ ] Unsupported literal forms are diagnosed rather than stringified or
  treated as opaque domain members.
- [ ] Mixed supported literal types remain distinct and deterministic through
  fact encoding and reporting.

## Task 6 — Assert exhaustive simple `CASE` expressions

**What to build:** Authors can require a simple `CASE` expression to handle
every value in its operand's known Enumerated Domain explicitly. A proved case
also supplies the finite domain of its result when every result arm is a
supported literal.

**Blocked by:** Task 5 — Complete typed `ENUM` support.

**Status:** ready-for-agent

- [ ] An `ASSERT EXHAUSTIVE` marker attaches unambiguously to the immediately
  following simple `CASE` expression.
- [ ] The assertion is proved only when every member of the operand's known
  Enumerated Domain has an explicit matching `WHEN` arm.
- [ ] An `ELSE` arm does not substitute for explicit enum-member coverage.
- [ ] Null handling remains separate and depends on the operand's `NOT NULL`
  property when required by the caller.
- [ ] Missing domain members are reported clearly for an `UNKNOWN` outcome.
- [ ] A proved exhaustive case with supported literal results contributes an
  Enumerated Domain for its output expression.
- [ ] Searched cases, duplicate arms, unsupported `WHEN` values,
  `DECLARE EXHAUSTIVE`, and invalid attachment sites are diagnosed.

## Task 7 — Assert and declare direct Foreign Keys

**What to build:** Authors can obtain, assert, or declare a Foreign Key between
columns of a relation and a Unique Set of a named relation. Direct table
relationships support qualified names, composite keys, and SQL `MATCH SIMPLE`
null semantics.

**Blocked by:** Task 2 — Declare trusted Unique Sets and Candidate Keys.

**Status:** ready-for-agent

- [ ] Public Knowledge can represent ordered referencing columns, a qualified
  referenced relation, and ordered referenced columns.
- [ ] Column-level and table-level Foreign Keys are collected from supported
  table declarations.
- [ ] `ASSERT FOREIGN KEY` and `DECLARE FOREIGN KEY` work for single and
  composite keys with positional column pairing.
- [ ] A Foreign Key requires both referential coverage and a proved or declared
  Unique Set on the referenced columns.
- [ ] Under `MATCH SIMPLE`, a row with any null referencing component is exempt;
  nullable referencing columns do not invalidate the Foreign Key itself.
- [ ] Unknown source columns, target relations, target columns, relationships,
  and target uniqueness produce precise diagnostics or explanations.
- [ ] A declaration produces no assertion result and is identified as trusted
  evidence in downstream proofs.

## Task 8 — Propagate Foreign Keys through queries

**What to build:** Established Foreign Keys survive transformations that cannot
invalidate referential coverage, and inner joins can establish relationships
for output columns structurally restricted to matching target rows.

**Blocked by:** Task 7 — Assert and declare direct Foreign Keys.

**Status:** ready-for-agent

- [ ] Foreign Keys propagate through direct projections, renaming, filters of
  the referencing relation, aliases, CTEs, and views.
- [ ] Composite column order and target identity remain intact during
  propagation.
- [ ] An inner equality join can establish a Foreign Key for corresponding
  output columns when the target columns form a Unique Set.
- [ ] Filtering or otherwise narrowing the referenced relation does not inherit
  a Foreign Key to its broader source unless coverage is independently proved.
- [ ] Unsupported expressions or transformations conservatively stop
  propagation instead of guessing.
- [ ] Assertions over derived relations report the closest missing relationship
  evidence.

## Task 9 — Assert simple `EXACTLY ONE` joins

**What to build:** Authors can prove that every left row of a simple
single-column equality join matches exactly one row on the right. The result
distinguishes uniqueness from guaranteed matching when proof is incomplete.

**Blocked by:** Task 3 — Assert and declare `NOT NULL`; Task 7 — Assert and
declare direct Foreign Keys.

**Status:** ready-for-agent

- [ ] `ASSERT EXACTLY ONE` attaches to inner and left joins with a direct
  single-column equality predicate.
- [ ] The at-most-one half is proved by coverage of a right-side Unique Set.
- [ ] The at-least-one half is proved by a non-null left column with a Foreign
  Key to the exact right relation and predicate column.
- [ ] A proved inner join neither multiplies nor removes left rows; a proved
  left join also guarantees that no left row is null-extended for lack of a
  match.
- [ ] An unknown result reports RHS uniqueness, guaranteed matching, and
  non-nullness as separate missing evidence.
- [ ] Proven joins preserve established left-input properties on corresponding
  output columns.

## Task 10 — Complete `EXACTLY ONE` reasoning

**What to build:** Exactly One Join Assertions work across composite keys,
aliases, and safely derived relations while conservatively rejecting predicates
or transformations that can invalidate guaranteed matching.

**Blocked by:** Task 8 — Propagate Foreign Keys through queries; Task 9 —
Assert simple `EXACTLY ONE` joins.

**Status:** ready-for-agent

- [ ] Composite equality predicates prove both uniqueness and matching using
  positional Foreign Key and Unique Set coverage.
- [ ] Aliases, renamed pass-through columns, CTEs, views, and supported derived
  relations retain the identities needed for the proof.
- [ ] RHS filters and additional predicates make guaranteed matching unknown
  unless their safety is independently proved.
- [ ] A Foreign Key to a base relation is not treated as coverage of a narrowed
  version of that relation.
- [ ] Explanations identify every missing proof half for composite and derived
  joins.
- [ ] A proved assertion propagates the left input's Unique Sets, non-nullness,
  Enumerated Domains, and Foreign Keys to corresponding join outputs.
- [ ] Unsupported join kinds and predicates remain explicit `UNKNOWN` outcomes
  or diagnostics rather than optimistic proofs.
