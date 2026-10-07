#!/usr/bin/env bash
# Database roles. Runs once, on first container start, as the bootstrap superuser.
#
#   nl2sql_reader  executes model-generated SQL. Column-level SELECT on the exposed
#                  columns only, read-only transactions, hard statement timeout.
#   nl2sql_app     writes the audit log. INSERT/SELECT on audit.query_log, nothing else.
#
# The column list here must match config/semantic_layer.yaml; an integration test
# (tests/integration/test_db_permissions.py) fails if they drift apart.
set -euo pipefail

psql -v ON_ERROR_STOP=1 \
     -v reader_pw="${READER_PASSWORD:?}" \
     -v app_pw="${APP_PASSWORD:?}" \
     -v dbname="${POSTGRES_DB:?}" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'EOSQL'

-- Nobody gets anything by default.
REVOKE ALL ON DATABASE :"dbname" FROM PUBLIC;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE ROLE nl2sql_reader LOGIN PASSWORD :'reader_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION CONNECTION LIMIT 20;
ALTER ROLE nl2sql_reader SET default_transaction_read_only = on;
ALTER ROLE nl2sql_reader SET statement_timeout = '5s';
ALTER ROLE nl2sql_reader SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE nl2sql_reader SET work_mem = '32MB';
ALTER ROLE nl2sql_reader SET search_path = public;

GRANT CONNECT ON DATABASE :"dbname" TO nl2sql_reader;
GRANT USAGE ON SCHEMA public TO nl2sql_reader;

GRANT SELECT (client_id, risk_profile, onboarded_on) ON clients TO nl2sql_reader;
GRANT SELECT (portfolio_id, client_id, name, strategy, inception_date) ON portfolios TO nl2sql_reader;
GRANT SELECT (account_id, portfolio_id, account_type, custodian, opened_on) ON accounts TO nl2sql_reader;
GRANT SELECT (security_id, ticker, name, asset_class, sector) ON securities TO nl2sql_reader;
GRANT SELECT (security_id, price_date, close) ON daily_prices TO nl2sql_reader;
GRANT SELECT (trade_id, account_id, security_id, trade_date, side, quantity, price, fees)
    ON trades TO nl2sql_reader;
GRANT SELECT (account_id, security_id, as_of_date, quantity, market_value, cost_basis)
    ON positions_eod TO nl2sql_reader;
GRANT SELECT (flow_id, account_id, flow_date, flow_type, amount) ON cash_flows TO nl2sql_reader;

CREATE ROLE nl2sql_app LOGIN PASSWORD :'app_pw'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION;
GRANT CONNECT ON DATABASE :"dbname" TO nl2sql_app;
GRANT USAGE ON SCHEMA audit TO nl2sql_app;
GRANT INSERT, SELECT ON audit.query_log TO nl2sql_app;
EOSQL
