{{ config(materialized='incremental') }}

select order_id, status
from {{ ref('stg_orders') }}
{% if is_incremental() %}
where order_id > (select max(order_id) from {{ this }})
{% endif %}
