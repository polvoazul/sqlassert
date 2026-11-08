{{ config(materialized='table') }}

select id as customer_id, code as signup_code
from {{ source('raw', 'signups') }}
