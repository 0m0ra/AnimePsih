-- Test schema for isolation anomaly demos
DROP TABLE IF EXISTS accounts;
CREATE TABLE accounts (
    id      INTEGER PRIMARY KEY,
    owner   TEXT    NOT NULL,
    balance INTEGER NOT NULL
);

INSERT INTO accounts (id, owner, balance) VALUES
    (1, 'Alice',  1000),
    (2, 'Bob',     500),
    (3, 'Carol',   200);

DROP TABLE IF EXISTS products;
CREATE TABLE products (
    id       SERIAL PRIMARY KEY,
    category TEXT    NOT NULL,
    name     TEXT    NOT NULL,
    price    INTEGER NOT NULL
);

INSERT INTO products (category, name, price) VALUES
    ('books',       'SQL Internals',     100),
    ('books',       'Designing DB',      150),
    ('books',       'Postgres Up&Running', 200),
    ('electronics', 'Laptop',            500),
    ('electronics', 'Phone',             800);