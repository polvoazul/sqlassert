# 51. Validate Snowflake against a real engine

- **State:** not planned
- **Labels:** dialects, soundness, P4
- **Created:** 2026-11-08
- **Parent:** [43. External integrations](0043-external-integrations.md)
- **Related:** [46. Certify dialect facts against real engines](0046-certify-dialect-facts.md)

Snowflake's semantics, Builtin flags, catalog fetcher and `.sqltest` cases are
implemented ([44](done/0044-v1-external-integrations.md)), but nothing has run
on a real Snowflake: its facts have `doc` provenance, and the fetcher is tested
only against scripted fakes (`tests/test_warehouse_catalogs.py`). A wrong
fact is a false proof. This needs an account with credentials.

- [ ] Certify the facts: `python scripts/certify_dialects.py snowflake --driver ...
  --connect ...` under the defaults and the `PERMISSIVE` settings. A failing
  probe weakens its fact in `sqlassert/dialects.py`, with a `.sqltest` case.
- [ ] A live catalog test, as `tests/test_postgres_catalog.py`: create
  tables with keys, NOT NULL, Foreign Keys and a view; fetch; check the
  Knowledge and Declarations (unenforced keys under `trust_unenforced_keys`,
  on and off). Skips unless an env var names a connection.
- [ ] Document the setup (driver, connect arguments, env var) in
  `docs/dialects.md`, "Certifying facts".
- [ ] Check the fetcher's `SHOW PRIMARY / UNIQUE / IMPORTED KEYS` parsing and `RELY` on real output (`RELY` in the catalog is not read today), and that integer overflow does or does not raise (`checked_arithmetic` is off because the docs don't say).

## Done when

Every fact of this dialect in `docs/dialect-provenance.json` is `engine`
provenance (or turned off by a failing probe, with a `.sqltest` case), and the
fetcher has a live test that skips without credentials.
