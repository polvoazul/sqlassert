CREATE TABLE customers (id INT PRIMARY KEY, name VARCHAR);
CREATE TABLE orders (
    id INT PRIMARY KEY,
    customer_id INT NOT NULL REFERENCES customers(id)
);

SELECT o.id, c.name
FROM orders o
LEFT JOIN customers c ON c.id = o.customer_id;
