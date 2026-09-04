# Property Engine Implementation Conventions

This document records the implementation-level decisions that complement the
domain language in `CONTEXT.md` and the semantic MVP boundary in
`docs/mvp-scope.md`.

## Package Shape

- `main.py` is the small composition root and owns the public `analyze`
  operation. It composes parsing, conversion, fact generation, solving, and
  reporting; it is not a general orchestration module.
- `sql_parse.py` parses one SQL Program with SQLGlot and resolves assertion
  markers onto the `Join` nodes they mark. A marker is real syntax: a custom
  dialect tokenizes `/**UNIQUE**/` as a token of its own and the join rule
  consumes it, so the grammar is what requires a marker to sit immediately
  before its join. The line is recorded in the node's `meta`, and nothing
  downstream re-reads SQL text.

  `MARKERS` in that module is the single source of truth for what a marker is:
  the dialect tokenizes exactly those literals, and any other `/**...*/` comment
  is reported as an unrecognized marker rather than ignored. `/**` is assertion
  syntax, so a near miss is likelier a typo than a note, and a typo that was
  silently dropped would read to its author as a proof.

  A Unique Set Assertion marker (`/**UNIQUE(col, ...)**/`,
  `/**PRIMARY KEY(col, ...)**/`) carries a variable column list, so it cannot
  be one fixed `KEYWORDS` literal the way the bare join marker is. `MARKERS`
  instead holds that marker's fixed open literal (`"/**UNIQUE("`,
  `"/**PRIMARY KEY("`) and one shared close literal (`"**/"`); the column list
  between them is ordinary SQL, parsed with the parser's own identifier-list
  machinery once the open token is matched. `_is_recognized_marker` is what
  reconciles a whole `/**...*/`-shaped span of source text against this split
  representation -- a marker is recognized when it is either an exact bare
  literal or starts with one of the parameterized opens and ends with the
  close -- and `_unrecognized_markers`'s user-facing list of recognized
  markers is built from whole shapes, not `MARKERS`'s own fragment keys.
  Consumed at the one grammar production common to a Root Select, a CTE
  definition, a view body, and a subquery (`_parse_select_query`), so a
  marker means the same thing regardless of which of these it sits on.

  The added token is a member of sqlassert's own plain `Enum`, never of
  SQLGlot's `TokenType`. `TokenType` is an `IntEnum`, so a member sharing an
  integer value would compare equal to a real token type and be swallowed by
  SQLGlot's keyword sets; a plain member equals nothing but itself, which is
  also why no rule but ours can consume it. Tests assert that ordinary SQL
  parses identically with and without the customisation.
- `ir/model.py` contains the immutable semantic model.
- `ir/convert.py` is the only boundary that reads SQLGlot ASTs and lowers them
  into that model.
- `facts.py` assigns deterministic encoding symbols at the Clingo boundary and
  states the IR, column sets of interest, and accepted Properties as ground ASP facts. It states facts only; every
  inference lives in `rules/`.
- `engine.py` adds encoded facts and rules to Clingo, grounds them, and solves,
  handing each model to a callback.
- `reporting.py` consumes the live model, enforces the one-model policy, and
  returns durable reporting values.
- `rules/` contains the Clingo logic-program resources.
- `discovery/duckdb` is a future adapter that binds database metadata to IR
  nodes and produces Properties accepted as knowledge; it is not part of the MVP engine. See
  [#14](https://github.com/polvoazul/sqlassert/issues/14).

## Stage Objects

Each pipeline stage is an object holding only the services it needs:
`SqlParser(dialect)`, `IrParser(dialect)`, `Engine()`, and
`Reporter(encoding)`. The program being analysed travels through the call
instead — `parse(sql)`, `parse(ast)`, `encode(program, knowledge)`,
and `run(encoding, on_solution_callback)`. `main.py` constructs them per call.

`IrParser` links syntax into direct semantic references and stores Origins on
the nodes themselves. `facts.encode` performs one deterministic identity walk,
assigns every reachable node a Clingo symbol, and returns both directions of
the mapping. No generated identifier exists in the IR.

`IrParser` accumulates the named relations and assertions of one program, and a
`Reporter` accumulates the results of one solve, so neither survives a second
analysis. The `Reporter` needs nothing about the assertions while solving:
`on_model` harvests evidence out of the live model as plain values, and
`report(assertions, diagnostics)` assembles the Report once solving is done.

The one-model policy lives with the `Reporter` rather than the `Engine`, since
the consumer is what can count the models it was handed. `on_model` raises on a
second call, and `report` raises if it was never called at all.

## Public Analysis Operation

The public shape is:

`analyze(sql, *, dialect="duckdb") -> Report`

`sql` is the complete SQL Program. Analysis returns data and never prints.

## Clingo Identifiers and Fact Transport

Every generated ASP identifier has a short kind prefix, a sanitized readable
hint when one exists, and one deterministic incrementing suffix shared by the
encoding walk. The suffix establishes identity; the hint is for readability only.

- Relation Expressions use `rel`, for example `rel_users_1` and
  `rel_customer_2`.
- Output Columns use `col`, for example `col_id_3`.
- Accepted Properties use `property`, for example `property_candidate_key_4`.
- Joins use `join`; an identifier with no natural hint omits the hint segment,
  for example `join_5`.
- Expressions and assertions use `expr` and `assert`, following the same rule.

Hints are lower-cased and sanitized. Different source names may sanitize to the
same hint; the shared suffix still makes their identities distinct. Raw quoted
SQL names used as data values in facts are not generated identifiers.

Facts are emitted as readable, ground ASP source and added through
`clingo.Control.add()`, then grounded together with the rule resources. The
MVP does not use Clingo's low-level backend or AST-builder APIs.
