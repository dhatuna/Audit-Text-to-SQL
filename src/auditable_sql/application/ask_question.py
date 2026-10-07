"""Use case: answer a natural-language question, or decline, and record every step."""

from __future__ import annotations

import time

from auditable_sql.application.ports import (
    AnswerComposer,
    AuditRepository,
    Clock,
    IdGenerator,
    QueryExecutor,
    RepairFeedback,
    SchemaContextProvider,
    SqlGenerator,
    SqlValidator,
)
from auditable_sql.domain.errors import GenerationError, QueryExecutionError
from auditable_sql.domain.grounding import check_grounding
from auditable_sql.domain.models import (
    Answer,
    Attempt,
    AuditRecord,
    Decision,
    Outcome,
    ValidationResult,
    Violation,
)
from auditable_sql.domain.policy import RuntimeLimits
from auditable_sql.domain.results import fingerprint

EMPTY_SQL = Violation("G001_EMPTY_SQL", "decision was 'answer' but no SQL was produced")


class AskQuestion:
    def __init__(
        self,
        *,
        context_provider: SchemaContextProvider,
        generator: SqlGenerator,
        validator: SqlValidator,
        executor: QueryExecutor,
        composer: AnswerComposer,
        audit: AuditRepository,
        clock: Clock,
        ids: IdGenerator,
        limits: RuntimeLimits,
    ) -> None:
        self._context_provider = context_provider
        self._generator = generator
        self._validator = validator
        self._executor = executor
        self._composer = composer
        self._audit = audit
        self._clock = clock
        self._ids = ids
        self._limits = limits

    def execute(self, question: str, user_label: str = "anonymous") -> AuditRecord:
        """Run the pipeline. The record is persisted before it is returned.

        If the audit write fails, AuditWriteError propagates and no answer is released:
        an answer that cannot be explained later is treated as no answer.
        """
        started = time.perf_counter()
        request_id = self._ids.new_id()
        created_at = self._clock.now()
        context = self._context_provider.get()

        attempts: list[Attempt] = []
        feedback: list[RepairFeedback] = []
        outcome = Outcome.FAILED
        answer: Answer | None = None
        error: str | None = None

        try:
            for number in range(1, self._limits.max_repair_attempts + 2):
                gen = self._generator.generate(question, context, feedback)

                if gen.decision is Decision.CLARIFY:
                    attempts.append(Attempt(number, gen))
                    outcome = Outcome.CLARIFIED
                    answer = Answer(text=gen.clarifying_question or gen.reason)
                    break
                if gen.decision is Decision.REFUSE:
                    attempts.append(Attempt(number, gen))
                    outcome = Outcome.REFUSED
                    answer = Answer(text=gen.reason)
                    break

                validation = (
                    self._validator.validate(gen.sql)
                    if gen.sql and gen.sql.strip()
                    else ValidationResult(violations=(EMPTY_SQL,), normalized_sql=None)
                )
                if not validation.passed or validation.normalized_sql is None:
                    attempts.append(Attempt(number, gen, validation))
                    feedback.append(
                        RepairFeedback(
                            sql=gen.sql or "",
                            problem="; ".join(
                                f"{v.rule_id}: {v.message}" for v in validation.violations
                            ),
                        )
                    )
                    outcome = Outcome.BLOCKED
                    continue

                try:
                    result = self._executor.execute(validation.normalized_sql)
                except QueryExecutionError as exc:
                    attempts.append(Attempt(number, gen, validation, execution_error=str(exc)))
                    feedback.append(RepairFeedback(sql=validation.normalized_sql, problem=str(exc)))
                    outcome = Outcome.FAILED
                    continue

                attempts.append(Attempt(number, gen, validation, result=result))
                answer = self._composer.compose(
                    question, validation.normalized_sql, result, gen.assumptions
                )
                outcome = Outcome.ANSWERED
                break
        except GenerationError as exc:
            outcome = Outcome.FAILED
            answer = None
            error = f"generation failed: {exc}"

        final = attempts[-1] if attempts else None
        answered = final.result if final and outcome is Outcome.ANSWERED else None
        record = AuditRecord(
            request_id=request_id,
            created_at=created_at,
            user_label=user_label,
            question=question,
            context=context,
            prompt_version=self._generator.prompt_version,
            attempts=tuple(attempts),
            outcome=outcome,
            final_sql=(
                final.validation.normalized_sql if answered and final and final.validation else None
            ),
            result_fingerprint=fingerprint(answered) if answered else None,
            answer=answer,
            grounding=(
                check_grounding(answer.text, answered, question) if answered and answer else None
            ),
            total_latency_ms=(time.perf_counter() - started) * 1000,
            error=error,
        )
        self._audit.save(record)
        return record
