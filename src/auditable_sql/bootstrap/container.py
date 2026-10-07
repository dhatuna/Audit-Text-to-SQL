"""Composition root: the only place that knows which adapter implements which port."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from functools import cache
from pathlib import Path

import anthropic
import psycopg

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.application.evaluate import CheckGold, EvalCase, RunEvaluation
from auditable_sql.application.ports import SchemaContextProvider
from auditable_sql.application.replay import ReplayAudit
from auditable_sql.bootstrap.settings import Settings
from auditable_sql.domain.policy import RuntimeLimits
from auditable_sql.infrastructure.context.semantic_layer import (
    RawDdlContext,
    SemanticLayer,
    SemanticLayerContext,
)
from auditable_sql.infrastructure.db import seed as seeder
from auditable_sql.infrastructure.db.postgres_audit import PostgresAuditRepository
from auditable_sql.infrastructure.db.postgres_executor import PostgresExecutor
from auditable_sql.infrastructure.eval_cases import load_cases
from auditable_sql.infrastructure.llm.anthropic_adapters import (
    ClaudeAnswerComposer,
    ClaudeSqlGenerator,
)
from auditable_sql.infrastructure.validation.sqlglot_validator import SqlglotValidator
from auditable_sql.interfaces.services import Services


class Variant(StrEnum):
    """Pipeline configurations compared in the evaluation."""

    IMPROVED = "improved"  # semantic layer context + one repair attempt
    BASELINE = "baseline"  # raw DDL context, no repair


class UtcClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class UuidIds:
    def new_id(self) -> str:
        return str(uuid.uuid4())


@cache
def _client(api_key: str | None) -> anthropic.Anthropic:
    # None lets the SDK resolve credentials itself (environment, `ant auth login` profile).
    return anthropic.Anthropic(api_key=api_key, max_retries=3)


class Container:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or Settings()
        self.layer = SemanticLayer.load(self.settings.semantic_layer_path)
        self.policy = self.layer.policy(self.settings.max_rows)
        self.validator = SqlglotValidator(self.policy)
        self.executor = PostgresExecutor(
            self.settings.reader_dsn,
            max_rows=self.settings.max_rows,
            statement_timeout_ms=self.settings.statement_timeout_ms,
        )
        self.audit = PostgresAuditRepository(self.settings.app_dsn)

    def ask(self, variant: Variant = Variant.IMPROVED) -> AskQuestion:
        s = self.settings
        context: SchemaContextProvider
        if variant is Variant.BASELINE:
            context, repairs = RawDdlContext(self.layer), 0
        else:
            context, repairs = SemanticLayerContext(self.layer), s.max_repair_attempts
        key = s.anthropic_api_key.get_secret_value() if s.anthropic_api_key else None
        return AskQuestion(
            context_provider=context,
            generator=ClaudeSqlGenerator(
                _client(key), model=s.model, effort=s.effort, use_fallbacks=s.use_fallbacks
            ),
            validator=self.validator,
            executor=self.executor,
            composer=ClaudeAnswerComposer(
                _client(key), model=s.model, effort="low", use_fallbacks=s.use_fallbacks
            ),
            audit=self.audit,
            clock=UtcClock(),
            ids=UuidIds(),
            limits=RuntimeLimits(
                statement_timeout_ms=s.statement_timeout_ms, max_repair_attempts=repairs
            ),
        )

    def replay(self) -> ReplayAudit:
        return ReplayAudit(audit=self.audit, validator=self.validator, executor=self.executor)

    def evaluation(self, variant: Variant) -> RunEvaluation:
        return RunEvaluation(ask=self.ask(variant), gold_executor=self.executor)

    def seed(self, seed: int) -> dict[str, int]:
        with psycopg.connect(self.settings.admin_dsn) as conn:
            return seeder.load(conn, seeder.generate(seed=seed))

    def load_cases(self, path: Path) -> list[EvalCase]:
        return load_cases(path)

    def services(self) -> Services:
        return Services(
            ask=lambda baseline: self.ask(Variant.BASELINE if baseline else Variant.IMPROVED),
            replay=self.replay,
            audit=self.audit,
            evaluation=lambda name: self.evaluation(Variant(name)),
            check_gold=lambda: CheckGold(validator=self.validator, executor=self.executor),
            variants=tuple(v.value for v in Variant),
            seed=self.seed,
            load_cases=self.load_cases,
            model=self.settings.model,
        )
