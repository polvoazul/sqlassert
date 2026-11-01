# 26. Equality groups

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Split from:** [issue 16](0016-assert-and-declare-string-enum-domains.md)
  (its design note)

## What to build

Value equality between columns: with `WHERE a = b` (or an inner-join
`ON a = b`), `a` and `b` carry the same value in every row. Functional
dependencies cannot carry this: `a ↔ b` also holds for an injective
`a = f(b)`, whose values differ. Facts about values need the equality itself:
Enumerated Domains ([issue 16](0016-assert-and-declare-string-enum-domains.md)),
constant values, and Foreign Key propagation
([issue 20](0020-propagate-foreign-keys-through-queries.md)).

Equality groups are a union-find per relation over **strict** equalities only:
inner-join ON and `WHERE` conjuncts, never outer-join ON, whose equality fails
on padded rows. Groups pass through projections like any column fact.

Design: [`functional-dependencies.md`](../functional-dependencies.md#kept-outside-the-fd-closure).
Engine placement: stratum 3 in the planned order
([Strata](../functional-dependencies.md#strata)), after raw predicate facts and
before unique joins.

Split out so the value lane (16) and the Foreign Key lane (20) do not block
each other.

## Blocked by

- [issue 23](0023-close-ir-lowering-gaps.md) — `Filter` carries its predicate
  (also step 1 of [issue 24](0024-functional-dependency-reasoning.md)).

## Acceptance criteria

- [ ] An internal analysis returns, per relation, the equality groups over its
  output columns, with evidence naming each equality predicate and its line.
- [ ] Inner-join ON and `WHERE` equalities merge groups; outer-join ON
  equalities do not.
- [ ] Groups survive pass-through projections, renaming, aliases, CTEs, views,
  and FROM subqueries; columns computed by expressions start their own group.
- [ ] A new `Stratum` places the analysis per the planned order; the arena's
  cycle check stays quiet across the suite.
- [ ] No user-facing Property or marker: groups are an internal value consumed
  by 16 and 20.
