# Agent Guidelines

## No backwards compatibility while in alpha

sqlassert is alpha. Code simplicity wins over compatibility:

- Replace mechanisms outright; do not keep old and new side by side.
- No feature flags, shims, deprecation paths, or fallback branches for old
  behavior. They are an anchor on every later change.
- Delete superseded code, tests, and docs in the same change.

The one contract to keep stable is the user-facing one: the Property Markers
(`/**ASSERT ...**/`, `/**DECLARE ...**/`) and the Properties they introduce.
Internal representation (IR, analysis functions, rules) may change freely.

## Where things are

- Domain language: `CONTEXT.md`. Use its terms; it lists terms to avoid.
- Implementation conventions: `docs/implementation-conventions.md`.
- Rule style: `sqlassert/rules/STYLEGUIDE.md`.
- Open work: `docs/issues/` (index in `docs/issues/README.md`).
- Architecture decisions: `docs/adr/`.
