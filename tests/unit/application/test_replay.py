from __future__ import annotations

import pytest

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.application.replay import ReplayAudit
from auditable_sql.domain.errors import QueryExecutionError, RecordNotFoundError
from auditable_sql.domain.models import Decision, Generation, ValidationResult, Violation
from auditable_sql.domain.policy import RuntimeLimits
from tests.fakes import (
    EchoComposer,
    FixedClock,
    InMemoryAudit,
    RuleValidator,
    ScriptedExecutor,
    ScriptedGenerator,
    SequentialIds,
    StaticContext,
    result,
)

SQL = "SELECT 1 AS n"


def recorded(audit: InMemoryAudit, gen: Generation, first_result: object) -> str:
    use_case = AskQuestion(
        context_provider=StaticContext(),
        generator=ScriptedGenerator(gen),
        validator=RuleValidator(),
        executor=ScriptedExecutor(first_result),  # type: ignore[arg-type]
        composer=EchoComposer("one"),
        audit=audit,
        clock=FixedClock(),
        ids=SequentialIds(),
        limits=RuntimeLimits(max_repair_attempts=0),
    )
    return use_case.execute("q").request_id


def test_same_rows_in_different_order_match() -> None:
    audit = InMemoryAudit()
    rid = recorded(audit, Generation(Decision.ANSWER, "", sql=SQL), result(["n"], [1], [2]))
    replay = ReplayAudit(
        audit=audit, validator=RuleValidator(), executor=ScriptedExecutor(result(["n"], [2], [1]))
    )

    report = replay.execute(rid)

    assert report.matches and report.reason == "match"


def test_changed_data_is_detected() -> None:
    audit = InMemoryAudit()
    rid = recorded(audit, Generation(Decision.ANSWER, "", sql=SQL), result(["n"], [1]))
    replay = ReplayAudit(
        audit=audit, validator=RuleValidator(), executor=ScriptedExecutor(result(["n"], [2]))
    )

    report = replay.execute(rid)

    assert report.replayable and not report.matches
    assert report.reason == "result changed"


def test_policy_tightened_since_original_run() -> None:
    audit = InMemoryAudit()
    rid = recorded(audit, Generation(Decision.ANSWER, "", sql=SQL), result(["n"], [1]))
    strict = RuleValidator(lambda _: ValidationResult((Violation("R1", "now forbidden"),), None))
    executor = ScriptedExecutor()

    report = ReplayAudit(audit=audit, validator=strict, executor=executor).execute(rid)

    assert not report.replayable
    assert report.current_violations[0].rule_id == "R1"
    assert executor.executed == []


def test_execution_failure_is_reported() -> None:
    audit = InMemoryAudit()
    rid = recorded(audit, Generation(Decision.ANSWER, "", sql=SQL), result(["n"], [1]))
    executor = ScriptedExecutor(QueryExecutionError("relation dropped"))

    report = ReplayAudit(audit=audit, validator=RuleValidator(), executor=executor).execute(rid)

    assert not report.replayable and "relation dropped" in report.reason


def test_refused_request_has_nothing_to_replay() -> None:
    audit = InMemoryAudit()
    rid = recorded(audit, Generation(Decision.REFUSE, "no"), None)

    report = ReplayAudit(
        audit=audit, validator=RuleValidator(), executor=ScriptedExecutor()
    ).execute(rid)

    assert not report.replayable and "refused" in report.reason


def test_unknown_id() -> None:
    replay = ReplayAudit(
        audit=InMemoryAudit(), validator=RuleValidator(), executor=ScriptedExecutor()
    )
    with pytest.raises(RecordNotFoundError):
        replay.execute("missing")
