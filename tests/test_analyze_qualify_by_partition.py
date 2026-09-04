"""Unique Sets derived from recognized `ROW_NUMBER() OVER (PARTITION BY ...) = 1`
QualifyByPartition qualification.

Only this narrow shape earns a Unique Set, by its complete Partition Key,
because that predicate keeps exactly one row per partition. Every case here
wraps the qualified query in a CTE, exercising #5's lowering together with
this recognition, exactly like `test_analyze_aggregate_and_distinct.py` does
for GROUP BY and DISTINCT.
"""

from __future__ import annotations

import pytest

from sqlassert import Outcome, analyze


@pytest.fixture
def orders_and_customers() -> str:
    """`orders(customer_id, order_id, amount)`, joined against by `customers(id)`."""
    return """
    CREATE TABLE orders (customer_id INTEGER, order_id INTEGER, amount INTEGER);
    CREATE TABLE customers (id INTEGER);
    """


def test_a_qualify_by_partition_subquery_joined_on_its_full_partition_key_is_proved(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH latest_order AS (
            SELECT customer_id, order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN latest_order
            ON customers.id = latest_order.customer_id
        """
    )

    assert report.proved


def test_a_qualify_by_partition_subquery_joined_on_its_full_composite_partition_key_is_proved():
    report = analyze(
        """
        CREATE TABLE orders (customer_id INTEGER, region VARCHAR, order_id INTEGER, amount INTEGER);
        CREATE TABLE customer_regions (customer_id INTEGER, region VARCHAR);

        WITH latest_order AS (
            SELECT customer_id, region, order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id, region ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customer_regions
        /**UNIQUE**/ JOIN latest_order
            ON customer_regions.customer_id = latest_order.customer_id
            AND customer_regions.region = latest_order.region
        """
    )

    assert report.proved


def test_joining_on_part_of_a_composite_partition_key_is_unknown():
    report = analyze(
        """
        CREATE TABLE orders (customer_id INTEGER, region VARCHAR, order_id INTEGER, amount INTEGER);
        CREATE TABLE customer_regions (customer_id INTEGER, region VARCHAR);

        WITH latest_order AS (
            SELECT customer_id, region, order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id, region ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customer_regions
        /**UNIQUE**/ JOIN latest_order
            ON customer_regions.customer_id = latest_order.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_an_aliased_partition_key_maps_the_unique_set_to_the_outer_column(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH latest_order AS (
            SELECT customer_id AS cid, order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN latest_order
            ON customers.id = latest_order.cid
        """
    )

    assert report.proved


def test_a_partition_key_absent_from_the_select_list_is_unsupported_and_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH latest_order AS (
            SELECT order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN latest_order
            ON customers.id = latest_order.order_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_qualification_that_can_retain_more_than_one_row_per_partition_is_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH top_two_orders AS (
            SELECT customer_id, order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id DESC) <= 2
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN top_two_orders
            ON customers.id = top_two_orders.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_rank_instead_of_row_number_is_unsupported_and_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH latest_order AS (
            SELECT customer_id, order_id, amount
            FROM orders
            QUALIFY RANK() OVER (PARTITION BY customer_id ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN latest_order
            ON customers.id = latest_order.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_qualify_predicate_with_no_window_function_is_unsupported_and_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH big_orders AS (
            SELECT customer_id, order_id, amount
            FROM orders
            QUALIFY amount > 100
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN big_orders
            ON customers.id = big_orders.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_row_number_with_no_partition_by_is_unsupported_and_unknown(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH first_order AS (
            SELECT customer_id, order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN first_order
            ON customers.id = first_order.customer_id
        """
    )

    assert [assertion.outcome for assertion in report.assertions] == [Outcome.UNKNOWN]


def test_a_where_clause_before_qualify_does_not_block_the_proof(orders_and_customers):
    report = analyze(
        orders_and_customers
        + """
        WITH latest_order AS (
            SELECT customer_id, order_id, amount
            FROM orders
            WHERE amount > 0
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id DESC) = 1
        )
        SELECT *
        FROM customers
        /**UNIQUE**/ JOIN latest_order
            ON customers.id = latest_order.customer_id
        """
    )

    assert report.proved


def test_a_qualify_by_partition_view_reports_its_own_unique_set():
    report = analyze(
        """
        CREATE TABLE orders (customer_id INTEGER, order_id INTEGER, amount INTEGER);
        CREATE VIEW latest_order AS
            SELECT customer_id, order_id, amount
            FROM orders
            QUALIFY ROW_NUMBER() OVER (PARTITION BY customer_id ORDER BY order_id DESC) = 1;

        SELECT * FROM latest_order
        """
    )

    assert report.facts.unique_sets("latest_order") == (("customer_id",),)
