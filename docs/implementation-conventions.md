# Property Engine Implementation Conventions

This document records the implementation-level decisions that complement the
domain language in `CONTEXT.md` and the semantic MVP boundary in
`docs/mvp-scope.md`.

## Package Shape

- `main.py` is the small composition root and owns the public `analyze`
  operation. It composes parsing, conversion, fact generation, solving, and
  reporting; it is not a general orchestration module.
- `sql_parse.py` parses one SQL Program with SQLGlot and preserves assertion
  markers.
- `ir/model.py` contains the immutable semantic model.
- `ir/convert.py` is the only boundary that reads SQLGlot ASTs and lowers them
  into that model.
- `facts.py` states the IR and Knowledge as ground ASP facts. It states facts
  only; every inference lives in `rules/`.
- `engine.py` drives fact generation, adds facts and rules to Clingo, grounds
  them, solves, and enforces the one-model policy.
- `reporting.py` consumes the live model and returns durable reporting values.
- `rules/` contains the Clingo logic-program resources.
- `discovery/duckdb` is a future adapter that produces `Knowledge`; it is not
  part of the MVP engine.

## Stage Objects

Each pipeline stage is an object, and each one analyses exactly one program:
`SqlParser(dialect)`, `IrParser(dialect)`, and `Engine(reporter, names)`.
`main.py` constructs them per call. `IrParser` accumulates the declarations and
assertions of one program, and `Engine`'s reporter accumulates the results of
one solve, so reusing an instance across analyses would mix their state.

`IrParser` creates the analysis-wide `NameGiver` and `OriginRegistry`, and the
stages downstream take them from it: `Engine` names Unique Sets with
`ir_parser.names`, and `Reporter` resolves origins with `ir_parser.origins`.
Constant identity comes from the NameGiver's single counter, not from the kind
prefix, so a stage that started its own could hand out a name already taken.

## Public Analysis Operation

The public shape is:

`analyze(sql, *, knowledge=None, dialect="duckdb") -> Report`

`sql` is the complete SQL Program. Omitting `knowledge` supplies empty
Knowledge. Analysis returns data and never prints.

## Clingo Identifiers and Fact Transport

Every generated ASP identifier has a short kind prefix, a sanitized readable
hint when one exists, and one deterministic incrementing suffix shared by the
entire analysis. The suffix establishes identity; the hint is for readability only.

- Relation Definitions and Relation Instances use `rel`, for example
  `rel_users_1` and `rel_customer_2`.
- Column references use `col`, for example `col_id_3`.
- Unique Sets use `key`, for example `key_users_pk_4`.
- Joins use `join`; an identifier with no natural hint omits the hint segment,
  for example `join_5`.
- Expressions, assertions, and internal plan identities use `expr`, `assert`,
  and `plan` respectively, following the same rule.

Hints are lower-cased and sanitized. Different source names may sanitize to the
same hint; the shared suffix still makes their identities distinct. Raw quoted
SQL names used as data values in facts are not generated identifiers.

Facts are emitted as readable, ground ASP source and added through
`clingo.Control.add()`, then grounded together with the rule resources. The
MVP does not use Clingo's low-level backend or AST-builder APIs.
