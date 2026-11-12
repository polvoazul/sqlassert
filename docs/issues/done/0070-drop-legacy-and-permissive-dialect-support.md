# 70. Drop legacy and permissive dialect support

- **State:** done (2026-11-12)
- **Priority:** P2 (open): [ADR 0006](../../adr/0006-model-only-current-strict-engines.md) makes this code dead weight.
- **Theme:** dialects
- **Labels:** dialects, simplification
- **Created:** 2026-11-12
- **Related:** [46: certify dialect facts](../0046-certify-dialect-facts.md)

[ADR 0006](../../adr/0006-model-only-current-strict-engines.md) models each engine at its latest release, under its default settings, with strict modes on. Remove what models anything else, in one change per item, with tests and docs:

1. **Bare columns in grouped selects.** `_aggregate_output_columns` ([convert.py](../../../sqlassert/ir/convert.py)) lowers a select item that is neither grouped nor inside an aggregate as `AnyAggregate` ("where a dialect allows one"). Make it opaque instead, with an `unsupported-construct` warning. A column is allowed outside an aggregate only when the grouping columns determine it, where the dialect allows that (functional dependence on a key). Do this first: aggregates inside expressions (`max(x) + 1`) depend on it.
2. **Spark ANSI mode off.** Model Spark with ANSI on, its default since 4.0. Remove `PERMISSIVE` from `scripts/certify_dialects.py`. Revisit the Spark flags it weakened (`casts_raise`, checked arithmetic) and the `<=>` rejection gated on `casts_raise`.
3. **`spark2`.** Remove the dialect name from `sqlassert/dialects.py`.
4. **MySQL `||`.** It is OR under the default `sql_mode`. Lower it as OR and remove the `ambiguous-operator` warning, the `_ambiguous_pipes` parse hook, and their docs (CONTEXT.md, dialects.md).
5. **Docs.** Rewrite "The rule for every flag" in [dialects.md](../../dialects.md) as the ADR's rule. Remove `PERMISSIVE` and STRONGER from the certifier section.
6. **Sweep.** Look for other flags or branches that model a non-default setting or an old version (`grep -i` for legacy, permissive, lenient, `sql_mode`, version checks). Remove them or list them here.

## Acceptance

- No dialect flag, rule branch, warning or certifier session models a non-default setting or an older version.
- dialects.md states the ADR's rule.
- Full suite and `pytest --soundness-oracle` pass.

## Closing

Done, one commit per item.

1. A column outside every aggregate of a grouped select must be a grouping
   column, or a column of a table occurrence whose primary key, declared in
   the SQL, is grouped (SQL's functional dependence on a key, which
   PostgreSQL and MySQL accept); those lower to `AnyAggregate`. Any other
   makes its item an `OpaqueExpression` that warns `unsupported-construct`.
   FILTER and WITHIN GROUP count as aggregates. MySQL's wider dependencies
   (equalities, UNIQUE NOT NULL) are not read: such an item is opaque.
2. Spark and Databricks are modelled with ANSI mode on. Both have
   `checked_arithmetic`; Databricks also has `checked_division` and
   `casts_raise` (its ANSI_MODE page), so its `<=>` rejects. Spark keeps
   those two weak: its docs say `div` by zero is NULL and do not say a
   failed implicit cast in a comparison raises. The certifier has no
   `PERMISSIVE` session. STRONGER stays, since it does not depend on
   PERMISSIVE: it now hints at a flag to strengthen by hand on the latest
   release.
3. `spark2` is no longer a modelled dialect.
4. MySQL `||` lowers as OR; `ambiguous-operator` and the parse hook are gone.
5. [dialects.md](../../dialects.md) states the ADR's rule.
6. Sweep: README and the warehouse issues (51-54). Left as they are, since
   each models the default of a supported version: MySQL's
   `inline_references=False` (8.4 ignores a column-level `REFERENCES`, 9.0
   enforces it) and Oracle's `set_operations_all=False` (only gates
   suggestions; Oracle 21c added `EXCEPT ALL`).
