"""JSON encoding of audit attempts and answers, including records written by older versions."""

from __future__ import annotations

from auditable_sql.domain.models import Answer, Attempt, Decision, Generation
from auditable_sql.infrastructure.db.audit_codec import (
    decode_answer,
    decode_attempt,
    encode_answer,
    encode_attempt,
)

GEN = Generation(
    Decision.ANSWER,
    "r",
    sql="SELECT 1",
    input_tokens=40,
    output_tokens=12,
    cache_read_tokens=900,
    cache_write_tokens=0,
)


def test_attempt_round_trip_keeps_cache_token_counts() -> None:
    attempt = Attempt(number=1, generation=GEN)
    assert decode_attempt(encode_attempt(attempt)) == attempt


def test_answer_round_trip_keeps_cache_token_counts() -> None:
    answer = Answer("ok", model="m", input_tokens=5, output_tokens=7, cache_write_tokens=300)
    assert decode_answer(encode_answer(answer)) == answer


def test_records_without_cache_fields_still_decode() -> None:
    j = encode_attempt(Attempt(number=1, generation=GEN))
    del j["generation"]["cache_read_tokens"], j["generation"]["cache_write_tokens"]
    old_answer = {"text": "ok", "model": "m", "input_tokens": 5, "output_tokens": 7}

    g = decode_attempt(j).generation
    a = decode_answer(old_answer)

    assert (g.cache_read_tokens, g.cache_write_tokens) == (0, 0)
    assert a is not None and a.cache_read_tokens == 0
