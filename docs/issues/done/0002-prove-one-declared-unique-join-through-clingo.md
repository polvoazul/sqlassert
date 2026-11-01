# 2. Prove one declared Unique Join through Clingo

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-26
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/2

## Parent

#1

## What to build

Deliver the first complete tracer bullet of the new property engine: analyze a self-contained SQL Program with one Create Table declaration and one marked INNER equality join, lower the minimum bound relational IR, add readable ground facts and deterministic rules to Clingo, and return a durable PROVED or UNKNOWN Report through a callback-based Reporter.

The new path must coexist with the legacy analyzer for now. This ticket establishes the public `analyze` seam and the architectural boundaries needed by later slices without attempting to model the full MVP upfront.

## Acceptance criteria

- [ ] A SQL Program declaring a relation with a single-column Unique Set and joining that relation on the unique column returns PROVED through the public analysis seam.
- [ ] The equivalent join against a relation without a matching Unique Set returns UNKNOWN rather than guessing or reporting a Disproof.
- [ ] The minimum Create Statement and Root Select grammar is parsed, and more than one Root Select produces an explicit program diagnostic.
- [ ] SQLGlot objects do not cross into the immutable bound IR, and Clingo objects do not enter the IR.
- [ ] Generated relation, column, key, join, expression, and assertion constants use readable sanitized hints plus deterministic incremental suffixes.
- [ ] Clingo receives ground facts and deterministic rules and produces exactly one stable model.
- [ ] A Reporter instance consumes the live model through its bound callback, retains only a durable Report, and does not print.
- [ ] Direct assertion provenance is available to the returned Report.
- [ ] Automated tests through `analyze` cover at least the successful proof and the UNKNOWN missing-proof path without asserting internal AST, IR, fact, or atom shapes.

## Blocked by

- None — can start immediately.

---

**Comment by polvoazul on 2026-08-21:**

Implementation-convention clarification: [docs/implementation-conventions.md](https://github.com/polvoazul/sqlassert/blob/74b8416/docs/implementation-conventions.md) is the canonical record for the agreed package layout, keyword-only analyze signature, exact shared identifier sequence (`rel_users_1` … `join_5`), and Control.add() fact transport. Implemented in 74b8416.
