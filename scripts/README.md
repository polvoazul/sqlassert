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

## SQL repository facts

Run `uv run --script scripts/report_vendor_sql_repository_facts.py data-infra`
from the repository root to print a Markdown table for one vendor checkout.
Pass several directory names or omit them to report on every configured repo.
The script reads each checkout's `HEAD` and links to that commit in the table.

To open a styled report with sorting and search on macOS, run:

```bash
uv run --script scripts/report_vendor_sql_repository_facts.py --html > /tmp/sqlassert-repos.html &&
  open /tmp/sqlassert-repos.html
```

The HTML loads DataTables and its stylesheet from a CDN.

Edit `vendor/vendor_config.yaml` to add repos or change the label, source URL,
description and workload. The keys under `repositories` are checkout
directory names. To report only on certain dbt projects, set `dbt_projects`
to a list of paths relative to that checkout, using `.` for its root project.
Use `scope` to limit all counts to a package inside a monorepo. Private rows
show a local commit ID and omit GitHub stars. GitHub star counts are cached per repo
for 24 hours under `/tmp/sqlassert-github-stars-<uid>/`. On a cache miss or
expiry, the script fetches the count with `gh`.

## Vendor dbt audit

Run `python scripts/audit_vendor_dbt_projects.py` in an environment with dbt,
the required adapters, the local `sqlassert-dbt` package, PyYAML, and
markdown-it-py. Pass checkout names to run a subset. Each run writes
`results.json`, dbt logs, and compiler artifacts under
`tmp/vendor-dbt-audit/run-*`. The runner compiles a temporary copy of each
checkout's HEAD.

In `vendor/vendor_config.yaml`, set `audit` to a mapping keyed by dbt project
path within the checkout. Use `.` for the root. An empty mapping for a project
uses an in-memory DuckDB profile. For warehouse projects, set `profiles_dir`
and `target` to an existing dbt profile directory and target. Optional
`compile_args` are passed to dbt compile. Set `catalog` to a matching
catalog JSON path if available. Repos without audit settings appear in the
results with that status.

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

Each project result records whether the analysis had any known unique key or
NOT NULL facts on the source and base tables it reads. The audit assumes eligible upstream tests passed before the model ran. Set
`trust_upstream_tests: false` on a target to disable that assumption.

An audit target can set `seed_facts` to a YAML file beside the resolved vendor
config. Its `seeds` mapping uses dbt manifest seed IDs. Each entry has the
CSV's `sha256` and a `columns` mapping with observed `unique` and `not_null`
flags. The audit rejects changed CSVs. Proofs using these facts apply to the
profiled snapshot.
