"""Core domain types. Pure Python, no framework imports."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any


class Decision(StrEnum):
    """What the generator decided to do with a question."""

    ANSWER = "answer"
    CLARIFY = "clarify"
    REFUSE = "refuse"


class Outcome(StrEnum):
    """Terminal status of one request, as recorded in the audit log."""

    ANSWERED = "answered"
    CLARIFIED = "clarified"
    REFUSED = "refused"
    BLOCKED = "blocked"  # every attempt failed validation
    FAILED = "failed"  # execution or infrastructure error after retries


@dataclass(frozen=True, slots=True)
class SchemaContext:
    """The exact schema description shown to the model, plus a stable fingerprint."""

    text: str
    version: str
    sha256: str


@dataclass(frozen=True, slots=True)
class Generation:
    """Structured output of the SQL generator."""

    decision: Decision
    reason: str
    sql: str | None = None
    clarifying_question: str | None = None
    assumptions: tuple[str, ...] = ()
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class Violation:
    rule_id: str
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    violations: tuple[Violation, ...]
    # SQL that will actually run (e.g. with an enforced LIMIT). None when rejected.
    normalized_sql: str | None

    @property
    def passed(self) -> bool:
        return not self.violations and self.normalized_sql is not None


Cell = str | int | float | Decimal | bool | date | datetime | None


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]
    truncated: bool
    elapsed_ms: float

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True, slots=True)
class Attempt:
    """One generate -> validate -> execute cycle."""

    number: int
    generation: Generation
    validation: ValidationResult | None = None
    result: QueryResult | None = None
    execution_error: str | None = None


@dataclass(frozen=True, slots=True)
class GroundingReport:
    """Whether every number stated in the answer can be traced to the result set."""

    checked_numbers: tuple[str, ...]
    unmatched_numbers: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.unmatched_numbers


@dataclass(frozen=True, slots=True)
class Answer:
    text: str
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True, slots=True)
class AuditRecord:
    """Everything needed to explain, and re-execute, one answer."""

    request_id: str
    created_at: datetime
    user_label: str
    question: str
    context: SchemaContext
    prompt_version: str
    attempts: tuple[Attempt, ...]
    outcome: Outcome
    final_sql: str | None
    result_fingerprint: str | None
    answer: Answer | None
    grounding: GroundingReport | None
    total_latency_ms: float
    error: str | None = None

    @property
    def final_attempt(self) -> Attempt | None:
        return self.attempts[-1] if self.attempts else None
