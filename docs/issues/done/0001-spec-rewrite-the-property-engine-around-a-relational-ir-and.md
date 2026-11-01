# 1. Spec: Rewrite the property engine around a relational IR and Clingo

- **State:** closed
- **Labels:** spec
- **Created:** 2026-08-21
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/1

## Problem Statement

SQL authors need to prove structural properties such as join uniqueness before queries run, without scanning production data. The current Unique Join implementation combines marker parsing, SQLGlot traversal, DuckDB catalog access, uniqueness inference, and message formatting in one analyzer. That shape is difficult to extend comprehensively to views, nested queries, additional relational operations, richer evidence, and future properties.

The analyzer needs a clean semantic boundary between SQL syntax and property reasoning. It must retain enough provenance to explain both successful and incomplete proofs at the originating SQL, while remaining conservative: absence of a proof must never be presented as proof that an assertion is false.

## Solution

Replace the current property engine with a clean pipeline that parses a SQL Program, converts it into a framework-independent bound relational IR, combines that IR with optional external Knowledge, emits ground facts to Clingo, and returns a durable Report produced while Clingo's single deterministic model is live.

The SQL Program consists of Create Statements followed by at most one Root Select. Create Statements introduce Relation Definitions and schema Knowledge. The converter registers definitions first, then resolves and lowers their bodies, allowing forward references while detecting cycles. Tables, views, CTEs, and subqueries share the same relational-plan representation. Each use of a relation is a distinct Relation Instance, preserving correct identity for aliases and self-joins.

Clingo is used as a deterministic, stratified property engine rather than as a search or optimization engine. The MVP proves Unique Join Assertions for a conservative SQL subset and returns either PROVED or UNKNOWN. Unsupported or insufficiently understood semantics remain explicit and are never approximated.

## User Stories

