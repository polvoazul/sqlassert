# Handoff Backup: SQLAssert Clingo Engine Grilling Session

Backup location: `./tmp/sqlassert-clingo-grilling-handoff.md`

## Purpose

This document backs up the full architectural context and decisions from the SQLAssert property-engine grilling session. It is intended to let a fresh agent reconstruct why the design exists, not merely start the first implementation ticket.

Repository: `/Users/fred/src/sqlassert`

Current branch: `clingo`

Planning commit at handoff: `a5e3a02` (`grilled`)

Worktree was clean when checked.

## Starting Point

SQLAssert is an alpha Python library that statically checks SQL assertions without scanning production data. The existing implementation supports `/**UNIQUE**/` join markers, uses SQLGlot and DuckDB metadata, and combines marker handling, AST traversal, catalog reads, uniqueness inference, and diagnostics in one large module.

The motivating architecture was:

`SQL -> SQLGlot AST -> semantic IR -> logic facts -> prove properties -> report success or error with a path back through IR, AST, and source SQL`

The user wanted a declarative, fourth-generation-language-like property engine. Lean and Coq were considered too heavy and too far removed from runtime SQL analysis. Datalog engines were investigated, including Souffle, egglog, Flix, Formulog, Z3 Fixedpoint, and pyDatalog. Egglog was initially the practical prototype recommendation, while Souffle was the strongest conventional static-analysis engine but awkward for Python distribution.

The direction then moved to Clingo. The critical qualification is that Clingo is Answer Set Programming, not ordinary Datalog. SQLAssert will deliberately use a deterministic, stratified subset so the primary property engine behaves as predictable fixed-point inference rather than nondeterministic search.

## Core Responsibility Boundary

Clingo owns only property derivation. It does not parse SQL, resolve names, query database metadata, store SQL provenance, or decide how final output is printed.

The pipeline is:

`SQL Program -> SQLGlot ASTs -> bound relational IR + Knowledge -> ground ASP facts -> Clingo -> Reporter -> durable Report`

SQLGlot, database connections, and Clingo objects must not enter the framework-independent IR.

The Reporter is intentionally coupled to Clingo for the MVP. This was chosen to learn the real Clingo and reporting APIs before inventing an abstraction.

Relevant decisions are committed in:

- `/Users/fred/src/sqlassert/docs/adr/0001-isolate-clingo-as-the-property-engine.md`
- `/Users/fred/src/sqlassert/docs/adr/0002-use-a-framework-independent-bound-ir.md`

## Main Components

The agreed conceptual package shape is:

- `main`: composition root and public analysis operation.
- `sql_parse`: parse the complete SQL Program with SQLGlot and preserve assertion markers.
- `ir/model`: immutable semantic types.
- `ir/convert`: convert SQLGlot ASTs into the bound relational IR.
- `engine`: generate Clingo input, load rules, ground, solve, and enforce one model.
- `reporting`: consume the live Clingo model and create a durable Report.
- `rules`: Clingo logic-program resources.
- `discovery/duckdb`: future adapter that derives Knowledge from DuckDB; explicitly not MVP.

The user chose `main`, not `validate` or an abstract `orchestration` module. `main` should remain a small composition root rather than a dumping ground.

## SQL Program Grammar

The complete input is a SQL Program, not a query plus a separate view registry.

A SQL Program contains:

- zero or more Create Statements;
- at most one Root Select.

Create View bodies and the Root Select use the same Select-expression grammar and lower through the same relational conversion. The Root Select is not called an implicit or main view in the domain language.

Initially supported Create Statements are Create Table and Create View. Create Index is a future feature.

Conversion parses SQL once, then performs two semantic passes:

1. Declaration pass: register names, assign stable Relation Definition identifiers, and detect duplicates.
2. Definition pass: lower table declarations, resolve/lower view bodies, and finally lower the Root Select.

Definitions may reference later declarations. Resolution uses `UNRESOLVED -> RESOLVING -> RESOLVED` state, so cyclic view definitions are detected without infinite recursion.

## Relational IR

The IR should normalize SQL into relational operations rather than mirror SQLGlot's AST hierarchy.

