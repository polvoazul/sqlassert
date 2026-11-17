# 74. ADR: when an equality compares exactly (integer arithmetic and implicit casts)

- **State:** open
- **Priority:** P2 (needs an ADR): The single comparison rule (`exact_comparison`) now gates every fact an equality gives; its dialect choices were made ad hoc and lose sound proofs.
- **Theme:** soundness
- **Created:** 2026-11-16
- **Related:** [73: pending lowering and classification decisions](0073-pending-lowering-and-classification-decisions.md), [46: certify dialect facts](0046-certify-dialect-facts.md)

`sqlassert/rules/value_typing.py` types every scalar expression
(`scalar_typing`) and answers one question for every equality
(`exact_comparison`): do the two sides compare without a lossy coercion? Join
key binding, every equality dependency, Equality Groups, value facts and the
Lossy comparison Hazard all ask it. What it answers per dialect has no
recorded direction. Write an ADR that settles it, then align the code and
`docs/dialects.md`.

## Questions for the ADR

1. **Integer arithmetic.** `+`, `-`, `*` and unary `-` are typed only where
   `checked_arithmetic` holds (overflow raises). That flag was written for
   error and NULL reasoning and is reused here. MySQL is not flagged because
   `-x` of the least BIGINT returns a DECIMAL, so `b = a + 1` stops proving
   `a -> b` in MySQL, though `+` and `*` overflow raises there (from memory).
   Decide: one flag per operator, a separate typing flag, or keep it.
   SQLite (overflow converts to REAL) and the other unflagged dialects need
   the same answer.
2. **Integer widths.** Integers of any width compare exactly with each other,
   and an exact literal compares exactly with any integer or decimal. State
   it as a rule and say where it fails (unsigned types, HUGEINT, BigQuery
   `INT64`).
3. **Decimal arithmetic.** `DECIMAL(10,2) + 1` widens precision in DuckDB and
   may fall back to DOUBLE past 38 digits elsewhere. It is untyped today,
   which lost one sound proof (`an_added_literal_over_a_decimal_on_the_matched_side`).
   Decide whether computed decimals compare exactly with a declared decimal.
4. **Literal types.** `DialectSemantics.literal_types` types decimal (`0.1`)
   and exponent (`1e0`) literals per dialect. Only DuckDB is probed; the rest
   is from memory, and SQLite and Oracle are untyped. Decide the certification
   bar and add the probes to `scripts/certify_dialects.py`.
5. **Implicit casts.** Text compared with another type, and a float on either
   side, are never exact; the Lossy comparison Hazard fires only when both
   sides are typed. Decide whether a float against an unknown type also warns
   (the old Float equality Hazard did), and whether a string literal beside a
   typed column is always taken as that type in every dialect.
6. **Where the rule lives.** Whether `checked_arithmetic`, `text_is_binary`
   and the new flags stay separate `DialectSemantics` fields, or become one
   per-dialect comparison profile the ADR defines.

## Done when

- An ADR in `docs/adr/` answers 1-6.
- `exact_comparison`, `scalar_typing` and `DialectSemantics` follow it, with
  each dialect claim probed, cited or marked from memory.
- Tests cover each decision per dialect.
