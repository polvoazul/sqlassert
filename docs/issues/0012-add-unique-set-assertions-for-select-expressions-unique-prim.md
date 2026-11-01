# 12. Add Unique Set Assertions for Select Expressions (UNIQUE/PRIMARY KEY markers)

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-25
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/12

## Problem Statement

As the author of a SQL Program, I can already assert that a `JOIN` cannot multiply rows (`/**UNIQUE**/`), but I have no way to assert that a Select Expression's own *output* columns — individually or as a set — are unique or form a key. Today the only way to get uniqueness facts about a view or CTE's output into the analysis is to declare them out-of-band as external Knowledge, duplicating what the SQL itself should be able to state and separating the claim from the query it's actually about.

## Solution

Introduce a second assertion kind, the **Unique Set Assertion**, written as a trailing marker on any Select Expression (a Root Select, a `CREATE VIEW` body, or a CTE definition): `/**UNIQUE(col1, col2, ...)**/` asserts the named columns form a Unique Set (nullable-tolerant); `/**PRIMARY KEY(col1, ...)**/` asserts the stricter, non-null Candidate Key — mirroring the `UNIQUE`/`PRIMARY KEY` vocabulary already used inside `CREATE TABLE`. Both are proved the same way every other property in this engine is proved — from query structure and Knowledge, never trusted by decree — and reuse the existing PROVED/UNKNOWN outcome model and diagnostic-over-silent-approximation policy. A proved assertion also becomes available as Knowledge for the rest of the same Program, so other statements referencing that view or CTE benefit without a separate declaration.

## User Stories

1. As a SQL author, I want to mark a single output column of a view as unique, so that I can assert a property my downstream queries rely on without adding it to external Knowledge.
2. As a SQL author, I want to mark a composite set of output columns as collectively unique, so that I can assert multi-column uniqueness the same way I would in a `CREATE TABLE`.
3. As a SQL author, I want the same marker vocabulary I already use for `CREATE TABLE` (`UNIQUE`, `PRIMARY KEY`) reused on selects, so that I don't have to learn a new notation.
4. As a SQL author, I want to distinguish a nullable Unique Set from a strict, non-null Candidate Key using two distinct keywords, so that the choice is explicit rather than an easily-missed modifier flag.
5. As a SQL author, I want to write more than one Unique Set Assertion on the same Select Expression, so that I can declare several independent keys at once.
6. As a SQL author, I want to write a Unique Set Assertion on a CTE definition, so that I can assert properties of intermediate results, not only final views.
7. As a SQL author, I want to write a Unique Set Assertion on the Root Select of a SQL Program, so that ad hoc queries get the same guarantee views do.
8. As a SQL author, I want the marker to sit at the end of the Select Expression it constrains, immediately before whatever closes it (`;`, `)`, or the end of a `CREATE VIEW` statement), so that its scope is unambiguous regardless of where that Select Expression is nested.
9. As a SQL author, I want a comment shaped like a Unique Set Assertion marker that isn't recognized (a typo in the keyword, malformed argument list) to be reported as a diagnostic, so that a typo never silently reads as a proof.
10. As a SQL author, I want a Unique Set Assertion that names a column absent from the Select Expression's own output to be reported as a diagnostic, so that I catch the mistake instead of getting a misleading UNKNOWN.
11. As a SQL author, I want a Unique Set Assertion written on a part of the program this analysis doesn't model (e.g. a JOIN inside a CTE body) to be reported as unanalyzed, so that I know my assertion didn't just silently vanish.
12. As a SQL author, I want a proved Unique Set Assertion on a view to make its columns available as a Unique Set to every other statement in the same program that references that view, so that I don't have to also declare it as external Knowledge to get any benefit outside the view itself.
13. As a SQL author, I want a `PRIMARY KEY`-flavored assertion to require every member column to be independently known non-null, not just unique, so that it actually proves a Candidate Key rather than merely a Unique Set relabeled.
14. As a SQL author, I want an assertion whose columns are already covered by a smaller proved Unique Set to be proved too (the superset rule), so that I don't have to declare the minimal key precisely to get a proof.
15. As a SQL author, I want to write a Unique Set Assertion on a Root Select that itself has a `WHERE`, `GROUP BY`, `DISTINCT`, or its own column list, so that ordinary top-level queries get the same modeling views and CTEs already have.
16. As a maintainer of the property engine, I want the Root Select's own filtering, grouping, and projection modeled the same way a CTE or view body's already is, so that a Unique Set Assertion — and any future assertion kind — means the same thing regardless of where the Select Expression sits in the program.
17. As a maintainer of the property engine, I want the new assertion proved via the same Clingo solve as everything else, so that there is exactly one stable model and no second reasoning pass to keep synchronized.
18. As a maintainer of the property engine, I want the assertion's IR and engine representation defined purely in terms of a Relation Definition and its columns, so that a view-attached, CTE-attached, and Root-Select-attached assertion are indistinguishable to the engine — never special-cased by where the Select Expression sits.
19. As a consumer of `Report`, I want the new assertion kind to appear as its own reported entry (outcome, origin, proving evidence), so that I can consume it the same way I already consume Unique Join Assertion results.
20. As a consumer of `Report`, I want `report.facts` (queryable per named relation) to include Unique Sets earned through a proved assertion, not only ones from Knowledge or structural propagation, so that the two sources are indistinguishable to a caller.
21. As a SQL author, I want an empty or self-duplicating column list in the marker (`/**UNIQUE()**/`, `/**UNIQUE(a, a)**/`) to be reported as a diagnostic, so that a malformed assertion isn't silently accepted or silently "proved."

