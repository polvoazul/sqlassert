# 10. Cut over to analyze and retire the legacy analyzer

- **State:** closed
- **Labels:** ready-for-agent
- **Created:** 2026-08-21
- **Closed:** 2026-08-27
- **Migrated from:** https://github.com/polvoazul/sqlassert/issues/10

## Parent

#1

## What to build

Make the new `analyze` architecture the sole public property engine, migrate every current test scenario to remain valid against that architecture, update user documentation, and remove the legacy monolithic analyzer. No existing happy, unhappy, marker, parse-error, multiple-assertion, view, subquery, aggregate, distinct, or qualification scenario may be silently dropped during cutover.

## Acceptance criteria

- [ ] Every test case that exists before this ticket is reviewed and either runs unchanged against the new architecture or is rewritten one-for-one through `analyze`; no existing scenario is deleted without equivalent behavioral coverage.
- [ ] Existing expectations that would be unsound under the agreed semantics are rewritten to expect UNKNOWN with the reason documented in the test rather than preserved for compatibility.
- [ ] Current successful Unique Join scenarios remain successful when supported by the MVP, including primary or unique constraints, composite coverage, views, aliases, CTEs, subqueries, GROUP BY, DISTINCT, and recognized row-number qualification.
- [ ] Current unhappy scenarios remain valid, including incomplete keys, absent uniqueness, non-preserving relations, unsupported joins, marker edge cases, and parse errors.
- [ ] The public package exports the new `analyze` operation and durable Report types as the authoritative API.
- [ ] The legacy connection-backed analyzer and duplicate reasoning paths are removed after migration, while DuckDB autodiscovery remains deferred.
- [ ] User documentation describes the SQL Program input, optional Knowledge, PROVED and UNKNOWN outcomes, supported MVP behavior, and current limitations.
- [ ] Packaging includes the Clingo runtime required by the new engine and the package can be imported in its declared supported Python environment.
- [ ] Automated tests include at least one final happy public-API example and one final UNKNOWN/unsupported public-API example in addition to the fully migrated suite.
- [ ] The complete test suite, including the deterministic Clingo policy guard, passes with all current scenarios valid under the new architecture.

## Blocked by

- #9

---

**Comment by polvoazul on 2026-08-27:**

The analyze architecture is now the sole public property engine and the public API, packaging, documentation, and migrated behavioral suite are in place. Intentional deviation from literal deletion: the old connection-backed checker is retained only in deprecated/unique.py as unexported reference code. Current verification: 236 tests pass and ty check passes. Closing as complete.
