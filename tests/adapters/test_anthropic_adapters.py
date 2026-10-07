"""Request shaping and error mapping for the Claude adapters. No network calls."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from auditable_sql.application.ports import RepairFeedback
from auditable_sql.domain.errors import GenerationError
from auditable_sql.domain.models import Decision
from auditable_sql.infrastructure.llm import prompts
from auditable_sql.infrastructure.llm.anthropic_adapters import (
    FALLBACK_BETA,
    ClaudeAnswerComposer,
    ClaudeSqlGenerator,
    GenerationOutput,
    render_table,
)
from tests.fakes import CONTEXT, result

# The SDK reports cached prefix tokens separately from input_tokens; absent counts are None.
USAGE = SimpleNamespace(
    input_tokens=120,
    output_tokens=30,
    cache_read_input_tokens=900,
    cache_creation_input_tokens=None,
)


def parsed_response(out: GenerationOutput | None, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        parsed_output=out, stop_reason=stop_reason, model="claude-opus-5-5", usage=USAGE
    )


def text_response(text: str, stop_reason: str = "end_turn") -> Any:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        model="claude-opus-5-5",
        usage=USAGE,
    )


class FakeMessages:
    def __init__(self, response: Any = None, error: Exception | None = None) -> None:
        self.response, self.error = response, error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _do(self, kind: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((kind, kwargs))
        if self.error:
            raise self.error
        return self.response

    def parse(self, **kwargs: Any) -> Any:
        return self._do("parse", kwargs)

    def create(self, **kwargs: Any) -> Any:
        return self._do("create", kwargs)


def client_with(messages: FakeMessages) -> Any:
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def generator(messages: FakeMessages, **kw: Any) -> ClaudeSqlGenerator:
    opts = {"model": "claude-opus-5-5", "effort": "medium", "use_fallbacks": True} | kw
    return ClaudeSqlGenerator(client_with(messages), **opts)


ANSWER = GenerationOutput(
    decision="answer", reason="direct lookup", sql="SELECT 1", assumptions=["latest date"]
)


class TestGenerator:
    def test_maps_structured_output_to_domain(self) -> None:
        g = generator(FakeMessages(parsed_response(ANSWER))).generate("q", CONTEXT, [])

        assert g.decision is Decision.ANSWER
        assert g.sql == "SELECT 1"
        assert g.assumptions == ("latest date",)
        assert g.model == "claude-opus-5-5"
        assert g.raw["decision"] == "answer"
        assert (g.input_tokens, g.output_tokens) == (120, 30)
        assert (g.cache_read_tokens, g.cache_write_tokens) == (900, 0)

    def test_request_shape(self) -> None:
        messages = FakeMessages(parsed_response(ANSWER))
        generator(messages).generate("What is AUM?", CONTEXT, [])

        kind, kw = messages.calls[0]
        assert kind == "parse"
        assert kw["model"] == "claude-opus-5-5"
        assert kw["output_format"] is GenerationOutput
        assert kw["output_config"] == {"effort": "medium"}
        assert CONTEXT.text in kw["system"]  # schema context lives in the cacheable prefix
        assert kw["messages"] == [{"role": "user", "content": "Question: What is AUM?"}]
        assert kw["betas"] == [FALLBACK_BETA] and kw["fallbacks"] == "default"
        assert "temperature" not in kw

    def test_fallbacks_can_be_disabled(self) -> None:
        messages = FakeMessages(parsed_response(ANSWER))
        generator(messages, use_fallbacks=False).generate("q", CONTEXT, [])
        assert "fallbacks" not in messages.calls[0][1] and "betas" not in messages.calls[0][1]

    def test_repair_feedback_is_included(self) -> None:
        messages = FakeMessages(parsed_response(ANSWER))
        feedback = [RepairFeedback("SELECT tax_id FROM clients", "R006: not readable")]
        generator(messages).generate("q", CONTEXT, feedback)

        user = messages.calls[0][1]["messages"][0]["content"]
        assert "SELECT tax_id FROM clients" in user and "R006: not readable" in user

    def test_prompt_version_is_exposed(self) -> None:
        assert ClaudeSqlGenerator.prompt_version == prompts.GENERATOR_VERSION

    @pytest.mark.parametrize(
        ("response", "match"),
        [
            (parsed_response(None), "structured output missing"),
            (parsed_response(None, stop_reason="refusal"), "declined"),
            (parsed_response(None, stop_reason="max_tokens"), "max_tokens"),
        ],
    )
    def test_unusable_responses(self, response: Any, match: str) -> None:
        with pytest.raises(GenerationError, match=match):
            generator(FakeMessages(response)).generate("q", CONTEXT, [])

    @pytest.mark.parametrize(
        ("error", "match"),
        [
            (
                anthropic.RateLimitError(
                    "slow down",
                    response=httpx2.Response(429, request=httpx2.Request("POST", "https://x")),
                    body=None,
                ),
                "rate limited",
            ),
            (
                anthropic.InternalServerError(
                    "boom",
                    response=httpx2.Response(500, request=httpx2.Request("POST", "https://x")),
                    body=None,
                ),
                "API error 500",
            ),
            (
                anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")),
                "connection error",
            ),
        ],
    )
    def test_api_errors_become_domain_errors(self, error: Exception, match: str) -> None:
        with pytest.raises(GenerationError, match=match):
            generator(FakeMessages(error=error)).generate("q", CONTEXT, [])


class TestComposer:
    def composer(self, messages: FakeMessages) -> ClaudeAnswerComposer:
        return ClaudeAnswerComposer(
            client_with(messages), model="claude-opus-5-5", effort="low", use_fallbacks=True
        )

    def test_uses_plain_create_and_returns_text(self) -> None:
        messages = FakeMessages(text_response("  AUM is $1.2M.  "))
        answer = self.composer(messages).compose(
            "q", "SELECT 1", result(["aum"], [1_200_000]), ["latest date"]
        )

        kind, kw = messages.calls[0]
        assert kind == "create" and "output_format" not in kw
        assert answer.text == "AUM is $1.2M."
        assert (answer.input_tokens, answer.cache_read_tokens) == (120, 900)
        assert "- latest date" in kw["messages"][0]["content"]
        assert kw["output_config"] == {"effort": "low"}

    def test_truncation_is_disclosed_to_the_model(self) -> None:
        messages = FakeMessages(text_response("partial"))
        self.composer(messages).compose("q", "s", result(["a"], [1], truncated=True), [])
        assert "truncated" in messages.calls[0][1]["messages"][0]["content"]

    def test_empty_answer_is_an_error(self) -> None:
        with pytest.raises(GenerationError, match="empty"):
            self.composer(FakeMessages(text_response("   "))).compose("q", "s", result(["a"]), [])


def test_render_table_caps_rows() -> None:
    r = result(["n"], *[[i] for i in range(60)])
    text = render_table(r, limit=50)
    assert text.splitlines()[0] == "n"
    assert "10 more rows not shown" in text
    assert render_table(result(["a"], [None])) == "a\nNULL"