## Implementation Decisions

- A new IR assertion type sits alongside the existing Unique Join Assertion, holding the asserted Relation Definition, its ordered column list, whether it is the Candidate-Key-flavored (`PRIMARY KEY`) variant, and its origin. The Program's assertions grow a second collection of this type.
- **The assertion's IR and engine representation is defined purely in terms of a Relation Definition and its ordered columns** — nothing in the IR, the Clingo facts, or the proof rule refers to whether that Relation Definition came from a Root Select, a view, a CTE, or a subquery. Only the parser (locating the marker's source-syntax attachment point) and Knowledge-declaration bookkeeping (a real name vs. anonymous, `report_name`) differ by kind — exactly as they already do for every existing Plan node, and no more. This is a hard constraint, not a stylistic preference: no per-attachment-kind branching belongs anywhere downstream of the parser.
- Grammar: the marker is `/**UNIQUE(col, ...)**/` or `/**PRIMARY KEY(col, ...)**/`, written immediately before whatever closes the Select Expression it constrains. Unlike the existing bare `/**UNIQUE**/` join marker, this one carries a variable column list, so recognition generalizes from "look up one exact literal" to "match one of a small, fixed set of marker shapes" — still real grammar, not a re-read comment. The existing helpers that reconcile source text against what parsing produced (catching an unrecognized or silently-dropped marker) generalize the same way, so a malformed or dropped Unique Set Assertion is reported exactly as a malformed or dropped join marker already is.
- The marker attaches at the one grammar production common to a Root Select, a CTE definition, a view body, and a subquery, mirroring the existing single-override-point approach the join marker already uses rather than one override per attachment site.
- **Root Select lowering is unified with nested-select lowering.** Today, the Root Select's own lowering only handles `FROM`/`JOIN`; the `WHERE`/`GROUP BY`/`DISTINCT`/`QUALIFY`/projection logic exists only for CTE, subquery, and view bodies, which in turn don't support joins. This unifies them: after lowering `FROM`+`JOIN`s as today, the Root Select applies the same post-processing nested selects already use, earning it a real, nameable-shape final Relation Definition for the first time. This is additive — a `UNION`/`INTERSECT`/`EXCEPT` arm, lowered through the same path, incidentally gains the same modeling it lacks today.
- **Proof**: check the asserted columns against Unique Sets already derivable for the same Relation Definition — from propagation or Knowledge, no distinction — using the same "asserted columns are a superset of some known Unique Set" rule `RelationFacts.is_unique` already applies for callers, now performed inside the same Clingo solve so it can feed forward. The `PRIMARY KEY`-flavored variant additionally requires every asserted column to be independently known non-null.
- **`column_not_null` propagation is extended through `Project`** (mirroring its existing Unique Set propagation: an output column computed unrenamed from one non-null input column is itself non-null), since without this the `PRIMARY KEY`-flavored assertion would almost never prove through an ordinary projecting view. Extending it through `Aggregate`/`Distinct`/`QualifyByPartition` is out of scope.
- A proved Unique Set Assertion asserts the same facts for its own Relation Definition that Knowledge or propagation would, so a later statement in the same program referencing that view/CTE sees it as if declared as Knowledge — no separate cross-statement logic needed.
- Two diagnostics generalize existing ones: an unattached Unique Set Assertion marker (mirrors the existing unattached-marker diagnostic), and a malformed argument list (empty, or a repeated column).
- A Unique Set Assertion naming a column absent from its Select Expression's own output is a diagnostic, not an UNKNOWN — UNKNOWN would misleadingly suggest it might yet be proved.

