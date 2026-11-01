# 4. Propagate uniqueness through relation instances, filters, and projections

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/4

## Parent

#1

## What to build

Propagate Unique Sets through Filter and Project operations while representing every query occurrence as a distinct Relation Instance. SQL aliases, renamed projected columns, and self-joins must preserve correct identity so a proof from one occurrence cannot leak into another.

## Acceptance criteria

- [ ] Filtering a relation preserves its existing Unique Sets and can still prove a supported outer join.
- [ ] Projecting every member of a Unique Set preserves that set on the projected output.
- [ ] Renaming a projected unique column maps the Unique Set to the output name and proves a join using that alias.
- [ ] Dropping a required key member or replacing it with an unsupported computed expression returns UNKNOWN.
- [ ] Two aliases of the same Relation Definition become separate Relation Instances with separate bound column identities.
- [ ] A self-join proves or remains UNKNOWN independently for each relation occurrence, with no fact leakage between aliases.
- [ ] Unsupported projection or filter semantics are represented explicitly rather than silently ignored.
- [ ] Automated tests through `analyze` include happy paths for filters, projections, aliases, and a valid self-join plus unhappy paths for dropped/computed key members and alias isolation.

## Blocked by

- #3

---

**Comment by polvoazul on 2026-08-27:**

Delivered by commit 5434f09 and covered by propagation, alias, projection, filter, and self-join tests through analyze. Current verification: 236 tests pass and ty check passes. Closing as complete.
