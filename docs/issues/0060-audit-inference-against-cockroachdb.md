# 60. Audit inference against CockroachDB's FD library

- **State:** open
- **Priority:** P3 (ready): Provisional: not prioritized yet.
- **Theme:** soundness
- **Labels:** check, ready-for-agent
- **Created:** 2026-11-11
- **Related:** [35: Paulley audit](done/0035-audit-fd-propagation-against-paulley.md), [40: Calcite audit](done/0040-audit-inference-against-calcite.md), [45: Calcite missing proofs](done/0045-close-the-calcite-audit-missing-proofs.md), [27: soundness oracle](0027-soundness-oracle-against-duckdb.md)

## Why

CockroachDB's optimizer has the production implementation closest to ours:
`FuncDepSet` in `pkg/sql/opt/props/func_dep.go` tracks strict and lax FDs and
keys, constants, equivalences and not-null columns, cites Paulley, and lists
where it departs from him. Its lax FD is ours (determinant-only; see
"Strict vs lax FD" in [functional-dependencies.md](../functional-dependencies.md)),
so unlike Calcite the contracts line up and answers compare directly. Its
test data prints an `fd:` line for every plan node (260 in `logprops/join`
alone): a large, independent corpus of expected FDs.

Same shape as issue 40: semantic comparison plus targeted regressions; gaps
become follow-up issues. It also feeds paper idea 1 (soundness of derived
properties in optimizers): any CockroachDB unsoundness found here counts.

## Setup

Pinned commit `d30c905fff79ef825adc96bcc647f1872a90f2ff`, shallow clone in
the gitignored `vendor/` folder (sparse checkout of `pkg/sql/opt` is enough):

```bash
mkdir -p vendor/cockroach && cd vendor/cockroach && git init -q && git remote add origin https://github.com/cockroachdb/cockroach.git && git sparse-checkout set pkg/sql/opt && git fetch --depth 1 origin d30c905fff79ef825adc96bcc647f1872a90f2ff && git checkout FETCH_HEAD
```

**License:** CockroachDB Software License (source-available, not open
source). Read it to compare; do not copy code or test files. Write our own
`.sqltest` cases, citing the upstream file and case by name in a provenance
comment (`-- inspired by logprops/join, "left join with lax key"`).

## What to do

1. Compare `FuncDepSet` operations with our Analysis Functions in
   `rules/uniqueness.py` and `rules/nullability.py`: `MakeLeftOuter`,
   `MakeFullOuter`, `MakeApply` (lateral and correlated subqueries),
   `MakeProduct`, `MakeMax1Row`, `MakeNotNull`, `AddSynthesizedCol`,
   `ProjectCols`, `ReduceCols`, `AddConstants`, `AddEquivalency`. Table:
   upstream method, its assumptions, our code and cases, verdict (agrees /
   intentionally different / missing / CockroachDB unsound).
2. Lax promotion: under our definition a lax FD turns strict once its
   determinant is non-null. Check whether our left-join (η, Lemma 8) and
   set-operation rules use that or Paulley's weaker "every column non-null"
   condition; [functional-dependencies.md](../functional-dependencies.md)
   still states the latter in both places. Tighten code and doc if we miss
   proofs.
3. Walk `pkg/sql/opt/memo/testdata/logprops/*` (join, groupby, set, select,
   project, limit, window, max1row, scalar, values, with) and, where useful,
   `pkg/sql/opt/norm/testdata/rules/*` for rewrites justified by FDs
   (eliminated GROUP BY columns, DISTINCT, joins). Restate each relevant `fd:`
   or key claim as an ASSERT in DuckDB SQL with explicit DDL.
4. Derive expected outcomes from our Property contracts, not CockroachDB's
   answers. Check every PROVED case with `pytest --soundness-oracle`; every
   claim we keep UNKNOWN that CockroachDB makes gets a DuckDB counterexample
   or a missing-proof entry.
5. Optional, after 1-4: the same pass over TiDB's `pkg/planner/funcdep`
   (Apache 2.0, so code and tests may be adapted), pinned at
   `8626dd7860717679b217b000637ab97fdc3df515`.

## Acceptance criteria

- [ ] `docs/cockroach-audit.md`: versioned comparison table over the
      operations above.
- [ ] Lax-promotion question answered; code and FD doc agree with our
      definition.
- [ ] `tests/cockroach_audit*.sqltest` cases with provenance comments, own
      wording, schema in DDL or Property Declarations; positive and boundary
      cases per area.
- [ ] All pass with `pytest --soundness-oracle`; unsupported cases listed.
- [ ] Results reported in three lists: soundness bugs in sqlassert (fixed
      here), soundness problems in CockroachDB (each with a DuckDB
      counterexample), missing proofs (follow-up issue).
