# SQL Assertion Analysis

This context describes static claims about SQL queries and the evidence used to establish them without inspecting table data.

## Language

**Assertion**:
A request from a SQL author to validate that a Property holds, like an
assertion in a programming language. An Assertion is a proof obligation: it
does not make the Property true, and remains Unknown when no proof is available.
_Avoid_: Property Declaration, assumption, annotation

**Property Declaration**:
A trusted statement from a SQL author that a Property is true. A Property
Declaration places that Property directly among the program's declarations,
contributes Knowledge without requiring proof, and does not produce an
assertion result.
_Avoid_: Assertion, Proof, Create Statement

**Unique Join Assertion**:
An assertion that a join cannot increase the number of rows contributed by its left-hand input.
_Avoid_: Unique join marker, duplicate check

**Exactly One Join Assertion**:
An assertion that every row of a join's left-hand input matches exactly one
right-hand row, so the join neither multiplies nor removes left rows and a left
join null-extends none of them. It combines a Unique Join (at most one match)
with guaranteed matching (at least one): an ON made only of equalities between
left and right columns, non-null left columns, and Referential Coverage of the
left columns, as one tuple, onto the right columns they are paired with.
_Avoid_: Lookup join, mandatory join, 1:1 join

**Exhaustive Case Assertion**:
An assertion that a simple `CASE` has an explicit `WHEN` arm for every value
of its operand's Enumerated Domain, so no non-null operand value reaches
`ELSE`. `ELSE` never counts as an arm; a NULL operand is left to `NOT NULL`.
_Avoid_: Complete case, total match

**Unique Set Assertion**:
An assertion that a named set of a Select Expression's output columns forms a Unique Set, or, when asserted as a key, a Candidate Key.
_Avoid_: Unique constraint, primary key marker, uniqueness check

**Property Marker**:
The comment syntax an author writes to introduce an Assertion or Property
Declaration. An Assertion Marker means “please validate that this is true”; a
Property Declaration Marker means “I state that this is true.” The marker is
syntax, while the Assertion or Property Declaration determines how its Property
enters reasoning. A comment shaped like a marker but not recognized as one is
reported rather than ignored, because a marker its author believed in is worse
than no marker at all.
_Avoid_: Assertion, Property Declaration, annotation, hint

**Property**:
A structural semantic statement about a relation or query construct. The same
Property types are used in two ways: an Assertion wraps one as a query, while a
Property Declaration lists one as accepted Knowledge. A public Analysis
Function evaluates a Property.
_Avoid_: Rule, flag

**Analysis Function**:
A computation over the semantic program that returns an Analysis Result. A
public Analysis Function evaluates a Property; an internal one derives a value
needed by another Analysis Function.
_Avoid_: Rule, property

**Analysis Result**:
The value computed by one Analysis Function together with the evidence for that
value. Its evidence is represented directly by its reason, subjects, premises,
and members rather than by a separate evidence object.
_Avoid_: Proof, property

**Result Value**:
The computed value inside an Analysis Result. It can be a Boolean property
answer or a derived value used only during analysis.
_Avoid_: Property, proof

**Evidence**:
The reasons and prior Analysis Results that justify an Analysis Result's value.
A Proof is Evidence for a Boolean Result Value that is true.
_Avoid_: Result Value, assertion

**Proof**:
A true Boolean Analysis Result whose Evidence establishes that a Property
follows from known facts and inference rules.
_Avoid_: Successful check, guess

**Unknown**:
An outcome meaning that available knowledge is insufficient to prove or disprove an assertion.
_Avoid_: Invalid, false

**Disproof**:
Evidence establishing that an assertion is false, rather than merely lacking a proof.
_Avoid_: Unknown, validation failure

**Diagnostic**:
An explicit report that part of a SQL program could not be analyzed, such as an unsupported statement, a duplicate declaration, or an assertion the analysis never reached. A diagnostic describes a limit of the analysis rather than a property of the query, and is never a Disproof.
_Avoid_: Error, warning, validation failure, unknown

**Connection Warning**:
A Diagnostic reported apart, in `Report.warnings`, when the SQL alone lacks a fact a database connection would supply: a table with no `CREATE`, or an unqualified column more than one undeclared source may own. The affected facts are unknown; the warning names the fix. It never fails a Report, and `analyze_database` emits none ([ADR 0005](docs/adr/0005-analyze-sql-without-a-connection.md)).

**Grouping Key**:
A scalar expression whose value identifies a group produced by aggregation. The complete set of grouping keys uniquely identifies rows produced by an ordinary aggregation.
_Avoid_: Aggregate predicate

**Aggregate Expression**:
An expression that computes one value from the rows in a group, such as `sum(amount)`.
_Avoid_: Aggregate predicate, grouping key

**QualifyByPartition**:
A relational operation that retains rows according to a window-derived position within each partition. It establishes uniqueness only when its condition guarantees at most one retained row per partition.
_Avoid_: Deduplicate

**Relation Expression**:
A semantic operation that produces a relation with its own output columns.
**Relation Operation** is a synonym used when focusing specifically on the
topmost operation of the expression.
_Avoid_: Relation definition, relation instance, plan

**Named Relation**:
The reusable meaning declared by a table, view, or CTE, shared by every reference to that declaration.
_Avoid_: Relation definition, alias

