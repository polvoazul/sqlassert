CREATE TABLE orders (id INT PRIMARY KEY, coupon_id INT);
CREATE TABLE coupons (id INT PRIMARY KEY);

SELECT o.id
FROM orders o
LEFT JOIN coupons c ON c.id = o.coupon_id;
