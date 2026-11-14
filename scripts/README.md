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
