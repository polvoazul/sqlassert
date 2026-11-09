create table orders (id integer primary key, status varchar not null);
with o as (
  select * from orders
  /**DECLARE ENUM(status IN ('new', 'paid', 'void', 'refunded'))**/
)
select id,
  /**ASSERT EXHAUSTIVE**/ case status when 'new' then 'open' when 'paid' then 'closed' when 'void' then 'closed' end as state
from o;
