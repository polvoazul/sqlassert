"""The soundness oracle judges true and deliberately false claims against DuckDB."""

from __future__ import annotations

from dataclasses import replace

import pytest

from soundness_oracle import Judgement, SearchResult, _Database, _Invalid, judge
from sqlassert import Outcome, Report, analyze
from sqlassert.sql_parse import SqlParser

# Few examples: these programs are tiny, and the counterexamples they need
# are found almost at once.
EXAMPLES = 50


def claimed_proved(sql: str) -> tuple[str, Report]:
    """A Report claiming every assertion PROVED, true or not, to show the
    oracle refutes a false claim."""
    report = analyze(sql)
    return sql, replace(report, assertions=tuple(replace(assertion, outcome=Outcome.PROVED) for assertion in report.assertions))


def judged(sql: str, report: Report) -> Judgement:
    return judge(sql, report, max_examples=EXAMPLES)


def test_a_true_unique_join_proof_has_no_counterexample():
    sql = """
        CREATE TABLE users (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE sessions (user_id INTEGER, ts TIMESTAMP);
        SELECT * FROM sessions /**ASSERT MANY TO ONE**/ JOIN users ON sessions.user_id = users.id
    """
    report = analyze(sql)
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]

    judgement = judged(sql, report)

    assert judgement.checked == 1
    assert judgement.counterexamples == ()
    assert judgement.skipped == ()


