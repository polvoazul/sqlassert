# 48. dbt plugin as its own package

- **State:** done
- **Labels:** dbt, packaging
- **Created:** 2026-11-08
- **Related:** [44. v1 external integrations](0044-v1-external-integrations.md)

The repo becomes a monorepo. The dbt integration moves out of the
`sqlassert` package into its own folder that builds and tests on its own.

- [x] `integrations/dbt/`: own `pyproject.toml`, package `sqlassert-dbt`,
  import `sqlassert_dbt`; depends on `sqlassert`, and `sqlassert` does not
  depend on it (`tests/test_module_boundaries.py` forbids importing it or
  dbt).
- [x] Move `sqlassert/dbt/` and its tests there; delete them from the main
  package.
- [x] End-to-end tests in that folder: a small dbt project compiled with
  dbt-duckdb (no warehouse), checked through the plugin, so the
  manifest shape and the contract-constraint table are tested against a real
  `dbt compile` rather than a hand-written manifest.
- [x] Its own CI job; the main package's CI does not install dbt
  (`.github/workflows/ci.yml`).

## Decisions

- **Command:** the plugin installs its own `sqlassert-dbt` script;
  `sqlassert dbt` is gone. An entry point `sqlassert` discovers would keep one
  command name but make the main CLI a plugin host for one plugin.
- **dbt is a test dependency, not a runtime one.** The plugin reads
  `target/manifest.json` and never imports dbt (a plugin test guards it), so
  it runs where only the compiled manifest is, and never pins the user's dbt
  version. dbt-core and dbt-duckdb are in its `test` extra.
- **No hand-written manifest.** Every plugin test reads a manifest a real
  `dbt compile` wrote, compiled once per session (about 2 s); tests that need
  another manifest edit a copy of it, as before. The hand-written fixture had
  drifted from real dbt (test names, a `patch_path` on a model with no YAML).
- **Contract constraints:** `customer_keys` (an enforced contract over a
  source with no tests) proves the tests of the model downstream of it, and
  not once the contract is off; the plugin's DuckDB constraint table equals
  dbt-duckdb's `CONSTRAINT_SUPPORT`; and `dbt run` rejects a duplicate
  primary key, so taking an enforced DuckDB constraint as a fact is sound.
- **Tests run against this checkout's `sqlassert`** (`pythonpath` and ty's
  `extra-paths` name `../..`, `[tool.uv.sources]` installs it editable).
