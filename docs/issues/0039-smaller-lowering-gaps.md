# 39. Smaller lowering gaps

- **State:** open
- **Created:** 2026-11-06
- **Split from:** issue 23

Leftovers found while closing issue 23's items. Each is small and loses some
proofs; none is unsound.

- [ ] More builtins in the registry (`convert._builtins`): most built-in
  functions are strict, and each entry moves one from `ir.OpaquePureFunction`
  to `ir.Call`, which rejects NULL and is non-null when its total arguments are.
- [ ] Injectivity of lossless casts (`CAST(id AS BIGINT)`) and of
  multiplication by a non-zero constant, so keys carry through them.
- [ ] Subquery forms not lowered as semi or anti joins: `NOT IN` (NULL
  semantics), tuple `IN`, an EXISTS body with `GROUP BY`, `HAVING`,
  `DISTINCT`, `LIMIT` or `WITH`, and `ANY`/`ALL` comparisons.
- [ ] A unique index over deterministic expressions implies its columns are
  unique; it is reported `unsupported-create-statement` today.
- [ ] Join kinds lowered opaque (each has a TODO in `convert._join_kind`):
  ClickHouse outer, SEMI, ANTI, ANY and ASOF joins (outer joins pad with the
  type's default, not NULL, unless `join_use_nulls` is set), ARRAY JOIN, and
  any other kind not modelled. Model one only after checking its dialect's
  semantics.
