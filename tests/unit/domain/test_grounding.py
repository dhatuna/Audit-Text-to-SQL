from __future__ import annotations

from decimal import Decimal

import pytest

from auditable_sql.domain.grounding import check_grounding
from tests.fakes import result

AUM = result(["portfolio", "aum", "return_pct"], ["Alpha", Decimal("1234567.891"), 0.0731])


@pytest.mark.parametrize(
    "answer",
    [
        "Alpha has AUM of $1,234,567.89.",
        "Alpha has AUM of 1234567.891",
        "Alpha manages about $1.2M.",
        "Alpha manages about $1.23 million.",
        "Alpha manages about 1,235 thousand dollars.",
        "Alpha returned 7.3% over the period.",
        "Alpha returned 7.31%.",
        "Alpha returned 0.0731 (fractional).",
        "There is 1 portfolio in the result.",  # row count
        "Alpha has no figures worth quoting.",
    ],
)
def test_grounded_answers_pass(answer: str) -> None:
    report = check_grounding(answer, AUM)
    assert report.passed, report.unmatched_numbers


@pytest.mark.parametrize(
    ("answer", "bad"),
    [
        ("Alpha has AUM of $1,300,000.", "$1,300,000"),
        ("Alpha manages about $1.3M.", "$1.3M"),
        ("Alpha returned 8.1%.", "8.1%"),
        ("Alpha has 3 holdings.", "3"),
    ],
)
def test_invented_numbers_are_flagged(answer: str, bad: str) -> None:
    report = check_grounding(answer, AUM)
    assert report.unmatched_numbers == (bad,)


def test_numbers_from_the_question_are_not_claims() -> None:
    report = check_grounding("Here are the top 5 portfolios.", AUM, question="top 5 portfolios")
    assert report.passed and report.checked_numbers == ()


def test_dates_and_years_are_ignored() -> None:
    report = check_grounding("As of 2025-12-31, in 2025, Alpha led.", AUM)
    assert report.checked_numbers == ()


def test_negative_values_compare_by_magnitude() -> None:
    res = result(["pnl"], [Decimal("-1520.40")])
    assert check_grounding("Alpha lost $1,520.40.", res).passed


def test_precision_is_respected() -> None:
    # "1.2346" claims more precision than 1.2341 can round to.
    res = result(["x"], [Decimal("1.2341")])
    assert check_grounding("x is 1.234", res).passed
    assert not check_grounding("x is 1.2346", res).passed


def test_text_and_null_cells_are_ignored() -> None:
    res = result(["name", "v"], ["12", None], [True, 7])
    assert check_grounding("v is 7 across 2 rows.", res).passed
    assert not check_grounding("v is 12", res).passed


# The cases below come from false positives seen in the first real evaluation run.
@pytest.mark.parametrize(
    "answer",
    [
        "116 trades, dated from 1 to 30 November 2025.",
        "Dividends paid between January 1 and December 31, 2025.",
        "Q3 2025 is the quarter from July 1 through September 30, 2025.",
        "Month-end totals:\n- January 31: 116\n- Feb. 28: 116",
        "Counted from the 1st of May.",
        "Trades dated 1\u201330 November.",
    ],
)
def test_prose_dates_are_context(answer: str) -> None:
    report = check_grounding(answer, result(["n"], [116]))
    assert report.passed, report.unmatched_numbers


def test_numbered_list_markers_are_not_figures() -> None:
    res = result(["name", "aum"], ["Alpha", 10], ["Beta", 20])
    answer = "Ranked:\n1. Alpha: 10\n2. Beta: 20\n  3) none"
    assert check_grounding(answer, res).passed


def test_digits_inside_text_labels_are_not_figures() -> None:
    res = result(["portfolio", "inception"], ["Aspen Value 56", "2024-02-09"])
    assert check_grounding("Aspen Value 56 started on 2024-02-09.", res).passed


def test_context_stripping_does_not_hide_invented_figures() -> None:
    res = result(["portfolio", "aum"], ["Aspen Value 56", 100])
    report = check_grounding(
        "1. Aspen Value 56 held 250 on December 31, up from 100 on 1 to 30 June.", res
    )
    assert report.unmatched_numbers == ("250",)
