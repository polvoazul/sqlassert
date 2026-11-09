create table users (id integer primary key, name varchar);
create table coupons (id integer primary key, code varchar);
create table orders (id integer primary key, user_id integer not null references users(id), coupon_id integer references coupons(id));

select o.id, u.name
from orders o
left join users u on u.id = o.user_id
left join coupons c on c.id = o.coupon_id
order by u.name
limit 10;
