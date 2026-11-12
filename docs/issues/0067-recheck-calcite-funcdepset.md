# 67. Recheck the Calcite audit against `FuncDepSet` (CALCITE-7205)

- **State:** open (waiting on upstream)
- **Priority:** P3 (waiting on upstream): Provisional: not prioritized yet.
- **Theme:** soundness
- **Labels:** check
- **Created:** 2026-11-11
- **Related:** [40: Calcite audit](done/0040-audit-inference-against-calcite.md), [60: CockroachDB audit](0060-audit-inference-against-cockroachdb.md)

## Why

Calcite is adding a `FuncDepSet` class (CALCITE-7205; the mailing list
debates bitsets vs `Set<Integer>` for column sets). The audit in
[calcite-audit.md](../calcite-audit.md) read `RelMdFunctionalDependency` at
commit `180f167e`; a new FD representation may fix or reintroduce C1-C9.

## What to do

Once CALCITE-7205 merges: re-pin `vendor/calcite`, diff the FD handlers,
recheck C1-C9 and the "missing" rows of calcite-audit.md, and add cases for
any new FD rule. Report new Calcite soundness problems with DuckDB
counterexamples.

## Acceptance criteria

- [ ] calcite-audit.md updated to the new commit, each C-number marked
      fixed / still present.
- [ ] New rules covered by cases in `tests/calcite_audit*.sqltest`.
