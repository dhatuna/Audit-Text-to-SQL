from __future__ import annotations

import pytest

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.application.evaluate import (
    CheckGold,
    EvalCase,
    RunEvaluation,
    summarize,
)
from auditable_sql.domain.errors import QueryExecutionError
from auditable_sql.domain.models import Decision, Generation, Outcome, ValidationResult, Violation
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

GOLD = "SELECT gold"


def case(cid: str, expected: Decision, gold: str | None = GOLD, **kw: object) -> EvalCase:
    return EvalCase(
        id=cid, question=f"q-{cid}", expected=expected, split="dev", gold_sql=gold, **kw
    )  # type: ignore[arg-type]


def runner(
    generations: list[Generation], executor_results: list[object]
) -> tuple[RunEvaluation, InMemoryAudit]:
    """The same executor serves pipeline and gold queries, in call order."""
    audit = InMemoryAudit()
    executor = ScriptedExecutor(*executor_results)  # type: ignore[arg-type]
    ask = AskQuestion(
        context_provider=StaticContext(),
        generator=ScriptedGenerator(*generations),
        validator=RuleValidator(),
        executor=executor,
        composer=EchoComposer("ok"),
        audit=audit,
        clock=FixedClock(),
        ids=SequentialIds(),
        limits=RuntimeLimits(max_repair_attempts=0),
    )
    return RunEvaluation(ask=ask, gold_executor=executor), audit


def answer(sql: str = "SELECT model") -> Generation:
    return Generation(Decision.ANSWER, "r", sql=sql, input_tokens=100, output_tokens=10)


class TestCaseDefinition:
    def test_answer_requires_gold(self) -> None:
        with pytest.raises(ValueError, match="gold_sql"):
            case("a", Decision.ANSWER, gold=None)

    def test_decline_must_not_have_gold(self) -> None:
        with pytest.raises(ValueError, match="gold_sql"):
            case("a", Decision.REFUSE, gold="SELECT 1")


class TestScoring:
    def test_matching_result_is_correct(self) -> None:
        r = result(["n"], [7])
        run, _ = runner([answer()], [r, r])

        (res,) = run.execute([case("a", Decision.ANSWER)], label="t")

        assert res.correct and res.execution_match is True
        assert res.outcome is Outcome.ANSWERED
        assert res.input_tokens == 100 and res.output_tokens == 10

    def test_wrong_result_is_incorrect_with_note(self) -> None:
        run, _ = runner([answer()], [result(["n"], [7]), result(["n"], [8], [9])])

        (res,) = run.execute([case("a", Decision.ANSWER)], label="t")

        assert not res.correct and res.execution_match is False
        assert "gold rows=2, got rows=1" in res.note

    def test_declining_an_answerable_question_is_incorrect(self) -> None:
        run, _ = runner([Generation(Decision.CLARIFY, "?", clarifying_question="which?")], [])

        (res,) = run.execute([case("a", Decision.ANSWER)], label="t")

        assert not res.correct and res.outcome is Outcome.CLARIFIED

    @pytest.mark.parametrize(
        ("expected", "decision", "correct"),
        [
            (Decision.REFUSE, Decision.REFUSE, True),
            (Decision.CLARIFY, Decision.CLARIFY, True),
            (
                Decision.REFUSE,
                Decision.CLARIFY,
                False,
            ),  # strict per-case; summary has the lenient view
        ],
    )
    def test_decline_cases(self, expected: Decision, decision: Decision, correct: bool) -> None:
        run, _ = runner([Generation(decision, "no", clarifying_question="?")], [])
        (res,) = run.execute([case("r", expected, gold=None)], label="t")
        assert res.correct is correct and res.execution_match is None

    def test_answering_a_should_refuse_question_is_incorrect(self) -> None:
        run, _ = runner([answer()], [result(["n"], [1])])
        (res,) = run.execute([case("r", Decision.REFUSE, gold=None)], label="t")
        assert not res.correct and res.outcome is Outcome.ANSWERED

    def test_every_case_is_audited_with_a_traceable_label(self) -> None:
        run, audit = runner([Generation(Decision.REFUSE, "no")], [])
        (res,) = run.execute([case("r1", Decision.REFUSE, gold=None)], label="improved-x")
        assert audit.records[res.request_id].user_label == "eval:improved-x:r1"

    def test_broken_gold_query_is_raised_not_scored(self) -> None:
        run, _ = runner([answer()], [result(["n"], [1]), QueryExecutionError("syntax")])
        with pytest.raises(RuntimeError, match="gold SQL for a failed"):
            run.execute([case("a", Decision.ANSWER)], label="t")

    def test_progress_callback(self) -> None:
        seen: list[str] = []
        run, _ = runner([Generation(Decision.REFUSE, "no")], [])
        run.execute(
            [case("r", Decision.REFUSE, gold=None)],
            label="t",
            on_result=lambda r: seen.append(r.case_id),
        )
        assert seen == ["r"]


