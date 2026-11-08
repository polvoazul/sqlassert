{{ config(materialized='table') }}

with customers as (
    select * from {{ ref('stg_customers') }}
),
orders as (
    select * from {{ ref('customer_orders') }}
)
select customers.customer_id, customers.first_name, coalesce(orders.n_orders, 0) as n_orders
from customers
/**ASSERT MANY TO ONE**/ left join orders on orders.customer_id = customers.customer_id
/**ASSERT NOT NULL(first_name)**/
