"""Claude-backed SqlGenerator and AnswerComposer.

Structured output (a Pydantic schema) is used for generation so the decision, SQL and
assumptions arrive as typed fields rather than text to be parsed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, Field

from auditable_sql.application.ports import RepairFeedback
from auditable_sql.domain.errors import GenerationError
from auditable_sql.domain.models import Answer, Decision, Generation, QueryResult, SchemaContext
from auditable_sql.infrastructure.llm import prompts

FALLBACK_BETA = "server-side-fallback-2026-07-01"
MAX_RESULT_ROWS_IN_PROMPT = 50


class GenerationOutput(BaseModel):
    decision: Literal["answer", "clarify", "refuse"]
    reason: str = Field(description="One sentence explaining the decision.")
    sql: str | None = Field(default=None, description="Required when decision is 'answer'.")
    clarifying_question: str | None = Field(
        default=None, description="Required when decision is 'clarify'."
    )
    assumptions: list[str] = Field(default_factory=list)


def _usage(response: Any) -> dict[str, int]:
    u = response.usage
    return {
        "input_tokens": u.input_tokens,
        "output_tokens": u.output_tokens,
        "cache_read_tokens": u.cache_read_input_tokens or 0,
        "cache_write_tokens": u.cache_creation_input_tokens or 0,
    }


class _ClaudeCaller:
    def __init__(
        self, client: anthropic.Anthropic, *, model: str, effort: str, use_fallbacks: bool
    ) -> None:
        self._client = client
        self._model = model
        self._effort = effort
        self._use_fallbacks = use_fallbacks

    def _call(self, *, system: str, user: str, output_format: Any = None) -> Any:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 16_000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": self._effort},
            "cache_control": {"type": "ephemeral"},
        }
        if self._use_fallbacks:
            kwargs["betas"] = [FALLBACK_BETA]
            kwargs["fallbacks"] = "default"
        try:
            if output_format is not None:
                response = self._client.beta.messages.parse(output_format=output_format, **kwargs)
            else:
                response = self._client.beta.messages.create(**kwargs)
        except anthropic.RateLimitError as exc:
            raise GenerationError(f"rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise GenerationError(f"API error {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise GenerationError(f"connection error: {exc}") from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise GenerationError(f"model declined the request (category={category})")
        if response.stop_reason == "max_tokens":
            raise GenerationError("model output hit max_tokens")
        return response


class ClaudeSqlGenerator(_ClaudeCaller):
    prompt_version = prompts.GENERATOR_VERSION

    def generate(
        self, question: str, context: SchemaContext, feedback: Sequence[RepairFeedback]
    ) -> Generation:
        user = prompts.GENERATOR_USER.format(question=question)
        if feedback:
            attempts = "\n".join(
                f"Attempt {i}:\nSQL: {f.sql or '(none)'}\nProblem: {f.problem}"
                for i, f in enumerate(feedback, start=1)
            )
            user += prompts.REPAIR_SUFFIX.format(attempts=attempts)

        response = self._call(
            system=prompts.GENERATOR_SYSTEM.format(context=context.text),
            user=user,
            output_format=GenerationOutput,
        )
        parsed: GenerationOutput | None = response.parsed_output
        if parsed is None:
            raise GenerationError("structured output missing from response")

        return Generation(
            decision=Decision(parsed.decision),
            reason=parsed.reason,
            sql=parsed.sql,
            clarifying_question=parsed.clarifying_question,
            assumptions=tuple(parsed.assumptions),
            model=response.model,
            raw=parsed.model_dump(),
            **_usage(response),
        )


def render_table(result: QueryResult, limit: int = MAX_RESULT_ROWS_IN_PROMPT) -> str:
    def fmt(v: object) -> str:
        return "NULL" if v is None else str(v)

    lines = [" | ".join(result.columns)]
    lines += [" | ".join(fmt(c) for c in row) for row in result.rows[:limit]]
    if result.row_count > limit:
        lines.append(f"... ({result.row_count - limit} more rows not shown)")
    return "\n".join(lines)


class ClaudeAnswerComposer(_ClaudeCaller):
    prompt_version = prompts.COMPOSER_VERSION

    def compose(
        self, question: str, sql: str, result: QueryResult, assumptions: Sequence[str]
    ) -> Answer:
        user = prompts.COMPOSER_USER.format(
            question=question,
            assumptions="\n".join(f"- {a}" for a in assumptions) or "- none",
            sql=sql,
            row_count=result.row_count,
            truncated=", truncated" if result.truncated else "",
            table=render_table(result),
        )
        response = self._call(system=prompts.COMPOSER_SYSTEM, user=user)
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text")
        if not text.strip():
            raise GenerationError("empty answer")
        return Answer(
            text=text.strip(),
            model=response.model,
            **_usage(response),
        )
