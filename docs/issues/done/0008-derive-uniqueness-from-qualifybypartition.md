# 8. Derive uniqueness from QualifyByPartition

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/8

## Parent

#1

## What to build

Lower supported window expressions and recognize row-number qualification that retains at most one row per partition as QualifyByPartition. Derive the corresponding Unique Set for outer Unique Join proofs without generalizing to unsupported window semantics.

## Acceptance criteria

- [ ] A subquery using row number partitioned by a bound key and qualified equal to one produces a QualifyByPartition operation.
- [ ] Joining on every partition key from a recognized QualifyByPartition result returns PROVED.
- [ ] Projection aliases on partition keys map the derived Unique Set to the correct outer columns.
- [ ] Joining on only part of a composite partition key returns UNKNOWN.
- [ ] Qualification that can retain more than one row per partition does not establish uniqueness.
- [ ] Other rank functions, unsupported QUALIFY predicates, and general window semantics remain UNKNOWN.
- [ ] Partition and ordering expressions retain usable provenance for best-effort reporting.
- [ ] Automated tests through `analyze` include happy paths for single and composite row-number partition qualification and unhappy paths for incomplete keys, limits above one, other ranks, and unsupported predicates.

## Blocked by

- #5

---

**Comment by polvoazul on 2026-08-27:**

Delivered by commit d6d8ecd and covered by QualifyByPartition happy-path and conservative negative tests. Current verification: 236 tests pass and ty check passes. Closing as complete.
