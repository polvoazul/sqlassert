# SQL Assertion Analysis

This context describes static claims about SQL queries and the evidence used to establish them without inspecting table data.

## Language

**Assertion**:
A property that a SQL author requires a query construct to satisfy.
_Avoid_: Check, annotation

**Unique Join Assertion**:
An assertion that a join cannot increase the number of rows contributed by its left-hand input.
_Avoid_: Unique join marker, duplicate check

**Unique Set Assertion**:
An assertion that a named set of a Select Expression's output columns forms a Unique Set, or, when declared as a key, a Candidate Key.
_Avoid_: Unique constraint, primary key marker, uniqueness check

**Marker**:
The comment syntax an author writes to state an assertion, such as `/**UNIQUE**/`. The marker is the syntax; the assertion is the property it states. A comment shaped like a marker but not recognized as one is reported rather than ignored, because a marker its author believed in is worse than no marker at all.
_Avoid_: Assertion, annotation, hint

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

**Diagnostic**:
An explicit report that part of a SQL program could not be analyzed, such as an unsupported statement, a duplicate declaration, or an assertion the analysis never reached. A diagnostic describes a limit of the analysis rather than a property of the query, and is never a Disproof.
_Avoid_: Error, warning, validation failure, unknown

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
Typed semantic facts known about Named Relations and their columns independently of the analyzed query, such as uniqueness, nullability, and column membership. Knowledge contains no SQL definitions or parser structures.
_Avoid_: View SQL, database connection, SQLGlot AST

**Unique Set**:
A set of relation columns whose fully non-null value combinations cannot repeat. Rows containing null in the set are not guaranteed to be distinct from one another.
_Avoid_: Candidate key

**Derived Unique Set**:
A Unique Set established for one Relation Expression from a Unique Set of an upstream Relation Expression.
_Avoid_: Propagated key

**Non-Null Unique Set**:
A Unique Set whose columns are all non-null, so every row has a distinct, complete value combination.
_Avoid_: Candidate key, nullable unique set

**Select Expression**:
The `SELECT` query body common to a Root Select, a view or CTE definition, and a subquery, independent of how it is introduced into the program.
_Avoid_: Select-like expression, query, statement

**Create Statement**:
A top-level SQL declaration that introduces a Named Relation or related schema Knowledge into the analyzed program.
_Avoid_: Root select

**Root Select**:
The optional, single top-level select expression analyzed by a SQL program. It uses the same select-expression form as the body of a view definition but is not introduced by a create statement.
_Avoid_: Main view, implicit view
