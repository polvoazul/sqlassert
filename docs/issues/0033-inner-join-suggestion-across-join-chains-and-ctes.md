# 33. INNER JOIN suggestion across join chains and CTEs

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01

## Problem

`suggest` proposes `INNER JOIN` for a LEFT JOIN whose padding is always
removed (`sqlassert/improvements.py`). It misses two cases:

- **Chains.** `a LEFT JOIN b ... LEFT JOIN c ON c.x = b.x ... JOIN d ON d.y = c.y`:
  the inner join on `c` removes every row where `b` was padded, but this is
  not traced back to `b`.
- **Across a CTE.** A filter in the query that reads a CTE never justifies a
  rewrite inside the CTE, even when the CTE is referenced once. Doing so
  needs the IR to record `LIMIT` and window functions, which change rows the
  outer filter sees.

## Acceptance criteria

- [ ] Chains: padding removal is traced through null-rejecting joins and
  `WHERE`, with a DuckDB equivalence test per new case.
- [ ] CTEs: either supported (with the `LIMIT`/window guard) or documented as
  out of scope.
