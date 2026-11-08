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

- [x] For each fact, a minimal SQL probe whose result confirms or refutes it
  (e.g. `SELECT 'a' || NULL IS NULL` for strict `||`; `SELECT 1/0` raises
  for `checked_division`; `SELECT 'A' = 'a'` for the default collation).
  One probe per fact, generated from the fact, not hand-written per dialect
  where avoidable.
- [x] A runner (`scripts/certify_dialects.py`, not part of the package)
  that runs the probes on a DB-API connection and prints pass/fail per fact.
  On success it writes the engine version into the fact's provenance.
- [x] A probe that fails turns its fact off (the weaker setting), never on.

## Engines

- [ ] Local engines in docker, certified: Postgres, MySQL (and MariaDB if
  supported), DuckDB (in process), SQLite (in process), Spark (local),
  Trino (484, done).
- [x] Cloud warehouses (Snowflake, BigQuery, Databricks SQL, Redshift):
  `doc` provenance for now. The probes exist so they can be certified later
  with credentials.

## Status (2026-11-08)

Done: provenance for every fact (`docs/dialect-provenance.json`), probes
generated from the facts and the runner (`docs/dialects.md`, "Certifying
facts"), `tests/test_dialect_certification.py`. Certified on engines: DuckDB
1.5.2, SQLite 3.50.4, PostgreSQL 16.15, 17.11, 18.6, MySQL 8.0.46, 8.4.13,
9.7.4, ClickHouse 26.10.3.7, Trino 484. MariaDB is not a modelled dialect.

Found and fixed (now the weaker setting, with `.sqltest` cases):
DuckDB's text is not compared by identity without a COLLATE
(`SET default_collation = 'nocase'`; `COLLATE binary`, `c` and `posix` are);
`concat_ws` is modelled per dialect, NULL for a NULL value in Snowflake and
ClickHouse, absent from BigQuery, Redshift, Hive, Oracle and unlisted
dialects.

Left: the local engine Spark (Databricks, `spark`, `spark2`) keeps `doc`
provenance: certifying it needs Java or a Spark image and a DB-API bridge.
MySQL's NDB engine facts are `doc` (no NDB locally). Hive and Oracle are
`doc`.

## Done when

Every fact has a provenance; every fact of a local engine is `engine` with
versions; the runner and its docker setup are documented in
`docs/dialects.md`.
