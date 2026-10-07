"""What the delivery mechanisms (CLI, Streamlit) need, expressed without naming any adapter.

The composition root builds this; interfaces never import infrastructure directly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.application.evaluate import EvalCase, RunEvaluation
from auditable_sql.application.ports import AuditRepository
from auditable_sql.application.replay import ReplayAudit


@dataclass(frozen=True)
class Services:
    ask: Callable[[bool], AskQuestion]  # arg: use the baseline variant
    replay: Callable[[], ReplayAudit]
    audit: AuditRepository
    evaluation: Callable[[str], RunEvaluation]
    variants: tuple[str, ...]
    seed: Callable[[int], dict[str, int]]
    load_cases: Callable[[Path], list[EvalCase]]
    model: str
