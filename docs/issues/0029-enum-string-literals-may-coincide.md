# 29. ENUM: distinct string literals may compare equal

- **State:** open
- **Labels:** bug, soundness
- **Created:** 2026-11-01
- **Found by:** issue 17's agent, confirmed by the soundness oracle

## Problem

The domain rules (`sqlassert/rules/domain.py`) treat two different string
literals as never equal. SQL compares a string with a column of its own type:
against an INTEGER column, `'01'` and `'1'` both cast to 1. A text column with
a case-insensitive or other non-default collation has the same problem with
`'a'` and `'A'`. So an intersection can come out empty or too small, and a
false `ASSERT ENUM` is PROVED:

```sql
CREATE TABLE items (qty INTEGER);
WITH t AS (SELECT * FROM items /**DECLARE ENUM(qty IN ('01'))**/)
SELECT qty FROM t WHERE qty = '1' /**ASSERT ENUM(qty IN ('2'))**/
-- PROVED today; qty = 1 refutes it.
```

Numeric and boolean members already go through `_may_coincide` (issue 17).
Strings were never moved onto it.

## What to build

Make string members meet conservatively: two distinct strings may coincide
unless the column is known to be text with default (binary) collation. When
the column's type is unknown (no `CREATE`), assume they may coincide.
Coverage checks keep using exact identity.

## Acceptance criteria

- [ ] The example above is UNKNOWN.
- [ ] Text columns with known default collation keep today's precise results;
  every existing ENUM test stays green.
- [ ] The example is added to the suite, so the soundness oracle judges it.
- [ ] 0 oracle counterexamples.
