"""Result-set fingerprinting (for audit replay) and comparison (for evaluation)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from auditable_sql.domain.models import QueryResult


def _canonical_cell(cell: Any, places: int | None = None) -> str:
    if cell is None:
        return "NULL"
    if isinstance(cell, bool):
        return "true" if cell else "false"
    if isinstance(cell, int | float | Decimal):
        d = Decimal(str(cell))
        if places is not None:
            d = d.quantize(Decimal(1).scaleb(-places))
        d = d.normalize()
        return "0" if d.is_zero() else format(d, "f")
    if isinstance(cell, datetime | date):
        return cell.isoformat()
    return str(cell)


def fingerprint(result: QueryResult) -> str:
    """Order-insensitive hash of columns and rows.

    Row order is excluded on purpose: without ORDER BY, PostgreSQL may legitimately return
    the same rows in a different order, and that should not count as a replay mismatch.
    """
    rows = sorted(json.dumps([_canonical_cell(c) for c in row]) for row in result.rows)
    payload = json.dumps({"columns": list(result.columns), "rows": rows}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def results_match(
    expected: QueryResult,
    actual: QueryResult,
    *,
    ordered: bool = False,
    places: int = 4,
) -> bool:
    """Execution-accuracy comparison used by the evaluation harness.

    - column names and column order are ignored (models alias freely)
    - rows are compared as multisets unless `ordered` is set
    - numbers are rounded to `places` decimals before comparing
    """
    if expected.truncated or actual.truncated:
        return False
    if len(expected.columns) != len(actual.columns) or expected.row_count != actual.row_count:
        return False

    def canon(rows: Sequence[Sequence[Any]]) -> list[tuple[str, ...]]:
        return [tuple(sorted(_canonical_cell(c, places) for c in row)) for row in rows]

    exp, act = canon(expected.rows), canon(actual.rows)
    if ordered:
        return exp == act
    return Counter(exp) == Counter(act)