1. As a SQL author, I want to mark a join with a Unique Join Assertion, so that accidental row multiplication is caught before execution.
2. As a SQL author, I want analysis to use query structure and declared Knowledge rather than table contents, so that proofs remain valid as data changes.
3. As a library caller, I want to analyze one complete SQL Program, so that table and view declarations can be considered together with the Root Select.
4. As a library caller, I want to supply optional external Knowledge, so that relations not declared in the SQL Program can still participate in proofs.
5. As a library caller, I want omitted Knowledge to behave as empty Knowledge, so that self-contained SQL Programs remain simple to analyze.
6. As a SQL author, I want Create Table statements to establish relation columns, Unique Sets, and nullability, so that schema declarations contribute to proofs.
7. As a SQL author, I want Create View statements to introduce reusable Relation Definitions, so that properties can propagate through views.
8. As a SQL author, I want view bodies and the Root Select to use the same Select-expression semantics, so that analysis does not maintain two query languages.
9. As a SQL author, I want views to reference definitions declared later in the SQL Program, so that declaration ordering does not unnecessarily constrain analysis inputs.
10. As a SQL author, I want recursive view cycles to be detected, so that analysis terminates with an explicit unsupported result.
11. As a SQL author, I want nested views to be expanded transitively, so that properties of underlying relations can prove assertions at the outer query.
12. As a SQL author, I want CTEs and FROM subqueries to be lowered as relational subplans, so that nesting does not block property propagation.
13. As a SQL author, I want aliases and self-joins to create distinct Relation Instances, so that facts from one occurrence do not bleed into another.
14. As a SQL author, I want simple projections to preserve and rename unique columns correctly, so that harmless view and subquery layers do not lose proofs.
15. As a SQL author, I want filters to preserve existing uniqueness, so that restricting a relation does not invalidate its Unique Sets.
16. As a SQL author, I want a filter equating a key member to a constant to cover that member, so that partially joined composite keys can still prove uniqueness.
17. As a SQL author, I want range predicates and other non-determining filters not to cover key members, so that the engine does not make unsound proofs.
18. As a SQL author, I want INNER joins with fully covered right-side Unique Sets to be proved unique, so that common enrichment joins are safe.
19. As a SQL author, I want LEFT joins with fully covered right-side Unique Sets to be proved unique, so that optional enrichment joins are safe.
20. As a SQL author, I want USING predicates to participate in the same key-coverage reasoning as ON equalities, so that equivalent join syntax behaves consistently.
21. As a SQL author, I want composite Unique Sets to require every member to be determined, so that partially covered composite constraints remain UNKNOWN.
22. As a SQL author, I want a nullable Unique Set to prove uniqueness for ordinary equality joins, so that SQL null semantics are handled soundly.
23. As a SQL author, I want a Candidate Key to represent a Unique Set whose columns are all non-null, so that total and nullable uniqueness remain distinct concepts.
24. As a SQL author, I want ordinary GROUP BY results to be unique by the complete set of Grouping Keys, so that aggregate subqueries can prove joins.
25. As a SQL author, I want Grouping Keys to support bound scalar expressions, so that expression-based grouping can be represented accurately.
26. As a SQL author, I want Aggregate Expressions to remain distinct from Grouping Keys, so that aggregate outputs are not mistaken for uniqueness guarantees.
27. As a SQL author, I want SELECT DISTINCT to establish uniqueness over its output expressions, so that deduplicated subqueries can prove joins.
28. As a SQL author, I want recognized row-number qualification to produce a QualifyByPartition operation, so that at-most-one-row-per-partition queries can prove uniqueness.
29. As a SQL author, I want unsupported window semantics to remain UNKNOWN, so that the recognized partition qualification is not generalized unsafely.
30. As a SQL author, I want unsupported joins and predicates to return a clear limitation rather than a guessed result, so that I can trust every proof.
31. As a SQL author, I want UNKNOWN to mean that available knowledge was insufficient, so that a missing proof is not confused with a Disproof.
32. As a future SQL author, I want DISPROVED to be added only when genuine refutation evidence exists, so that negative conclusions remain sound.
33. As a SQL author, I want reports to identify the assertion site and the relevant proof or missing evidence, so that I can act on the result.
34. As a SQL author, I want nested-view reports to retain the complete Expansion Context, so that a cause inside a deeply referenced view can be traced.
35. As a maintainer, I want every semantic node and piece of Knowledge to retain an Origin, so that reporting does not depend on SQLGlot objects.
36. As a maintainer, I want the relational IR to contain immutable Python values independent of SQLGlot, Clingo, and database connections, so that each boundary can evolve independently.
37. As a maintainer, I want unsupported expressions represented explicitly as opaque expressions, so that conversion never silently discards semantics.
38. As a rule author, I want inspectable ground facts with readable unique identifiers, so that I can understand and debug the Clingo program.
39. As a rule author, I want relation, column, key, join, expression, and assertion identifiers to carry readable hints plus unique incremental suffixes, so that facts are both unambiguous and approachable.
40. As a rule author, I want the primary Clingo program restricted to deterministic constructs, so that every analysis input has exactly one stable model.
41. As a rule author, I want choice rules, disjunctive heads, and optimization constructs rejected by policy tests, so that accidental nondeterminism is caught early.
42. As a reporting maintainer, I want a Reporter instance to consume the live Clingo model through its bound callback, so that reporting can use Clingo's native contains and symbol APIs without copying or re-modeling the answer set.
43. As a reporting maintainer, I want the Reporter to retain only a durable Report and never the short-lived Clingo model, so that no invalid solver object escapes its lifetime.
44. As a library caller, I want analysis to return a Report without printing, so that applications and command-line tools can decide how to present it.
45. As a maintainer, I want one public behavioral testing seam, so that refactoring parser, IR, facts, or rules does not require rewriting implementation-coupled tests.
46. As a maintainer, I want useful scenarios from the current engine migrated to the new public seam, so that the clean rewrite preserves sound supported behavior.
47. As a maintainer, I want the old monolithic analyzer removed after the new sound subset reaches parity, so that there is one source of truth for property reasoning.

## Implementation Decisions

