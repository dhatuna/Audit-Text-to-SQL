"""Streamlit demo UI. Launched via bootstrap/streamlit_main.py, which supplies Services."""

from __future__ import annotations

import pandas as pd
import streamlit as st

from auditable_sql.domain.models import AuditRecord, Outcome, QueryResult
from auditable_sql.interfaces.services import Services

EXAMPLES = (
    "What is our total AUM right now?",
    "Show the top 5 portfolios by current AUM, largest first.",
    "What is our sector exposure across all portfolios as of the latest date?",
    "Which portfolio is doing best?",
    "What is the tax ID of the client who owns Cedar Income 04?",
    "Delete all trades from December 2025.",
)
BADGE = {
    Outcome.ANSWERED: ":green[ANSWERED]",
    Outcome.CLARIFIED: ":orange[NEEDS CLARIFICATION]",
    Outcome.REFUSED: ":orange[REFUSED]",
    Outcome.BLOCKED: ":red[BLOCKED]",
    Outcome.FAILED: ":red[FAILED]",
}


def frame(result: QueryResult) -> pd.DataFrame:
    return pd.DataFrame([list(r) for r in result.rows], columns=list(result.columns))


def show_record(services: Services, record: AuditRecord, scope: str) -> None:
    """`scope` keeps widget keys unique when the same record is shown in two tabs."""
    st.markdown(
        f"### {BADGE[record.outcome]}\n"
        f"`request_id {record.request_id}` · {record.total_latency_ms / 1000:.1f}s · "
        f"context `{record.context.version}` · prompt `{record.prompt_version}`"
    )
    if record.answer:
        (st.success if record.outcome is Outcome.ANSWERED else st.warning)(record.answer.text)
    if record.error:
        st.error(record.error)
    if record.grounding is not None:
        if record.grounding.passed:
            n = len(record.grounding.checked_numbers)
            st.caption(f"Grounding: all {n} numbers in the answer appear in the result.")
        else:
            st.error(
                "Grounding: not found in result: " + ", ".join(record.grounding.unmatched_numbers)
            )

    final = record.final_attempt
    if final and final.result is not None and record.outcome is Outcome.ANSWERED:
        note = " (truncated)" if final.result.truncated else ""
        st.markdown(
            f"**Result** · {final.result.row_count} rows · {final.result.elapsed_ms:.0f} ms{note}"
        )
        st.dataframe(frame(final.result), hide_index=True, width="stretch")

    st.markdown("**Trace**")
    for attempt in record.attempts:
        g = attempt.generation
        with st.expander(
            f"Attempt {attempt.number} · {g.decision.value} · {g.reason}", expanded=True
        ):
            if g.assumptions:
                st.markdown("Assumptions: " + "; ".join(g.assumptions))
            if g.clarifying_question:
                st.markdown(f"Clarifying question: _{g.clarifying_question}_")
            if g.sql:
                st.code(g.sql, language="sql")
            if attempt.validation is not None:
                if attempt.validation.passed:
                    st.markdown(":green[✓ validation passed] → executed:")
                    st.code(attempt.validation.normalized_sql or "", language="sql")
                for v in attempt.validation.violations:
                    st.markdown(f":red[✗ `{v.rule_id}`] {v.message}")
            if attempt.execution_error:
                st.markdown(f":red[✗ database:] {attempt.execution_error}")
            st.caption(
                f"model {g.model or '—'} · tokens {g.input_tokens:,} in / {g.output_tokens:,} out"
            )

    if record.final_sql and st.button(
        "Replay stored SQL", key=f"{scope}-replay-{record.request_id}"
    ):
        report = services.replay().execute(record.request_id)
        (st.success if report.matches else st.error)(
            f"{report.reason} · fingerprint {report.replay_fingerprint}"
        )
    with st.expander("Schema context sent to the model"):
        st.caption(f"sha256 {record.context.sha256}")
        st.text(record.context.text)


def run(services: Services) -> None:
    st.set_page_config(page_title="Auditable Text-to-SQL", layout="wide")
    st.title("Auditable Text-to-SQL")
    st.caption(
        "Synthetic investment-portfolio data. Every question is validated, executed under a "
        "read-only role, and written to an append-only audit log."
    )

    with st.sidebar:
        st.markdown("**Try**")
        for q in EXAMPLES:
            if st.button(q, width="stretch"):
                st.session_state["question"] = q
        baseline = st.toggle("Baseline pipeline (raw DDL, no repair)", value=False)
        user = st.text_input("User label (audit)", value="demo")

    ask_tab, audit_tab = st.tabs(["Ask", "Audit log"])
    with ask_tab:
        question = st.text_input("Question", key="question")
        if st.button("Ask", type="primary") and question.strip():
            with st.spinner("Generating, validating, executing…"):
                st.session_state["last"] = services.ask(baseline).execute(question, user_label=user)
        if "last" in st.session_state:
            show_record(services, st.session_state["last"], scope="ask")

    with audit_tab:
        records = services.audit.recent(50)
        if not records:
            st.info("No requests yet.")
        else:
            table = pd.DataFrame(
                [
                    {
                        "created": r.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                        "outcome": r.outcome.value,
                        "user": r.user_label,
                        "question": r.question,
                        "request_id": r.request_id,
                    }
                    for r in records
                ]
            )
            st.dataframe(table, hide_index=True, width="stretch")
            rid = st.selectbox("Inspect", [r.request_id for r in records])
            record = services.audit.get(rid) if rid else None
            if record:
                show_record(services, record, scope="audit")
