"""Result-set fingerprinting (for audit replay) and comparison (for evaluation)."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from itertools import combinations
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


MAX_EXTRA_COLUMN_SEARCH = 12


def results_match(
    expected: QueryResult,
    actual: QueryResult,
    *,
    ordered: bool = False,
    places: int = 4,
    allow_extra_columns: bool = True,
) -> bool:
    """Execution-accuracy comparison used by the evaluation harness.

    - column names and column order are ignored (models alias freely)
    - with `allow_extra_columns`, the actual result may carry additional columns as long
      as some subset of its columns reproduces the expected result exactly; row counts
      must still match (an extra "change" column next to the asked-for name is fine,
      an extra row is not)
    - rows are compared as multisets unless `ordered` is set
    - numbers are rounded to `places` decimals before comparing
    """
    if expected.truncated or actual.truncated:
        return False
    if expected.row_count != actual.row_count:
        return False
    n_exp, n_act = len(expected.columns), len(actual.columns)
    if n_act < n_exp or (n_act > n_exp and not allow_extra_columns):
        return False
    if n_act > MAX_EXTRA_COLUMN_SEARCH:
        return False

    def canon(rows: Sequence[Sequence[Any]]) -> list[tuple[str, ...]]:
        return [tuple(sorted(_canonical_cell(c, places) for c in row)) for row in rows]

    exp_rows = canon(expected.rows)
    exp_bag = Counter(exp_rows)
    for cols in combinations(range(n_act), n_exp):
        act_rows = canon([[row[i] for i in cols] for row in actual.rows])
        if (act_rows == exp_rows) if ordered else (Counter(act_rows) == exp_bag):
            return True
    return False
