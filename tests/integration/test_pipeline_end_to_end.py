"""Full pipeline against a real database: real validator, real read-only executor, real
append-only audit log. Only the language model is scripted, so these run offline and free.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.application.replay import ReplayAudit
from auditable_sql.bootstrap.container import UuidIds
from auditable_sql.bootstrap.settings import Settings
from auditable_sql.domain.models import Decision, Generation, Outcome
from auditable_sql.domain.policy import RuntimeLimits
from auditable_sql.infrastructure.context.semantic_layer import SemanticLayer, SemanticLayerContext
from auditable_sql.infrastructure.db.postgres_audit import PostgresAuditRepository
from auditable_sql.infrastructure.db.postgres_executor import PostgresExecutor
from auditable_sql.infrastructure.validation.sqlglot_validator import SqlglotValidator
from tests.fakes import EchoComposer, FixedClock, ScriptedGenerator

AUM_SQL = (
    "SELECT SUM(market_value) AS aum FROM positions_eod "
    "WHERE as_of_date = (SELECT MAX(as_of_date) FROM positions_eod)"
)


class Pipeline:
    def __init__(self, settings: Settings) -> None:
        layer = SemanticLayer.load(settings.semantic_layer_path)
        self.validator = SqlglotValidator(layer.policy(settings.max_rows))
        self.executor = PostgresExecutor(
            settings.reader_dsn,
            max_rows=settings.max_rows,
            statement_timeout_ms=settings.statement_timeout_ms,
        )
        self.audit = PostgresAuditRepository(settings.app_dsn)
        self.context = SemanticLayerContext(layer)

    def ask(self, *gens: Generation, answer: str = "Total AUM is shown above.") -> AskQuestion:
        return AskQuestion(
            context_provider=self.context,
            generator=ScriptedGenerator(*gens),
            validator=self.validator,
            executor=self.executor,
            composer=EchoComposer(answer),
            audit=self.audit,
            clock=FixedClock(),
            ids=UuidIds(),
            limits=RuntimeLimits(max_repair_attempts=1),
        )


@pytest.fixture
def pipeline(settings: Settings) -> Pipeline:
    return Pipeline(settings)


def gen(sql: str) -> Generation:
    return Generation(Decision.ANSWER, "aggregate", sql=sql, assumptions=("latest date",))


def test_answer_is_persisted_and_replayable(
    pipeline: Pipeline, admin: psycopg.Connection[Any]
) -> None:
    (expected,) = admin.execute(AUM_SQL).fetchone()  # type: ignore[misc]
    record = pipeline.ask(gen(AUM_SQL), answer=f"Total AUM is ${expected:,.2f}.").execute("AUM?")

    assert record.outcome is Outcome.ANSWERED
    assert record.final_sql is not None and record.final_sql.endswith("LIMIT 51")
    assert record.attempts[0].result is not None
    assert record.attempts[0].result.rows[0][0] == expected
    assert record.grounding is not None and record.grounding.passed

    stored = pipeline.audit.get(record.request_id)
    assert stored == record

    report = ReplayAudit(
        audit=pipeline.audit, validator=pipeline.validator, executor=pipeline.executor
    ).execute(record.request_id)
    assert report.matches


def test_pii_attempt_is_blocked_then_repaired(pipeline: Pipeline) -> None:
    record = pipeline.ask(
        gen("SELECT c.tax_id FROM clients c"),
        gen("SELECT risk_profile, COUNT(*) AS n FROM clients GROUP BY risk_profile"),
        answer="done",
    ).execute("clients by risk")

    assert record.outcome is Outcome.ANSWERED
    first, second = record.attempts
    assert first.validation is not None and not first.validation.passed
    assert first.validation.violations[0].rule_id == "R006_COLUMN_NOT_ALLOWED"
    assert first.result is None  # never reached the database
    assert second.result is not None and second.result.row_count == 3


def test_database_error_is_fed_back_and_recorded(pipeline: Pipeline) -> None:
    # Passes static checks (string comparison against a date) but PostgreSQL rejects it.
    bad = "SELECT COUNT(*) AS n FROM trades WHERE trade_date = 'not a date'"
    record = pipeline.ask(gen(bad), gen("SELECT COUNT(*) AS n FROM trades"), answer="ok").execute(
        "q"
    )

    assert record.outcome is Outcome.ANSWERED
    assert record.attempts[0].execution_error is not None
    assert "22007" in record.attempts[0].execution_error  # invalid_datetime_format


def test_blocked_request_is_still_audited(pipeline: Pipeline) -> None:
    record = pipeline.ask(gen("DELETE FROM trades"), gen("SELECT * FROM clients")).execute(
        "delete everything"
    )

    assert record.outcome is Outcome.BLOCKED
    stored = pipeline.audit.get(record.request_id)
    assert stored is not None and stored.outcome is Outcome.BLOCKED
    assert [a.validation.violations[0].rule_id for a in stored.attempts] == [  # type: ignore[union-attr]
        "R003_NOT_SELECT",
        "R007_SELECT_STAR",
    ]


def test_refusal_is_audited_with_context_hash(pipeline: Pipeline) -> None:
    record = pipeline.ask(
        Generation(Decision.REFUSE, "Personal details are not available.")
    ).execute("tax id of client 4")
    stored = pipeline.audit.get(record.request_id)
    assert stored is not None
    assert stored.outcome is Outcome.REFUSED
    assert stored.context.sha256 == pipeline.context.get().sha256
    assert "tax_id" not in stored.context.text  # hidden columns are never shown to the model


def test_audit_rows_cannot_be_altered_after_the_fact(
    pipeline: Pipeline, settings: Settings
) -> None:
    record = pipeline.ask(Generation(Decision.REFUSE, "no")).execute("q")
    with (
        psycopg.connect(settings.app_dsn, autocommit=True) as conn,
        pytest.raises(psycopg.errors.InsufficientPrivilege),
    ):
        conn.execute(
            "UPDATE audit.query_log SET question = 'edited' WHERE request_id = %s",
            (record.request_id,),
        )


def test_created_at_comes_from_the_clock(pipeline: Pipeline) -> None:
    record = pipeline.ask(Generation(Decision.REFUSE, "no")).execute("q")
    stored = pipeline.audit.get(record.request_id)
    assert stored is not None and stored.created_at == datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
