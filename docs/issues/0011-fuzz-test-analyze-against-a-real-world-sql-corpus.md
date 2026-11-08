# 11. Fuzz-test analyze against a real-world SQL corpus

- **State:** open
- **Labels:** ready-for-agent
- **Created:** 2026-08-25
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/11

## What to build

Build a corpus-driven regression harness that stress-tests `analyze()`'s "trustworthy result or explicit diagnostic, never a crash" boundary against real-world SQL, rather than only hand-authored examples.

Curate a fixture corpus of realistic SELECT statements with joins (SQL text only, no expected results — e.g. a hand-picked subset drawn from an existing public SQL test suite such as DuckDB's `test/sql` logic tests, favoring join diversity: RIGHT/FULL/SEMI/ANTI/CROSS, correlated subqueries, set operations), checked in under `tests/corpus/*.sql`. For each corpus statement:

1. Run `SqlParser.parse` and `IrParser.parse` and confirm no exception escapes (as opposed to being turned into a `Diagnostic`, which `SqlParser` already does for known-bad input).
2. For each JOIN in the statement, produce one mutated variant with a single `/**ASSERT UNIQUE**/` marker inserted immediately before that JOIN's keyword (one marker per variant, isolating which join is under test), and run the mutated SQL through `analyze()` end-to-end, confirming no exception escapes.
3. For unsupported join kinds (RIGHT/FULL/SEMI/ANTI/CROSS), assert the resulting `AssertionReport.outcome` is not `PROVED`. Unrecognized conditions must not supply proof facts, but their presence does not forbid a proof from supported conjuncts: `lhs.id = rhs.id AND rhs.amount > 0` remains unique when `rhs.id` is unique, because the range only removes matches. The same applies to an additional OR or null-safe condition. Use the oracle to judge any `PROVED` result, rather than rejecting it solely because such a condition appears.
4. When a run surfaces an interesting failure or an unexpected `PROVED`, promote the reduced SQL into a named test in the relevant `tests/test_analyze_*.py` file with a comment noting what it exercises and where it came from, rather than keeping the whole corpus as the permanent regression surface.

Keep this suite opt-in / separate from the default fast run (a dedicated marker or a file excluded from the default `pytest` invocation), since running `analyze()` over a whole corpus is not free.

## Acceptance criteria

- [ ] A curated SQL corpus of realistic multi-join SELECT statements is checked into the repo as static fixtures, with its provenance documented (source, license, why each file was included).
- [ ] Every corpus statement parses through `SqlParser` and `IrParser` without an unhandled exception; any statement that cannot be handled produces a `Diagnostic` instead.
- [ ] Every corpus statement, mutated with one `/**ASSERT UNIQUE**/` marker per JOIN in turn, runs through `analyze()` without an unhandled exception.
- [ ] Unsupported join kinds never report `PROVED`. Unsupported predicates supply no proof facts; additional filters do not invalidate uniqueness established by supported conjuncts.
- [ ] The corpus-fuzz suite is excluded from the default fast test run and documented as an opt-in suite with instructions to run it.
- [ ] At least one real finding from running the harness (a crash or an incorrect `PROVED`) is fixed or promoted into a first-class named test in `tests/test_analyze_*.py`, demonstrating the harness catches something the hand-written suite would miss.

## Related

- [0040](0040-audit-inference-against-calcite.md) — a targeted Calcite audit
  and pilot adaptations. [Calcite source notes](../calcite-notes.md) identify
  metadata/expression tests, `.iq` SQL, catalog fixtures and reuse requirements
  for an additional corpus source. Preserve fixture assumptions when adapting
  Java tests; `.iq` files include script directives as well as SQL.
- [0027](0027-soundness-oracle-against-duckdb.md) — the soundness oracle, split
  out of this issue. It judges `PROVED` outcomes on real data; this issue only
  checks that `analyze()` never crashes. The corpus here can feed the oracle
  later.
