select id as order_id, user_id as customer_id, status, amount
from {{ source('raw', 'orders') }}