Agreed relational operations:

- Scan
- Project
- Filter
- Join
- Aggregate
- Window
- Distinct
- QualifyByPartition
- Opaque

FROM subqueries and CTEs recursively become relational subplans; they are not standalone semantic operators.

`QualifyByPartition` replaced the earlier `Deduplicate` name. The MVP recognizes the exact semantic pattern that retains at most one row per partition, primarily `row_number() ... = 1`. General window reasoning remains outside scope.

The scalar expression model is intentionally smaller than SQLGlot's hierarchy:

- ColumnReference
- Literal
- FunctionCall
- Comparison
- BooleanOperation
- AggregateExpression
- WindowExpression
- SubqueryExpression
- OpaqueExpression

Grouping Keys may be arbitrary bound scalar expressions. Aggregate Expressions such as `sum(amount)` are separate from Grouping Keys such as `customer_id`; Aggregate Expressions do not themselves establish uniqueness.

Unsupported semantics become explicit Opaque nodes or UNKNOWN outcomes. They are never silently discarded or approximated.

## Relation Identity

A Relation Definition is the reusable meaning of a table, view, CTE, or subquery. A Relation Instance is one occurrence of a definition in a query, with its own identity and optional alias.

Self-joins therefore produce separate Relation Instances pointing to the same Relation Definition. This prevents aliases, columns, and derived properties from bleeding between occurrences.

For readable Clingo constants, IDs use a kind prefix, sanitized human hint, and deterministic incremental suffix:

- `rel_users_1`
- `rel_customer_2` for an aliased occurrence
- `col_id_3`
- `key_users_pk_4`
- `join_5` when no natural hint exists
- future expression and assertion IDs use analogous prefixes

The hint is diagnostic only. The suffix establishes identity. Original SQL names remain in IR/provenance, so lossy sanitization is harmless.

## Knowledge

Knowledge is typed semantic information about Relation Definitions and their columns. It may contain:

- relation column membership;
- Unique Sets;
- nullability;
- future semantic facts.

Knowledge must not contain SQL definitions, SQLGlot ASTs, parser structures, or database connections.

Create Table statements contribute Relation Definitions and Knowledge. External Knowledge may be supplied directly to analysis. A future DuckDB discovery adapter will produce the same Knowledge model.

Create View is not Knowledge; it is more SQL to parse and lower.

## Uniqueness and Null Semantics

A Unique Set is a set of relation columns whose fully non-null value combinations cannot repeat. Rows containing null in the set are not guaranteed to be distinct from one another.

A Candidate Key is a Unique Set whose members are all non-null, so every row has a complete distinct value combination.

For ordinary SQL equality, a nullable Unique Set still proves at-most-one right-side match because null values do not match. Null-safe equality is not supported in the MVP.

A Unique Join Assertion means each left-side row can match at most one right-side row. The MVP supports INNER and LEFT joins. Other join types remain outside scope.

For a composite Unique Set, every member must be determined by the join condition. A right-side key member can be determined by equality to an expression independent of that right-side relation, including a constant. A range predicate does not determine a key member. Partial key coverage produces UNKNOWN.

The MVP outcome model is:

- PROVED
- UNKNOWN

UNKNOWN fails the assertion but is not a Disproof. A future DISPROVED state must only be added when the engine can return genuine refutation evidence. This future requirement is recorded in documentation.

## Provenance

IR nodes carry an Origin ID that resolves through a provenance registry. They do not retain SQLGlot nodes.

An Origin identifies either a SQL source site or catalog-derived Knowledge. Expanded views use a persistent per-instance Expansion Context, conceptually a linked stack:

- parent Expansion Context;
- reference location where a relation was used;
- referenced Relation Definition.

For `query -> view_a -> view_b -> table`, a fact originating at the table carries the innermost context, whose parent chain reconstructs the entire path. The context is per Relation Instance, so two uses of the same view do not share the wrong call site.

Normal reporting should initially show the assertion site and deepest relevant cause, retaining the full expansion chain for structured/debug output. Presentation is intentionally best effort and will be refined later.

## Clingo Concepts and Usage

ASP means Answer Set Programming, the language accepted by Clingo.

