# Scripts

Give each script a descriptive filename that identifies its purpose without
requiring context. Include the subject and action in the name.

## Launch assets

Regenerate the standalone interactive demo with
`python3 scripts/render_sqlassert_interactive_demo.py docs/assets`.

Regenerate the GitHub social preview with
`python3 scripts/render_sqlassert_social_preview.py docs/assets`.
The preview renderer requires Pillow and the macOS system fonts. Both scripts
use the repository's `.venv/bin/sqlassert`.

## Vendor repository audit

`scripts/audit.py` extracts facts from each checkout in `vendor/`, caches
them, and renders `tmp/vendor-report.html`. Run it from the virtualenv that
has dbt, the required dbt adapters, the local `sqlassert-dbt` package, PyYAML
and markdown-it-py:

```bash
python scripts/audit.py                       # rerun stale facts, rebuild the report
python scripts/audit.py danish-democracy-data # only these checkouts
python scripts/audit.py --dry-run             # list stale facts and why
python scripts/audit.py --steps analyze --force
python scripts/audit.py --markdown            # also print the inventory table
python scripts/audit.py -j 1                  # serial; default is 8 repositories in parallel threads
```

The run ends by printing the report's `file://` URL.

Each step writes one JSON fact per checkout, or per dbt project, under
`tmp/vendor-facts/<step>/`. A fact stores the key it was computed from and
reruns only when that key changes, its time to live passes, or `--force` is
given. Failures are stored as facts too, so a broken project is not retried
until its inputs change. The report is rebuilt from all stored facts on every
run, so it also covers checkouts not selected.

| Step | Produces | Reruns when |
| --- | --- | --- |
| `inventory` | SQL files, SQL lines, dbt models and data tests at `HEAD` | commit, `scope`/`dbt_projects`/`workload`, or the inventory module changes |
| `stars` | GitHub stars, through `gh` | URL changes, or after 24 hours |
| `compile` | `dbt deps` and `dbt compile` output beside the fact | commit, tracked uncommitted edits, project settings, dbt or adapter version, or compile code changes |
| `analyze` | proved tests, join forms and cardinality | manifest, base facts, `catalog.json`, the `sqlassert` or `sqlassert_dbt` source tree (uncommitted edits included), sqlglot version, or analysis code changes |

A new compile always produces a new manifest, so `analyze` follows it.
The report headline counts public projects only.

Edit `pvt/public_repos_audit/vendor_config.yaml` (in the private workspace
checkout at `pvt/`) to add repos or change the label, source URL,
description and workload. The keys under `repositories` are checkout
directory names. To count only certain dbt projects, set `dbt_projects`
to a list of paths relative to that checkout, using `.` for its root project.
Use `scope` to limit inventory counts to a package inside a monorepo. Private
rows show a local commit ID and omit GitHub stars. The HTML loads DataTables
and its stylesheet from a CDN.

To analyze dbt projects, set `audit` to a mapping keyed by dbt project path
within the checkout. Use `.` for the root. The audit compiles each checkout in
place, so `dbt deps` installs packages into it; keep the clone otherwise clean.

The audit never connects to a database server. It writes a placeholder
profile for the project's `adapter` (default `duckdb`, or `snowflake`).
DuckDB projects compile against an empty in-memory database, so introspective
macros run but see no tables. Other adapters compile with `--no-introspect
--no-populate-cache`; a model that needs query results fails that project's
compile. Set `ignore_tables` to relation names left out of the base-table counts: a
table a model reads that nothing in the project defines or loads (say why in
a YAML comment). Set `env` to a mapping of placeholder environment variables a project
requires, and `compile_args` to extra dbt compile arguments. Set
`catalog` to a `catalog.json` from `dbt docs generate` against the real
warehouse, as a path relative to the audit config (conventionally
`<checkout>/catalog.json`): it types the base tables it lists. sqlassert uses a catalog entry only when its
relation name matches the compiled one, so set `database` to the real
warehouse's database name: a DuckDB project then compiles against an empty
local file of that name instead of `:memory:`.

Join percentages use the joins found in successfully parsed compiled project
models as their denominator. The report records skipped models. These are
SQL JOIN forms such as LEFT and INNER. Cardinality requires separate evidence.
Eligible data tests are tests that sqlassert can express as a Property.

The cardinality section counts join sites lowered by sqlassert, with each
compiled or rendered branch counted separately. It reports the strongest
proved bound for each site. A many-to-one bound means a left row matches at
most one right row. UNKNOWN means neither direction was proved. Confirming
many-to-many requires evidence that rows can repeat on both sides. Semi and
anti joins are counted as NOT_APPLICABLE.

Each project result counts the base tables (sources, seeds and tables) its
models read: how many have every read column typed, how many have a known
unique key, and how many have a NOT NULL column. A join equality on untyped
columns proves nothing, so untyped base tables leave joins UNKNOWN. The audit
assumes eligible upstream tests passed before the model ran. Set
`trust_upstream_tests: false` on a target to disable that assumption.
A test whose severity is `warn` is no fact, since its failure does not stop
the build; `trust_warn_tests: true` on a target assumes those pass too, and
the report marks that project's proofs as conditional on it.

Facts about the seeds and sources a repository reads (column types, keys,
NOT NULL columns) live in `pvt/public_repos_audit/<checkout>/base_facts.yaml`,
recorded for one commit of the checkout; the analyze step uses them when
present and fails if the checkout is at another commit. Proofs that use them
hold under those facts. `pvt/public_repos_audit/README.md` describes the
format and how facts are obtained.
