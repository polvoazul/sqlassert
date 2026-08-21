# Clingo Rule Policy

The primary property engine must produce one deterministic stable model for a given analysis input. It uses Clingo as a stratified rule engine, not as a search or optimization engine.

Rules may use:

- facts and normal derivation rules;
- recursion that does not pass through negation;
- stratified default negation;
- deterministic aggregates when they are needed.

Rules must not use:

- choice rules;
- disjunctive rule heads;
- weak constraints, `#minimize`, or `#maximize`;
- model ordering or arbitrary selection among multiple stable models.

The static test is intentionally a small guard, not a complete ASP parser: it catches the main forbidden syntactic forms in rule files. Engine behavior tests must ask Clingo for up to two models and fail if more than one model is produced. That runtime check is added with the first executable rule set.

The MVP has `PROVED` and `UNKNOWN` outcomes. `UNKNOWN` fails an assertion but is not a disproof.

<!-- TODO: Add DISPROVED only when the engine can return genuine refutation evidence. -->
