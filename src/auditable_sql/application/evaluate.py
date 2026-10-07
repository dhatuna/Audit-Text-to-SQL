"""Use case: run an evaluation set through the full pipeline and score it.

Answerable cases are scored by execution accuracy: the gold SQL and the pipeline's SQL are
both executed and their results compared (see domain.results.results_match). Cases that
should be declined are scored on the decision alone.

Every eval question goes through AskQuestion, so each scored case has an audit record.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.application.ports import QueryExecutor
from auditable_sql.domain.errors import QueryExecutionError
from auditable_sql.domain.models import AuditRecord, Decision, Outcome
from auditable_sql.domain.results import results_match

_DECLINED = {Outcome.CLARIFIED, Outcome.REFUSED}
_EXPECTED_OUTCOME = {
    Decision.ANSWER: Outcome.ANSWERED,
    Decision.CLARIFY: Outcome.CLARIFIED,
    Decision.REFUSE: Outcome.REFUSED,
}


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    question: str
    expected: Decision
    split: str
    gold_sql: str | None = None
    ordered: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.expected is Decision.ANSWER) != (self.gold_sql is not None):
            raise ValueError(f"{self.id}: gold_sql is required iff expected == answer")


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    split: str
    expected: Decision
    outcome: Outcome
    correct: bool
    # For answerable cases: did the results match? None when not applicable.
    execution_match: bool | None
    request_id: str
    attempts: int
    latency_ms: float
    input_tokens: int
    output_tokens: int
    grounding_passed: bool | None
    note: str = ""


@dataclass(frozen=True, slots=True)
class Summary:
    total: int
    answerable: int
    unanswerable: int
    execution_accuracy: float
    decline_accuracy: float  # declined (clarify or refuse) when it should have
    decision_accuracy: float  # exact decision match across all cases
    false_decline_rate: float  # answerable questions the system declined
    unsafe_answer_rate: float  # should-decline questions it answered anyway
    blocked_or_failed: int
    grounding_failures: int
    repaired: int
    latency_p50_ms: float
    latency_p95_ms: float
    input_tokens: int
    output_tokens: int
    by_tag: dict[str, tuple[int, int]] = field(default_factory=dict)  # tag -> (correct, total)


def _tokens(record: AuditRecord) -> tuple[int, int]:
    inp = sum(a.generation.input_tokens for a in record.attempts)
    out = sum(a.generation.output_tokens for a in record.attempts)
    if record.answer:
        inp += record.answer.input_tokens
        out += record.answer.output_tokens
    return inp, out


class RunEvaluation:
    def __init__(self, *, ask: AskQuestion, gold_executor: QueryExecutor) -> None:
        self._ask = ask
        self._gold = gold_executor

    def score(self, case: EvalCase, record: AuditRecord) -> CaseResult:
        match: bool | None = None
        note = ""
        if case.expected is Decision.ANSWER:
            final = record.final_attempt
            if record.outcome is Outcome.ANSWERED and final and final.result and case.gold_sql:
                try:
                    gold = self._gold.execute(case.gold_sql)
                except QueryExecutionError as exc:
                    # A broken gold query is a bug in the eval set, not a model error.
                    raise RuntimeError(f"gold SQL for {case.id} failed: {exc}") from exc
                match = results_match(gold, final.result, ordered=case.ordered)
                if not match:
                    note = f"gold rows={gold.row_count}, got rows={final.result.row_count}"
            else:
                match = False
                note = f"not answered ({record.outcome})"
            correct = bool(match)
        else:
            correct = record.outcome is _EXPECTED_OUTCOME[case.expected]
            if record.outcome in _DECLINED and not correct:
                note = f"declined as {record.outcome}, expected {case.expected}"

        inp, out = _tokens(record)
        return CaseResult(
            case_id=case.id,
            split=case.split,
            expected=case.expected,
            outcome=record.outcome,
            correct=correct,
            execution_match=match,
            request_id=record.request_id,
            attempts=len(record.attempts),
            latency_ms=record.total_latency_ms,
            input_tokens=inp,
            output_tokens=out,
            grounding_passed=record.grounding.passed if record.grounding else None,
            note=note or (record.error or ""),
        )

    def execute(
        self,
        cases: Sequence[EvalCase],
        *,
        label: str,
        on_result: Callable[[CaseResult], None] | None = None,
    ) -> list[CaseResult]:
        results = []
        for case in cases:
            record = self._ask.execute(case.question, user_label=f"eval:{label}:{case.id}")
            result = self.score(case, record)
            results.append(result)
            if on_result:
                on_result(result)
        return results


def summarize(results: Sequence[CaseResult], cases: Sequence[EvalCase]) -> Summary:
    tags = {c.id: c.tags for c in cases}
    answerable = [r for r in results if r.expected is Decision.ANSWER]
    unanswerable = [r for r in results if r.expected is not Decision.ANSWER]
    latencies = sorted(r.latency_ms for r in results) or [0.0]

    def ratio(n: int, d: int) -> float:
        return n / d if d else 0.0

    by_tag: dict[str, tuple[int, int]] = {}
    for r in results:
        for t in tags.get(r.case_id, ()):
            ok, total = by_tag.get(t, (0, 0))
            by_tag[t] = (ok + r.correct, total + 1)

    return Summary(
        total=len(results),
        answerable=len(answerable),
        unanswerable=len(unanswerable),
        execution_accuracy=ratio(sum(r.correct for r in answerable), len(answerable)),
        decline_accuracy=ratio(
            sum(r.outcome in _DECLINED for r in unanswerable), len(unanswerable)
        ),
        decision_accuracy=ratio(
            sum(r.outcome is _EXPECTED_OUTCOME[r.expected] for r in results), len(results)
        ),
        false_decline_rate=ratio(sum(r.outcome in _DECLINED for r in answerable), len(answerable)),
        unsafe_answer_rate=ratio(
            sum(r.outcome is Outcome.ANSWERED for r in unanswerable), len(unanswerable)
        ),
        blocked_or_failed=sum(r.outcome in (Outcome.BLOCKED, Outcome.FAILED) for r in results),
        grounding_failures=sum(r.grounding_passed is False for r in results),
        repaired=sum(r.attempts > 1 and r.outcome is Outcome.ANSWERED for r in results),
        latency_p50_ms=statistics.median(latencies),
        latency_p95_ms=latencies[min(len(latencies) - 1, round(0.95 * (len(latencies) - 1)))],
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        by_tag=dict(sorted(by_tag.items())),
    )
