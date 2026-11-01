# 25. Check rule consistency invariants

- **State:** open
- **Labels:** needs-design
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

- [ ] Check rules internal consistency invariants: verify that every column-set member belongs to a
  relation and that every member belongs to each relation associated with the
  set. Cover accepted and inferred Unique Sets, sets of interest, and join
  coverage inputs. Consider running these checks across all engine tests;
  decide the checking mechanism and empty-set semantics before implementing.
