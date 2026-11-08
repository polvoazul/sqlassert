# 48. dbt plugin as its own package

- **State:** open
- **Labels:** dbt, packaging
- **Created:** 2026-11-08
- **Related:** [44. v1 external integrations](0044-v1-external-integrations.md)

The repo becomes a monorepo. The dbt integration moves out of the
`sqlassert` package into its own folder that builds and tests on its own.

- [ ] `integrations/dbt/` (or `sqlassert-dbt/`): own `pyproject.toml`,
  depends on `sqlassert` and dbt-core; `sqlassert` does not depend on it.
- [ ] Move `sqlassert/dbt/` and its tests there; delete them from the main
  package.
- [ ] End-to-end tests in that folder: a small dbt project compiled with
  dbt-duckdb (no warehouse), checked through the plugin, so the
  manifest shape and the contract-constraint table are tested against a real
  `dbt compile` rather than a hand-written manifest.
- [ ] Its own CI job; the main package's CI does not install dbt.
