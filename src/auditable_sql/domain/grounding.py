"""Deterministic check that numbers in a natural-language answer come from the result set.

This is a heuristic guard, not a proof: it catches invented or mis-copied figures, but it
cannot tell whether a correctly copied number was attached to the wrong label.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from decimal import Decimal, InvalidOperation

from auditable_sql.domain.models import GroundingReport, QueryResult

_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?"
    r"|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?\b"
# "December 31", "1 to 30 November", "1st of May": calendar context, not figures.
_PROSE_DATE = re.compile(
    rf"\b{_MONTH}\s+{_DAY}|\b{_DAY}(?:\s*(?:to|through|and|-|\u2013)\s*{_DAY})?\s+(?:of\s+)?{_MONTH}"
)
_LIST_MARKER = re.compile(r"^\s*\d{1,3}[.)]\s", re.MULTILINE)
_LETTER = re.compile(r"[A-Za-z]")
_NUMBER = re.compile(
    r"""
    (?<![\w.])                    # not glued to a word or another number
    -?\$?
    (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)
    (?:\s*(?P<suffix>%|[kKmMbB]\b|thousand|million|billion))?
    """,
    re.VERBOSE,
)
_MULTIPLIERS = {
    "k": Decimal(1_000), "thousand": Decimal(1_000),
    "m": Decimal(1_000_000), "million": Decimal(1_000_000),
    "b": Decimal(1_000_000_000), "billion": Decimal(1_000_000_000),
}  # fmt: skip
_YEAR_RANGE = range(1990, 2101)


def _numeric_cells(result: QueryResult) -> list[Decimal]:
    values: list[Decimal] = [Decimal(result.row_count)]
    for row in result.rows:
        for cell in row:
            if isinstance(cell, bool) or cell is None:
                continue
            if isinstance(cell, int | float | Decimal):
                values.append(abs(Decimal(str(cell))))
    return values


def _strip_context(text: str, result: QueryResult | None = None) -> str:
    """Remove digits that are labels rather than figures: text values copied from the
    result (a portfolio called "Aspen Value 56"), dates, and numbered-list markers."""
    if result is not None:
        # A purely numeric string cell is a figure, not a label: leave it to be checked.
        labels = {c for row in result.rows for c in row if isinstance(c, str) and _LETTER.search(c)}
        for label in sorted(labels, key=len, reverse=True):
            text = text.replace(label, " ")
    for pattern in (_DATE, _PROSE_DATE, _LIST_MARKER):
        text = pattern.sub(" ", text)
    return text


def _parse(text: str) -> Iterable[tuple[str, Decimal, int, Decimal, bool]]:
    """Yield (token, value, decimals, multiplier, is_percent) for each number in text."""
    for m in _NUMBER.finditer(text):
        raw = m.group("num").replace(",", "")
        try:
            value = Decimal(raw)
        except InvalidOperation:  # pragma: no cover - regex guarantees digits
            continue
        decimals = len(raw.split(".")[1]) if "." in raw else 0
        suffix = (m.group("suffix") or "").lower()
        yield (
            m.group(0).strip(),
            value,
            decimals,
            _MULTIPLIERS.get(suffix, Decimal(1)),
            suffix == "%",
        )


def _matches(value: Decimal, decimals: int, multiplier: Decimal, pct: bool, cell: Decimal) -> bool:
    # Half a unit in the last stated digit: "12.3%" matches anything that rounds to 12.3.
    tolerance = Decimal(5) * Decimal(10) ** -(decimals + 1) * multiplier
    target = value * multiplier
    candidates = [cell, cell * 100] if pct else [cell]
    return any(abs(c - target) <= tolerance for c in candidates)


def check_grounding(answer: str, result: QueryResult, question: str = "") -> GroundingReport:
    question_numbers = {v for _, v, _, _, _ in _parse(_strip_context(question))}
    cells = _numeric_cells(result)
    checked: list[str] = []
    unmatched: list[str] = []
    for token, value, decimals, multiplier, pct in _parse(_strip_context(answer, result)):
        if value in question_numbers:
            continue  # echoing the question ("top 5") is not a claim about the data
        if decimals == 0 and multiplier == 1 and not pct and int(value) in _YEAR_RANGE:
            continue  # bare years are context, not figures
        checked.append(token)
        if not any(_matches(value, decimals, multiplier, pct, c) for c in cells):
            unmatched.append(token)
    return GroundingReport(checked_numbers=tuple(checked), unmatched_numbers=tuple(unmatched))
