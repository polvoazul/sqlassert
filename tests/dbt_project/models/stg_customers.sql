{# Customers as the source delivers them. #}
select id as customer_id, first_name
from {{ source('raw', 'customers') }}
