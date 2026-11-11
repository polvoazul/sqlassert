# 37. Close the Paulley audit's missing rules

- **State:** open
- **Created:** 2026-11-06
- **Split from:** issue 23 ("Found by the Paulley audit")

The rows marked *missing* in the audit table (`docs/functional-dependencies.md`,
"Paulley audit", issue 35). None is unsound; each loses proofs Paulley makes.
Suggested order: E4 with L14, L13, S5, R9, I1 with I2, U2, F2 with F4,
then OR branches.

- [ ] **E4** (eq7): a lax equivalence whose columns a later filter makes
  definite does not become an Equality Group: after `LEFT JOIN r ON r.z = l.a
  WHERE r.z IS NOT NULL`, z and a are not merged, so Enumerated Domains and
  Foreign Key stand-ins do not cross between them. (The FD closure already
  strengthens the lax FD.)
  Related, **L14**: for Enumerated Domains a lax equivalence is enough, since
  NULL is outside every domain: after `LEFT JOIN u ON u.z = t.a WHERE t.a =
  5`, z is 5 or NULL, so `ENUM(z IN (5))` holds; today it is UNKNOWN.
- [ ] **L13** (Thm 3, strict equivalence 4): an ON equality within the padded
  side of an outer join (`ON ... AND u.x = u.z`) is a strict equivalence in
  the result (both NULL on padded rows), but does not merge an Equality Group,
  so `u.z = 3` in the same ON does not bound x's domain.
- [x] **S5** (§3.2.1): Done: an AND of `col IN (...)` and `col = v` over literals is an Enumerated Domain, trusted like keys (`convert._check_domains`, `DialectSemantics.check_constraints`); any other CHECK warns `ignored-check-constraint`. Was: CHECK constraints are ignored without a diagnostic.
  `CHECK (m = 1)` or `CHECK (m IN ('a', 'b'))` states a constant or an
  Enumerated Domain of m's non-null values (CHECK is true-interpreted: NULL
  passes), which is exactly what ENUM means.
- R10 (§3.2.4, `a = (uncorrelated scalar subquery)` gives `∅ → a`) moved to
  [38](done/0038-lower-order-by-and-select-list-subqueries.md) (done): it falls out of lowering scalar subqueries as joins.
- [ ] **R9** (§3.2.4): `LIKE 'x'` with no wildcard is `= 'x'`.
- [ ] **I1, I2** (Lemma 6, Corollary 1): the right input's FDs and keys
  through INTERSECT. Sound only when the paired columns compare by identity:
  an INTERSECT row carries the left value, equal to the right one under the
  left column's comparison (`identity_comparison`).
- [ ] **U2** (§3.2.6, Example 15): equivalences and constants that hold in
  both UNION ALL inputs at the same positions hold in the output.
- [ ] **F2, F4** (Lemma 10, Thm 4): a full join's ON gives no dependencies
  today. `t FULL JOIN u ON u.z = t.a` gives `a → z` (a non-null a is a left
  row, and a fixes which rows it matches) when η holds for z and for every
  right column of p, and the lax `z → a`.
- [ ] **OR branches** (beyond R13): constants and Equality Groups that hold in
  every branch of an OR hold after it. `(k = 5 AND …) OR (k = 5 AND …)` makes
  `k` constant; today an OR gives only null rejection and Enumerated Domains.
