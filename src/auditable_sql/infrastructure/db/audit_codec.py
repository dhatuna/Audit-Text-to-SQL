"""Lossless JSON encoding of audit records.

Result cells are type-tagged ({"$dec": "1.50"}) so a record read back from the database
re-fingerprints to exactly the same hash as the one written.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from auditable_sql.domain.models import (
    Answer,
    Attempt,
    AuditRecord,
    Cell,
    Decision,
    Generation,
    GroundingReport,
    Outcome,
    QueryResult,
    SchemaContext,
    ValidationResult,
    Violation,
)

Json = dict[str, Any]


def encode_cell(cell: Cell) -> Any:
    if isinstance(cell, Decimal):
        return {"$dec": str(cell)}
    if isinstance(cell, datetime):
        return {"$ts": cell.isoformat()}
    if isinstance(cell, date):
        return {"$date": cell.isoformat()}
    if cell is None or isinstance(cell, bool | int | float | str):
        return cell
    return {"$str": str(cell)}  # e.g. timedelta, UUID: keep something readable


def decode_cell(value: Any) -> Cell:
    if isinstance(value, dict):
        if "$dec" in value:
            return Decimal(value["$dec"])
        if "$ts" in value:
            return datetime.fromisoformat(value["$ts"])
        if "$date" in value:
            return date.fromisoformat(value["$date"])
        if "$str" in value:
            return str(value["$str"])
    return value  # type: ignore[no-any-return]


def encode_result(r: QueryResult) -> Json:
    return {
        "columns": list(r.columns),
        "rows": [[encode_cell(c) for c in row] for row in r.rows],
        "truncated": r.truncated,
        "elapsed_ms": r.elapsed_ms,
    }


def decode_result(j: Json) -> QueryResult:
    return QueryResult(
        columns=tuple(j["columns"]),
        rows=tuple(tuple(decode_cell(c) for c in row) for row in j["rows"]),
        truncated=j["truncated"],
        elapsed_ms=j["elapsed_ms"],
    )


def encode_attempt(a: Attempt) -> Json:
    g = a.generation
    return {
        "number": a.number,
        "generation": {
            "decision": g.decision.value,
            "reason": g.reason,
            "sql": g.sql,
            "clarifying_question": g.clarifying_question,
            "assumptions": list(g.assumptions),
            "model": g.model,
            "raw": g.raw,
            "input_tokens": g.input_tokens,
            "output_tokens": g.output_tokens,
        },
        "validation": None
        if a.validation is None
        else {
            "violations": [
                {"rule_id": v.rule_id, "message": v.message} for v in a.validation.violations
            ],
            "normalized_sql": a.validation.normalized_sql,
        },
        "result": None if a.result is None else encode_result(a.result),
        "execution_error": a.execution_error,
    }


def decode_attempt(j: Json) -> Attempt:
    g = j["generation"]
    v = j["validation"]
    return Attempt(
        number=j["number"],
        generation=Generation(
            decision=Decision(g["decision"]),
            reason=g["reason"],
            sql=g["sql"],
            clarifying_question=g["clarifying_question"],
            assumptions=tuple(g["assumptions"]),
            model=g["model"],
            raw=g["raw"],
            input_tokens=g["input_tokens"],
            output_tokens=g["output_tokens"],
        ),
        validation=None
        if v is None
        else ValidationResult(
            violations=tuple(Violation(x["rule_id"], x["message"]) for x in v["violations"]),
            normalized_sql=v["normalized_sql"],
        ),
        result=None if j["result"] is None else decode_result(j["result"]),
        execution_error=j["execution_error"],
    )


def encode_answer(a: Answer | None) -> Json | None:
    if a is None:
        return None
    return {
        "text": a.text,
        "model": a.model,
        "input_tokens": a.input_tokens,
        "output_tokens": a.output_tokens,
    }


def decode_answer(j: Json | None) -> Answer | None:
    return None if j is None else Answer(**j)


def encode_grounding(g: GroundingReport | None) -> Json | None:
    if g is None:
        return None
    return {"checked": list(g.checked_numbers), "unmatched": list(g.unmatched_numbers)}


def decode_grounding(j: Json | None) -> GroundingReport | None:
    if j is None:
        return None
    return GroundingReport(tuple(j["checked"]), tuple(j["unmatched"]))


COLUMNS = (
    "request_id", "created_at", "user_label", "question",
    "context_version", "context_sha256", "context_text", "prompt_version",
    "outcome", "attempts", "final_sql", "result_fingerprint",
    "answer", "grounding", "total_latency_ms", "error",
)  # fmt: skip


def to_row(r: AuditRecord) -> dict[str, Any]:
    return {
        "request_id": r.request_id,
        "created_at": r.created_at,
        "user_label": r.user_label,
        "question": r.question,
        "context_version": r.context.version,
        "context_sha256": r.context.sha256,
        "context_text": r.context.text,
        "prompt_version": r.prompt_version,
        "outcome": r.outcome.value,
        "attempts": [encode_attempt(a) for a in r.attempts],
        "final_sql": r.final_sql,
        "result_fingerprint": r.result_fingerprint,
        "answer": encode_answer(r.answer),
        "grounding": encode_grounding(r.grounding),
        "total_latency_ms": r.total_latency_ms,
        "error": r.error,
    }


def from_row(row: dict[str, Any]) -> AuditRecord:
    return AuditRecord(
        request_id=row["request_id"],
        created_at=row["created_at"],
        user_label=row["user_label"],
        question=row["question"],
        context=SchemaContext(
            text=row["context_text"], version=row["context_version"], sha256=row["context_sha256"]
        ),
        prompt_version=row["prompt_version"],
        attempts=tuple(decode_attempt(a) for a in row["attempts"]),
        outcome=Outcome(row["outcome"]),
        final_sql=row["final_sql"],
        result_fingerprint=row["result_fingerprint"],
        answer=decode_answer(row["answer"]),
        grounding=decode_grounding(row["grounding"]),
        total_latency_ms=row["total_latency_ms"],
        error=row["error"],
    )
