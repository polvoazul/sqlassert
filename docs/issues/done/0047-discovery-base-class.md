# 47. Discovery: one base fetcher, a subclass per dialect

- **State:** done (2026-11-08)
- **Labels:** dialects, discovery
- **Created:** 2026-11-08
- **Related:** [44. v1 external integrations](../0044-v1-external-integrations.md)

Replaces the separate `discovery/postgres.py` and `discovery/mysql.py`
fetchers (#44) with a shared base.

- [x] A base fetcher over DB-API that reads the standard
  `INFORMATION_SCHEMA` views: `tables`, `columns` (`is_nullable`,
  `data_type`), `table_constraints`, `key_column_usage`,
  `referential_constraints`, `views`.
  (`discovery/information_schema.py`, `InformationSchemaKnowledgeFetcher`.)
- [x] One subclass per dialect, overriding only the methods whose standard
  view is missing or incomplete there (Postgres and Redshift unique
  indexes from `pg_index`; MySQL `statistics`; BigQuery's per-dataset
  `INFORMATION_SCHEMA`; parameter style). Postgres, MySQL, Redshift,
  Snowflake, BigQuery, Databricks; what each overrides is in
  `docs/dialects.md`. Postgres overrides every hook: its
  `information_schema` hides constraints from a read-only role. Redshift
  has no indexes, so no `pg_index` read. Snowflake has no
  `KEY_COLUMN_USAGE`, so keys come from `SHOW ... KEYS`.
- [x] Collation: the dialect's default collation only. Per-column and
  per-table collations are not read. Except Postgres's nondeterministic
  ones, which would otherwise be taken as comparing by identity.
- [x] Keys, unique constraints, unique indexes and Foreign Keys are taken as
  enforced. No per-constraint trust (`RELY`) field. The fetchers report them
  as declared; `bind` applies the dialect's key trust, as for SQL keys (see
  Decided).
- [x] Partial unique indexes (with a `WHERE`) and expression indexes are
  skipped, never read as keys.
- [x] Delete the old fetchers and their tests; port the Postgres live test
  and the MySQL fakes to the new classes.

## Decided

- Taking warehouse keys as enforced trades soundness on unenforced keys for
  simplicity. The docs say so: an unenforced key that is violated makes
  proofs that depend on it wrong.
- Refined while implementing: "taken as enforced" means the fetcher reports
  every declared key with no trust field, not that it is Knowledge. #44 made
  an unenforced key never Knowledge; `bind` keeps that rule for catalog keys
  (Knowledge where the dialect enforces keys, a Property Declaration under
  `trust_unenforced_keys`, otherwise an `unenforced-constraint` warning). So
  the soundness trade is the caller's opt-in, not the default. `RELY` in a
  warehouse catalog is not read.
- Postgres keeps its exclusions of constraints that do not hold on every
  row (NOT VALID, NOT ENFORCED, DEFERRABLE, foreign keys onto tables with
  row-level security, inheritance parents, foreign tables): PostgreSQL
  enforces keys, so these would otherwise be Knowledge.