## Testing Decisions

- Tests use the existing single behavioral seam, `analyze(sql, *, knowledge=None, dialect=...) -> Report` — no new seam. The new assertion kind is exercised entirely through `Report.assertions`/`Report.facts`/`Report.diagnostics`, exactly as the existing kind is.
- A good test proves external behavior — the outcome and evidence in the returned `Report` — never an internal fact name or IR shape. `test_clingo_rule_policy.py`'s static rule-shape guard and `test_engine_policy.py`'s one-model-count check are prior art extending naturally to the new rule file.
- New/extended modules, following the existing one-concern-per-file convention: a new module for Unique Set Assertion outcomes (single column, composite, `PRIMARY KEY`-flavored, multiple assertions on one Select Expression, feed-forward into a later statement, the superset case); extensions to `test_sql_parse.py` for the new marker shapes; extensions to the view/CTE test modules for attachment there; and a module (or extension to `test_analyze_propagation.py`) for the Root Select unification, including a `UNION`/`INTERSECT`/`EXCEPT` arm gaining modeling it lacks today.
- `test_analyze_soundness.py` is prior art for negative cases — every case where the assertion must *not* be proved belongs there or alongside it, not only the happy path.
- At least one test should prove the no-special-casing constraint directly: the same assertion (same columns, same underlying structure) proved through a Root Select, a view, and a CTE should produce equivalent `AssertionReport`s, differing only in origin.

## Out of Scope

- A `DISPROVED` outcome — absence of proof is UNKNOWN, never a Disproof, exactly as for Unique Join Assertion.
- `column_not_null` propagation through `Aggregate`, `Distinct`, or `QualifyByPartition` — only `Project` is extended.
- Attaching the assertion inside an already-unsupported construct (a JOIN nested in a CTE/subquery/view body, a scalar/correlated subquery, a set operation's own combined output, grouping sets/ROLLUP/CUBE) — these stay unmodeled exactly as today; an assertion there is reported as unanalyzed.
- Per-column inline marking or any syntax alternative to the two chosen keywords — settled during design.
- Everything already listed under Limitations in `docs/mvp-scope.md` remains out of scope; this doesn't narrow or widen that list beyond what's stated above.

## Further Notes

- The Root Select has no caller-visible name, so "feed forward as Knowledge" is inherently moot for it — nothing else in the program can reference an unnamed Root Select. It matters only for view- and CTE-attached assertions.
- `CONTEXT.md` already carries the two new domain terms this spec relies on — **Unique Set Assertion** and **Select Expression** — added during design.
- Exact Clingo predicate names, `.meta` key names, and the precise SQLGlot private method(s) to override are implementation-time decisions, bound by the same private-API-coupling discipline (loud failure over silent drop) already documented for the join marker.

---

**Comment by polvoazul on 2026-08-27:**

Unique Set Assertions and non-null Candidate Key Assertions are implemented for Select Expressions, including UNIQUE(...) and PRIMARY KEY(...) markers, reporting, diagnostics, and feed-forward facts. Covered by test_analyze_unique_set_assertion.py, parser tests, and propagation tests. Current verification: 236 tests pass and ty check passes. Closing as complete.
