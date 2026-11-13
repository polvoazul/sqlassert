# 70. Hidden Join Fanout: decide whether many-to-many joins should be a Hazard by default

- **State:** open
- **Priority:** P2 (deferred): Establish prevalence before changing default warnings in a later sprint.
- **Theme:** suggestions
- **Created:** 2026-11-13
- **Related:** [69: real-world SQL corpora](0069-real-world-sql-corpora.md)

A single join can repeat rows on both inputs. For example, joining payments
directly to refunds on order ID can repeat each payment once per refund.
A subsequent `SUM(payments.amount)` then counts the same payment more than
once. [Aggregation Consistency Errors in Semantic Layers and How to Avoid
Them](https://arxiv.org/abs/2307.00417) describes this as double counting from
join fanout. The product already has a default **Many-to-many join** Hazard
for a subset of unmarked joins and a separate aggregate **Fan trap** Hazard.
Measure prevalence and warning volume before expanding the default behavior
or adding an explicit marker for intentional many-to-many joins.

## Study

- Use the public dbt projects from the existing public-project runs, starting
  with jaffle_shop, GitLab's analytics project, and Mattermost. Pin the source
  revisions and record the dbt adapter and target. Compile model SQL and
  count each eligible join occurrence once per source model. Exclude
  explicit cross, semi, and anti joins. Report skipped models and
  unsupported join sites.
- Use declared schema facts, enforced keys, and available dbt tests and
  contracts as evidence. Distinguish joins proved to repeat at most one
  input, joins confirmed to repeat both inputs, and unresolved joins. For
  confirmation, use observed matches or review that establishes the
  intended cardinality from model grain and join predicates. Record the
  evidence type. A missing proof alone leaves the join unresolved.
- Report raw counts and rates per project and pooled over eligible joins.
  Review candidates for intentional versus accidental fanout, and count
  how many feed duplicate-sensitive aggregates. Run the existing Hazard on
  the same corpus and report warning volume and reviewed false positives.

## Decision

- [ ] Publish the counts, unresolved coverage, reviewed examples, and
  reproducible run instructions.
- [ ] Present the measured prevalence alongside the tentative **10%**
  reference point. Show how unresolved joins affect the estimate, and
  include the warning volume and false positives from the existing Hazard.
- [ ] Review the evidence with Fred and record his decision on the default
  Hazard and whether intentional many-to-many joins should require an
  explicit acknowledgment. If an acknowledgment is adopted, specify how
  it interacts with the separate aggregate Hazard.
