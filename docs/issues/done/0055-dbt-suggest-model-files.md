# 55. Run `sqlassert-dbt suggest` on dbt model files

- **State:** done
- **Labels:** dbt, integrations
- **Created:** 2026-11-10
- **Related:** [50. dbt: read `catalog.json`](../0050-dbt-catalog-json.md)

As a dbt user, I want to pass one or more model files to
`sqlassert-dbt suggest` and see what SQLAssert can prove about them. The
suggestions should use the model's upstream dependencies and available
source schema, so I can run this directly from the files I'm editing.

```sh
sqlassert-dbt suggest models/bills.sql models/accounts.sql --explain -- --target dev
```

I can pass the dbt options I need, such as the target, profile, or variables.
The output tells me which model each suggestion applies to and shows the
evidence when I ask for it.

## Done

`sqlassert-dbt suggest MODEL_FILE... [--explain] [--trust-upstream-tests]
[--project-dir DIR] [-- DBT_ARGS]` runs `dbt compile --select +path:FILE...`
(the caller's own selection replaces it), then runs core `suggest` on each
model's Program (`Builder._program`, shared with `check`), so a view upstream
is read through its query and the passing tests of what it reads are
declarations. Suggestions are listed per model at the model file's line;
those in inlined ephemeral CTEs, upstream views or the generated Root Select
are dropped. Without a trusted rendering, a line is matched by its text, else
by identical lines before or after it; otherwise the compiled file's line is
given. In an incremental model, rewrites and markers must be proved in both
branches (none when a branch is unrendered); a marker after the Root Select
goes after the latest end, past a closing `{% endif %}`. Every pasted marker
in the test project round-trips as PROVED.

Upstream tests are trusted only with `--trust-upstream-tests`, as in `check`.
Sources still have unknown columns ([50](../0050-dbt-catalog-json.md)).
