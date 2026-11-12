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

## Current, strict engines only

Model each engine's latest release under its default settings, strict modes
on ([ADR 0006](docs/adr/0006-model-only-current-strict-engines.md)). No
older versions, no permissive settings (non-strict `sql_mode`, Spark ANSI
off), and no meaning for SQL the standard rejects but a flavor tolerates
(a column neither grouped nor aggregated): make it opaque and warn
`unsupported-construct`. Simpler code beats supporting outdated SQL.

## Where things are

- Domain language: `CONTEXT.md`. Use its terms; it lists terms to avoid.
- Implementation conventions: `docs/implementation-conventions.md`.
- Rule style: `sqlassert/rules/STYLEGUIDE.md`.
- Test style: SQL cases in `tests/*.sqltest` (format in `tests/sqltest.py`;
  see "Tests" in `docs/implementation-conventions.md`).
- Open work: `docs/issues/` (index in `docs/issues/README.md`). Marketing
  work is in `docs/issues/mkt/`, client work in `docs/issues/pvt/`; both are
  gitignored and never committed.
- Architecture decisions: `docs/adr/`.

## Before pushing

The repository is public. Before every push, check that nothing private
leaves:

- Client work (paths, project and model names, env vars, commands run on a
  client project) goes only in gitignored places: `docs/issues/pvt/`,
  `docs/issues/mkt/`, `tmp/`. A public issue that comes from client work is
  phrased without client details.
- No outgoing file is under those directories:
  `git diff --name-only @{upstream}..HEAD`.
- No added line or commit message matches a term in
  `docs/issues/pvt/denylist.txt`:
  `git log -p @{upstream}..HEAD | grep -iEf <(grep -v '^#' docs/issues/pvt/denylist.txt)`
  must print nothing. Add a term there whenever a new client name shows up.

On any hit, stop and ask; do not push.
