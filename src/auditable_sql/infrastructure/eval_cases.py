"""Load evaluation cases from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml

from auditable_sql.application.evaluate import EvalCase
from auditable_sql.domain.models import Decision


def load_cases(path: Path) -> list[EvalCase]:
    raw = yaml.safe_load(path.read_text())
    cases = [
        EvalCase(
            id=str(c["id"]),
            question=str(c["question"]).strip(),
            expected=Decision(c["expected"]),
            split=str(c["split"]),
            gold_sql=(str(c["gold_sql"]).strip() if c.get("gold_sql") else None),
            ordered=bool(c.get("ordered", False)),
            tags=tuple(c.get("tags", ())),
        )
        for c in raw["cases"]
    ]
    ids = [c.id for c in cases]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise ValueError(f"duplicate case ids: {sorted(duplicates)}")
    return cases
