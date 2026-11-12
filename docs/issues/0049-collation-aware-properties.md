# 49. Collation-aware Properties

- **State:** not planned
- **Priority:** P4 (not planned): Simplified on 2026-11-08 to text without
  COLLATE in DuckDB and PostgreSQL compared by identity, any COLLATE not. On
  2026-11-12 sqlassert dropped COLLATE support altogether: each dialect's
  default collation only, and a `COLLATE` lowers opaque and warns
  `unsupported-construct`. Revisit when MySQL users or collated keys come up.
- **Theme:** lowering
- **Labels:** dialects, P4
- **Created:** 2026-11-08
- **Parent:** [42. Lowering support v2](0042-lowering-support-v2.md)

Today text compares by identity only when it is `VARCHAR` or `TEXT` with no
`COLLATE`, in a dialect whose shipped default collation is binary
(`DialectSemantics.text_is_binary`: DuckDB and PostgreSQL). Any `COLLATE`,
binary or not, makes the column not compared by identity, and a setting that
changes the default (DuckDB's `default_collation`) is out of scope
([`docs/dialects.md`](../dialects.md)). That is simple and sound, at the cost
of proofs on collated text and on MySQL, whose default ignores case.

## Idea

A Property holds *under an equality*. A Unique Set or Functional Dependency
under collation C is valid for joins, grouping and `DISTINCT` under C, so it
needs no identity at all. It does not carry:

- through a function that does not respect C (`length`, `md5`, a cast to
  bytes; `upper` respects a case-insensitive C but not an accent-insensitive
  one);
- into a value fact: a pinned constant, an Enumerated Domain (`IN ('a')`
  admits `'A'` under `nocase`), or an output compared outside the database;
- into a comparison under another collation (an explicit `COLLATE` in the
  query, a MySQL join between `_bin` and `_ci` columns).

## What it would take

- [ ] Each text column carries its collation (declared, catalog, or the
  dialect default), and the dialect lists its binary collations again
  (removed when this was simplified: DuckDB `c`/`posix`/`binary`, PostgreSQL
  deterministic ones, MySQL `_bin`).
- [ ] Equality-only rules (keys, FDs through joins and grouping) accept any
  consistent collation; value rules (pinning, domains, Builtin injectivity)
  require identity.
- [ ] Builtins say which collations they respect.
- [ ] Read per-column collations in every fetcher (`COLUMNS.COLLATION_NAME`).

Revisit when MySQL users or collated keys come up.