- The public entry point is an analyze operation accepting the complete SQL text, optional Knowledge, and a SQL dialect, and returning a durable Report without printing.
- The top-level composition component invokes SQL parsing, IR conversion, Clingo execution, and reporting.
- SQL parsing produces SQLGlot statement ASTs and preserves Unique Join markers. SQLGlot objects do not cross into the bound IR.
- The SQL Program grammar permits zero or more Create Statements and at most one Root Select. Select expressions inside view definitions and the Root Select share the same lowering path.
- IR conversion is two-pass: a declaration pass assigns Relation Definition identities and catches duplicate declarations; a definition pass resolves and lowers table declarations, view bodies, and finally the Root Select.
- Definition resolution is order-independent. A definition moves through unresolved, resolving, and resolved states; encountering a resolving definition reports a cycle.
- The relational IR includes Scan, Project, Filter, Join, Aggregate, Distinct, Window, QualifyByPartition, and Opaque operations.
- FROM subqueries and CTEs become relational subplans rather than dedicated semantic operators.
- Scalar expressions use a small bound hierarchy covering column references, literals, function calls, comparisons, boolean operations, aggregate expressions, window expressions, subquery expressions, and opaque expressions.
- Grouping Keys may be arbitrary bound scalar expressions. Aggregate Expressions are modeled separately and do not themselves establish uniqueness.
- QualifyByPartition is the canonical operation for recognized window-derived selection within a partition. The MVP proof pattern is row number equal to one.
- Relation Definitions and Relation Instances have distinct identities. Every table, view, CTE, or subquery use creates a Relation Instance, with an alias used as its readable identity hint when present.
- Origins are stored in a provenance registry and referenced by stable identifiers. Expanded relations carry persistent, per-instance Expansion Context parent links rather than a mutable global stack.
- A nested Origin may identify either a SQL source span or catalog Knowledge. Reporting can walk Expansion Context links from the assertion use site to the deepest relevant cause.
- Knowledge contains typed semantic facts about relation definitions and columns only. It contains no SQL text, SQLGlot ASTs, database connections, or view-definition registry.
- Create Table statements contribute relation definitions and Knowledge. Create View statements contribute relation definitions backed by relational plans. Index-derived Knowledge is deferred.
- A Unique Set guarantees that fully non-null value combinations do not repeat. A Candidate Key is a Unique Set whose columns are all non-null.
- Nullable Unique Sets prove at-most-one matching row for ordinary equality because null values do not match. Null-safe equality is unsupported in the MVP.
- Unique Join proof means that each left-side row matches at most one right-side row. The MVP supports INNER and LEFT joins only.
- Join coverage supports USING and conjunctions of simple equalities. A right-side key member is covered when equated to an expression independent of that right-side relation, including a constant. Range predicates do not establish coverage.
- Clingo receives generated ground ASP facts through its standard program-addition API. Rules are maintained as separate logic-program resources and grounded with the facts.
- Generated constants use readable kind prefixes and sanitized hints followed by a deterministic incremental suffix. Examples of kinds include relation, column, key, join, expression, and assertion. The hint is diagnostic only; the suffix establishes identity.
- The Clingo rule program uses facts, normal derivation, recursion that does not pass through negation, stratified default negation, and deterministic aggregates when needed.
- Choice rules, disjunctive heads, weak constraints, minimization, maximization, arbitrary model selection, and model ordering are forbidden in the primary property engine.
- The engine requests enough models to detect nondeterminism and requires exactly one stable model for a valid analysis execution.
- A Reporter instance is created for one analysis and its bound on-model method is passed to Clingo. It may call contains for exact atoms and enumerate symbols for evidence patterns while the model is live.
- The Reporter never stores the Clingo model. It stores a durable Report that can be returned or printed after solving completes.
- Reporting is deliberately coupled to Clingo for the MVP. A separate evidence representation is deferred until actual API friction justifies the indirection.
- The outcome model contains PROVED and UNKNOWN. UNKNOWN fails an assertion but is not a Disproof. DISPROVED is reserved for future genuine refutation evidence.
- Reporting is best effort in the MVP: preserve structured provenance and evidence now, while deferring presentation polish.
- The rewrite is implemented alongside the current analyzer until the new public seam covers the agreed sound subset. Public exports then switch to the new analyzer and the old monolith is removed.

## Testing Decisions