**Alias**:
One occurrence of a relation expression in a query scope, including an occurrence whose name is implicit.
_Avoid_: Relation instance, named relation

**Output Column**:
One named value produced by one relation expression. A pass-through operation produces a fresh Output Column referring to the upstream one.
_Avoid_: Column instance

**Column Reference**:
A scalar expression that reads an upstream Output Column.
_Avoid_: Input column, column identifier

**Origin**:
The SQL location or catalog object from which a semantic element or piece of knowledge arose.
_Avoid_: SQLGlot node

**Knowledge**:
Properties accepted as true for inference. Knowledge may come from a catalog,
an explicit Property Declaration, query structure, or a proved Assertion. A
Property Declaration is therefore a source of Knowledge; an Assertion is a
query whose Property is not accepted as Knowledge unless proved. Knowledge is
the status and collection of accepted Properties, not a separate Property base
type. It contains no SQL definitions or parser structures.
_Avoid_: View SQL, database connection, SQLGlot AST

**Unique Set**:
A set of relation columns whose fully non-null value combinations cannot repeat. Rows containing null in the set are not guaranteed to be distinct from one another.
_Avoid_: Candidate key

**Derived Unique Set**:
A Unique Set established for one Relation Expression by functional dependencies rather than stated directly: its columns determine the relation's Row Identity, possibly through Unique Sets and dependencies of upstream Relation Expressions.
_Avoid_: Propagated key

**Non-Null Unique Set**:
A Unique Set whose columns are all non-null, so every row has a distinct, complete value combination.
_Avoid_: Candidate key, nullable unique set

**Referential Coverage**:
A Property between ordered referencing columns of one relation and equally
many ordered referenced columns of another, paired by position: every row whose
referencing columns are all non-null has a referenced row equal to it on the
paired columns. A row with any null referencing component is exempt (SQL
`MATCH SIMPLE`). Knowledge -- a `REFERENCES` constraint, a catalog foreign key,
or a Foreign Key Property Declaration -- supplies Referential Coverage only.
_Avoid_: Inclusion dependency, reference check

**Foreign Key**:
Referential Coverage onto a Unique Set of the referenced Named Relation. Each
referencing row with no null component therefore matches exactly one referenced
row. The Unique Set must be proved or declared on its own; declaring a Foreign
Key never vouches for it.
_Avoid_: Reference, relationship, join key

**Functional Dependency**:
A Property of a relation, written `X → Y` (`DETERMINES`): any two rows that agree on the columns X also agree on the columns Y, for every database the schema allows. Strict when NULL counts as agreeing with NULL; lax when it holds only on rows whose X columns are all non-null.
_Avoid_: Correlation, derived column

**Enumerated Domain**:
A Property of one column: every non-null value it takes belongs to a finite set
of literals, for every database the schema allows. It says nothing about which
values occur (occurrence), and NULL is outside it, governed by `NOT NULL`
(nullability). It is a structural claim sqlassert proves or is told, not a
database enum type, though a catalog's enum type could supply it as Knowledge.
_Avoid_: Enum type, value set, check constraint

**Equality Group**:
An analysis-only set of one relation's output columns that carry the same value on every row: merged by strict equalities (`WHERE` and inner-join `ON` `a = b`) between known matching comparison types, never by an outer join's `ON`. Implicit casts and collations do not establish value identity. Stronger than a pair of Functional Dependencies, which an injective `a = f(b)` also satisfies. Value facts such as Enumerated Domains and Foreign Keys read it; uniqueness does not.
_Avoid_: Equivalence class, alias set

**Null Source**:
A reason an output column may be NULL: organically (a nullable source column, an expression whose nullability is not modelled, a NULL literal) or because an outer join padded its row. A column is non-null when it has no Null Source; a null-rejecting condition on a padded column proves its join matched and removes that padding from every column it padded.
_Avoid_: Nullable flag, null-extension

**Row Identity**:
An analysis-only value that tells apart every row of one Relation Expression, duplicates included. A column set is a Unique Set when it determines the relation's Row Identity. Never appears in the IR, in SQL, or to authors.
_Avoid_: rowid, ι, row number

**Select Expression**:
The `SELECT` query body common to a Root Select, a view or CTE definition, and a subquery, independent of how it is introduced into the program.
_Avoid_: Select-like expression, query, statement

**Attachment Site**:
A place in the SQL text where a Property Marker can be written: right before a
JOIN, right after the last token of a Select Expression, or right before a
simple `CASE`. Every marker attaches at one; `suggest` names each site by its
line and what it marks.
_Avoid_: Marker position, anchor

**Improvement**:
A rewrite of the SQL itself, at one Attachment Site, that a Proof shows leaves
the program's result unchanged as a multiset: an outer join that cannot keep a
padded row written as an inner join, a join that is redundant or only filters,
a redundant `DISTINCT` or grouping column, an unused CTE. `suggest` ranks
Improvements (High level) before markers to paste (Mid level).
_Avoid_: Optimization, lint, hint

**Create Statement**:
A top-level SQL declaration that introduces a Named Relation or related schema Knowledge into the analyzed program.
_Avoid_: Root select

**Root Select**:
The optional, single top-level select expression analyzed by a SQL program. It uses the same select-expression form as the body of a view definition but is not introduced by a create statement.
_Avoid_: Main view, implicit view
