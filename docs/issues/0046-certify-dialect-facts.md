# 46. Certify dialect facts against real engines

- **State:** open
- **Labels:** dialects, soundness
- **Created:** 2026-11-08
- **Related:** [44. v1 external integrations](0044-v1-external-integrations.md)

Every flag in `DialectSemantics` and every dialect Builtin entry
(`strict`, `total`, `injective`) is a soundness claim. Today they are argued
from vendor docs; only Postgres has a live test. A wrong fact is a false
proof.

## Provenance per fact

Each fact records where it comes from:

- `doc`: a vendor doc URL (and the doc version or date read).
- `engine`: certified by running probe SQL on a real engine, with the engine
  versions it passed on (e.g. `postgres 16.4, 17.2, 18.0`).

Facts don't regress across engine releases often, so certification is
one-off, not a CI job: re-run it when adding a dialect, changing a fact, or
supporting a new major version.

## Probes

- [ ] For each fact, a minimal SQL probe whose result confirms or refutes it
  (e.g. `SELECT 'a' || NULL IS NULL` for strict `||`; `SELECT 1/0` raises
  for `checked_division`; `SELECT 'A' = 'a'` for the default collation).
  One probe per fact, generated from the fact, not hand-written per dialect
  where avoidable.
- [ ] A runner (`scripts/certify_dialects.py`, not part of the package)
  that runs the probes on a DB-API connection and prints pass/fail per fact.
  On success it writes the engine version into the fact's provenance.
- [ ] A probe that fails turns its fact off (the weaker setting), never on.

## Engines

- [ ] Local engines in docker, certified: Postgres, MySQL (and MariaDB if
  supported), DuckDB (in process), SQLite (in process), Spark (local),
  Trino if added.
- [ ] Cloud warehouses (Snowflake, BigQuery, Databricks SQL, Redshift):
  `doc` provenance for now. The probes exist so they can be certified later
  with credentials.

## Done when

Every fact has a provenance; every fact of a local engine is `engine` with
versions; the runner and its docker setup are documented in
`docs/dialects.md`.
