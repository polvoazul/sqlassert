"""Unique Sets derived from ordinary Aggregate (`GROUP BY`) and `DISTINCT`.

Both earn a Unique Set of their own regardless of their input relation's
existing Unique Sets: an Aggregate by its complete set of Grouping Keys, a
Distinct by its complete set of output expressions. Every case here wraps the
aggregate or distinct query in a CTE, exercising #5's and #7's lowering
together, exactly like `test_analyze_cte_and_subquery.py` does for plain
Filter/Project bodies.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze


@pytest.fixture
def orders_and_customers() -> str:
    """`orders(customer_id, region, amount)`, joined against by `customers(id)`."""
    return """
    CREATE TABLE orders (customer_id INTEGER, region VARCHAR, amount INTEGER);
    CREATE TABLE customers (id INTEGER);
    """


# Aggregate ------------------------------------------------------------------


def test_a_group_by_subquery_joined_on_its_full_grouping_key_is_proved(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.customer_id
        """
    )

    assert report.proved


def test_a_group_by_subquery_joined_on_part_of_a_composite_grouping_key_is_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer_region AS (
            SELECT customer_id, region, COUNT(*) AS n FROM orders GROUP BY customer_id, region
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer_region
            ON customers.id = per_customer_region.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_an_aliased_grouping_key_maps_the_unique_set_to_the_outer_column(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT customer_id AS cid, COUNT(*) AS n FROM orders GROUP BY customer_id
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.cid
        """
    )

    assert report.proved


def test_joining_on_an_aggregate_expression_instead_of_a_grouping_key_is_unknown(orders_and_customers):
    """`n` is a count, not a Grouping Key -- it must never be treated as a
    member of the Unique Set an Aggregate earns."""
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.n
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_where_clause_before_group_by_does_not_block_the_proof(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT customer_id, COUNT(*) AS n
            FROM orders
            WHERE amount > 0
            GROUP BY customer_id
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.customer_id
        """
    )

    assert report.proved


def test_a_grouping_key_absent_from_the_select_list_is_unsupported_and_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT COUNT(*) AS n FROM orders GROUP BY customer_id
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.n
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_rollup_is_unsupported_and_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id WITH ROLLUP
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_grouping_sets_is_unsupported_and_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY GROUPING SETS ((customer_id))
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_having_keeps_the_grouping_key(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH per_customer AS (
            SELECT customer_id, COUNT(*) AS n
            FROM orders
            GROUP BY customer_id
            HAVING COUNT(*) > 1
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN per_customer
            ON customers.id = per_customer.customer_id
        """
    )

    # HAVING only removes groups, so customer_id stays a key of per_customer.
    assert [assertion.outcome for assertion in report.assertions] == [Outcome.PROVED]


# Distinct ---------------------------------------------------------------


def test_a_distinct_subquery_joined_on_its_full_output_set_is_proved(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH distinct_customers AS (
            SELECT DISTINCT customer_id FROM orders
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN distinct_customers
            ON customers.id = distinct_customers.customer_id
        """
    )

    assert report.proved


def test_a_distinct_subquery_joined_on_part_of_its_output_set_is_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH distinct_pairs AS (
            SELECT DISTINCT customer_id, region FROM orders
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN distinct_pairs
            ON customers.id = distinct_pairs.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


# DISTINCT ON ----------------------------------------------------------------
#
# `DISTINCT ON (k...)` keeps one row per distinct `k...`, so it earns the same
# Unique Set as `QUALIFY ROW_NUMBER() OVER (PARTITION BY k...) = 1`: its
# complete set of ON keys, whatever else it selects.


@pytest.mark.parametrize("dialect", ["postgres", "duckdb"])
def test_a_distinct_on_subquery_joined_on_its_full_on_key_is_proved(orders_and_customers, dialect):
    report = analyze(
        orders_and_customers
        + """
        WITH first_per_customer AS (
            SELECT DISTINCT ON (customer_id) customer_id, region, amount FROM orders ORDER BY customer_id, amount DESC
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN first_per_customer
            ON customers.id = first_per_customer.customer_id
        """,
        dialect=dialect,
    )

    assert report.proved


def test_a_distinct_on_subquery_joined_on_part_of_a_composite_on_key_is_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH first_per_customer_region AS (
            SELECT DISTINCT ON (customer_id, region) customer_id, region, amount FROM orders
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN first_per_customer_region
            ON customers.id = first_per_customer_region.customer_id
        """,
        dialect="postgres",
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_distinct_on_view_reports_its_on_key_as_its_unique_set():
    report = analyze(
        """
        CREATE TABLE orders (customer_id INTEGER, region VARCHAR, amount INTEGER);
        CREATE VIEW first_per_customer_region AS
            SELECT DISTINCT ON (region, customer_id) customer_id AS cid, region, amount FROM orders WHERE amount > 0;

        SELECT * FROM first_per_customer_region
        """,
        dialect="postgres",
    )

    assert report.facts.unique_sets("first_per_customer_region") == (("cid", "region"),)


def test_a_distinct_on_key_absent_from_the_select_list_is_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH first_per_region AS (
            SELECT DISTINCT ON (region) customer_id, amount FROM orders
        )
        SELECT *
        FROM customers
        /**ASSERT MANY TO ONE**/ JOIN first_per_region
            ON customers.id = first_per_region.customer_id
        """,
        dialect="postgres",
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


# Nesting ----------------------------------------------------------------


def test_an_aggregate_nested_inside_a_distinct_still_earns_its_own_unique_set():
    """Regression test for a traversal bug the code review for #7 caught:
    `ir.aggregates`/`ir.distincts` originally stopped at a plan of the
    *other* kind instead of recursing into its `.input`, so an Aggregate
    reachable only through a wrapping Distinct silently lost its own Unique
    Set fact. Naming both layers as views (rather than CTEs) makes the inner
    one directly inspectable through `report.facts`, independent of whatever
    the outer Distinct's own Unique Set happens to require.
    """
    report = analyze(
        """
        CREATE TABLE orders (customer_id INTEGER, amount INTEGER);
        CREATE VIEW per_customer AS SELECT customer_id, COUNT(*) AS n FROM orders GROUP BY customer_id;
        CREATE VIEW distinct_wrap AS SELECT DISTINCT customer_id, n FROM per_customer;

        SELECT * FROM distinct_wrap
        """
    )

    assert report.facts.unique_sets("per_customer") == (("customer_id",),)
    # `customer_id` determines `n` in `per_customer`, so it alone is the minimal key.
    assert report.facts.unique_sets("distinct_wrap") == (("customer_id",),)
    assert report.facts.is_unique("distinct_wrap", ["customer_id", "n"])


# Scalar aggregates --------------------------------------------------------


# SELECT without FROM ------------------------------------------------------
