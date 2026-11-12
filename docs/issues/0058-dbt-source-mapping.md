# 58. dbt: test and decide how findings map back to model files

- **State:** open (deferred)
- **Priority:** P3 (deferred): Deferred until the shared-Program plugin settles:
  every Jinja, macro, ephemeral and incremental case gets a decided, tested
  location.
- **Theme:** integrations
- **Labels:** dbt, integrations, testing
- **Created:** 2026-11-10

Every finding (a marker's outcome, a Diagnostic, a suggestion) is reported at
a line of the model file. Getting there goes through several transformations,
and each has cases with no exact answer. Today's behavior is only partly
tested and partly accidental; decide it, document it, and test every case.

## The chain

1. Raw model file (Jinja) → dbt's compiled SQL. The plugin re-renders the raw
   code with its own Jinja subset; when that reproduces dbt's output exactly,
   every character maps to a raw offset.
2. dbt's ephemeral injection: CTE text pasted after `WITH`, located and
   mapped around; that span belongs to the ephemeral model.
3. Compiled SQL → the project's Program: each body at a known line and offset.
4. Program → SQLGlot tokens → IR origins, spans and sites.

When step 1 fails, the fallbacks are: a marker by its text, in order; another
line by text that occurs once in both; by identical lines before or after;
else the compiled file's line.

## Cases to cover, each with a decided outcome

- Tags: `{{ ref() }}`, `{{ source() }}`, `{{ this }}`, `{{ config() }}`,
  `{{ var() }}`, `{{ env_var() }}`, whitespace control (`{%-`, `-%}`).
- Blocks: `{% if %}`/`{% else %}`, `{% for %}` that repeats SQL (one raw
  line, many compiled ones), `{% set %}`, `{% call statement %}`.
- Macros: project macros, package macros (`dbt_utils.star`,
  `dbt_utils.generate_surrogate_key`, `dbt_utils.union_relations`,
  `dbt_utils.pivot`), adapter dispatch, macros that emit whole CTEs or many
  lines; a marker inside a macro's output (where is it reported?).
- Ephemeral models: chained ephemerals, an ephemeral read twice, an
  ephemeral whose own marker fails, injection into a model with and without
  its own `WITH`.
- Incremental models: both branches (57), markers inside `{% if
  is_incremental() %}`.
- Comments: markers in `--` and `{# #}` comments (never markers), a marker
  next to a Jinja comment.
- Line endings and encodings: CRLF files, tabs, non-ASCII before a marker.
- Versioned models (`_v2`), models in packages, analyses.

## Decisions to make

- Lines only, or columns too (GitHub and SARIF can show a column).
- SQL a macro generated: point at the call, at the compiled file, or both.
- A finding with no raw line: the compiled file's line (today) or the model
  file without a line.
- Line numbers inside `--explain` evidence: map them, or say they are the
  compiled SQL's.

Deferred: do this once the shared-Program plugin settles.
