# 36. Move semantic tests to `.sqltest` files

- **State:** done
- **Labels:** tests, refactor
- **Created:** 2026-11-01
- **Related:** [27: soundness oracle](0027-soundness-oracle-against-duckdb.md)

## Why

Most semantic tests are SQL plus expected outcomes wrapped in Python: build a
string, call `analyze()`, compare outcomes or Diagnostic codes. The SQL is the
test; the Python is noise. Move every test that fits into SQL files where each
expectation sits next to the line it checks.

Corpus import (issue 11) and generated candidate Properties (issue 27) are out
of scope.

## Format

Cases live in `.sqltest` files. A file is valid SQL: test metadata is SQL
comments, so editors highlight it and it can be pasted into DuckDB. The format
follows inline-expectation tests (rustc UI tests' `//~`, mypy's `# E:`): each
expectation sits next to the line it checks, so no line numbers are written.

```sql
-- include: paulley.sql
CREATE TABLE new_table (id INTEGER PRIMARY KEY, x INTEGER);

-- case: left_join_pads_with_null
SELECT p.id, n.x
FROM part p LEFT JOIN new_table n ON p.id = n.id
/**ASSERT NOT NULL(n.x)**/;  --~ UNKNOWN

-- case: unresolved_column_is_diagnosed
SELECT * FROM part p
/**ASSERT NOT MANY TO MANY**/ JOIN new_table n ON p.id = n.id  --~ PROVED
/**ASSERT UNIQUE(name)**/;
--~ UNRESOLVED_ASSERTED_COLUMN "name"

-- case: postgres_specific_behavior
-- dialect: postgres
SELECT ...;
```

### Grammar

- **Setup.** Everything before the first `-- case:` is setup. Each case's
  Program is the setup followed by the case's SQL. Setup is not special: its
  lines may carry annotations like any other line, and those annotations apply
  to every case in the file, because every case's Program contains them.
- **Include.** `-- include: path.sql` inserts that file's text. The path is
  relative to `tests/` and must name a `.sql` file. Includes may nest (with
  cycle detection) and may carry annotations, so a shared schema with a known
  warning annotates it once. Python tests that need the same schema read the
  file.
- **Case.** `-- case: name` starts a case; it runs to the next `-- case:` or
  the end of the file. Names are unique within a file. The pytest ID is
  `<file>.sqltest::<name>`.
- **Dialect.** `-- dialect: name` in setup sets the file's dialect; inside a
  case it overrides it for that case. The default is `duckdb`. A file keeps its
  subject; one case needing another dialect does not need its own file.
- **Inline annotation.** `SQL  --~ X` checks the markers on that line and the
  Diagnostics whose origin is that line.
- **Standalone annotation.** `--~ X` alone on a line checks the nearest line
  above that is not blank, a comment, or an annotation. Standalone annotations
  stack; all annotations for one line add up to that line's expectation.
- **Program-level annotation.** `--~! CODE "substring"` checks a Diagnostic
  with no origin. It goes at the end of the case, after the final `;`.
- **Outcomes.** `PROVED` or `UNKNOWN`, comma-separated in written order when a
  line holds several markers.
- **Diagnostics.** `CODE "a" "b"`: a Diagnostic with that code (the code's
  string value, not the Python constant name) whose message contains every
  quoted substring. Line numbers do not belong in substrings; the annotation's
  position checks the origin line.
- **Exhaustive.** Every marker needs an outcome and every Diagnostic needs an
  annotation. A missing, unexpected, or mismatched expectation fails the case,
  naming the line. An unannotated Diagnostic in setup therefore fails every
  case in the file; fix the SQL or annotate it there.
- **Trailing markers** come before the statement's `;`, as in the README.
  Anything after the `;` is outside the statement.

### Style

- One marker per line. Split the SQL rather than writing comma lists of
  outcomes.
- Outcomes inline, two spaces before `--~`, no column alignment (alignment
  churns diffs).
- Diagnostics standalone, under the line they check: they carry message
  substrings and are long.
- Standalone whenever an inline annotation would push the line past 88
  columns.
- Annotate a shared warning once, where it originates (setup or include), not
  in each case.

### Layout

`.sqltest` files and included `.sql` schemas live in `tests/` alongside the
Python tests. Move shared schemas such as Paulley's there as `.sql` files.

### Runner

