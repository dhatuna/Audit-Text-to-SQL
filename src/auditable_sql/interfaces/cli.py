"""Command-line interface. Built from a Services bundle so it can be tested with fakes."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.panel import Panel
from rich.syntax import Syntax
from rich.table import Table

from auditable_sql.application.evaluate import CaseResult, Summary, summarize
from auditable_sql.domain.errors import RecordNotFoundError
from auditable_sql.domain.models import AuditRecord, Outcome, QueryResult
from auditable_sql.interfaces.services import Services

# Claude Opus 5.5 list prices (USD per million tokens), used only for the cost estimate.
PRICE_PER_MTOK = {"input": 4.00, "output": 20.00}

OUTCOME_STYLE = {
    Outcome.ANSWERED: "green",
    Outcome.CLARIFIED: "yellow",
    Outcome.REFUSED: "yellow",
    Outcome.BLOCKED: "red",
    Outcome.FAILED: "red",
}


def result_table(result: QueryResult, limit: int = 20) -> Table:
    table = Table(show_lines=False, header_style="bold")
    for col in result.columns:
        table.add_column(col)
    for row in result.rows[:limit]:
        table.add_row(*("NULL" if c is None else str(c) for c in row))
    if result.row_count > limit or result.truncated:
        more = "more" if result.truncated else str(result.row_count - limit)
        shown = min(limit, result.row_count)
        table.caption = f"showing {shown} of {result.row_count} rows ({more})"
    return table


def render_record(console: Console, record: AuditRecord, *, verbose: bool = False) -> None:
    style = OUTCOME_STYLE[record.outcome]
    console.print(
        f"[bold]{record.question}[/bold]\n"
        f"[{style}]{record.outcome.value.upper()}[/{style}]  "
        f"request_id={record.request_id}  {record.total_latency_ms:,.0f} ms"
    )
    for attempt in record.attempts:
        g = attempt.generation
        console.print(
            f"\n[dim]attempt {attempt.number} · decision={g.decision.value} · {g.reason}[/dim]"
        )
        if g.assumptions:
            console.print("[dim]assumptions: " + "; ".join(g.assumptions) + "[/dim]")
        if g.sql:
            console.print(Syntax(g.sql, "sql", word_wrap=True, theme="ansi_dark"))
        if attempt.validation and not attempt.validation.passed:
            for v in attempt.validation.violations:
                console.print(f"  [red]✗ {v.rule_id}[/red] {v.message}")
        elif attempt.validation:
            console.print("  [green]✓ validation passed[/green]")
        if attempt.execution_error:
            console.print(f"  [red]✗ execution:[/red] {attempt.execution_error}")
        if attempt.result is not None and (verbose or attempt is record.final_attempt):
            console.print(result_table(attempt.result))
    if record.answer:
        console.print(Panel(record.answer.text, title="answer", border_style=style))
    if record.grounding is not None:
        if record.grounding.passed:
            n = len(record.grounding.checked_numbers)
            console.print(f"[green]✓ grounding[/green] {n} numbers traced to result")
        else:
            console.print(
                "[red]✗ grounding[/red] not found in result: "
                + ", ".join(record.grounding.unmatched_numbers)
            )
    if record.error:
        console.print(f"[red]error:[/red] {record.error}")
    if verbose:
        console.print(
            f"\n[dim]context {record.context.version} sha256={record.context.sha256[:12]}… "
            f"prompt={record.prompt_version} fingerprint={record.result_fingerprint}[/dim]"
        )


def summary_table(name: str, s: Summary) -> Table:
    t = Table(title=name, header_style="bold")
    t.add_column("metric")
    t.add_column("value", justify="right")
    cost = (
        s.input_tokens * PRICE_PER_MTOK["input"] + s.output_tokens * PRICE_PER_MTOK["output"]
    ) / 1e6
    for label, value in [
        ("cases (answerable / decline)", f"{s.total} ({s.answerable} / {s.unanswerable})"),
        ("execution accuracy", f"{s.execution_accuracy:.1%}"),
        ("decline accuracy", f"{s.decline_accuracy:.1%}"),
        ("exact decision accuracy", f"{s.decision_accuracy:.1%}"),
        ("false decline rate", f"{s.false_decline_rate:.1%}"),
        ("unsafe answer rate", f"{s.unsafe_answer_rate:.1%}"),
        ("blocked / failed", str(s.blocked_or_failed)),
        ("answered after repair", str(s.repaired)),
        ("grounding failures", str(s.grounding_failures)),
        ("latency p50 / p95", f"{s.latency_p50_ms / 1000:.1f}s / {s.latency_p95_ms / 1000:.1f}s"),
        ("tokens in / out", f"{s.input_tokens:,} / {s.output_tokens:,}"),
        ("est. cost (list price)", f"${cost:.2f}"),
    ]:
        t.add_row(label, value)
    return t


def build_app(services: Services, console: Console | None = None) -> typer.Typer:
    console = console or Console()
    app = typer.Typer(no_args_is_help=True, add_completion=False, help="Auditable text-to-SQL.")
    audit_app = typer.Typer(no_args_is_help=True, help="Inspect and replay the audit log.")
    eval_app = typer.Typer(no_args_is_help=True, help="Run the evaluation set.")
    app.add_typer(audit_app, name="audit")
    app.add_typer(eval_app, name="eval")

    @app.command()
    def seed(seed: Annotated[int, typer.Option(help="RNG seed")] = 7) -> None:
        """(Re)load the synthetic dataset. Uses the admin role."""
        counts = services.seed(seed)
        for table, n in counts.items():
            console.print(f"{table:<14} {n:>9,}")

    @app.command()
    def ask(
        question: str,
        user: Annotated[str, typer.Option(help="Label stored in the audit log")] = "cli",
        baseline: Annotated[bool, typer.Option(help="Use the naive baseline pipeline")] = False,
        verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    ) -> None:
        """Ask a question in plain English."""
        record = services.ask(baseline).execute(question, user_label=user)
        render_record(console, record, verbose=verbose)

    @audit_app.command("list")
    def audit_list(limit: int = 20) -> None:
        """Most recent requests."""
        t = Table(header_style="bold")
        for col in ("created", "request_id", "outcome", "user", "question"):
            t.add_column(col)
        for r in services.audit.recent(limit):
            t.add_row(
                r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                r.request_id,
                f"[{OUTCOME_STYLE[r.outcome]}]{r.outcome.value}[/]",
                r.user_label,
                r.question[:60],
            )
        console.print(t)

    @audit_app.command("show")
    def audit_show(request_id: str) -> None:
        """Everything recorded for one request: context, every attempt, result, answer."""
        record = services.audit.get(request_id)
        if record is None:
            console.print(f"[red]no record {request_id}[/red]")
            raise typer.Exit(1)
        render_record(console, record, verbose=True)

    @audit_app.command("replay")
    def audit_replay(request_id: str) -> None:
        """Re-execute the stored SQL and compare the result fingerprint."""
        try:
            report = services.replay().execute(request_id)
        except RecordNotFoundError:
            console.print(f"[red]no record {request_id}[/red]")
            raise typer.Exit(1) from None
        mark = "[green]✓ MATCH[/green]" if report.matches else "[red]✗ MISMATCH[/red]"
        console.print(f"{mark}  {report.reason}")
        console.print(f"original {report.original_fingerprint}")
        console.print(f"replay   {report.replay_fingerprint}")
        for v in report.current_violations:
            console.print(f"  [red]{v.rule_id}[/red] {v.message}")
        if not report.matches:
            raise typer.Exit(2)

    @eval_app.command("check-gold")
    def eval_check_gold(
        cases_path: Annotated[Path, typer.Option("--cases")] = Path("evals/cases.yaml"),
    ) -> None:
        """Validate and execute every gold query (no API calls)."""
        checks = services.check_gold().execute(services.load_cases(cases_path))
        for c in checks:
            mark = "[green]✓[/green]" if c.ok else "[red]✗[/red]"
            preview = "; ".join(", ".join(map(str, row)) for row in c.preview)
            console.print(
                f"{mark} {c.case_id:<5} rows={c.row_count:<4} {c.detail:<10} {preview[:90]}"
            )
        failed = [c for c in checks if not c.ok]
        console.print(f"{len(checks) - len(failed)}/{len(checks)} gold queries OK")
        if failed:
            raise typer.Exit(1)

    @eval_app.command("run")
    def eval_run(
        cases_path: Annotated[Path, typer.Option("--cases")] = Path("evals/cases.yaml"),
        variant: Annotated[str, typer.Option(help="Pipeline variant")] = "improved",
        split: Annotated[str, typer.Option(help="dev | holdout | all")] = "all",
        only: Annotated[list[str] | None, typer.Option(help="Run only these case ids")] = None,
        out_dir: Annotated[Path, typer.Option("--out")] = Path("evals/results"),
    ) -> None:
        """Run cases through the full pipeline (calls the Claude API: costs money)."""
        if variant not in services.variants:
            raise typer.BadParameter(f"variant must be one of {services.variants}")
        cases = [
            c
            for c in services.load_cases(cases_path)
            if (split == "all" or c.split == split) and (not only or c.id in only)
        ]
        if not cases:
            raise typer.BadParameter("no cases selected")
        label = f"{variant}-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"

        def progress(r: CaseResult) -> None:
            mark = "[green]✓[/green]" if r.correct else "[red]✗[/red]"
            console.print(
                f"{mark} {r.case_id:<8} expected={r.expected.value:<8} got={r.outcome.value:<9} "
                f"{r.latency_ms / 1000:5.1f}s  {r.note}"
            )

        results = services.evaluation(variant).execute(cases, label=label, on_result=progress)
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "label": label,
            "variant": variant,
            "split": split,
            "model": services.model,
            "summary": asdict(summarize(results, cases)),
            "splits": {
                name: asdict(summarize([r for r in results if r.split == name], cases))
                for name in sorted({r.split for r in results})
            },
            "results": [asdict(r) for r in results],
        }
        path = out_dir / f"{label}.json"
        path.write_text(json.dumps(payload, indent=2, default=str))
        console.print(summary_table(label, summarize(results, cases)))
        console.print(f"[dim]written to {path}[/dim]")

    return app
