CREATE TABLE orders (id INT PRIMARY KEY);
CREATE TABLE payments (id INT PRIMARY KEY, order_id INT, amount INT);
CREATE TABLE refunds (id INT PRIMARY KEY, order_id INT);

SELECT o.id, SUM(p.amount) AS paid
FROM orders o
JOIN payments p ON p.order_id = o.id
JOIN refunds r ON r.order_id = o.id
GROUP BY o.id;
