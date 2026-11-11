# 64. Triage WeTune's discovered rewrite rules

- **State:** open
- **Labels:** research
- **Created:** 2026-11-11
- **Related:** [15: suggest](0015-suggest-provable-properties-for-unannotated-sql.md) (Kossmann Table 1 triage, same format)

## Why

WeTune (Wang et al., SIGMOD 2022) enumerates query plan templates and
verifies rewrite rules with SMT; it found 1106 promising, non-reducible
rules. Each rule is a pair of templates plus constraints, among them
uniqueness, NOT NULL and reference (FK) constraints: exactly the Properties
sqlassert proves. The rules are a second, machine-generated catalog next to
Kossmann's Table 1 (already triaged in issue 15).

Rule list: the paper's artifact. The GitHub repo
[SJTU-IPADS/wetune](https://github.com/SJTU-IPADS/wetune) is empty; check
the SQLSolver repo (Apache-2.0, same group) and the SIGMOD reproducibility
report for the rule set.

## What to do

1. Get the rule list; keep rules whose constraints include a key, NOT NULL
   or FK.
2. Group them by rewrite shape; for each group record *covered* (the
   sqlassert Improvement or smell), *add* (High rewrite, Low smell or
   assertion, with its precondition as a Property), or *n/a* (why).
3. File the *add* rows as items under issue 15's Low level, or new issues.

## Acceptance criteria

- [ ] Triage table in this issue, one row per rule group.
- [ ] *Add* rows filed.