- An atom is one concrete proposition such as `proved(join_1, unique)`.
- A fact asserts a ground atom with no variables.
- A rule derives atoms and may contain variables before grounding.
- A stable model is the set of all ground atoms true in one answer set, including input and derived atoms.
- A `clingo.Model` is a short-lived Python handle to that model during solving.
- A `clingo.Symbol` represents a term or atom in Python.

The primary rule program must produce exactly one stable model for each valid input.

Allowed rule constructs:

- facts and normal derivation rules;
- recursion that does not pass through negation;
- stratified default negation;
- deterministic aggregates when necessary.

Forbidden constructs:

- choice rules;
- disjunctive heads;
- weak constraints;
- minimization and maximization;
- arbitrary selection or ordering among multiple models.

The agreed fact transport is readable ground ASP source added with Clingo's normal program-addition API, then grounded together with rule resources. Do not use low-level backend or AST-builder APIs in the MVP.

The rule policy is documented at `/Users/fred/src/sqlassert/docs/clingo-rule-policy.md`. A deliberately small static guard exists at `/Users/fred/src/sqlassert/tests/test_clingo_rule_policy.py`; it is not intended to become a complete ASP parser or stratification checker.

## Reporter and Clingo Model Lifetime

The final MVP design does not copy the entire stable model and does not create an intermediate Python evidence hierarchy.

For each analysis:

1. Construct a Reporter instance with access to IR/provenance.
2. Pass its bound `on_model` method to Clingo solve.
3. While the model is valid, the Reporter may use `model.contains(symbol)` for exact atom membership and `model.symbols(...)` for pattern enumeration.
4. The Reporter constructs and retains only a durable Report.
5. After solve completes, enforce satisfiability and exactly one model, then return the Report.

The Reporter must never retain the `clingo.Model`; its documented lifetime ends with the callback or when solving advances.

Printing happens after solve and is controlled by the caller. The analysis operation returns data and does not print.

This deliberate reporting-to-Clingo coupling is accepted for now. The user wants to understand the real Clingo and reporting API surfaces before adding an abstraction. Decouple only when concrete friction appears.

## Public API and Testing Seam

The agreed public shape is conceptually:

`analyze(sql, *, knowledge=None, dialect="duckdb") -> Report`

The SQL argument is the full SQL Program. Missing Knowledge means empty Knowledge. The operation returns a durable Report without printing.

The confirmed primary behavioral testing seam is the public analysis operation:

`SQL Program + optional Knowledge -> analyze -> Report`

Tests assert externally meaningful outcomes, evidence, locations, and diagnostics. They should not assert SQLGlot AST shapes, internal IR nodes, generated ASP text, raw Clingo atoms, or private conversion helpers.

Every implementation ticket must add at least one happy-path and one unhappy-path automated test through this public seam. The deterministic-rule guard is the intentional exception: it is a narrow structural policy test.

The final cutover ticket must review every current test case. Every existing scenario must remain valid under the new architecture, either unchanged or rewritten one-for-one against `analyze`; no scenario may be silently dropped. Unsound legacy expectations should become explicit UNKNOWN expectations rather than being preserved for compatibility.

## MVP Boundary

Included:

- Create Table and Create View;
- at most one Root Select;
- forward references and cycle detection;
- CTEs, FROM subqueries, and recursively expanded views;
- the agreed relational operations;
- arbitrary bound Grouping Keys and separate Aggregate Expressions;
- INNER and LEFT Unique Join Assertions;
- USING and conjunctions of simple equality predicates;
- Unique Sets and Candidate Keys from DDL or explicit Knowledge;
- propagation through Filter and Project;
- uniqueness from GROUP BY, DISTINCT, and recognized `row_number() = 1` partition qualification;
- PROVED and UNKNOWN;
- deterministic Clingo solving and callback-based reporting;
- best-effort provenance reporting.

Excluded and intended for later:

