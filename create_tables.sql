-- Users table
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    username VARCHAR(100) UNIQUE NOT NULL,
    password VARCHAR(100) NOT NULL,
    role VARCHAR(10) CHECK (role IN ('buyer', 'supplier')) NOT NULL
);

-- Auctions table
CREATE TABLE IF NOT EXISTS auctions (
    id SERIAL PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    base_price NUMERIC(12, 2) NOT NULL,
    min_decrement NUMERIC(12, 2) NOT NULL,
    current_price NUMERIC(12, 2),
    duration_minutes INTEGER NOT NULL,
    status VARCHAR(20) CHECK (status IN ('not_started', 'running', 'ended')) DEFAULT 'not_started',
    start_time TIMESTAMP,
    end_time TIMESTAMP,
    created_by INTEGER REFERENCES users(id)
);

-- Bids table
CREATE TABLE IF NOT EXISTS bids (
    id SERIAL PRIMARY KEY,
    auction_id INTEGER REFERENCES auctions(id),
    supplier_id INTEGER REFERENCES users(id),
    bid_amount NUMERIC(12, 2) NOT NULL,
    bid_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
