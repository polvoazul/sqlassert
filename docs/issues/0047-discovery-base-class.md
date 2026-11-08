# 47. Discovery: one base fetcher, a subclass per dialect

- **State:** open
- **Labels:** dialects, discovery
- **Created:** 2026-11-08
- **Related:** [44. v1 external integrations](0044-v1-external-integrations.md)

Replaces the separate `discovery/postgres.py` and `discovery/mysql.py`
fetchers (#44) with a shared base.

- [ ] A base fetcher over DB-API that reads the standard
  `INFORMATION_SCHEMA` views: `tables`, `columns` (`is_nullable`,
  `data_type`), `table_constraints`, `key_column_usage`,
  `referential_constraints`, `views`.
- [ ] One subclass per dialect, overriding only the methods whose standard
  view is missing or incomplete there (Postgres and Redshift unique
  indexes from `pg_index`; MySQL `statistics`; BigQuery's per-dataset
  `INFORMATION_SCHEMA`; parameter style).
- [ ] Collation: the dialect's default collation only. Per-column and
  per-table collations are not read.
- [ ] Keys, unique constraints, unique indexes and Foreign Keys are taken as
  enforced. No per-constraint trust (`RELY`) field.
- [ ] Partial unique indexes (with a `WHERE`) and expression indexes are
  skipped, never read as keys.
- [ ] Delete the old fetchers and their tests; port the Postgres live test
  and the MySQL fakes to the new classes.

## Decided

- Taking warehouse keys as enforced trades soundness on unenforced keys for
  simplicity. The docs say so: an unenforced key that is violated makes
  proofs that depend on it wrong.
