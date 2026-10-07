from __future__ import annotations

import pytest

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.domain.errors import AuditWriteError, GenerationError, QueryExecutionError
from auditable_sql.domain.models import Decision, Generation, Outcome, ValidationResult, Violation
from auditable_sql.domain.policy import RuntimeLimits
from tests.fakes import (
    CONTEXT,
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

SQL = "SELECT SUM(market_value) AS aum FROM positions_eod WHERE as_of_date = '2025-12-31'"
AUM = result(["aum"], [42])


def answer(sql: str | None = SQL, *assumptions: str) -> Generation:
    return Generation(Decision.ANSWER, "aggregate", sql=sql, assumptions=assumptions)


def build(
    generator: ScriptedGenerator,
    *,
    validator: RuleValidator | None = None,
    executor: ScriptedExecutor | None = None,
    composer: EchoComposer | None = None,
    audit: InMemoryAudit | None = None,
    repairs: int = 1,
) -> tuple[AskQuestion, InMemoryAudit]:
    audit = audit or InMemoryAudit()
    use_case = AskQuestion(
        context_provider=StaticContext(),
        generator=generator,
        validator=validator or RuleValidator(),
        executor=executor or ScriptedExecutor(AUM),
        composer=composer or EchoComposer(),
        audit=audit,
        clock=FixedClock(),
        ids=SequentialIds(),
        limits=RuntimeLimits(max_repair_attempts=repairs),
    )
    return use_case, audit


def reject_all(sql: str) -> ValidationResult:
    return ValidationResult((Violation("R100_TEST", f"nope: {sql}"),), None)


class TestHappyPath:
    def test_answers_and_persists_full_record(self) -> None:
        use_case, audit = build(ScriptedGenerator(answer(SQL, "latest as_of_date")))

        record = use_case.execute("What is total AUM?", user_label="analyst-1")

        assert record.outcome is Outcome.ANSWERED
        assert audit.records[record.request_id] is record
        assert record.question == "What is total AUM?"
        assert record.user_label == "analyst-1"
        assert record.context == CONTEXT
        assert record.prompt_version == "test-prompt-v1"
        assert record.final_sql == SQL
        assert record.result_fingerprint is not None
        assert record.answer is not None and record.answer.text == "The answer is 42."
        assert record.grounding is not None and record.grounding.passed
        assert record.attempts[0].generation.assumptions == ("latest as_of_date",)
        assert record.error is None

    def test_executes_normalized_sql_not_raw_sql(self) -> None:
        executor = ScriptedExecutor(AUM)
        validator = RuleValidator(lambda s: ValidationResult((), s + " LIMIT 500"))
        use_case, _ = build(ScriptedGenerator(answer()), validator=validator, executor=executor)

        record = use_case.execute("q")

        assert executor.executed == [SQL + " LIMIT 500"]
        assert record.final_sql == SQL + " LIMIT 500"

    def test_ungrounded_answer_is_flagged_not_hidden(self) -> None:
        use_case, _ = build(ScriptedGenerator(answer()), composer=EchoComposer("AUM is 99."))

        record = use_case.execute("q")

        assert record.outcome is Outcome.ANSWERED
        assert record.grounding is not None
        assert record.grounding.unmatched_numbers == ("99",)


class TestDeclining:
    @pytest.mark.parametrize(
        ("decision", "outcome"),
        [(Decision.CLARIFY, Outcome.CLARIFIED), (Decision.REFUSE, Outcome.REFUSED)],
    )
    def test_never_touches_validator_or_database(
        self, decision: Decision, outcome: Outcome
    ) -> None:
        validator, executor = RuleValidator(), ScriptedExecutor()
        gen = Generation(decision, "ambiguous period", clarifying_question="Which period?")
        use_case, _ = build(ScriptedGenerator(gen), validator=validator, executor=executor)

        record = use_case.execute("best portfolio?")

        assert record.outcome is outcome
        assert validator.seen == [] and executor.executed == []
        assert record.final_sql is None and record.result_fingerprint is None

    def test_clarify_returns_the_question(self) -> None:
        gen = Generation(Decision.CLARIFY, "ambiguous", clarifying_question="Which period?")
        use_case, _ = build(ScriptedGenerator(gen))
        assert use_case.execute("best?").answer.text == "Which period?"  # type: ignore[union-attr]

    def test_refuse_returns_reason(self) -> None:
        gen = Generation(Decision.REFUSE, "Client tax IDs are not available.")
        use_case, _ = build(ScriptedGenerator(gen))
        record = use_case.execute("show tax ids")
        assert record.answer is not None
        assert record.answer.text == "Client tax IDs are not available."


class TestRepairLoop:
    def test_validation_failure_is_fed_back_then_repaired(self) -> None:
        bad = "SELECT tax_id FROM clients"
        generator = ScriptedGenerator(answer(bad), answer(SQL))
        validator = RuleValidator(lambda s: reject_all(s) if s == bad else None)
        use_case, _ = build(generator, validator=validator)

        record = use_case.execute("q")

        assert record.outcome is Outcome.ANSWERED
        assert len(record.attempts) == 2
        assert not record.attempts[0].validation.passed  # type: ignore[union-attr]
        feedback = generator.calls[1][1]
        assert feedback[0].sql == bad
        assert "R100_TEST" in feedback[0].problem

    def test_execution_error_is_fed_back_then_repaired(self) -> None:
        generator = ScriptedGenerator(answer("SELECT nope"), answer(SQL))
        executor = ScriptedExecutor(QueryExecutionError("column nope does not exist"), AUM)
        use_case, _ = build(generator, executor=executor)

        record = use_case.execute("q")

        assert record.outcome is Outcome.ANSWERED
        assert record.attempts[0].execution_error == "column nope does not exist"
        assert generator.calls[1][1][0].problem == "column nope does not exist"

    def test_gives_up_as_blocked_after_budget(self) -> None:
        generator = ScriptedGenerator(answer("x"), answer("y"))
        use_case, _ = build(generator, validator=RuleValidator(reject_all), repairs=1)

        record = use_case.execute("q")

        assert record.outcome is Outcome.BLOCKED
        assert len(record.attempts) == 2
        assert record.answer is None and record.final_sql is None

    def test_gives_up_as_failed_when_last_error_is_execution(self) -> None:
        generator = ScriptedGenerator(answer(), answer())
        executor = ScriptedExecutor(QueryExecutionError("timeout"), QueryExecutionError("timeout"))
        use_case, _ = build(generator, executor=executor)

        assert use_case.execute("q").outcome is Outcome.FAILED

    def test_zero_repair_budget_means_single_attempt(self) -> None:
        generator = ScriptedGenerator(answer("x"))
        use_case, _ = build(generator, validator=RuleValidator(reject_all), repairs=0)

        record = use_case.execute("q")

        assert len(record.attempts) == 1 and len(generator.calls) == 1

    @pytest.mark.parametrize("sql", [None, "", "   "])
    def test_answer_without_sql_is_a_violation(self, sql: str | None) -> None:
        validator = RuleValidator()
        use_case, _ = build(ScriptedGenerator(answer(sql)), validator=validator, repairs=0)

        record = use_case.execute("q")

        assert record.outcome is Outcome.BLOCKED
        assert validator.seen == []
        assert record.attempts[0].validation.violations[0].rule_id == "G001_EMPTY_SQL"  # type: ignore[union-attr]


class TestFailureModes:
    def test_generation_error_is_audited(self) -> None:
        use_case, audit = build(ScriptedGenerator(GenerationError("rate limited")))

        record = use_case.execute("q")

        assert record.outcome is Outcome.FAILED
        assert record.error == "generation failed: rate limited"
        assert record.request_id in audit.records

    def test_composer_error_after_execution_is_audited_without_answer(self) -> None:
        use_case, _ = build(
            ScriptedGenerator(answer()), composer=EchoComposer(error=GenerationError("overloaded"))
        )

        record = use_case.execute("q")

        assert record.outcome is Outcome.FAILED
        assert record.answer is None
        assert record.attempts[-1].result is not None  # the executed query is still on record

    def test_no_audit_no_answer(self) -> None:
        use_case, _ = build(ScriptedGenerator(answer()), audit=InMemoryAudit(fail=True))

        with pytest.raises(AuditWriteError):
            use_case.execute("q")

    def test_each_request_gets_a_unique_id(self) -> None:
        use_case, audit = build(
            ScriptedGenerator(answer(), answer()), executor=ScriptedExecutor(AUM, AUM)
        )
        use_case.execute("a")
        use_case.execute("b")
        assert sorted(audit.records) == ["req-0001", "req-0002"]
