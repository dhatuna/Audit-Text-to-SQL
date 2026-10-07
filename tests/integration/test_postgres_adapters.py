"""Executor and audit repository against a real PostgreSQL."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from auditable_sql.bootstrap.settings import Settings
from auditable_sql.domain.errors import AuditWriteError, QueryExecutionError
from auditable_sql.domain.models import (
    Answer,
    Attempt,
    AuditRecord,
    Decision,
    Generation,
    GroundingReport,
    Outcome,
    ValidationResult,
    Violation,
)
from auditable_sql.domain.results import fingerprint
from auditable_sql.infrastructure.db.postgres_audit import PostgresAuditRepository
from auditable_sql.infrastructure.db.postgres_executor import PostgresExecutor
from tests.fakes import CONTEXT


@pytest.fixture
def executor(settings: Settings) -> PostgresExecutor:
    return PostgresExecutor(
        settings.reader_dsn,
        max_rows=settings.max_rows,
        statement_timeout_ms=settings.statement_timeout_ms,
    )


class TestExecutor:
    def test_returns_typed_rows(self, executor: PostgresExecutor) -> None:
        r = executor.execute(
            "SELECT ticker, close, price_date FROM daily_prices JOIN securities USING (security_id) "
            "ORDER BY price_date, ticker LIMIT 1"
        )
        assert r.columns == ("ticker", "close", "price_date")
        ticker, close, d = r.rows[0]
        assert isinstance(ticker, str) and isinstance(close, Decimal) and isinstance(d, date)
        assert not r.truncated and r.elapsed_ms >= 0

    def test_detects_truncation(self, executor: PostgresExecutor, settings: Settings) -> None:
        r = executor.execute(f"SELECT trade_id FROM trades LIMIT {settings.max_rows + 1}")
        assert r.truncated and r.row_count == settings.max_rows

    def test_exact_max_rows_is_not_truncated(
        self, executor: PostgresExecutor, settings: Settings
    ) -> None:
        r = executor.execute(f"SELECT trade_id FROM trades LIMIT {settings.max_rows}")
        assert not r.truncated and r.row_count == settings.max_rows

    def test_timeout_becomes_domain_error(self, executor: PostgresExecutor) -> None:
        with pytest.raises(QueryExecutionError, match="57014"):
            executor.execute("SELECT pg_sleep(5)")

    def test_permission_error_becomes_domain_error(self, executor: PostgresExecutor) -> None:
        with pytest.raises(QueryExecutionError, match="42501"):
            executor.execute("SELECT tax_id FROM clients")

    def test_write_is_refused_even_if_validator_were_bypassed(
        self, executor: PostgresExecutor
    ) -> None:
        with pytest.raises(QueryExecutionError, match="25006"):  # read_only_sql_transaction
            executor.execute("WITH d AS (DELETE FROM trades RETURNING 1) SELECT * FROM d")

    def test_statement_timeout_cannot_be_raised_by_query(self, executor: PostgresExecutor) -> None:
        # Even a multi-statement string (which the validator rejects) cannot lift the limit:
        # SET LOCAL is scoped to our transaction and re-applied on every call.
        with pytest.raises(QueryExecutionError):
            executor.execute("SET statement_timeout = 0; SELECT pg_sleep(5)")

    def test_like_with_percent_is_not_treated_as_placeholder(
        self, executor: PostgresExecutor
    ) -> None:
        r = executor.execute("SELECT COUNT(*) AS n FROM securities WHERE name LIKE '%ETF%'")
        assert r.rows[0][0] >= 0  # type: ignore[operator]


def _record(rid: str, executor: PostgresExecutor) -> AuditRecord:
    result = executor.execute(
        "SELECT ticker, close, price_date FROM daily_prices "
        "JOIN securities USING (security_id) ORDER BY 3, 1 LIMIT 3"
    )
    gen = Generation(
        Decision.ANSWER, "lookup", sql="SELECT ...", assumptions=("latest date",),
        model="claude-x", raw={"decision": "answer"}, input_tokens=10, output_tokens=5,
    )  # fmt: skip
    blocked = Attempt(
        1,
        replace(gen, sql="SELECT tax_id FROM clients"),
        ValidationResult((Violation("R006_COLUMN_NOT_ALLOWED", "nope"),), None),
    )
    ok = Attempt(2, gen, ValidationResult((), "SELECT ... LIMIT 51"), result=result)
    return AuditRecord(
        request_id=rid,
        created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        user_label="it",
        question="What were the first prices?",
        context=CONTEXT,
        prompt_version="p1",
        attempts=(blocked, ok),
        outcome=Outcome.ANSWERED,
        final_sql="SELECT ... LIMIT 51",
        result_fingerprint=fingerprint(result),
        answer=Answer("Three prices.", model="claude-x", input_tokens=3, output_tokens=2),
        grounding=GroundingReport(("3",), ()),
        total_latency_ms=12.5,
    )


class TestAuditRepository:
    def test_round_trip_is_lossless(self, settings: Settings, executor: PostgresExecutor) -> None:
        repo = PostgresAuditRepository(settings.app_dsn)
        record = _record("rt-1", executor)

        repo.save(record)
        loaded = repo.get("rt-1")

        assert loaded == record
        assert loaded is not None and loaded.attempts[1].result is not None
        assert fingerprint(loaded.attempts[1].result) == record.result_fingerprint

    def test_duplicate_id_is_rejected(self, settings: Settings, executor: PostgresExecutor) -> None:
        repo = PostgresAuditRepository(settings.app_dsn)
        repo.save(_record("dup-1", executor))
        with pytest.raises(AuditWriteError):
            repo.save(_record("dup-1", executor))

    def test_unknown_id(self, settings: Settings) -> None:
        assert PostgresAuditRepository(settings.app_dsn).get("missing") is None

    def test_recent_is_newest_first(self, settings: Settings, executor: PostgresExecutor) -> None:
        repo = PostgresAuditRepository(settings.app_dsn)
        base = _record("recent-a", executor)
        # Far-future timestamps so rows written by other tests don't interleave.
        repo.save(replace(base, created_at=datetime(2031, 1, 1, tzinfo=UTC)))
        repo.save(replace(base, request_id="recent-b", created_at=datetime(2032, 1, 1, tzinfo=UTC)))
        assert [r.request_id for r in repo.recent(2)] == ["recent-b", "recent-a"]

    def test_write_failure_surfaces_as_domain_error(self, settings: Settings) -> None:
        # The reader role has no access to the audit schema.
        repo = PostgresAuditRepository(settings.reader_dsn)
        with pytest.raises(AuditWriteError):
            repo.save(
                _record(
                    "x",
                    PostgresExecutor(settings.reader_dsn, max_rows=5, statement_timeout_ms=1000),
                )
            )