def test_a_false_unique_join_claim_gets_a_two_row_counterexample():
    judgement = judged(*claimed_proved("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);
        SELECT * FROM users /**ASSERT MANY TO ONE**/ JOIN sessions ON users.id = sessions.user_id
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.assertion_id == "assert_1"
    # Shrunk to the minimal instance: one user matched by two sessions.
    assert len(counterexample.instance["users"]) == 1
    assert len(counterexample.instance["sessions"]) == 2
    assert "sessions" in counterexample.explain()


def test_a_false_unique_join_claim_over_a_cte_is_refuted_with_its_context():
    judgement = judged(*claimed_proved("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);
        WITH active AS (SELECT id FROM users WHERE id > 0)
        SELECT * FROM active /**ASSERT MANY TO ONE**/ JOIN sessions ON active.id = sessions.user_id
    """))

    assert len(judgement.counterexamples) == 1


@pytest.mark.parametrize(
    ("marker", "left", "right"),
    [
        # A false one-to-many claim: two sessions share one user's user_id, so that user row repeats.
        ("ONE TO MANY", "sessions", "users_by_name"),
        # A false not-many-to-many claim: neither side is unique on user_id.
        ("UNIQUE", "sessions", "sessions AS other"),
        ("ONE TO ONE", "sessions", "users_by_name"),
    ],
)
def test_a_false_cardinality_claim_is_refuted(marker: str, left: str, right: str):
    on = "sessions.user_id = users_by_name.user_id" if right == "users_by_name" else "sessions.user_id = other.user_id"
    judgement = judged(*claimed_proved(f"""
        CREATE TABLE users_by_name (user_id INTEGER, name VARCHAR PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);
        SELECT * FROM {left} /**ASSERT {marker}**/ JOIN {right} ON {on}
    """))

    assert len(judgement.counterexamples) == 1


def test_an_edge_case_counterexample_needs_no_random_search():
    # Two sessions sharing a user's user_id is an edge case (a duplicate row),
    # checked before any random example: one example of budget still finds it.
    judgement = judge(*claimed_proved("""
        CREATE TABLE users_by_name (user_id INTEGER, name VARCHAR PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);
        SELECT * FROM sessions /**ASSERT ONE TO MANY**/ JOIN users_by_name ON sessions.user_id = users_by_name.user_id
    """), max_examples=1)

    counterexample, = judgement.counterexamples
    assert len(counterexample.instance["sessions"]) == 2


def test_true_cardinality_proofs_have_no_counterexample():
    sql = """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);
        CREATE TABLE profiles (user_id INTEGER PRIMARY KEY);
        SELECT * FROM users
        /**ASSERT ONE TO MANY**/ LEFT JOIN sessions ON sessions.user_id = users.id
        /**ASSERT UNIQUE**/ RIGHT JOIN profiles ON profiles.user_id = users.id
    """
    report = analyze(sql)
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED, Outcome.PROVED]

    judgement = judged(sql, report)

    assert judgement.checked == 2
    assert judgement.counterexamples == ()


def test_a_false_claim_on_a_cte_is_refuted_with_the_ctes_before_it():
    judgement = judged(*claimed_proved("""
        CREATE TABLE sessions (user_id INTEGER);
        WITH active AS (SELECT user_id FROM sessions WHERE user_id > 0),
             per_user AS (SELECT user_id FROM active /**ASSERT UNIQUE(user_id)**/)
        SELECT * FROM per_user
    """))

    assert len(judgement.counterexamples) == 1


def test_a_false_unique_set_claim_on_the_root_select_is_refuted():
    judgement = judged(*claimed_proved("""
        CREATE TABLE sessions (user_id INTEGER);
        SELECT user_id FROM sessions /**ASSERT UNIQUE(user_id)**/
    """))

    counterexample, = judgement.counterexamples
    assert len(counterexample.instance["sessions"]) == 2


def test_a_false_foreign_key_claim_is_refuted_by_an_unmatched_row():
    judgement = judged(*claimed_proved("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE sessions (user_id INTEGER);
        SELECT user_id FROM sessions /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.instance["users"] == []
    assert len(counterexample.instance["sessions"]) == 1


def test_a_foreign_key_onto_columns_that_repeat_is_refuted():
    judgement = judged(*claimed_proved("""
        CREATE TABLE users (id INTEGER);
        CREATE TABLE sessions (user_id INTEGER);
        SELECT user_id FROM sessions
        /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/
        /**ASSERT FOREIGN KEY(user_id) REFERENCES users(id)**/
    """))

    # Declared coverage holds in every instance; the duplicate target refutes it.
    counterexample, = judgement.counterexamples
    assert len(counterexample.instance["users"]) == 2


def test_a_true_exactly_one_join_proof_has_no_counterexample():
    sql = """
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (user_id INTEGER NOT NULL REFERENCES users(id));
        SELECT * FROM orders /**ASSERT EXACTLY ONE**/ LEFT JOIN users ON orders.user_id = users.id
    """
    report = analyze(sql)
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]

    judgement = judged(sql, report)

    assert judgement.checked == 1
    assert judgement.counterexamples == ()
    assert judgement.skipped == ()


def test_a_false_exactly_one_join_claim_is_refuted_by_an_unmatched_left_row():
    judgement = judged(*claimed_proved("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (user_id INTEGER NOT NULL);
        SELECT * FROM orders /**ASSERT EXACTLY ONE**/ LEFT JOIN users ON orders.user_id = users.id
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.instance["users"] == []
    assert len(counterexample.instance["orders"]) == 1


def test_a_false_exactly_one_join_claim_is_refuted_by_a_null_left_value():
    judgement = judged(*claimed_proved("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (user_id INTEGER REFERENCES users(id));
        SELECT * FROM orders /**ASSERT EXACTLY ONE**/ JOIN users ON orders.user_id = users.id
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.instance["orders"] == [(None,)]


def test_a_false_exactly_one_join_claim_is_refuted_by_a_repeated_match():
    judgement = judged(*claimed_proved("""
        CREATE TABLE users (id INTEGER);
        CREATE TABLE orders (user_id INTEGER NOT NULL);
        CREATE VIEW o AS SELECT user_id FROM orders /**DECLARE FOREIGN KEY(user_id) REFERENCES users(id)**/;
        SELECT * FROM o /**ASSERT EXACTLY ONE**/ JOIN users ON o.user_id = users.id
    """))

    # Declared coverage matches every left row, so only a second match refutes the claim.
    counterexample, = judgement.counterexamples
    assert len(counterexample.instance["orders"]) == 1
    assert len(counterexample.instance["users"]) == 2


def test_a_false_dependency_claim_is_refuted_by_two_dependent_values():
    judgement = judged(*claimed_proved("""
        CREATE TABLE addresses (zip VARCHAR, city VARCHAR);
        SELECT zip, city FROM addresses /**ASSERT DETERMINES(zip -> city)**/
    """))

    counterexample, = judgement.counterexamples
    assert len(counterexample.instance["addresses"]) == 2


def test_a_null_determinant_never_violates_a_dependency():
    judgement = judged(*claimed_proved("""
        CREATE TABLE addresses (zip VARCHAR, city VARCHAR);
        SELECT zip, city FROM addresses WHERE zip IS NULL /**ASSERT DETERMINES(zip -> city)**/
    """))

    assert judgement.checked == 1
    assert judgement.counterexamples == ()


def test_rows_with_null_never_violate_a_unique_set():
    sql = """
        CREATE TABLE t (id INTEGER UNIQUE);
        SELECT id FROM t /**ASSERT UNIQUE(id)**/
    """
    report = analyze(sql)
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]

    assert judged(sql, report).counterexamples == ()


def test_a_null_refutes_a_non_null_unique_set_claim():
    judgement = judged(*claimed_proved("""
        CREATE TABLE t (id INTEGER UNIQUE);
        SELECT id FROM t /**ASSERT NOT NULL UNIQUE(id)**/
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.instance["t"] == [(None,)]


def test_a_null_refutes_a_not_null_claim():
    judgement = judged(*claimed_proved("""
        CREATE TABLE t (a INTEGER NOT NULL, b INTEGER);
        SELECT a, b FROM t /**ASSERT NOT NULL(a, b)**/
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.instance["t"] == [(1, None)]


def test_a_false_claim_on_a_view_is_refuted():
    judgement = judged(*claimed_proved("""
        CREATE TABLE sessions (user_id INTEGER);
        CREATE VIEW per_user AS SELECT user_id FROM sessions /**ASSERT UNIQUE(user_id)**/;
        SELECT * FROM per_user
    """))

    assert len(judgement.counterexamples) == 1


def test_a_false_claim_on_a_from_subquery_is_refuted():
    judgement = judged(*claimed_proved("""
        CREATE TABLE sessions (user_id INTEGER);
        SELECT * FROM (SELECT user_id FROM sessions /**ASSERT UNIQUE(user_id)**/) AS s
    """))

    assert len(judgement.counterexamples) == 1


def test_query_literals_join_the_value_domain():
    # Only a literal outside the tiny default domain reaches the rows that collide.
    judgement = judged(*claimed_proved("""
        CREATE TABLE sessions (user_id INTEGER);
        SELECT user_id FROM sessions WHERE user_id = 47 /**ASSERT UNIQUE(user_id)**/
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.instance["sessions"] == [(47,), (47,)]


def test_a_false_enum_claim_is_refuted_by_a_value_outside_the_set():
    judgement = judged(*claimed_proved("""
        CREATE TABLE orders (status VARCHAR);
        SELECT status FROM orders /**ASSERT ENUM(status IN ('new', 'paid'))**/
    """))

    counterexample, = judgement.counterexamples
    status, = counterexample.instance["orders"][0]
    assert status not in {"new", "paid", None}


def test_a_null_never_violates_an_enum():
    judgement = judged(*claimed_proved("""
        CREATE TABLE orders (status VARCHAR);
        SELECT status FROM orders WHERE status IS NULL /**ASSERT ENUM(status IN ('new'))**/
    """))

    assert judgement.checked == 1
    assert judgement.counterexamples == ()


def test_a_declared_enum_constrains_the_instances_searched():
    sql = """
        CREATE TABLE orders (status VARCHAR);
        WITH o AS (SELECT status FROM orders /**DECLARE ENUM(status IN ('new', 'paid'))**/)
        SELECT status FROM o WHERE status <> 'new' /**ASSERT ENUM(status IN ('paid'))**/
    """
    # Only the declaration rules out every other value; the analysis does not read `<>`.
    judgement = judged(*claimed_proved(sql))

    assert judgement.checked == 1
    assert judgement.counterexamples == ()


def test_a_false_numeric_enum_claim_is_refuted():
    judgement = judged(*claimed_proved("""
        CREATE TABLE items (qty INTEGER);
        SELECT qty FROM items WHERE qty IN (1, 2) /**ASSERT ENUM(qty IN (1))**/
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.witness == [(2,)]


def test_a_false_boolean_enum_claim_is_refuted():
    judgement = judged(*claimed_proved("""
        CREATE TABLE items (flag BOOLEAN);
        SELECT flag FROM items /**ASSERT ENUM(flag IN (TRUE))**/
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.witness == [(False,)]


def test_a_member_that_cannot_compare_with_the_column_matches_nothing():
    # 'a' cannot be cast to INTEGER; the claim stands or falls on 1 alone.
    sql = """
        CREATE TABLE items (qty INTEGER);
        SELECT qty FROM items WHERE qty = 1 /**ASSERT ENUM(qty IN (1, 'a'))**/
    """
    assert judged(*claimed_proved(sql)).counterexamples == ()

    false = judged(*claimed_proved(sql.replace("(1, 'a')", "(2, 'a')")))
    assert [counterexample.witness for counterexample in false.counterexamples] == [[(1,)]]


def test_two_distinct_literals_can_equal_one_float():
    # Why distinct numeric literals may meet: a FLOAT equals both, so the
    # declared and filtered domains do not intersect to nothing.
    judgement = judged(*claimed_proved("""
        CREATE TABLE items (ratio FLOAT);
        WITH r AS (SELECT * FROM items /**DECLARE ENUM(ratio IN (0.1))**/)
        SELECT ratio FROM r WHERE ratio = 0.100000001 /**ASSERT ENUM(ratio IN (5))**/
    """))

    assert len(judgement.counterexamples) == 1


def test_two_distinct_literals_can_equal_one_string():
    # A VARCHAR is cast to each numeric literal's type, rounding to its scale.
    judgement = judged(*claimed_proved("""
        CREATE TABLE items (tag VARCHAR);
        WITH t AS (SELECT * FROM items /**DECLARE ENUM(tag IN (1))**/)
        SELECT tag FROM t WHERE tag = 1.4 /**ASSERT ENUM(tag IN (5))**/
    """))

    assert len(judgement.counterexamples) == 1


def test_a_true_exhaustive_case_proof_has_no_counterexample():
    sql = """
        CREATE TABLE orders (status VARCHAR);
        SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 WHEN 'paid' THEN 2 END AS n
        FROM orders WHERE status IN ('new', 'paid')
    """
    report = analyze(sql)
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]

    judgement = judged(sql, report)

    assert judgement.checked == 1
    assert judgement.counterexamples == ()


def test_a_false_exhaustive_case_claim_is_refuted_by_a_value_with_no_arm():
    # ELSE catches the value, but does not count as an arm.
    judgement = judged(*claimed_proved("""
        CREATE TABLE orders (status VARCHAR);
        SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 ELSE 0 END AS n
        FROM orders WHERE status IN ('new', 'paid')
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.witness == [("paid",)]


def test_a_null_operand_never_violates_an_exhaustive_case():
    judgement = judged(*claimed_proved("""
        CREATE TABLE orders (status VARCHAR);
        SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 END AS n FROM orders WHERE status IS NULL
    """))

    assert judgement.checked == 1
    assert judgement.counterexamples == ()


def test_a_false_exhaustive_case_claim_over_a_cte_is_refuted_with_its_context():
    judgement = judged(*claimed_proved("""
        CREATE TABLE orders (status VARCHAR);
        WITH o AS (SELECT status FROM orders WHERE status IN ('new', 'paid'))
        SELECT /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 END AS n FROM o
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.witness == [("paid",)]


def test_a_false_exhaustive_case_claim_in_where_is_refuted():
    judgement = judged(*claimed_proved("""
        CREATE TABLE orders (id INTEGER, status VARCHAR);
        SELECT id FROM orders WHERE /**ASSERT EXHAUSTIVE**/ CASE status WHEN 'new' THEN 1 END = 1
    """))

    assert len(judgement.counterexamples) == 1


def test_a_false_exhaustive_case_claim_in_a_join_is_refuted_on_the_pairs_it_compares():
    # Evaluated on every pair the ON condition sees, matched or not.
    judgement = judged(*claimed_proved("""
        CREATE TABLE orders (id INTEGER, status VARCHAR);
        CREATE TABLE items (order_id INTEGER);
        SELECT o.id FROM orders o LEFT JOIN items i
          ON /**ASSERT EXHAUSTIVE**/ CASE o.status WHEN 'new' THEN 1 END = i.order_id
    """))

    counterexample, = judgement.counterexamples
    assert counterexample.instance["items"] != []


def test_instances_that_break_a_property_declaration_are_rejected():
    # The declaration is a trusted assumption, so data violating it is no
    # counterexample to the proof built on it.
    sql = """
        CREATE TABLE orders (user_id INTEGER);
        CREATE TABLE sessions (user_id INTEGER);
        CREATE VIEW latest AS SELECT user_id FROM sessions /**DECLARE UNIQUE(user_id)**/;
        SELECT * FROM orders /**ASSERT MANY TO ONE**/ JOIN latest ON orders.user_id = latest.user_id
    """
    report = analyze(sql)
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]

    judgement = judged(sql, report)

    assert judgement.checked == 1
    assert judgement.counterexamples == ()


def test_a_view_may_read_a_table_created_after_it():
    judgement = judged(*claimed_proved("""
        CREATE VIEW per_user AS SELECT user_id FROM sessions /**ASSERT UNIQUE(user_id)**/;
        CREATE TABLE sessions (user_id INTEGER);
        SELECT * FROM per_user
    """))

    assert len(judgement.counterexamples) == 1


def test_programs_reading_a_table_without_create_are_skipped():
    judgement = judged(*claimed_proved("""
        SELECT * FROM sessions /**ASSERT MANY TO ONE**/ JOIN users ON sessions.user_id = users.id
    """))

    assert judgement.checked == 0
    assert len(judgement.skipped) == 1


def test_unknown_assertions_are_not_judged():
    judgement = judged(*(lambda sql: (sql, analyze(sql)))("""
        CREATE TABLE sessions (user_id INTEGER);
        SELECT user_id FROM sessions /**ASSERT UNIQUE(user_id)**/
    """))

    assert judgement.checked == 0
    assert judgement.counterexamples == ()


@pytest.mark.parametrize("constraint", [
    "user_id INTEGER REFERENCES users(id)",
    "user_id INTEGER REFERENCES users",
    "user_id INTEGER, FOREIGN KEY(user_id) REFERENCES users(id)",
    "user_id INTEGER, CONSTRAINT fk FOREIGN KEY(user_id) REFERENCES users(id)",
])
def test_complete_fk_instances_can_be_reloaded_in_any_table_order(constraint: str):
    database = _Database(SqlParser("duckdb").parse(f"""
        CREATE TABLE orders ({constraint});
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        SELECT user_id FROM orders
    """))
    for value in (1, 2):
        database.load({"orders": [(value,)], "users": [(value,)]})
        assert all(database.connection.execute(check).fetchall() == [] for check in database.foreign_keys)


def test_fk_checks_reject_orphans_after_loading_the_whole_instance():
    database = _Database(SqlParser("duckdb").parse("""
        CREATE TABLE users (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (user_id INTEGER REFERENCES users(id));
        SELECT user_id FROM orders
    """))

    def orphan(instance) -> bool:
        # The callback runs only after validation, including FK coverage.
        assert not database.connection.execute(database.foreign_keys[0]).fetchall()
        return bool(instance["orders"]) and not instance["users"] and all(row[0] is not None for row in instance["orders"])

    search = database.search(orphan, max_examples=EXAMPLES)
    assert search.status == "no-counterexample"
    assert search.valid > 0
    assert search.rejected > 0


def test_composite_fk_checks_exempt_a_tuple_with_any_null_component():
    database = _Database(SqlParser("duckdb").parse("""
        CREATE TABLE users (org INTEGER, id INTEGER, PRIMARY KEY(org, id));
        CREATE TABLE orders (org INTEGER, user_id INTEGER, FOREIGN KEY(org, user_id) REFERENCES users(org, id));
        SELECT * FROM orders
    """))
    database.load({"orders": [(None, 1), (1, None)], "users": []})
    assert database.connection.execute(database.foreign_keys[0]).fetchall() == []
    database.load({"orders": [(1, 2)], "users": []})
    assert database.connection.execute(database.foreign_keys[0]).fetchall() == [(1, 2)]


def test_cyclic_fks_are_validated_without_database_enforcement():
    database = _Database(SqlParser("duckdb").parse("""
        CREATE TABLE a (id INTEGER PRIMARY KEY, b INTEGER REFERENCES b(id));
        CREATE TABLE b (id INTEGER PRIMARY KEY, a INTEGER REFERENCES a(id));
        SELECT * FROM a
    """))
    database.load({"a": [(1, 2)], "b": [(2, 1)]})
    assert all(database.connection.execute(check).fetchall() == [] for check in database.foreign_keys)


def test_foreign_keys_added_by_alter_table_are_validated_by_queries():
    database = _Database(SqlParser("duckdb").parse("""
        CREATE TABLE orders (user_id INTEGER);
        CREATE TABLE users (id INTEGER);
        ALTER TABLE users ADD PRIMARY KEY(id);
        ALTER TABLE orders ADD CONSTRAINT fk FOREIGN KEY(user_id) REFERENCES users;
        SELECT * FROM orders
    """))
    database.load({"orders": [(1,)], "users": []})
    assert database.connection.execute(database.foreign_keys[0]).fetchall() == [(1,)]
    database.load({"orders": [(1,)], "users": [(1,)]})
    assert database.connection.execute(database.foreign_keys[0]).fetchall() == []


def test_search_reports_completed_sampling_without_a_counterexample():
    database = _Database(SqlParser("duckdb").parse("CREATE TABLE t (id INTEGER); SELECT * FROM t"))
    search = database.search(lambda _: False, max_examples=EXAMPLES)
    assert search.status == "no-counterexample"
    assert search.instance is None
    assert search.attempted == search.valid > 0
    assert search.rejected == 0


def test_search_reports_insufficient_data_when_all_samples_are_rejected():
    database = _Database(SqlParser("duckdb").parse("CREATE TABLE t (id INTEGER); SELECT * FROM t"))

    def rejected(_) -> bool:
        raise _Invalid

    search = database.search(rejected, max_examples=EXAMPLES)
    assert search.status == "insufficient-data"
    assert search.instance is None
    assert search.valid == 0
    assert search.rejected == search.attempted > 0


def test_judgement_exposes_search_counts_per_assertion():
    sql = """
        CREATE TABLE t (id INTEGER PRIMARY KEY);
        SELECT id FROM t /**ASSERT UNIQUE(id)**/
    """
    judgement = judged(sql, analyze(sql))
    assertion_id, search = judgement.searches[0]
    assert assertion_id == "assert_1"
    assert search.status == "no-counterexample"
    assert search.valid > 0


def test_an_insufficient_search_is_reported_as_skipped(monkeypatch):
    monkeypatch.setattr(_Database, "search", lambda *args: SearchResult("insufficient-data", None, 10, 0))
    judgement = judged(*claimed_proved("""
        CREATE TABLE t (id INTEGER);
        SELECT id FROM t /**ASSERT UNIQUE(id)**/
    """))
    assert judgement.checked == 0
    assert "insufficient valid data" in judgement.skipped[0]
    assert judgement.searches[0][1].rejected == 10


def test_programs_in_another_dialect_are_skipped():
    # Rendering for DuckDB may change the query (sqlglot turns Spark's RIGHT
    # SEMI JOIN into a SEMI JOIN), so only DuckDB SQL is judged.
    sql = "CREATE TABLE t (id INTEGER PRIMARY KEY); SELECT id FROM t /**ASSERT UNIQUE(id)**/"
    judgement = judge(sql, analyze(sql, dialect="spark"), dialect="spark")

    assert judgement.checked == 0
    assert judgement.skipped == ("dialect spark: the oracle runs DuckDB SQL only",)
