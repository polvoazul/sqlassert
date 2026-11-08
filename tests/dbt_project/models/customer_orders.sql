{{ config(materialized='ephemeral') }}

select customer_id, count(*) as n_orders
from {{ ref('stg_orders') }}
group by customer_id
/**ASSERT UNIQUE(customer_id)**/
