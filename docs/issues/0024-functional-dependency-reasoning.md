# 24. Functional-dependency reasoning

- **State:** open
- **Labels:** epic, needs-design
- **Created:** 2026-11-01
- **Migrated from:** README Ideas, `TODO.md` Later Ideas

Represent and derive functional dependencies (`X → Y`): when equal values of
one column set guarantee equal values of another, including dependencies
created by keys, equality predicates, deterministic expressions, and
aggregation. Without them, uniqueness only flows through columns that pass
through unchanged.

Theory, propagation rules, open questions, and the prioritized checklist live
in [`docs/functional-dependencies.md`](../functional-dependencies.md#opportunities);
that doc is the source of truth. Next step: representation design.

## Someday/maybe (from `TODO.md` Later Ideas)

- [ ] Someday/maybe: optimization hints from proved dependencies. The facts that
  prove properties also justify rewrites (see
  [`docs/references.md`](../references.md), Lindner et al.): report a
  redundant `GROUP BY` column when another grouping column determines it, and a
  join whose RHS is unique and contributes no output columns ("join only
  filters", a semi-join candidate). Needs functional dependencies first.
