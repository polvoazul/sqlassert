# 6. Expand Create Views with provenance and cycle detection

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/6

## Parent

#1

## What to build

Support Create View statements as named Relation Definitions whose Select expressions use the existing relational lowering path. Resolve declarations in two passes, expand nested views per Relation Instance, retain complete Expansion Context, and terminate safely on invalid definition graphs.

## Acceptance criteria

- [ ] A Create View that preserves a Unique Set can prove a Unique Join Assertion in the Root Select.
- [ ] A view may reference a Relation Definition declared later in the SQL Program.
- [ ] Nested views expand transitively and preserve the correct Relation Definition and Relation Instance identities.
- [ ] Reusing the same view more than once creates separate Expansion Contexts for each occurrence.
- [ ] A successful or UNKNOWN Report can identify the outer assertion site and retain the relevant nested view-definition origins.
- [ ] Duplicate Relation Definitions produce an explicit program diagnostic.
- [ ] Recursive view cycles are detected through unresolved, resolving, and resolved states and produce an explicit unsupported diagnostic without recursing indefinitely.
- [ ] Automated tests through `analyze` include happy paths for direct, forward-referenced, nested, and repeated views and unhappy paths for duplicate definitions, cycles, and views that do not preserve a key.

## Blocked by

- #5

---

**Comment by polvoazul on 2026-08-27:**

Delivered by commit 0f2afa4 and covered by direct, forward-referenced, nested, repeated, duplicate, and recursive-view tests through analyze. Current verification: 236 tests pass and ty check passes. Closing as complete.
