"""In-memory test doubles for application ports."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from itertools import count

from auditable_sql.application.ports import RepairFeedback
from auditable_sql.domain.errors import AuditWriteError
from auditable_sql.domain.models import (
    Answer,
    AuditRecord,
    Generation,
    QueryResult,
    SchemaContext,
    ValidationResult,
)

CONTEXT = SchemaContext(text="tables: positions_eod(...)", version="test", sha256="abc123")


class StaticContext:
    def get(self) -> SchemaContext:
        return CONTEXT


class ScriptedGenerator:
    """Returns pre-baked generations in order and records what it was asked."""

    prompt_version = "test-prompt-v1"

    def __init__(self, *outputs: Generation | Exception) -> None:
        self._outputs = list(outputs)
        self.calls: list[tuple[str, list[RepairFeedback]]] = []

    def generate(
        self, question: str, context: SchemaContext, feedback: Sequence[RepairFeedback]
    ) -> Generation:
        self.calls.append((question, list(feedback)))
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class RuleValidator:
    """Passes SQL unless `reject(sql)` returns a ValidationResult."""

    def __init__(self, reject: Callable[[str], ValidationResult | None] = lambda _: None) -> None:
        self._reject = reject
        self.seen: list[str] = []

    def validate(self, sql: str) -> ValidationResult:
        self.seen.append(sql)
        return self._reject(sql) or ValidationResult(violations=(), normalized_sql=sql)


class ScriptedExecutor:
    def __init__(self, *outputs: QueryResult | Exception) -> None:
        self._outputs = list(outputs)
        self.executed: list[str] = []

    def execute(self, sql: str) -> QueryResult:
        self.executed.append(sql)
        out = self._outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


class EchoComposer:
    def __init__(self, text: str = "The answer is 42.", error: Exception | None = None) -> None:
        self._text = text
        self._error = error

    def compose(
        self, question: str, sql: str, result: QueryResult, assumptions: Sequence[str]
    ) -> Answer:
        if self._error:
            raise self._error
        return Answer(text=self._text, model="fake-composer")


class InMemoryAudit:
    def __init__(self, fail: bool = False) -> None:
        self.records: dict[str, AuditRecord] = {}
        self._fail = fail

    def save(self, record: AuditRecord) -> None:
        if self._fail:
            raise AuditWriteError("disk full")
        if record.request_id in self.records:
            raise AuditWriteError("duplicate request_id")
        self.records[record.request_id] = record

    def get(self, request_id: str) -> AuditRecord | None:
        return self.records.get(request_id)

    def recent(self, limit: int = 20) -> Sequence[AuditRecord]:
        return list(self.records.values())[-limit:]


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 1, 2, 9, 30, tzinfo=UTC)


class SequentialIds:
    def __init__(self) -> None:
        self._n = count(1)

    def new_id(self) -> str:
        return f"req-{next(self._n):04d}"


def result(columns: Sequence[str], *rows: Sequence[object], truncated: bool = False) -> QueryResult:
    return QueryResult(
        columns=tuple(columns),
        rows=tuple(tuple(r) for r in rows),  # type: ignore[misc]
        truncated=truncated,
        elapsed_ms=1.0,
    )
