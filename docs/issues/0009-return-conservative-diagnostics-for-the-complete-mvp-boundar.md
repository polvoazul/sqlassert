# 9. Return conservative diagnostics for the complete MVP boundary

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-25
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/9

## Parent

#1

## What to build

Harden the complete MVP boundary so a SQL Program containing multiple Unique Join Assertions produces trustworthy aggregate results and every excluded construct returns UNKNOWN or an explicit unsupported-program diagnostic. Complete best-effort evidence, source reporting, and deterministic-model enforcement across the feature slices.

## Acceptance criteria

- [x] A SQL Program with multiple supported Unique Join Assertions reports every assertion and returns an overall successful result when all are PROVED.
- [x] A mixed program containing PROVED and UNKNOWN assertions reports both outcomes and fails the overall assertion result without treating UNKNOWN as Disproof.
- [x] RIGHT, FULL, SEMI, ANTI, and CROSS joins do not receive unsupported uniqueness proofs.
- [x] Non-equality, OR, and null-safe join predicates return UNKNOWN or an explicit unsupported diagnostic.
- [x] Scalar and correlated subqueries, set operations, grouping sets, ROLLUP, CUBE, and unsupported window semantics remain conservative.
- [x] Parse errors, misplaced markers, unsupported Create Statements, and invalid SQL Program shapes produce durable diagnostics rather than crashes.
- [x] Every solve produces exactly one stable model; zero or multiple models produce an engine diagnostic and no misleading Report.
- [x] Reports provide best-effort evidence and direct or expanded source provenance without retaining a Clingo model after the callback.
- [x] The deterministic-rule guard covers all current logic resources without attempting to become a complete ASP parser.
- [x] Automated tests through `analyze` include happy paths with multiple successful assertions and unhappy paths covering mixed outcomes, unsupported join/predicate families, malformed programs, and model-count failures.

## Blocked by

- #6
- #7
- #8

---

**Comment by polvoazul on 2026-08-24:**

**Idea: fuzz-test against an industry SQL corpus**

Pull in a real-world SQL test set (e.g. DuckDB's `test/sql` logic-test suite) as a fixture corpus, then:
1. Run it through `SqlParser` + `IrParser` and confirm nothing crashes — surfaces unsupported syntax / parser bugs against real SQL, not just our own examples.
2. Programmatically sprinkle `/**UNIQUE**/` markers onto joins across the corpus and run `analyze` end-to-end, looking for crashes or diagnostics that shouldn't fire.
3. Promote (copy) individual interesting cases into `tests/` as first-class uniqueness tests once we understand what each one actually exercises — not the whole corpus, just the cases that teach us something.

This is a good stress test for the "produces trustworthy results or an explicit diagnostic, never a crash" boundary this ticket is about.

---

**Comment by polvoazul on 2026-08-25:**

All ten acceptance criteria are now covered, closed out in 5ead93a and 00e8025:

- Multi-assertion happy path (all PROVED → overall success) and the mixed-outcome path both now have direct `report.proved` assertions, not just per-assertion outcome checks.
- RIGHT/FULL/SEMI/ANTI/CROSS joins: added outcome-level tests, and along the way found and fixed a real soundness bug — `_join_kind` read the LEFT/RIGHT side modifier before the semi/anti/cross kind, so `LEFT SEMI JOIN` / `RIGHT ANTI JOIN` etc. were misclassified as plain LEFT/RIGHT joins and could wrongly earn a uniqueness proof.
- Non-equality/OR/null-safe predicates, subqueries, GROUPING SETS/ROLLUP/CUBE, and unsupported window semantics were already conservative and tested; UNION/INTERSECT/EXCEPT at the root of a program are now actually lowered arm-by-arm (a marked join inside either arm gets analyzed on its own relation instead of a blanket "unanalyzed" diagnostic), while remaining nested inside a CTE/view/subquery body is still conservative.
- Unsupported `CREATE` statement kinds (INDEX, SEQUENCE, SCHEMA, ...) previously vanished with no diagnostic at all; they now produce `UNSUPPORTED_CREATE_STATEMENT`.
- The one-stable-model policy (`EnginePolicyError` on 0 or 2+ models) now has direct tests, exercised by substituting a tiny nondeterministic/unsat rule string since the deterministic-rule guard makes this unreachable through real SQL.
- Best-effort evidence/provenance and the deterministic-rule guard were already solid and are unchanged.

Also moved the old DuckDB-connection-based checker (`validate_unique_joins`/`unique_assertions`) to `deprecated/` and dropped it from the public API, since it's a separate, no-longer-maintained path this ticket's criteria don't cover — full removal is #10's job, not this one's.

Not in scope here, tracked separately:
- #10 (retire the legacy analyzer for real: full removal, full doc rewrite, full test audit) is now unblocked but not started.
- #11 (corpus-driven fuzz testing) is unaffected by this work.
