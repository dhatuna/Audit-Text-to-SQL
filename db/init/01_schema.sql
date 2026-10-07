-- Synthetic investment-portfolio schema. All data is generated; no real clients or prices.

CREATE TABLE clients (
    client_id      INTEGER PRIMARY KEY,
    full_name      TEXT NOT NULL,          -- PII: not exposed to the query role
    email          TEXT NOT NULL,          -- PII: not exposed
    tax_id         TEXT NOT NULL,          -- PII: not exposed
    risk_profile   TEXT NOT NULL CHECK (risk_profile IN ('conservative', 'moderate', 'aggressive')),
    onboarded_on   DATE NOT NULL
);

CREATE TABLE portfolios (
    portfolio_id    INTEGER PRIMARY KEY,
    client_id       INTEGER NOT NULL REFERENCES clients,
    name            TEXT NOT NULL UNIQUE,
    strategy        TEXT NOT NULL CHECK (strategy IN ('growth', 'value', 'income', 'balanced', 'index')),
    inception_date  DATE NOT NULL
);

CREATE TABLE accounts (
    account_id     INTEGER PRIMARY KEY,
    portfolio_id   INTEGER NOT NULL REFERENCES portfolios,
    account_type   TEXT NOT NULL CHECK (account_type IN ('taxable', 'ira', 'trust')),
    custodian      TEXT NOT NULL,
    opened_on      DATE NOT NULL
);

CREATE TABLE securities (
    security_id   INTEGER PRIMARY KEY,
    ticker        TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    asset_class   TEXT NOT NULL CHECK (asset_class IN ('equity', 'etf', 'fixed_income')),
    sector        TEXT                     -- NULL for ETFs and fixed income
);

CREATE TABLE daily_prices (
    security_id  INTEGER NOT NULL REFERENCES securities,
    price_date   DATE NOT NULL,
    close        NUMERIC(14, 4) NOT NULL CHECK (close > 0),
    PRIMARY KEY (security_id, price_date)
);

CREATE TABLE trades (
    trade_id     BIGINT PRIMARY KEY,
    account_id   INTEGER NOT NULL REFERENCES accounts,
    security_id  INTEGER NOT NULL REFERENCES securities,
    trade_date   DATE NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity     NUMERIC(18, 4) NOT NULL CHECK (quantity > 0),
    price        NUMERIC(14, 4) NOT NULL CHECK (price > 0),
    fees         NUMERIC(10, 2) NOT NULL DEFAULT 0 CHECK (fees >= 0)
);
CREATE INDEX trades_account_date ON trades (account_id, trade_date);
CREATE INDEX trades_security_date ON trades (security_id, trade_date);

-- End-of-day holdings per account, derived from trades by the seeder.
CREATE TABLE positions_eod (
    account_id    INTEGER NOT NULL REFERENCES accounts,
    security_id   INTEGER NOT NULL REFERENCES securities,
    as_of_date    DATE NOT NULL,
    quantity      NUMERIC(18, 4) NOT NULL CHECK (quantity > 0),
    market_value  NUMERIC(18, 2) NOT NULL,
    cost_basis    NUMERIC(18, 2) NOT NULL,
    PRIMARY KEY (account_id, security_id, as_of_date)
);
CREATE INDEX positions_date ON positions_eod (as_of_date);

CREATE TABLE cash_flows (
    flow_id     BIGINT PRIMARY KEY,
    account_id  INTEGER NOT NULL REFERENCES accounts,
    flow_date   DATE NOT NULL,
    flow_type   TEXT NOT NULL CHECK (flow_type IN ('deposit', 'withdrawal', 'dividend', 'fee')),
    amount      NUMERIC(18, 2) NOT NULL    -- positive = into the account
);
CREATE INDEX cash_flows_account_date ON cash_flows (account_id, flow_date);
