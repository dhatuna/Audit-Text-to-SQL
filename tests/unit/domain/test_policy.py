from __future__ import annotations

import pytest

from auditable_sql.domain.policy import RuntimeLimits, SqlPolicy

POLICY = SqlPolicy(allowed_columns={"trades": frozenset({"trade_id", "quantity"})})


def test_lookups_are_case_insensitive() -> None:
    assert POLICY.is_table_allowed("TRADES")
    assert POLICY.is_column_allowed("Trades", "QUANTITY")
    assert POLICY.is_function_denied("PG_SLEEP")


def test_unknown_table_or_column_is_denied() -> None:
    assert not POLICY.is_table_allowed("clients")
    assert not POLICY.is_column_allowed("clients", "tax_id")
    assert not POLICY.is_column_allowed("trades", "fees")


def test_non_deterministic_functions_are_denied_by_default() -> None:
    for fn in ("now", "current_date", "random"):
        assert POLICY.is_function_denied(fn)


@pytest.mark.parametrize("kwargs", [{"max_rows": 0}, {"allowed_columns": {}}])
def test_invalid_policy(kwargs: dict[str, object]) -> None:
    base: dict[str, object] = {"allowed_columns": {"t": frozenset({"c"})}}
    with pytest.raises(ValueError):
        SqlPolicy(**{**base, **kwargs})  # type: ignore[arg-type]


@pytest.mark.parametrize("kwargs", [{"statement_timeout_ms": 0}, {"max_repair_attempts": -1}])
def test_invalid_limits(kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        RuntimeLimits(**kwargs)
