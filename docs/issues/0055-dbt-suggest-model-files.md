# 55. Run `sqlassert-dbt suggest` on dbt model files

- **State:** open
- **Labels:** dbt, integrations
- **Created:** 2026-11-10
- **Related:** [50. dbt: read `catalog.json`](0050-dbt-catalog-json.md)

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
