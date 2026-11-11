# 57. dbt: incremental models

- **State:** open
- **Labels:** dbt, integrations
- **Created:** 2026-11-10
- **Related:** [56. Live warehouse catalog](done/0056-dbt-live-connection.md), [50. catalog.json](done/0050-dbt-catalog-json.md)

An incremental model is two things: a table that keeps rows across runs (what
its readers see), and a query that inserts one batch per run, in one of two
`is_incremental()` branches. Support is the minimum that is sound; everything
else here is deferred.

## Today (bare minimum)

- Readers see a **table**: its columns from an enforced contract or the
  catalog, else unknown. Its passing tests are facts on it (when trusted).
- Each branch is **its own view** in the project's Program, with the model's
  tests as markers. A test or marker holds only if every branch proves it.
- `{{ this }}` in a branch reads a **previous-run table** with unknown
  columns and no facts, never the model's table: the model's tests ran after
  the last insert, so the previous table may violate them, and a batch must
  not prove its own test from itself.
- A key test holds only under `merge` or `delete+insert` on that `unique_key`
  and no `incremental_predicates`; not-null and accepted-values hold if every
  batch satisfies them; relationships never (earlier rows' references may be
  gone).
- The other branch is rendered only when the model uses `ref`, `source`,
  `this`, `config` and `is_incremental()` alone; otherwise the model is
  reported with one branch, its tests not proved, its markers `UNKNOWN`, and
  `suggest` lists only its hazards.

## Ideas (deferred)

- Facts of the previous table: an enforced constraint (and the catalog's
  columns) holds on it as on the current one; only trusted tests do not.
- Schema drift: with `on_schema_change` other than `fail`, the table's
  columns may differ from the query's; the catalog is the better source then,
  and a mismatch could be a warning.
- Render more Jinja (`var()`, common macros) so the other branch is
  analyzed more often; or run dbt itself twice (`--full-refresh` and not).
- `insert_overwrite` / partition strategies: a key of every batch is a key
  per partition; model that instead of giving up.
- Snapshots: a table with `dbt_valid_from`/`dbt_valid_to`; their tests could
  be proved from the snapshot strategy.
- A non-incremental model that reads `{{ this }}` becomes a view reading
  itself (a cycle error); treat it like an incremental model.

## Pitfalls to keep in mind

- When rendering fails, dbt's compiled branch may name the real table for
  `{{ this }}`, so its facts apply to the batch; tolerable only because such
  a model's results are already downgraded. Keep it that way.
- A model's tests are asserted inside its view and declared on it; sound
  because proofs flow from inputs to outputs, never into a body
  (`test_a_test_is_never_its_own_declaration` guards it).
