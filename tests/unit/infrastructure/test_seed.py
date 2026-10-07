"""Invariants of the synthetic dataset. Pure: no database needed."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import pytest

from auditable_sql.infrastructure.db.seed import FIRST_DAY, LAST_DAY, Dataset, Scale, generate


@pytest.fixture(scope="module")
def ds() -> Dataset:
    return generate(seed=3, scale=Scale.small())


def test_is_deterministic(ds: Dataset) -> None:
    again = generate(seed=3, scale=Scale.small())
    for (name, a), (_, b) in zip(ds.tables(), again.tables(), strict=True):
        assert a == b, name


def test_seed_changes_data(ds: Dataset) -> None:
    assert generate(seed=4, scale=Scale.small()).trades != ds.trades


def test_positions_equal_replayed_trades(ds: Dataset) -> None:
    """positions_eod.quantity == cumulative net traded quantity, for every row."""
    trades = sorted(ds.trades, key=lambda t: (t[3], t[0]))
    net: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    by_day: dict[date, list[tuple[object, ...]]] = defaultdict(list)
    for t in trades:
        by_day[t[3]].append(t)
    positions = defaultdict(dict)  # type: ignore[var-annotated]
    for p in ds.positions_eod:
        positions[p[2]][(p[0], p[1])] = p[3]

    for d in sorted(positions):
        for _, account, security, _, side, qty, *_ in by_day.get(d, []):
            net[(account, security)] += qty if side == "buy" else -qty
        expected = {k: v for k, v in net.items() if v != 0}
        assert positions[d] == expected, d


def test_market_value_is_quantity_times_close(ds: Dataset) -> None:
    close = {(s, d): c for s, d, c in ds.daily_prices}
    for account, security, d, qty, mv, _ in ds.positions_eod[::97]:
        # Half-up, like PostgreSQL's ROUND(numeric), so gold SQL can recompute it exactly.
        expected = (qty * close[(security, d)]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert mv == expected, (account, security, d)


def test_never_sells_more_than_held(ds: Dataset) -> None:
    held: dict[tuple[int, int], Decimal] = defaultdict(Decimal)
    for _, account, security, _, side, qty, *_ in sorted(ds.trades, key=lambda t: (t[3], t[0])):
        held[(account, security)] += qty if side == "buy" else -qty
        assert held[(account, security)] >= 0


def test_dates_are_weekdays_within_coverage(ds: Dataset) -> None:
    for d in {t[3] for t in ds.trades} | {p[2] for p in ds.positions_eod}:
        assert FIRST_DAY <= d <= LAST_DAY and d.weekday() < 5


def test_trades_start_no_earlier_than_account_opening(ds: Dataset) -> None:
    opened = {a[0]: a[4] for a in ds.accounts}
    assert all(t[3] >= opened[t[1]] for t in ds.trades)


def test_flow_signs(ds: Dataset) -> None:
    for _, _, _, kind, amount in ds.cash_flows:
        assert amount > 0 if kind in ("deposit", "dividend") else amount < 0


def test_synthetic_identifiers(ds: Dataset) -> None:
    for _, _, email, tax_id, *_ in ds.clients:
        assert email.endswith("@example.com")
        assert tax_id.startswith("9")  # ITIN-like range, never a valid SSN


def test_sector_null_only_for_non_equities(ds: Dataset) -> None:
    for _, _, _, asset_class, sector in ds.securities:
        assert (sector is None) == (asset_class != "equity")
