# 43. External integrations

- **State:** open
- **Labels:** epic, needs-design (parent of 44)
- **Created:** 2026-11-08

Ways to use sqlassert from the tools people already run, instead of calling
`analyze()` from Python. Most items fall into one of two shapes:

- **A front end:** a CLI, editor or test runner that runs analysis and reports
  results.
- **A Knowledge adapter:** an external check (dbt test, expectation, contract,
  catalog constraint) is read in as a `Declared` fact, and proved Properties are
  written back out in that tool's format, or flag its checks as statically
  proved.

## Children

- [44. v1 external integrations](0044-v1-external-integrations.md): analytical
  dialects, Postgres and MySQL; dbt.

## Backlog

**CLI and linter output** (the `check` command, config, JSON/SARIF output and
precise spans moved to 44)
- [ ] More output formats: JUnit XML, rdjson (reviewdog).
- [ ] rustc-style messages with a caret, `help:` (the fix) and `note:` (the
  evidence).
- [ ] Fix-its as structured text edits, shared by `--fix`, editor code actions
  and agents.
- [ ] A baseline file, so existing `UNKNOWN`s don't block adoption.
- [ ] A docs page per diagnostic code.
- [ ] pre-commit hook and GitHub Action.

**Editor**
- [ ] LSP server: diagnostics, hover showing proved facts of a relation or
  column, inlay hints with each CTE's grain, code actions that insert markers
  or apply Improvements.
- [ ] VS Code extension; syntax highlighting for `/**ASSERT` and
  `/**DECLARE` markers.

**Transformation frameworks**
- [ ] SQLMesh: same parser (sqlglot); model `grain` is a Unique Set and
  `references` a Foreign Key; audits map to Properties; its custom linter API.
- [ ] Dataform: `uniqueKey` and `nonNull` assertions.

**Data quality tools and contracts**
- [ ] Great Expectations: unique, compound unique, not null and in-set
  expectations, imported as declarations and exported from proofs. `mostly < 1`
  is not a fact.
- [ ] Soda (SodaCL), Elementary, dbt-expectations: the same adapter.
- [ ] Data contracts (ODCS, datacontract-cli): statically verify that a model
  satisfies its contract's keys, required columns, enums and references.

**Catalogs**
- [ ] A generic SQLAlchemy reflection fetcher.
- [ ] Iceberg identifier fields, Unity Catalog, Glue.

**Python ecosystem**
- [ ] pytest plugin: collect `*.sql` files, one test item per assertion, with
  the explanation as the failure message.
- [ ] Scan SQL embedded in Python string literals and `sqlalchemy.text()`.
- [ ] Ibis expressions (compiled through sqlglot); needs a programmatic
  assertion API, which is a contract decision.
- [ ] Dagster asset checks from proved facts.

**AI and agents**
- [ ] MCP server (`analyze`, `suggest`, `explain`, `fix`) and a Claude Code skill.
- [ ] Guardrail for generated SQL: flag fan-out before the query runs.

**Adoption and output**
- [ ] Browser playground via Pyodide (the dependencies are pure Python), with
  share links.
- [ ] Proof trees rendered as Mermaid or HTML.
- [ ] Export proved facts (grain, keys, Foreign Keys, enums per relation) to
  DataHub, OpenMetadata, dbt docs, or an inferred ERD.

## Open questions

- Inline suppression (`/**IGNORE ...**/`) would be a new Property Marker, which
  grows the one stable contract. Config-only ignores may be enough.
- Unenforced warehouse keys: Declared always, only with `RELY`, or opt-in?
  Decided in 44 for its dialects.
