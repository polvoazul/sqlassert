# 25. Check rule consistency invariants

- **State:** open
- **Labels:** needs-design
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## Blocked by

Issue 24 (done; see [functional-dependencies.md](../functional-dependencies.md)) deletes sets of interest
and `_unique_sets`, which most of this covers. Re-scope against the closure
and `RowIdentity` once 24 lands, or close it.

## What to check


- [ ] Check rules internal consistency invariants: verify that every column-set member belongs to a
  relation and that every member belongs to each relation associated with the
  set. Cover accepted and inferred Unique Sets, sets of interest, and join
  coverage inputs. Consider running these checks across all engine tests;
  decide the checking mechanism and empty-set semantics before implementing.