class TestSummary:
    def test_metrics(self) -> None:
        cases = [
            case("a1", Decision.ANSWER, tags=("easy",)),
            case("a2", Decision.ANSWER, tags=("hard",)),
            case("r1", Decision.REFUSE, gold=None, tags=("refuse",)),
            case("c1", Decision.CLARIFY, gold=None, tags=("clarify",)),
        ]
        good = result(["n"], [1])
        run, _ = runner(
            [
                answer(),  # a1: correct
                Generation(Decision.REFUSE, "no"),  # a2: false decline
                answer(),  # r1: unsafe answer
                Generation(Decision.REFUSE, "no"),  # c1: declined, wrong kind
            ],
            [good, good, good],
        )
        results = run.execute(cases, label="t")
        s = summarize(results, cases)

        assert (s.total, s.answerable, s.unanswerable) == (4, 2, 2)
        assert s.execution_accuracy == 0.5
        assert s.false_decline_rate == 0.5
        assert s.decline_accuracy == 0.5  # c1 declined (lenient), r1 answered
        assert s.unsafe_answer_rate == 0.5
        assert s.decision_accuracy == 0.25  # only a1 has the exact expected decision
        assert s.by_tag == {"clarify": (0, 1), "easy": (1, 1), "hard": (0, 1), "refuse": (0, 1)}

    def test_empty(self) -> None:
        s = summarize([], [])
        assert s.total == 0 and s.execution_accuracy == 0.0


class TestCheckGold:
    def test_reports_each_failure_mode(self) -> None:
        cases = [
            case("ok", Decision.ANSWER, gold="SELECT ok"),
            case("blocked", Decision.ANSWER, gold="SELECT tax_id"),
            case("broken", Decision.ANSWER, gold="SELECT broken"),
            case("empty", Decision.ANSWER, gold="SELECT empty"),
            case("trunc", Decision.ANSWER, gold="SELECT trunc"),
            case("null", Decision.ANSWER, gold="SELECT null"),
            case("skip", Decision.REFUSE, gold=None),
        ]
        validator = RuleValidator(
            lambda s: ValidationResult((Violation("R006", "x"),), None) if "tax_id" in s else None
        )
        executor = ScriptedExecutor(
            result(["n"], [1]),
            QueryExecutionError("syntax error"),
            result(["n"]),
            result(["n"], [1], truncated=True),
            result(["n"], [None]),
        )

        checks = {
            c.case_id: c for c in CheckGold(validator=validator, executor=executor).execute(cases)
        }

        assert set(checks) == {"ok", "blocked", "broken", "empty", "trunc", "null"}
        assert checks["ok"].ok and checks["ok"].row_count == 1
        assert checks["blocked"].detail == "validator rejected: R006"
        assert "syntax error" in checks["broken"].detail
        assert checks["empty"].detail == "empty result"
        assert checks["trunc"].detail == "truncated result"
        assert checks["null"].detail == "NULL in single-value result"
