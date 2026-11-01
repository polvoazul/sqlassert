# 17. Complete typed `ENUM` support

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-11-01
- **Migrated from:** `TODO.md`

## What to build

Enumerated Domains support string, numeric, and boolean SQL
literals without conflating values of different SQL types, and malformed
enumerations fail explicitly.

## Blocked by

 [#16](0016-assert-and-declare-string-enum-domains.md) (Task 4) — Assert and declare string `ENUM` domains.

## Acceptance criteria

- [ ] Numeric and boolean domain members work in assertions, declarations,
  Knowledge, propagation, containment proofs, and reports.
- [ ] Literal identity retains SQL type and value, so values such as numeric one
  and string one remain distinct.
- [ ] Empty value lists, null members, and duplicate typed values are rejected
  with clear diagnostics.
- [ ] Unsupported literal forms are diagnosed rather than stringified or
  treated as opaque domain members.
- [ ] Mixed supported literal types remain distinct and deterministic through
  fact encoding and reporting.
