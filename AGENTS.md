# Agent Guidelines

## No backwards compatibility while in alpha

sqlassert is alpha. Code simplicity wins over compatibility:

- Replace mechanisms outright; do not keep old and new side by side.
- No feature flags, shims, deprecation paths, or fallback branches for old
  behavior. They are an anchor on every later change.
- Delete superseded code, tests, and docs in the same change.

The one contract to keep stable is the user-facing one: the Property Markers
(`/**ASSERT ...**/`, `/**DECLARE ...**/`) and the Properties they introduce.
Internal representation (IR, analysis functions, rules) may change freely.

## Analysis works without a connection

`analyze(sql)` must work from the SQL alone ([ADR 0005](docs/adr/0005-analyze-sql-without-a-connection.md)).
Whenever a connection would have helped (a table with no `CREATE`, an unknown
column type, an ambiguous column), emit a warning Diagnostic saying what is
missing and how to supply it, and treat the affected facts as unknown; never
guess. With a connection (`analyze_database`), resolve it from the catalog. A
connection may only add proofs, never change what the SQL alone proves.

## Where things are

- Domain language: `CONTEXT.md`. Use its terms; it lists terms to avoid.
- Implementation conventions: `docs/implementation-conventions.md`.
- Rule style: `sqlassert/rules/STYLEGUIDE.md`.
- Test style: SQL cases in `tests/*.sqltest` (format in `tests/sqltest.py`;
  see "Tests" in `docs/implementation-conventions.md`).
- Open work: `docs/issues/` (index in `docs/issues/README.md`).
- Architecture decisions: `docs/adr/`.