- The primary and ideally only behavioral seam is the public analyze operation. Tests provide a complete SQL Program and optional Knowledge, then assert only the returned Report.
- Behavioral tests assert PROVED or UNKNOWN outcomes, user-relevant evidence, source locations, and diagnostics. They do not inspect SQLGlot ASTs, internal IR nodes, generated ASP text, raw Clingo atoms, or private conversion helpers.
- Existing Unique Join tests provide prior art for happy paths, incomplete composite-key coverage, marker handling, CTEs, subqueries, views, grouping, distinctness, qualification, and multiple assertions. Sound scenarios are migrated to the new seam rather than mechanically preserving old implementation behavior.
- The first tracer-bullet test covers one self-contained Create Table declaration and one marked equality join from SQL text through the returned Report.
- Soundness cases cover single and composite Unique Sets, Candidate Keys, nullable uniqueness under ordinary equality, complete and incomplete key coverage, constants, unsafe range predicates, and unsupported null-safe predicates.
- Propagation cases cover Filter, Project including aliases, Aggregate Grouping Keys, Distinct, CTEs, FROM subqueries, nested views, and recognized QualifyByPartition.
- Identity cases cover aliases and self-joins to ensure Relation Instances remain separate while sharing one Relation Definition.
- Provenance cases assert useful locations across direct SQL, nested views, catalog Knowledge, and cycle diagnostics through the public Report.
- Unsupported-feature cases assert UNKNOWN or explicit unsupported diagnostics and ensure unsupported semantics are not approximated.
- Every engine behavior test requires exactly one stable model. More than one model is an engine-policy failure, not a valid analysis result.
- A deliberately small static rule-policy test rejects the main forbidden Clingo constructs: choice rules, disjunctive heads, weak constraints, minimization, and maximization. It is a guard, not a complete ASP parser or stratification checker.
- Tests do not scan or mutate production data. Schema declarations and explicit Knowledge provide all external facts.

## Out of Scope

- DuckDB schema, constraint, and view autodiscovery.
- Create Index support and unique-index-derived Knowledge.
- RIGHT, FULL, SEMI, ANTI, and CROSS join reasoning.
- Non-equality joins, OR predicates, and null-safe equality.
- Scalar and correlated subquery reasoning.
- Set operations including UNION, INTERSECT, and EXCEPT.
- Grouping sets, ROLLUP, and CUBE.
- General window-function reasoning beyond the recognized row-number partition qualification.
- Data-backed duplicate checks, sampling, or execution of assertion queries.
- A DISPROVED outcome without genuine refutation evidence.
- A Clingo-independent reporting or evidence abstraction.
- Final presentation design for deeply nested provenance reports.
- Backward-compatible DuckDB connection discovery in the new public analyze operation.

## Further Notes

- The initial implementation should be a narrow end-to-end tracer bullet rather than a complete IR model built in isolation. It should prove one simple join from SQL text through Clingo and the Reporter before operators are added incrementally.
- The project remains conservative: a proof is trusted only when supported semantics establish it. UNKNOWN is the expected result when syntax, Knowledge, or inference is insufficient.
- The current DuckDB metadata extraction is useful prior art for the later autodiscovery adapter, but it does not belong in the MVP property engine.
- Direct reporting access to the live Clingo model is an intentional learning-stage decision. Revisit it after concrete reporting patterns reveal the smallest useful abstraction.
- The repository's domain glossary and architectural decisions remain the canonical source for terms such as Assertion, Unique Join Assertion, Property, Proof, Unknown, Disproof, Unique Set, Candidate Key, Relation Definition, Relation Instance, Origin, Expansion Context, Knowledge, Create Statement, and Root Select.

---

**Comment by polvoazul on 2026-08-21:**

Implementation-convention clarification: [docs/implementation-conventions.md](https://github.com/polvoazul/sqlassert/blob/74b8416/docs/implementation-conventions.md) is the canonical record for the agreed package layout, keyword-only analyze signature, exact shared identifier sequence (`rel_users_1` … `join_5`), and Control.add() fact transport. Implemented in 74b8416.

---

**Comment by polvoazul on 2026-08-27:**

The relational-IR and Clingo rewrite is complete: the implementation tickets #2 through #10 are closed, analyze is the public engine, the direct-reference IR and deterministic Clingo boundary are in place, and the supported proof/diagnostic behavior is documented and tested. Current verification: 236 tests pass and ty check passes. #11 remains open as the next corpus-scale robustness follow-up, not as unfinished MVP implementation.
