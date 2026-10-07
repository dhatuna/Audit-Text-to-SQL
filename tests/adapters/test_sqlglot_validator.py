"""Adversarial tests for the static validator. No database, no LLM.

Each rejected case names the rule that must fire, so a regression that blocks a query for
the wrong reason is caught too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from auditable_sql.domain.policy import SqlPolicy
from auditable_sql.infrastructure.context.semantic_layer import SemanticLayer
from auditable_sql.infrastructure.validation.sqlglot_validator import SqlglotValidator

ROOT = Path(__file__).resolve().parents[2]
MAX_ROWS = 500


@pytest.fixture(scope="module")
def validator() -> SqlglotValidator:
    layer = SemanticLayer.load(ROOT / "config" / "semantic_layer.yaml")
    return SqlglotValidator(layer.policy(MAX_ROWS))


ALLOWED = [
    "SELECT SUM(market_value) FROM positions_eod "
    "WHERE as_of_date = (SELECT MAX(as_of_date) FROM positions_eod)",
    "SELECT p.name, SUM(pe.market_value) AS aum FROM positions_eod pe "
    "JOIN accounts a ON a.account_id = pe.account_id "
    "JOIN portfolios p ON p.portfolio_id = a.portfolio_id GROUP BY p.name ORDER BY aum DESC",
    "SELECT ticker AS tk FROM securities ORDER BY tk",
    "WITH x AS (SELECT ticker FROM securities) SELECT ticker FROM x",
    "WITH x(sym) AS (SELECT ticker FROM securities) SELECT sym FROM x",
    "SELECT * FROM (SELECT ticker, sector FROM securities) s",
    "SELECT t.ticker FROM securities t WHERE EXISTS "
    "(SELECT 1 FROM trades x WHERE x.security_id = t.security_id)",
    "SELECT ticker FROM securities WHERE security_id IN (SELECT security_id FROM trades)",
    "SELECT ticker FROM securities UNION SELECT name FROM portfolios",
    "SELECT COUNT(*) FROM trades",
    "SELECT risk_profile, COUNT(*) FROM clients GROUP BY risk_profile",
    "SELECT ticker FROM public.securities",
    "SELECT security_id, price_date, close, "
    "LAG(close) OVER (PARTITION BY security_id ORDER BY price_date) AS prev FROM daily_prices",
    "SELECT DATE_TRUNC('month', trade_date) AS m, SUM(quantity * price) FROM trades GROUP BY 1",
    "SELECT EXTRACT(YEAR FROM trade_date) AS y FROM trades",
    "SELECT ticker FROM securities WHERE ticker = 'A'' OR 1=1 --'",  # just a string literal
    "SELECT ticker FROM securities s JOIN LATERAL "
    "(SELECT MAX(close) AS hi FROM daily_prices d WHERE d.security_id = s.security_id) h ON TRUE",
    "SELECT ticker -- trailing comment\nFROM securities",
]


@pytest.mark.parametrize("sql", ALLOWED)
def test_legitimate_queries_pass(validator: SqlglotValidator, sql: str) -> None:
    result = validator.validate(sql)
    assert result.passed, result.violations


REJECTED = [
    # --- not a single SELECT
    ("SELEC 1", "R001_PARSE_ERROR"),
    ("SELECT 1; DROP TABLE trades", "R002_STATEMENT_COUNT"),
    ("SELECT 1; SELECT 2", "R002_STATEMENT_COUNT"),
    ("", "R002_STATEMENT_COUNT"),
    ("UPDATE trades SET fees = 0", "R003_NOT_SELECT"),
    ("DELETE FROM trades", "R003_NOT_SELECT"),
    ("INSERT INTO trades VALUES (1)", "R003_NOT_SELECT"),
    ("DROP TABLE trades", "R003_NOT_SELECT"),
    ("COPY trades TO '/tmp/x'", "R003_NOT_SELECT"),
    ("SET statement_timeout = 0", "R003_NOT_SELECT"),
    ("GRANT SELECT ON clients TO PUBLIC", "R003_NOT_SELECT"),
    ("BEGIN", "R003_NOT_SELECT"),
    # --- writes hidden inside a SELECT
    (
        "WITH d AS (DELETE FROM trades RETURNING trade_id) SELECT trade_id FROM d",
        "R004_FORBIDDEN_CLAUSE",
    ),
    (
        "WITH u AS (UPDATE trades SET fees = 0 RETURNING trade_id) SELECT trade_id FROM u",
        "R004_FORBIDDEN_CLAUSE",
    ),
    ("SELECT trade_id INTO stolen FROM trades", "R004_FORBIDDEN_CLAUSE"),
    ("SELECT trade_id FROM trades FOR UPDATE", "R004_FORBIDDEN_CLAUSE"),
    # --- tables outside the policy
    ("SELECT request_id FROM audit.query_log", "R005_TABLE_NOT_ALLOWED"),
    ("SELECT table_name FROM information_schema.tables", "R005_TABLE_NOT_ALLOWED"),
    ("SELECT rolname FROM pg_catalog.pg_roles", "R005_TABLE_NOT_ALLOWED"),
    ("SELECT usename FROM pg_user", "R005_TABLE_NOT_ALLOWED"),
    ("SELECT ticker FROM other.securities", "R005_TABLE_NOT_ALLOWED"),
    ("SELECT ticker FROM otherdb.public.securities", "R005_TABLE_NOT_ALLOWED"),
    ("SELECT x FROM (SELECT request_id AS x FROM audit.query_log) q", "R005_TABLE_NOT_ALLOWED"),
    # --- hidden (PII) columns, by every route we could think of
    ("SELECT tax_id FROM clients", "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT c.tax_id FROM clients c", "R006_COLUMN_NOT_ALLOWED"),
    ('SELECT c."tax_id" FROM clients c', "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT TAX_ID FROM CLIENTS", "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT client_id FROM clients WHERE email LIKE '%@%'", "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT client_id FROM clients ORDER BY full_name", "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT (SELECT tax_id FROM clients LIMIT 1) AS t", "R006_COLUMN_NOT_ALLOWED"),
    ("WITH c AS (SELECT email FROM clients) SELECT email FROM c", "R006_COLUMN_NOT_ALLOWED"),
    (
        "SELECT ticker FROM securities s JOIN LATERAL (SELECT email FROM clients) c ON TRUE",
        "R006_COLUMN_NOT_ALLOWED",
    ),
    (
        "SELECT p.name FROM portfolios p WHERE EXISTS (SELECT 1 FROM clients c WHERE c.tax_id = '1')",
        "R006_COLUMN_NOT_ALLOWED",
    ),
    ("SELECT email FROM clients, (SELECT 1 AS z) d", "R006_COLUMN_NOT_ALLOWED"),
    # a CTE resolving a column against its own output (found while building this suite)
    ("WITH c AS (SELECT c.email FROM clients) SELECT email FROM c", "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT email FROM (SELECT email FROM clients) c", "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT ctid FROM trades", "R006_COLUMN_NOT_ALLOWED"),  # system column
    ("SELECT x.nope FROM trades t", "R006_COLUMN_NOT_ALLOWED"),
    ("SELECT nope FROM (SELECT ticker FROM securities) s", "R006_COLUMN_NOT_ALLOWED"),
    # --- reads that would expand to hidden columns
    ("SELECT * FROM clients", "R007_SELECT_STAR"),
    ("SELECT c.* FROM clients c", "R007_SELECT_STAR"),
    ("SELECT c FROM clients c", "R009_WHOLE_ROW_REFERENCE"),
    ("SELECT row_to_json(c) FROM clients c", "R009_WHOLE_ROW_REFERENCE"),
    ("SELECT to_jsonb(clients) FROM clients", "R009_WHOLE_ROW_REFERENCE"),
    ("SELECT b FROM clients AS c(a, b)", "R011_COLUMN_ALIAS_LIST"),
    ("SELECT risk_profile FROM clients AS c(client_id, risk_profile)", "R011_COLUMN_ALIAS_LIST"),
    # --- dangerous or non-deterministic functions
    ("SELECT pg_sleep(10)", "R008_FUNCTION_DENIED"),
    ("SELECT pg_catalog.pg_sleep(10)", "R008_FUNCTION_DENIED"),
    ("SELECT PG_SLEEP(10)", "R008_FUNCTION_DENIED"),
    ("SELECT pg_read_file('/etc/passwd')", "R008_FUNCTION_DENIED"),
    ("SELECT set_config('statement_timeout', '0', false)", "R008_FUNCTION_DENIED"),
    ("SELECT current_setting('is_superuser')", "R008_FUNCTION_DENIED"),
    ("SELECT dblink('host=evil', 'select 1')", "R008_FUNCTION_DENIED"),
    (
        "SELECT trade_id FROM trades WHERE trade_date > now() - INTERVAL '30 days'",
        "R008_FUNCTION_DENIED",
    ),
    ("SELECT trade_id FROM trades WHERE trade_date = CURRENT_DATE", "R008_FUNCTION_DENIED"),
    ("SELECT trade_id FROM trades WHERE trade_date = CURRENT_TIMESTAMP", "R008_FUNCTION_DENIED"),
    ("SELECT ticker FROM securities ORDER BY random()", "R008_FUNCTION_DENIED"),
    ("SELECT ticker FROM securities ORDER BY RANDOM()", "R008_FUNCTION_DENIED"),
    # --- table functions
    ("SELECT s FROM generate_series(1, 10) AS s", "R010_TABLE_FUNCTION"),
    ("SELECT * FROM pg_ls_dir('.')", "R008_FUNCTION_DENIED"),
]


@pytest.mark.parametrize(("sql", "rule"), REJECTED)
def test_attacks_are_rejected_for_the_right_reason(
    validator: SqlglotValidator, sql: str, rule: str
) -> None:
    result = validator.validate(sql)
    assert not result.passed
    assert result.normalized_sql is None
    assert rule in {v.rule_id for v in result.violations}, result.violations


class TestLimitEnforcement:
    cap = MAX_ROWS + 1  # one extra row lets the executor detect truncation

    def test_adds_limit_when_missing(self, validator: SqlglotValidator) -> None:
        r = validator.validate("SELECT ticker FROM securities")
        assert r.normalized_sql == f"SELECT ticker FROM securities LIMIT {self.cap}"

    def test_keeps_smaller_limit(self, validator: SqlglotValidator) -> None:
        r = validator.validate("SELECT ticker FROM securities LIMIT 10")
        assert r.normalized_sql == "SELECT ticker FROM securities LIMIT 10"

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT ticker FROM securities LIMIT 100000",
            "SELECT ticker FROM securities FETCH FIRST 5000 ROWS ONLY",
            "SELECT ticker FROM securities LIMIT (SELECT COUNT(*) FROM trades)",
            "SELECT ticker FROM securities LIMIT ALL",
        ],
    )
    def test_caps_large_or_dynamic_limits(self, validator: SqlglotValidator, sql: str) -> None:
        r = validator.validate(sql)
        assert r.normalized_sql is not None
        assert r.normalized_sql.endswith(f"LIMIT {self.cap}")

    def test_union_is_capped_as_a_whole(self, validator: SqlglotValidator) -> None:
        r = validator.validate(
            "SELECT ticker FROM securities UNION ALL SELECT name FROM portfolios"
        )
        assert r.normalized_sql is not None and r.normalized_sql.endswith(f"LIMIT {self.cap}")

    def test_inner_limits_are_untouched(self, validator: SqlglotValidator) -> None:
        r = validator.validate("SELECT ticker FROM (SELECT ticker FROM securities LIMIT 5000) s")
        assert r.normalized_sql is not None and "LIMIT 5000" in r.normalized_sql


def test_violation_messages_are_actionable(validator: SqlglotValidator) -> None:
    r = validator.validate("SELECT c.tax_id FROM clients c")
    assert r.violations[0].message == "column 'clients.tax_id' is not readable"


def test_custom_policy_is_honoured() -> None:
    v = SqlglotValidator(SqlPolicy(allowed_columns={"trades": frozenset({"trade_id"})}, max_rows=5))
    assert (
        v.validate("SELECT trade_id FROM trades").normalized_sql
        == "SELECT trade_id FROM trades LIMIT 6"
    )
    assert not v.validate("SELECT fees FROM trades").passed
    assert not v.validate("SELECT ticker FROM securities").passed
