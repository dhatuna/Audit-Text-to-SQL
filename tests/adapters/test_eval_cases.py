"""The committed evaluation set is well-formed and its gold SQL obeys the production policy."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from auditable_sql.domain.models import Decision
from auditable_sql.infrastructure.context.semantic_layer import SemanticLayer
from auditable_sql.infrastructure.eval_cases import load_cases
from auditable_sql.infrastructure.validation.sqlglot_validator import SqlglotValidator

ROOT = Path(__file__).resolve().parents[2]
CASES = load_cases(ROOT / "evals" / "cases.yaml")


def test_size_and_mix() -> None:
    assert 30 <= len(CASES) <= 50
    expected = Counter(c.expected for c in CASES)
    assert expected[Decision.ANSWER] >= 25
    assert expected[Decision.CLARIFY] >= 3 and expected[Decision.REFUSE] >= 5


def test_holdout_covers_every_kind() -> None:
    holdout = [c for c in CASES if c.split == "holdout"]
    assert {c.split for c in CASES} == {"dev", "holdout"}
    assert {c.expected for c in holdout} == set(Decision)
    assert {"easy", "medium", "hard"} <= {t for c in holdout for t in c.tags}


@pytest.mark.parametrize("case", [c for c in CASES if c.gold_sql], ids=lambda c: c.id)
def test_gold_sql_passes_the_production_validator(case: object) -> None:
    layer = SemanticLayer.load(ROOT / "config" / "semantic_layer.yaml")
    result = SqlglotValidator(layer.policy(500)).validate(case.gold_sql)  # type: ignore[attr-defined]
    assert result.passed, result.violations


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text(
        "cases:\n"
        "  - {id: a, split: dev, question: q, expected: refuse}\n"
        "  - {id: a, split: dev, question: q, expected: refuse}\n"
    )
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(p)