- DuckDB autodiscovery;
- Create Index and unique-index Knowledge;
- RIGHT, FULL, SEMI, ANTI, and CROSS join analysis;
- non-equality, OR, and null-safe join predicates;
- scalar and correlated subquery reasoning;
- UNION, INTERSECT, and EXCEPT;
- grouping sets, ROLLUP, and CUBE;
- general window-function reasoning;
- DISPROVED without genuine refutation evidence;
- Clingo-independent reporting abstractions;
- polished nested provenance presentation.

The committed scope document is `/Users/fred/src/sqlassert/docs/mvp-scope.md`.

## Research Conclusions Retained

Do not introduce Lean or Coq to re-read or certify the analyzer. They would add a separate formalization and proof-maintenance burden without solving the runtime analysis and diagnostics problem.

Souffle remains a strong mature Datalog/static-analysis alternative but has an awkward native compiler/SWIG distribution story for this Python package. Egglog is a useful structured-term/equality-saturation option but brought a newer ecosystem, weak Python proof exposure, dependency weight, and a Python-version mismatch during research. Flix was conceptually elegant but operationally wrong for the Python package. Z3 Fixedpoint is better reserved for future hard symbolic obligations, not primary reporting-oriented inference.

Clingo was selected as the practical declarative engine, with its ASP expressiveness deliberately constrained by project policy.

## Committed Artifacts

- Domain glossary: `/Users/fred/src/sqlassert/CONTEXT.md`
- Clingo responsibility ADR: `/Users/fred/src/sqlassert/docs/adr/0001-isolate-clingo-as-the-property-engine.md`
- Independent IR ADR: `/Users/fred/src/sqlassert/docs/adr/0002-use-a-framework-independent-bound-ir.md`
- Rule policy: `/Users/fred/src/sqlassert/docs/clingo-rule-policy.md`
- MVP scope and future features: `/Users/fred/src/sqlassert/docs/mvp-scope.md`
- Rule-policy tests: `/Users/fred/src/sqlassert/tests/test_clingo_rule_policy.py`

At the time of planning, `python -m pytest -q` passed 33 tests. Re-run before implementation because this handoff is a backup, not a guarantee about later repository state.

## GitHub Planning Artifacts

- Umbrella spec: https://github.com/polvoazul/sqlassert/issues/1
- First implementation frontier: https://github.com/polvoazul/sqlassert/issues/2
- Composite/nullability: https://github.com/polvoazul/sqlassert/issues/3
- Relation instances/filter/project: https://github.com/polvoazul/sqlassert/issues/4
- CTEs/subqueries: https://github.com/polvoazul/sqlassert/issues/5
- Views/provenance: https://github.com/polvoazul/sqlassert/issues/6
- Aggregate/Distinct: https://github.com/polvoazul/sqlassert/issues/7
- QualifyByPartition: https://github.com/polvoazul/sqlassert/issues/8
- MVP hardening: https://github.com/polvoazul/sqlassert/issues/9
- Cutover and complete current-test migration: https://github.com/polvoazul/sqlassert/issues/10

Issue #1 is an umbrella spec labeled `spec`; it is not implemented directly. Issues #2-#10 collectively implement it and are labeled `ready-for-agent`. The dependency frontier starts at #2. The blocker graph is recorded in each issue body.

## Suggested Skills

- Invoke `/implement` in a fresh session against issue #2 to begin the first tracer bullet. The implementation flow should drive TDD internally and finish with code review.
- Use `/domain-modeling` only if implementation reveals genuinely ambiguous domain terminology or a new durable decision; keep `CONTEXT.md` free of implementation details.
- Use `/handoff` again before changing sessions if work must continue with substantial uncommitted context.

## User Working Preferences

- Do not invoke skills unless the user explicitly names them.
- Think critically rather than agreeing automatically with suggestions.
- Prefer the simplest implementation that satisfies the current ticket; avoid speculative abstractions.
- Keep changes surgical and test-driven.
- The user is comfortable breaking the alpha API during the final planned cutover; DuckDB discovery is intentionally deferred.

## Recommended Fresh-Session Start

Open a fresh session in `/Users/fred/src/sqlassert`, reference this handoff for background, explicitly invoke `/implement`, and point it at GitHub issue #2. Read issue #2 and parent spec #1 before changing code. Keep the existing analyzer working until the final cutover ticket.