A `conftest.py` collector only parses `.sqltest` files: it reads includes,
splits cases, and attaches annotations to lines. It never calls `analyze()`; a
malformed file or missing include fails collection for that file, naming the
line. Each case is one pytest item that calls `analyze()` on its Program, maps
assertions and Diagnostic origins back to file lines (through includes and the
setup prefix), and compares them with the annotations. The existing per-test
oracle integration observes that call unchanged. Parallelism stays per case
under xdist; setup is text, so there is no file-level state. No
expectation-rewriting mode.

## What migrates

Every test that only calls `analyze()` and checks outcomes, `.proved`,
Diagnostic codes, or message substrings moves to `.sqltest`. Parametrized
tests over hand-written value lists are written out as one case per value.

Stays in Python: tests that need Evidence structure, origins beyond the line,
mocking, fixtures, suggestions, `analyze_database`, parsing or IR internals,
and large generated matrices such as marker accounting's 201 combinations.
Avoid snapshotting whole Reports, proof text, or generated identifiers.

An AST classification on 2026-11-08 (1,465 pytest items, 537 test functions)
found 213 functions / 742 items (51%) that qualify, about 56% with message
substrings. Largest groups: `not_null`, `enumerated_domain`,
`functional_dependencies`, `foreign_keys`, `paulley_audit`, `exactly_one_join`.

Migration order is free. Delete each migrated Python test, and helpers left
unused, in the same change.

## Coverage must not change

- Every migrated Python item maps to `.sqltest` cases that analyze the same
  Program and dialect and check the same outcomes, codes, and substrings. A
  Python test that checked less (only `.proved`, only one Diagnostic) gains
  exhaustive expectations; it never loses one.
- `pytest --cov=sqlassert` line and branch coverage is equal before and after.
- `--soundness-oracle` judged-program, checked, and skipped counts are equal
  before and after (or higher, never lower).
- The suite passes with `-n 0` and `-n auto`.

## Acceptance criteria

- [x] `.sqltest` collector implements the grammar above, with tests for its
      parse errors and expectation mismatches.
- [x] Every qualifying test is migrated; superseded Python tests and helpers
      are deleted.
- [x] Cases have stable `<file>.sqltest::<name>` IDs, select with `-k`, and
      report failures at the file line.
- [x] Line/branch coverage and oracle counts are recorded before and after
      and do not drop.
- [x] Serial and parallel runs pass.

## Result (2026-11-08)

- 832 pytest items from 263 Python test functions became 839 cases in 43
  `.sqltest` files; eleven Python test modules emptied and were deleted. The 7
  extra cases are tests that analyzed two Programs; each is now a case. Items
  went from 1,540 to 1,579: 708 Python items, 845 cases (the 839 plus 6 new
  marker cases below), and the collector's 26 tests.
- Outcomes take explanation substrings (`--~ UNKNOWN "may drop rows"`), so the
  49 tests that checked `evidence.explain()` text moved too. Tests that build
  Knowledge or inspect Evidence structure stay in Python.
- Every one of the 1,028 distinct Programs the suite analyzed before is still
  analyzed (comments and whitespace aside), with the same outcomes in order and
  the same Diagnostic codes.
- `--cov=sqlassert --cov-branch`: every line and branch covered before is
  covered after (4,247/4,435 lines and 1,633/1,774 branches before; the new
  marker scanner adds lines, all covered).
- `--soundness-oracle`: 1,057 programs, 687 PROVED judged, 12 skipped, 0
  counterexamples before; nothing lower after.
- `-n 0` and `-n auto` both pass.
- File names: the largest group of a subject takes its name; a file whose cases
  carry their own schema ends in `_standalone`; others say what they hold
  (`not_null_rejection`, `not_null_values`, `scalar_aggregates`, ...). A
  file's setup is a schema shared by its cases.
- `paulley_audit.sqltest` keeps the audit's rule IDs (`A11-` is `A11_minus`).

Done alongside:

- A marker shape inside a `--` comment or a quoted string is no longer a
  marker (`sql_parse._marker_shapes`): commenting a marker out switches it
  off, and marker accounting no longer reports it. Six cases in
  `marker_accounting.sqltest` cover comments, strings, escaped quotes and
  multi-line strings.
- `test_a_false_cardinality_claim_is_refuted` missed its counterexample in 3
  of 100 runs at 50 examples (none at 200 or 500). It is marked
  `@pytest.mark.slow`, which gives a test 500 examples through the `examples`
  fixture in `test_soundness_oracle.py`; 30 serial runs of that file passed.
