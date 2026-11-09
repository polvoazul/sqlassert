# Property Engine Implementation Conventions

This document records the implementation-level decisions that complement the
domain language in `CONTEXT.md` and the semantic MVP boundary in
`docs/mvp-scope.md`.

## Package Shape

- `main.py` is the small composition root and owns the public `analyze`
  operation. It composes parsing, conversion, the Python engine, and
  reporting; it is not a general orchestration module.
- `sql_parse.py` parses one SQL Program with SQLGlot and resolves assertion
  markers onto the `Join` nodes they mark. A marker is real syntax: a custom
  dialect tokenizes each join marker (`/**ASSERT UNIQUE**/`,
  `/**ASSERT EXACTLY ONE**/`; `JOIN_MARKER_KINDS`) as a token of its own and the
  join rule consumes it, so the grammar is what requires a marker to sit
  immediately before its join. The marker's `Span` (`provenance.py`: first
  and last line and column, from the tokens' source offsets) and kind are
  recorded in the node's `meta`, and nothing downstream re-reads SQL text.
  Every Origin of an Assertion, and of a Diagnostic about a marker, carries
  that span, so `check` annotates the marker itself.

  `MARKERS` in that module is the single source of truth for what a marker is:
  the dialect tokenizes exactly those literals, and any other `/**...*/` comment
  is reported as an unrecognized marker rather than ignored. `/**` is assertion
  syntax, so a near miss is likelier a typo than a note, and a typo that was
  silently dropped would read to its author as a proof.
  Only marker shapes written as SQL count: one inside a `--` comment or a
  quoted string is not a marker (`_marker_shapes`). A `--` comment is how an
  author switches a marker off, and quoted text is data.

  The case marker (`/**ASSERT EXHAUSTIVE**/`) is another fixed literal, written
  inside an expression: the dialect adds its token to `PRIMARY_PARSERS`, so it
  parses as a primary expression whose rule requires `CASE` next and records
  the marker's span on the `exp.Case` (`exhaustive_span`). The same rule
  rejects a searched CASE and any `WHEN` value that is not a distinct
  non-null literal (`literal_value`), and the join and Select rules reject the
  token where they meet it, so a misplaced marker is an unattached marker
  rather than a bare parse failure. `ir/convert.py` lowers every simple CASE
  to `ir.SimpleCase` and states `ExhaustiveCase` once per marked CASE it
  lowers; one inside an aggregate, window or subquery (an `ir.OpaqueExpression`) is reported unanalyzed.
  `DECLARE EXHAUSTIVE` is not a marker: exhaustiveness is only ever proved.

  A column marker (`/**ASSERT UNIQUE(col, ...)**/`, `/**ASSERT NOT NULL(...)**/`,
  `/**DECLARE NOT NULL UNIQUE(...)**/`, and the rest of `_COLUMN_MARKER_OPENS`) carries a
  variable column list, so it cannot be one fixed `KEYWORDS` literal the way the
  join marker is. `MARKERS` instead holds each marker's fixed open literal
  (`"/**ASSERT UNIQUE("`, `"/**DECLARE NOT NULL("`, ...) and one
  shared close literal (`"**/"`); the column list
  between them is ordinary SQL, parsed with the parser's own identifier-list
  machinery once the open token is matched. `_is_recognized_marker` is what
  reconciles a whole `/**...*/`-shaped span of source text against this split
  representation -- a marker is recognized when it is either an exact
  literal or starts with one of the parameterized opens and ends with the
  close -- and `_unrecognized_markers`'s user-facing list of recognized
  markers is built from whole shapes, not `MARKERS`'s own fragment keys.
  A Foreign Key marker (`/**ASSERT FOREIGN KEY(a, b) REFERENCES t(x, y)**/`)
  is a column marker whose column list continues with `REFERENCES` and a
  referenced relation's column list, paired by position; the parser records
  both on its `ColumnMarker`. The IR parser links the referenced relation only
  after lowering everything else, because a view's marker may name the view
  itself. `DECLARE FOREIGN KEY` and `REFERENCES` constraints become
  `ReferentialCoverage` Knowledge, never `ForeignKey`: an accepted Property
  skips its evaluator, and the referenced Unique Set must still be proved.
  A Functional Dependency marker (`/**ASSERT DETERMINES(a, b -> c)**/`) splits
  its column list at `->`; the parser records the determinant as the
  `ColumnMarker`'s `columns` and the rest as its `dependent`. An Enumerated
  Domain marker (`/**ASSERT ENUM(status IN ('new', 'paid'))**/`) names one
  column, recorded as `columns`, and its literals as `values`: each an
  `ir.LiteralValue` (a type -- string, numeric, or boolean -- plus canonical
  text) built by `sql_parse.literal_value`, so a number never collides with a
  string spelled alike. The parser rejects any other expression, NULL, and a
  repeated typed value. An `ir.Constant` carries the same `LiteralValue` when
  its type is modelled. A number's text is its spelling, normalized only
  where the SQL type is unchanged (an integer's sign and leading zeros); see
  `LiteralValue`. `rules/domain.py` covers by identity but meets domains by
  `_may_coincide`, since implicit casts can make one value equal two distinct
  literals.
  Consumed at the one grammar production common to a Root Select, a CTE
  definition, a view body, and a subquery (`_parse_select_query`), so a
  marker means the same thing regardless of which of these it sits on.
  Whitespace runs inside a marker collapse to one space, matching how the
  tokenizer matches multi-word literals. A `DECLARE` marker becomes a
  `Program.declarations` entry rather than an `Assertion` (`DECLARE NOT NULL UNIQUE`
  becomes a Non-Null Unique Set plus one `NonNullColumn` per column); the engine accepts it
  as trusted evidence labelled `Declared`, while schema Knowledge is labelled
  `Accepted`.

  The added token is a member of sqlassert's own plain `Enum`, never of
  SQLGlot's `TokenType`. `TokenType` is an `IntEnum`, so a member sharing an
  integer value would compare equal to a real token type and be swallowed by
  SQLGlot's keyword sets; a plain member equals nothing but itself, which is
  also why no rule but ours can consume it. Tests assert that ordinary SQL
  parses identically with and without the customisation.
- `ir/model.py` contains the immutable semantic model.
- `ir.Limit` retains a subset of its input rows, after the Select's other
  operations or a set operation. Its optional `count` records a known hard
  bound; percentages, ties, and unknown bounds supply none. OFFSET needs no
  separate node: it is the same subset boundary. Input facts follow retained
  rows; incoming Referential Coverage stops at this boundary.
- `ir/convert.py` is the only boundary that reads SQLGlot ASTs and lowers them
  into that model. Schema statements are read in program order:
  `ALTER TABLE` and `CREATE UNIQUE INDEX` append table-level constraints, shaped
  like `CREATE TABLE`'s own, to the table an earlier `CREATE TABLE` declared,
  so one `_table_constraints` view feeds Unique Sets, primary keys, and
  `REFERENCES` alike.
- Table columns carry a plain `comparison_type` when their declared type has
  value equality without collation. Equality Groups merge only known matching
  types, followed through pass-through columns; unknown and mixed types do not
  rely on SQL's implicit casts. Column-to-column domain narrowing uses the same
  check. Set operations do not supply comparison types from their left arm.
- `python_engine.py` evaluates Python analysis functions with an analysis-scoped
  arena, caching, and trusted declarations. An analysis function returns
  `Result[Value]`: `.value` is its computed Value and its direct fields carry
  the evidence. `@analysis` provides declaration lookup and analysis-scoped caching
  for public and underscore-prefixed internal analysis functions. A public
  function evaluates a `Property`: its snake-case
  name and parameters match the Property class and fields. `NodeMeta` collects
  concrete descendants in `_subclasses`; one contract test checks every public
  evaluator. Declarations supply accepted results without executing the public
  evaluator body. The arena rejects nested analysis sessions.
- `reporting.py` defines the durable Report values.
- `rules/` contains the analysis functions. `equality.py` computes each
  relation's Equality Groups; `nullability.py` computes each column's Null
  Sources; `uniqueness.py` holds functional dependencies, their
  closure, and the Unique Set and Functional Dependency evaluators built on it;
  `join_cardinality.py`, `foreign_key.py`, and `exactly_one.py` build on those.
  `domain.py` computes Enumerated Domains, intersected across Equality Groups,
  and proves exhaustive simple CASEs from their operand's domain. The
  design and its soundness arguments are in `docs/functional-dependencies.md`.
- An analysis that must answer for every node matches over `ir.AnyScalar` or
  `ir.AnyRelation` (rebind `expression = expression.closed`, then match it),
  lists each node with an explicit answer, conservative ones included, and
  ends in `assert_never`; so do matches over `Join.kind` and `SetOperation.operator`.
  A new node then fails `ty` wherever it is not handled, and
  `tests/test_ir_unions.py` fails until the unions name it. A wildcard is for
  shape-pickers only, where it means "not this shape".
- `ir/convert.py` lowers `WHERE` and `ON` into `Filter.conjuncts` and
  `Join.conjuncts`: `=`, `IN (...)`, `IS NULL`, and `IS NOT NULL` are recognized nodes, and
  any other conjunct is kept as an ordinary (usually opaque) scalar expression,
  so the tuple is always the whole condition.
- `discovery/` hydrates a SQL Program from a database catalog before the
  engine runs; the engine never sees it. `DatabaseKnowledgeFetcher` links the
  Program once without a connection to find the external Named Relations it
  reaches, then relinks it with each fetched table declared as a column-only
  `CREATE TABLE` and each fetched view as its stored `CREATE VIEW`, until no
  new Relation is reached. Names inside a fetched view are rewritten to
  canonical identities first, because they resolve in the view's own schema.
  Catalog constraints never enter those declarations: `bind` turns them into
  Properties over the final IR: `Accepted` Knowledge where the dialect
  enforces them, Property Declarations for unenforced keys under
  `trust_unenforced_keys` (on by default), otherwise an `unenforced-constraint` warning. SQL declarations win over catalog objects of the same name and
  are never fetched. A missing object, an unreadable catalog, or incomplete
  metadata is a Diagnostic, and the Relation stays opaque. Subclasses own one
  database's catalog boundary: name resolution and metadata queries.
  `discovery/information_schema.py` reads the standard
  `INFORMATION_SCHEMA` views through any DB-API connection, importing no
  driver; one subclass per dialect (`duckdb.py`, `postgres.py`, `mysql.py`,
  `redshift.py`, `snowflake.py`, `bigquery.py`, `databricks.py`, `trino.py`)
  overrides only the hooks whose standard view is missing or incomplete there
  (DuckDB and PostgreSQL override all of them, with their own catalogs). `analyze_database(sql, fetcher,
  *, dialect=...)` in `main.py` composes it; `analyze` stays pure.
- `dialects.py` is what each SQL dialect means by the constructs the rules
  depend on (`DialectSemantics`): which Builtins raise rather than return
  NULL, which text compares by identity, `||` and `concat()`, and which keys
  are enforced. Only `ir/convert.py` reads it, and `discovery/` for catalog
  keys; the rules see its effect in the IR. A flag claims only what holds under every setting of the dialect, and a
  dialect not listed gets cautious defaults. An unenforced key is never
  Knowledge: `RELY` (where the dialect has it) or `trust_unenforced_keys`
  (on by default) makes it a Property Declaration, otherwise an `unenforced-constraint`
  warning reports it. The argument for each entry is in `docs/dialects.md`.
- `suggest.py` composes the same stages as `main.py` to list provable
  Properties as markers. `sql_parse.py` records on every Join, Select, and
  simple CASE the source position where its marker would be written;
  `IrParser` turns each into a plain-valued `AttachmentSite` (`provenance.py`)
  paired with the IR Join, Select relation, or `SimpleCase` in
  `Program.attachment_sites`. `suggest` proves candidates at those sites
  through `Arena.prove` only. The one internal it reads is a column's
  Enumerated Domain (`rules.domain._domain`), to know which values an ENUM
  candidate lists before proving it. `__main__.py` is the CLI over it.
- `check.py` is `sqlassert check`, for CI: it runs `analyze` on each file,
  turns every `UNKNOWN` assertion and Diagnostic into a Finding (a Connection
  Warning into a warning-level one), and renders Findings as text, JSON,
  SARIF 2.1.0, or GitHub workflow annotations. Its exit codes, `Finding`,
  `[tool.sqlassert]` loading and formatters are shared by every command that
  checks SQL; `__main__.py` only parses arguments. A path that matches no file
  is a usage error, since a check of nothing would pass.
- `improvements.py` holds `suggest`'s High level: result-preserving rewrites
  (Improvements). None of their facts is a Property, so it reads rule
  internals in the same arena -- null sources and null-rejected conjuncts
  for an outer join that cannot pad, the strict (`definite = ∅`) closure for
  `DISTINCT` and grouping columns -- besides `Arena.prove` for Unique and
  Exactly One Joins. Whether a join's columns are used, and whether a CTE is
  referenced, is read from the SQLGlot AST, matched to each IR site by its
  marker position: the IR keeps neither ORDER BY nor the columns inside an
  aggregate, window or subquery. Padding is only followed through the joins and WHERE of
  its own Select Expression, where nothing looks at a row as a whole.
  Grouping replacements use the selected expression already identified by the
  IR output column, so GROUP BY ordinals never become aggregate constants.
  NATURAL joins are diagnosed syntactically, including unused bodies, and
  their Select Expressions remain opaque. `suggest` emits no suggestions for
  a program containing one.
- The dbt plugin is not in this package: `integrations/dbt/` is its own
  package, `sqlassert-dbt` (import `sqlassert_dbt`, command `sqlassert-dbt`),
  which depends on `sqlassert`; `sqlassert` never imports it or dbt
  (`tests/test_module_boundaries.py`). It checks a dbt project from its
  `manifest.json` without importing dbt, and uses sqlassert internals freely
  (alpha), so a change to them runs its tests too. `expectations.py` reads each test and contract constraint as the Property
  that proves it (`asserted`) and the one it guarantees once it passed
  (`declared`, None when it is no fact). `program.py` builds one Program per
  model and branch: the model's compiled SQL as a FROM subquery of a Root
  Select starting on line 1 (so its lines are the Program's), its tests as
  markers on that Root Select, and every relation it reads as a `CREATE VIEW`
  of its compiled SQL (Assertion Markers blanked) or a table. Declarations are
  bound as Properties onto the linked IR, as `discovery/` binds catalog facts,
  so a table with unknown columns can carry them too. `jinja.py` renders the
  `ref`/`source`/`this`/`config`/`is_incremental()` subset; a rendering is
  trusted only once it reproduces dbt's compiled code, and then maps every
  compiled character to its line in the model file.
## Stage Objects

Each pipeline stage is an object holding only the services it needs:
`SqlParser(dialect)`, `IrParser(dialect)`, and `Engine()`. The program being
analysed travels through the call instead — `parse(sql)`, `parse(ast)`, and
`run(program, knowledge, diagnostics)`. `main.py` constructs them per call.

`IrParser` links syntax into direct semantic references and stores Origins on
the nodes themselves. No generated identifier exists in the IR.

`IrParser` accumulates the named relations and assertions of one program, so it
does not survive a second analysis. `Engine.run` opens one analysis arena, so
cached results never leak between analyses.

## Public Analysis Operation

The public shape is:

`analyze(sql, *, dialect="duckdb") -> Report`

`sql` is the complete SQL Program. Analysis returns data and never prints.

`analyze_database(sql, fetcher, *, dialect="duckdb") -> Report` is the opt-in
catalog-backed variant: the same Program and Report, with external Relations
hydrated by a `DatabaseKnowledgeFetcher`.
Assertion evidence supports `.explain()`.

`suggest(sql, *, dialect="duckdb", max_size=3) -> SuggestionReport` lists the
Properties `analyze` would prove if their markers were pasted in. Like
`analyze`, it returns data and never prints.

## Tests

A test that only analyzes SQL and checks outcomes and Diagnostics is a case in
a `tests/*.sqltest` file: the SQL with `--~` annotations next to the lines they
check. `tests/sqltest.py` defines the format (setup, `-- include:`,
`-- dialect:`, `-- case:`, inline, standalone and `--~!` annotations) and the
pytest collector. Matching is exhaustive, so a case states every outcome and
every Diagnostic, warnings included. Quoted substrings after an outcome must
appear in that assertion's `evidence.explain()`; after a code, in the
Diagnostic's message. One marker per line; a bare outcome inline, an outcome
with substrings and every Diagnostic on its own line under the line it checks;
an annotation goes on its own line when inline it would pass 88 columns.

Tests stay in Python when they need Evidence structure, origins beyond the
line, mocking, fixtures, `analyze_database`, suggestions, parsing or IR
internals, or a large generated matrix (marker accounting).

The dbt plugin's tests live in `integrations/dbt/tests` and need dbt
(`pip install -e . -e "integrations/dbt[test]"`); run them from that folder
(`cd integrations/dbt && python -m pytest`, `ty check`). The root `pytest`
and `ty check sqlassert tests` never see them. Every plugin test reads a
manifest written by a real `dbt compile` of its `tests/dbt_project` (dbt-duckdb,
in memory: no warehouse), compiled once per session (`conftest.py`); a test
that needs another manifest edits a copy of that one, or recompiles an
edited copy of the project (`dbt_runs.py`). There is no hand-written
manifest. CI (`.github/workflows/ci.yml`) runs the package without dbt and
the plugin with it as separate jobs.
