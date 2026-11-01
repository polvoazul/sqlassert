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
  dialect tokenizes `/**ASSERT UNIQUE**/` as a token of its own and the join rule
  consumes it, so the grammar is what requires a marker to sit immediately
  before its join. The line is recorded in the node's `meta`, and nothing
  downstream re-reads SQL text.

  `MARKERS` in that module is the single source of truth for what a marker is:
  the dialect tokenizes exactly those literals, and any other `/**...*/` comment
  is reported as an unrecognized marker rather than ignored. `/**` is assertion
  syntax, so a near miss is likelier a typo than a note, and a typo that was
  silently dropped would read to its author as a proof.

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
- `ir/convert.py` is the only boundary that reads SQLGlot ASTs and lowers them
  into that model.
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
- `rules/` contains the analysis functions.
- `discovery/` hydrates a SQL Program from a database catalog before the
  engine runs; the engine never sees it. `DatabaseKnowledgeFetcher` links the
  Program once without a connection to find the external Named Relations it
  reaches, then relinks it with each fetched table declared as a column-only
  `CREATE TABLE` and each fetched view as its stored `CREATE VIEW`, until no
  new Relation is reached. Names inside a fetched view are rewritten to
  canonical identities first, because they resolve in the view's own schema.
  Catalog constraints never enter those declarations: `bind` turns them into
  Properties over the final IR, passed to `Engine.run` as `Accepted`
  Knowledge. SQL declarations win over catalog objects of the same name and
  are never fetched. A missing object, an unreadable catalog, or incomplete
  metadata is a Diagnostic, and the Relation stays opaque. Subclasses own one
  database's catalog boundary: name resolution and metadata queries.
  `discovery/duckdb.py` is the DuckDB adapter, behind the optional `duckdb`
  extra. `analyze_database(sql, fetcher, *, dialect=...)` in `main.py`
  composes it; `analyze` stays pure.

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
