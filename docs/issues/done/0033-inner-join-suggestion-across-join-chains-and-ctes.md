# 33. INNER JOIN suggestion across join chains and CTEs

- **State:** done (chains; CTEs out of scope)
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

- [x] Chains: padding removal is traced through null-rejecting joins and
  `WHERE`, with a DuckDB equivalence test per new case.
- [x] CTEs: either supported (with the `LIMIT`/window guard) or documented as
  out of scope.

## Done

**Chains** (`improvements.py` `_inner_join`): the walk up the Select keeps
the joins that pad every row the candidate pads. A later LEFT or FULL JOIN
whose ON rejects NULL in a column one of them pads (on its preserved side)
finds no match for those rows, so it pads them too; a rejection of any
column those joins pad then removes the candidate's padded rows. The walk
also passes through the semi and anti joins a `WHERE EXISTS`, `NOT EXISTS`
or `IN` subquery lowers to (issue 23): a semi join's ON rejects NULL like a
later inner join's. DuckDB equivalence tests:
`tests/test_suggest_improvements.py` (issue 33 entries).

**CTEs: out of scope.** A filter reading a CTE justifies nothing inside it:
the IR does not record windows (P4), and a LIMIT, window or aggregate
between the padding and the filter can see padded rows first; a CTE read
twice has other readers too.
