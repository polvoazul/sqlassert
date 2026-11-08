select customer_id, first_name
from {{ ref('stg_customers') }}
where customer_id > {{ var('min_id', 0) }}
/**ASSERT UNIQUE(customer_id)**/
