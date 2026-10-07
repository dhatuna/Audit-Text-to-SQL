"""CLI behaviour with in-memory services: no database, no API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from auditable_sql.application.ask_question import AskQuestion
from auditable_sql.application.evaluate import CheckGold, EvalCase, RunEvaluation
from auditable_sql.application.replay import ReplayAudit
from auditable_sql.domain.models import Decision, Generation, ValidationResult, Violation
from auditable_sql.domain.policy import RuntimeLimits
from auditable_sql.interfaces.cli import build_app
from auditable_sql.interfaces.services import Services
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

runner = CliRunner()
SQL = "SELECT SUM(market_value) AS aum FROM positions_eod"
CASES = [
    EvalCase("a1", "AUM?", Decision.ANSWER, "dev", gold_sql="SELECT gold"),
    EvalCase("r1", "tax ids", Decision.REFUSE, "holdout"),
]


class World:
    """Wires fakes into a Services bundle, the way the composition root wires adapters."""

    def __init__(self, *gens: Generation, results: tuple[object, ...] = ()) -> None:
        self.audit = InMemoryAudit()
        self.generator = ScriptedGenerator(*gens)
        self.executor = ScriptedExecutor(*results)  # type: ignore[arg-type]
        self.validator = RuleValidator(
            lambda s: (
                ValidationResult((Violation("R006_COLUMN_NOT_ALLOWED", "hidden"),), None)
                if "tax_id" in s
                else None
            )
        )
        self.ids = SequentialIds()
        self.seeded: list[int] = []

    def ask(self, baseline: bool = False) -> AskQuestion:
        return AskQuestion(
            context_provider=StaticContext(),
            generator=self.generator,
            validator=self.validator,
            executor=self.executor,
            composer=EchoComposer("Total AUM is 42."),
            audit=self.audit,
            clock=FixedClock(),
            ids=self.ids,
            limits=RuntimeLimits(max_repair_attempts=0 if baseline else 1),
        )

    def services(self) -> Services:
        return Services(
            ask=self.ask,
            replay=lambda: ReplayAudit(
                audit=self.audit, validator=self.validator, executor=self.executor
            ),
            audit=self.audit,
            evaluation=lambda variant: RunEvaluation(
                ask=self.ask(variant == "baseline"), gold_executor=self.executor
            ),
            check_gold=lambda: CheckGold(validator=self.validator, executor=self.executor),
            variants=("improved", "baseline"),
            seed=lambda s: self.seeded.append(s) or {"trades": 10},  # type: ignore[func-returns-value]
            load_cases=lambda _: CASES,
            model="fake-model",
        )

    def invoke(self, *args: str) -> tuple[int, str]:
        console = Console(width=200, force_terminal=False, color_system=None)
        app = build_app(self.services(), console)
        with console.capture() as cap:
            res = runner.invoke(app, list(args))
        return res.exit_code, cap.get() + res.output


def answered(sql: str = SQL) -> Generation:
    return Generation(Decision.ANSWER, "sum of market value", sql=sql, assumptions=("latest date",))


def test_ask_prints_sql_validation_result_answer_and_request_id() -> None:
    w = World(answered(), results=(result(["aum"], [42]),))
    code, out = w.invoke("ask", "What is AUM?", "--user", "jk")

    assert code == 0
    assert "ANSWERED" in out and "req-0001" in out
    assert "SELECT SUM(market_value)" in out
    assert "✓ validation passed" in out
    assert "Total AUM is 42." in out
    assert "✓ grounding" in out
    assert w.audit.records["req-0001"].user_label == "jk"


def test_ask_shows_rule_violations() -> None:
    w = World(answered("SELECT tax_id FROM clients"), answered(), results=(result(["aum"], [42]),))
    _, out = w.invoke("ask", "tax ids")
    assert "R006_COLUMN_NOT_ALLOWED" in out and "attempt 2" in out


def test_ask_flags_ungrounded_numbers() -> None:
    w = World(answered(), results=(result(["aum"], [7]),))
    _, out = w.invoke("ask", "q")
    assert "✗ grounding" in out and "42" in out


def test_seed() -> None:
    w = World()
    code, out = w.invoke("seed", "--seed", "3")
    assert code == 0 and w.seeded == [3] and "trades" in out


def test_audit_show_and_list() -> None:
    w = World(Generation(Decision.REFUSE, "Personal details are not available."))
    w.invoke("ask", "tax id please")

    code, out = w.invoke("audit", "show", "req-0001")
    assert code == 0 and "REFUSED" in out and "Personal details" in out and "sha256" in out

    code, out = w.invoke("audit", "list")
    assert code == 0 and "req-0001" in out and "refused" in out


def test_audit_show_unknown_id() -> None:
    code, out = World().invoke("audit", "show", "nope")
    assert code == 1 and "no record" in out


def test_audit_replay_match_and_mismatch() -> None:
    w = World(
        answered(),
        answered(),
        results=(result(["aum"], [42]), result(["aum"], [42]), result(["aum"], [43])),
    )
    w.invoke("ask", "q")

    code, out = w.invoke("audit", "replay", "req-0001")
    assert code == 0 and "MATCH" in out

    code, out = w.invoke("audit", "replay", "req-0001")
    assert code == 2 and "MISMATCH" in out


def test_audit_replay_unknown_id() -> None:
    code, _ = World().invoke("audit", "replay", "nope")
    assert code == 1


def test_eval_run_writes_results(tmp_path: Path) -> None:
    gold = result(["aum"], [42])
    w = World(answered(), Generation(Decision.REFUSE, "no"), results=(gold, gold))

    code, out = w.invoke("eval", "run", "--out", str(tmp_path))

    assert code == 0, out
    (path,) = tmp_path.glob("improved-*.json")
    payload = json.loads(path.read_text())
    assert payload["summary"]["execution_accuracy"] == 1.0
    assert payload["summary"]["decline_accuracy"] == 1.0
    assert set(payload["splits"]) == {"dev", "holdout"}
    assert [r["case_id"] for r in payload["results"]] == ["a1", "r1"]
    assert "execution accuracy" in out


def test_eval_run_split_filter(tmp_path: Path) -> None:
    w = World(Generation(Decision.REFUSE, "no"))
    code, _ = w.invoke("eval", "run", "--split", "holdout", "--out", str(tmp_path))
    payload = json.loads(next(tmp_path.glob("*.json")).read_text())
    assert code == 0 and [r["case_id"] for r in payload["results"]] == ["r1"]


@pytest.mark.parametrize("args", [["--variant", "nope"], ["--only", "zzz"]])
def test_eval_run_rejects_bad_selection(args: list[str], tmp_path: Path) -> None:
    code, _ = World().invoke("eval", "run", "--out", str(tmp_path), *args)
    assert code == 2


def test_eval_check_gold() -> None:
    w = World(results=(result(["aum"], [42]),))
    code, out = w.invoke("eval", "check-gold")
    assert code == 0 and "1/1 gold queries OK" in out
