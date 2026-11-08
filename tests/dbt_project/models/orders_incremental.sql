{{ config(materialized='incremental', unique_key='order_id', incremental_strategy='merge') }}

select order_id, customer_id, status
from {{ ref('stg_orders') }}
{% if is_incremental() %}
where order_id > (select max(order_id) from {{ this }})
{% endif %}
/**ASSERT UNIQUE(order_id)**/
