# 75. Make `--explain` lead to the cause

- **State:** open
- **Priority:** P3 (open): An UNKNOWN assertion's explanation names the
  last relation where the proof stopped, not what is missing upstream, so
  finding the cause means rebuilding the query piece by piece.
- **Theme:** integrations
- **Created:** 2026-11-18

## The case

A transactions model, found in an audited dbt project and restated here with
generic names. Rows from account and card transactions are joined to lookup
tables, combined with `UNION ALL BY NAME` (tagged by `is_card`), and then
labelled. The assertion is UNKNOWN, as it should be, for three reasons:

1. **A join that multiplies rows.** `account_rows` joins `accounts` on
   `account_id` alone. `accounts` is unique only on `(account_id,
   organisation_id, user_id)`, so an account shared by two users matches
   twice. The duplicates are real: with `user_id` taken from `a`, each
   transaction copy comes out once per user of the account.
2. **A key the tables do not declare.** Even joined on `(account_id,
   user_id)` with `t.user_id` selected, `accounts` needs `UNIQUE
   (account_id, user_id)`.
3. **A sqlassert gap.** The join to `(SELECT DISTINCT tax_id ...) AS f` is
   not proved many-to-one: its left operand is `lpad(regexp_replace(...))`
   (`expression-key`, see #73 item 32). With `t.counterparty = f.tax_id`
   it is.

Fixing 1 and 2 still leaves the assertion UNKNOWN; fixing all three proves
it. Run it with:

```bash
sqlassert check --dialect duckdb --explain repro.sql
```

```sql
CREATE TABLE account_transactions (
    transaction_id VARCHAR, account_id VARCHAR, user_id VARCHAR NOT NULL,
    description VARCHAR, amount DECIMAL(18, 2), counterparty VARCHAR,
    UNIQUE (transaction_id, account_id, user_id)
);
CREATE TABLE accounts (
    account_id VARCHAR, organisation_id VARCHAR, user_id VARCHAR NOT NULL,
    UNIQUE (account_id, organisation_id, user_id)
);
CREATE TABLE card_transactions (
    transaction_id VARCHAR, card_account_id VARCHAR, user_id VARCHAR,
    description VARCHAR, amount DECIMAL(18, 2),
    UNIQUE (transaction_id, card_account_id, user_id)
);
CREATE TABLE cards (
    card_account_id VARCHAR, user_id VARCHAR NOT NULL, organisation_id VARCHAR,
    UNIQUE (card_account_id, user_id)
);
CREATE TABLE banks (organisation_id VARCHAR NOT NULL UNIQUE, bank_name VARCHAR NOT NULL);
CREATE TABLE flagged_counterparties (tax_id VARCHAR);
CREATE TABLE transaction_category (
    transaction_id VARCHAR, account_id VARCHAR, is_card BOOLEAN, category_id VARCHAR,
    UNIQUE (transaction_id, account_id, is_card)
);
CREATE TABLE users (user_id VARCHAR PRIMARY KEY, user_type VARCHAR);
CREATE TABLE users_in_scope (user_id VARCHAR PRIMARY KEY);

WITH account_rows AS (
    SELECT
        t.transaction_id, t.account_id, a.user_id, t.description, t.amount,
        FALSE AS is_card, b.bank_name, f.tax_id AS flagged_tax_id
    FROM account_transactions AS t
    LEFT JOIN accounts AS a USING (account_id)
    LEFT JOIN banks AS b USING (organisation_id)
    LEFT JOIN (SELECT DISTINCT tax_id FROM flagged_counterparties) AS f
        ON lpad(regexp_replace(t.counterparty, '[^0-9]', '', 'g'), 14, '0') = f.tax_id
    WHERE t.user_id IN (SELECT user_id FROM users_in_scope)
),
card_rows AS (
    SELECT
        t.transaction_id, t.card_account_id AS account_id, t.user_id, t.description, t.amount,
        TRUE AS is_card, b.bank_name, NULL::VARCHAR AS flagged_tax_id
    FROM card_transactions AS t
    LEFT JOIN cards AS c USING (card_account_id, user_id)
    LEFT JOIN banks AS b USING (organisation_id)
    WHERE t.user_id IN (SELECT user_id FROM users_in_scope)
),
all_rows AS (
    SELECT * FROM account_rows
    UNION ALL BY NAME
    SELECT * FROM card_rows
),
labelled AS (
    SELECT t.*, tc.category_id, coalesce(u.user_type, '?') AS user_type
    FROM all_rows AS t
    LEFT JOIN transaction_category AS tc USING (transaction_id, account_id, is_card)
    LEFT JOIN users AS u ON t.user_id = u.user_id
)
SELECT transaction_id, account_id, is_card, user_id, description, amount, bank_name, category_id, user_type
FROM labelled
/**ASSERT UNIQUE(transaction_id, account_id, is_card, user_id)**/;
```

Output (sqlassert at 56bfd35):

```text
repro.sql:61:1: error unproved-unique: /**ASSERT UNIQUE(transaction_id, account_id, is_card, user_id)**/ is not proved (UNKNOWN)
    #1 UNKNOWN: unique_set
      #2 UNKNOWN: Any alternative suffices
        #3 UNKNOWN: All premises required
          #4 PROVED: A joined row is one row of each input — LEFT JOIN users AS u ON t.user_id = u.user_id
          #5 UNKNOWN: Unique-set columns are not covered
        #6 UNKNOWN: All premises required
          #7 PROVED: many_to_one_join — LEFT JOIN users AS u ON t.user_id = u.user_id
            #8 PROVED: All premises required
              #9 PROVED: Right columns bound for each left row — LEFT JOIN users AS u ON t.user_id = u.user_id
                #10 PROVED: Equality fixes the matched column for each row of the other input — t.user_id = u.user_id (line 57); users AS u; t.user_id
              #11 PROVED: The columns determine the row — users AS u
                #12 PROVED: Determined in the input — users AS u
                  #13 PROVED: Functional dependency — CREATE TABLE users (user_id TEXT PRIMARY KEY, user_type TEXT)
                    #14 PROVED: Any alternative suffices
                      #15 PROVED: Accepted UniqueSet — CREATE TABLE users (user_id TEXT PRIMARY KEY, user_type TEXT)
                    #16 PROVED: In the requested set — CREATE TABLE users (user_id TEXT PRIMARY KEY, user_type TEXT)
          #17 UNKNOWN: Unique-set columns are not covered
        #18 UNKNOWN: All premises required
          #19 PROVED: A joined row is one row of each input — LEFT JOIN transaction_category AS tc USING (transaction_id, account_id, is_card)
          #20 UNKNOWN: Unique-set columns are not covered
        #21 UNKNOWN: All premises required
          #22 PROVED: many_to_one_join — LEFT JOIN transaction_category AS tc USING (transaction_id, account_id, is_card)
            #23 PROVED: All premises required
              #24 PROVED: Right columns bound for each left row — LEFT JOIN transaction_category AS tc USING (transaction_id, account_id, is_card)
                #25 PROVED: Equality fixes the matched column for each row of the other input — transaction_id (line 56); transaction_category AS tc
                #26 PROVED: Equality fixes the matched column for each row of the other input — account_id (line 56); transaction_category AS tc
                #27 PROVED: Equality fixes the matched column for each row of the other input — is_card (line 56); transaction_category AS tc
              #28 PROVED: The columns determine the row — transaction_category AS tc
                #29 PROVED: Determined in the input — transaction_category AS tc
                  #30 PROVED: Functional dependency — CREATE TABLE transaction_category (transaction_id TEXT, account_id TEXT, is_card BOOLEAN, category_id TEXT, UNIQUE (transaction_id, account_id, is_card))
                    #31 PROVED: Any alternative suffices
                      #32 PROVED: Accepted UniqueSet — CREATE TABLE transaction_category (transaction_id TEXT, account_id TEXT, is_card BOOLEAN, category_id TEXT, UNIQUE (transaction_id, account_id, is_card))
                    #33 PROVED: In the requested set — CREATE TABLE transaction_category (transaction_id TEXT, account_id TEXT, is_card BOOLEAN, category_id TEXT, UNIQUE (transaction_id, account_id, is_card))
                    #34 PROVED: In the requested set — CREATE TABLE transaction_category (transaction_id TEXT, account_id TEXT, is_card BOOLEAN, category_id TEXT, UNIQUE (transaction_id, account_id, is_card))
                    #35 PROVED: In the requested set — CREATE TABLE transaction_category (transaction_id TEXT, account_id TEXT, is_card BOOLEAN, category_id TEXT, UNIQUE (transaction_id, account_id, is_card))
          #36 UNKNOWN: Unique-set columns are not covered
1 file checked: 1 assertion (0 PROVED, 1 UNKNOWN), 1 error, 0 warnings
```

None of the three causes appears. Every UNKNOWN leaf is "Unique-set columns
are not covered" at the outermost joins, whose own many-to-one proofs fill
most of the output. Nothing descends into `all_rows` or its inputs.

## What a useful explanation says

For this case, something like:

```text
UNIQUE(transaction_id, account_id, is_card, user_id) on the final SELECT is UNKNOWN:
  labelled keeps the Unique Sets of all_rows (both joins are many-to-one)
  all_rows: UNION ALL BY NAME, inputs separated by is_card; each input must be unique
    account_rows is not unique on (transaction_id, account_id, user_id):
      line 33  LEFT JOIN accounts AS a USING (account_id): not many-to-one,
               accounts has no Unique Set within (account_id)  [no-unique-set (partial-key)]
      line 35  LEFT JOIN (SELECT DISTINCT tax_id ...) AS f: not many-to-one,
               its key is compared with an expression  [expression-key]
    card_rows: unique on (transaction_id, account_id, user_id)  PROVED
```

## To do

- [ ] Follow an uncovered Unique Set into the input that lacks it, through
  projections, filters and CTE boundaries, to the first relation that loses
  it, and name that relation by its CTE or view name and line.
- [ ] Where a join loses it, give the join's own Unproved Join Reason (the
  same reason and detail the join cardinality report uses) instead of a
  generic "not covered".
