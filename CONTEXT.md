# SQL Assertion Analysis

This context describes static claims about SQL queries and the evidence used to establish them without inspecting table data.

## Language

**Assertion**:
A property that a SQL author requires a query construct to satisfy.
_Avoid_: Check, annotation

**Unique Join Assertion**:
An assertion that a join cannot increase the number of rows contributed by its left-hand input.
_Avoid_: Unique join marker, duplicate check

**Property**:
A semantic statement about a relation or query construct that can be established from query structure and catalog knowledge.
_Avoid_: Rule, flag

**Proof**:
Evidence establishing that a property follows from known facts and inference rules.
_Avoid_: Successful check, guess

**Unknown**:
An outcome meaning that available knowledge is insufficient to prove or disprove an assertion.
_Avoid_: Invalid, false

**Disproof**:
Evidence establishing that an assertion is false, rather than merely lacking a proof.
_Avoid_: Unknown, validation failure

**Grouping Key**:
A bound scalar expression whose value identifies a group produced by aggregation. The complete set of grouping keys uniquely identifies rows produced by an ordinary aggregation.
_Avoid_: Aggregate predicate

**Aggregate Expression**:
An expression that computes one value from the rows in a group, such as `sum(amount)`.
_Avoid_: Aggregate predicate, grouping key

**QualifyByPartition**:
A relational operation that retains rows according to a window-derived position within each partition. It establishes uniqueness only when its condition guarantees at most one retained row per partition.
_Avoid_: Deduplicate

**Relation Definition**:
The reusable meaning of a table, view, CTE, or subquery, independent of any particular use within a query.
_Avoid_: Relation instance, alias

**Relation Instance**:
One occurrence of a relation definition within a query, with its own identity and optional alias.
_Avoid_: Relation definition

**Origin**:
The SQL location or catalog object from which a semantic element or piece of knowledge arose.
_Avoid_: SQLGlot node

**Expansion Context**:
The per-instance chain of relation references traversed while expanding a view or other known relation definition.
_Avoid_: Global expansion stack

**Knowledge**:
Typed semantic facts known about relation definitions and their columns independently of the analyzed query, such as uniqueness, nullability, and column membership. Knowledge contains no SQL definitions or parser structures.
_Avoid_: View SQL, database connection, SQLGlot AST

**Unique Set**:
A set of relation columns whose fully non-null value combinations cannot repeat. Rows containing null in the set are not guaranteed to be distinct from one another.
_Avoid_: Candidate key

**Candidate Key**:
A unique set whose columns are all non-null, so every row has a distinct, complete value combination.
_Avoid_: Nullable unique set

**Create Statement**:
A top-level SQL declaration that introduces a named relation definition or related schema knowledge into the analyzed program.
_Avoid_: Root select

**Root Select**:
The optional, single top-level select expression analyzed by a SQL program. It uses the same select-expression form as the body of a view definition but is not introduced by a create statement.
_Avoid_: Main view, implicit view
