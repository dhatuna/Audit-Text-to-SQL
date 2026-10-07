from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = Path(__file__).with_name("streamlit_fake_app.py")


def test_ask_renders_answer_trace_and_replay() -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception

    at.text_input(key="question").set_value("What is AUM?")
    next(b for b in at.button if b.label == "Ask").click()
    at.run()

    assert not at.exception
    markdown = " ".join(m.value for m in at.markdown)
    assert "ANSWERED" in markdown
    assert "R006_COLUMN_NOT_ALLOWED" in markdown  # the blocked first attempt is shown
    assert at.success[0].value == "Total AUM is 42."
    assert any("SELECT SUM(market_value)" in c.value for c in at.code)

    next(b for b in at.button if b.label == "Replay stored SQL").click()
    at.run()
    assert not at.exception
    assert any("match" in s.value for s in at.success)


def test_audit_tab_lists_requests() -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert any("No requests yet" in i.value for i in at.info)


def test_same_record_in_both_tabs_does_not_collide() -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.text_input(key="question").set_value("What is AUM?")
    next(b for b in at.button if b.label == "Ask").click()
    at.run()
    # The audit tab now lists and renders the same request below the ask tab's copy.
    assert not at.exception
    assert sum(b.label == "Replay stored SQL" for b in at.button) == 2