- [ ] For a set operation, say whether its inputs are separated and which
  input is not unique.
- [ ] Hide the PROVED subtrees of an UNKNOWN proof by default (one line each,
  with a flag to expand), and name the alternatives tried in plain words
  instead of "Any alternative suffices" / "All premises required".
- [ ] Print each relation once by name, not its whole `CREATE TABLE` on
  every line that cites it.
- [ ] For `untyped-key`, name where the type is lost: the column, its
  expression, the relation and line (`_type_loss` walks down to it, then
  keeps only the label). Seen on a real project: 23 joins said `computed`,
  and all came down to two causes: `x::varchar` on the key columns (a cast
  to text has no type, `_cast_typing`) and calls lowered without a result
  type (`lpad`, `regexp_replace`, `date_trunc`, and a CASE over them).
  Finding that took a monkeypatched analysis; the explanation should say
  it.
- [ ] When a premise needs a column's type and it is unknown, say why, from
  the reason `scalar_typing` already returns (for example "a cast of text
  keeps the operand's collation").
- [ ] Warn `unknown-function` (a Diagnostic) when a call lowers with no
  result type the dialect fixes, naming the function, so a gap in
  `_result_type` shows where it is written instead of as an unknown type
  further down.
- [ ] The dbt audit report gives, for each unproved join, the cause the
  explanation names, grouped by cause, so one pattern behind many joins
  shows at a glance.
- [ ] Keep this case as a test: the explanation names lines 33 and 35, and
  after fixing them, the next cause.
