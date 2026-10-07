"""Use case: re-execute the SQL stored in an audit record and check the result is unchanged.

Replay re-runs the recorded SQL; it does not call the language model again. Model output is
not deterministic, so "would the model write the same SQL today" is a different question
(answered by the evaluation harness, not by replay).
"""

from __future__ import annotations

from dataclasses import dataclass

from auditable_sql.application.ports import AuditRepository, QueryExecutor, SqlValidator
from auditable_sql.domain.errors import QueryExecutionError, RecordNotFoundError
from auditable_sql.domain.models import Violation
from auditable_sql.domain.results import fingerprint


@dataclass(frozen=True, slots=True)
class ReplayReport:
    request_id: str
    replayable: bool
    reason: str
    original_fingerprint: str | None = None
    replay_fingerprint: str | None = None
    # Policy may have tightened since the original run; replay still reports it.
    current_violations: tuple[Violation, ...] = ()

    @property
    def matches(self) -> bool:
        return (
            self.replayable
            and self.original_fingerprint is not None
            and self.original_fingerprint == self.replay_fingerprint
        )


class ReplayAudit:
    def __init__(
        self, *, audit: AuditRepository, validator: SqlValidator, executor: QueryExecutor
    ) -> None:
        self._audit = audit
        self._validator = validator
        self._executor = executor

    def execute(self, request_id: str) -> ReplayReport:
        record = self._audit.get(request_id)
        if record is None:
            raise RecordNotFoundError(request_id)
        if record.final_sql is None:
            return ReplayReport(request_id, False, f"no executed SQL (outcome={record.outcome})")

        validation = self._validator.validate(record.final_sql)
        if not validation.passed:
            return ReplayReport(
                request_id,
                False,
                "stored SQL no longer passes the current policy",
                original_fingerprint=record.result_fingerprint,
                current_violations=validation.violations,
            )
        try:
            result = self._executor.execute(record.final_sql)
        except QueryExecutionError as exc:
            return ReplayReport(
                request_id, False, f"execution failed: {exc}", record.result_fingerprint
            )
        new_fp = fingerprint(result)
        return ReplayReport(
            request_id,
            True,
            "match" if new_fp == record.result_fingerprint else "result changed",
            original_fingerprint=record.result_fingerprint,
            replay_fingerprint=new_fp,
        )
