select customer_id, signup_code, upper(signup_code) as label
from {{ ref('customer_keys') }}
