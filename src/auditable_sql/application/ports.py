"""Interfaces the application layer depends on. Infrastructure provides the implementations."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from auditable_sql.domain.models import (
    Answer,
    AuditRecord,
    Generation,
    QueryResult,
    SchemaContext,
    ValidationResult,
)


@dataclass(frozen=True, slots=True)
class RepairFeedback:
    """What went wrong with a previous attempt, fed back to the generator."""

    sql: str
    problem: str


class SchemaContextProvider(Protocol):
    def get(self) -> SchemaContext: ...


class SqlGenerator(Protocol):
    prompt_version: str

    def generate(
        self, question: str, context: SchemaContext, feedback: Sequence[RepairFeedback]
    ) -> Generation:
        """Raises GenerationError on API failure or unusable output."""
        ...


class SqlValidator(Protocol):
    def validate(self, sql: str) -> ValidationResult: ...


class QueryExecutor(Protocol):
    def execute(self, sql: str) -> QueryResult:
        """Raises QueryExecutionError when the database rejects or aborts the query."""
        ...


class AnswerComposer(Protocol):
    def compose(
        self, question: str, sql: str, result: QueryResult, assumptions: Sequence[str]
    ) -> Answer:
        """Raises GenerationError on API failure."""
        ...


class AuditRepository(Protocol):
    def save(self, record: AuditRecord) -> None:
        """Raises AuditWriteError. Append-only: saving an existing id is an error."""
        ...

    def get(self, request_id: str) -> AuditRecord | None: ...

    def recent(self, limit: int = 20) -> Sequence[AuditRecord]: ...


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new_id(self) -> str: ...
