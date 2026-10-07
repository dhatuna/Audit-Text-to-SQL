"""Deterministic synthetic data for the portfolio schema.

`generate()` is pure (seeded numpy RNG -> rows) so its invariants can be unit-tested;
`load()` writes a dataset with COPY. Positions are derived by replaying trades, so
`positions_eod` and `trades` always agree, which keeps gold SQL answers checkable.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import numpy as np
import psycopg

FIRST_DAY = date(2024, 1, 2)
LAST_DAY = date(2025, 12, 31)

SECTORS = (
    "Technology", "Health Care", "Financials", "Consumer Discretionary", "Industrials",
    "Communication Services", "Consumer Staples", "Energy", "Utilities", "Real Estate", "Materials",
)  # fmt: skip
STRATEGIES = ("growth", "value", "income", "balanced", "index")
STRATEGY_TILT = {
    "growth": {"Technology": 3.0, "Consumer Discretionary": 2.0, "Communication Services": 2.0},
    "value": {"Financials": 2.5, "Energy": 2.0, "Industrials": 2.0, "Materials": 1.5},
    "income": {"Utilities": 3.0, "Real Estate": 2.5, "Consumer Staples": 2.0},
    "balanced": {},
    "index": {},
}
CUSTODIANS = ("Harbor Trust", "Summit Custody", "Meridian Clearing", "Northgate Securities")
FIRST_NAMES = (
    "Avery", "Jordan", "Morgan", "Riley", "Casey", "Quinn", "Taylor", "Rowan", "Emerson",
    "Hayden", "Parker", "Reese", "Sawyer", "Skyler", "Dakota", "Finley", "Harper", "Kendall",
)  # fmt: skip
LAST_NAMES = (
    "Ashford", "Bellamy", "Carrow", "Delacroix", "Ellery", "Fairbanks", "Garrow", "Hollis",
    "Ingram", "Jessup", "Kingsley", "Lockwood", "Marlow", "Northcott", "Oakley", "Pemberton",
)  # fmt: skip
TREES = ("Cedar", "Birch", "Willow", "Aspen", "Maple", "Juniper", "Sequoia", "Linden", "Alder")
NAME_A = (
    "Apex", "Blue", "Crest", "Delta", "Ember", "Frontier", "Granite", "Horizon", "Iron",
    "Keystone", "Lumen", "Meridian", "Nova", "Orion", "Pioneer", "Quarry", "Ridge", "Sterling",
)  # fmt: skip
NAME_B = (
    "Systems", "Holdings", "Industries", "Labs", "Networks", "Energy", "Health", "Capital",
    "Foods", "Materials", "Logistics", "Realty", "Power", "Media", "Devices", "Therapeutics",
)  # fmt: skip
ETF_KINDS = ("Total Market", "Dividend", "Growth", "Value", "Intl")
FUND_KINDS = ("Treasury", "Aggregate Bond", "Muni", "Corporate Bond")

CENT = Decimal("0.01")
QTY = Decimal("0.0001")
PX = Decimal("0.0001")


def business_days(start: date = FIRST_DAY, end: date = LAST_DAY) -> list[date]:
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def _q(value: float | Decimal, unit: Decimal) -> Decimal:
    return Decimal(str(value)).quantize(unit, rounding=ROUND_HALF_UP)


@dataclass
class Dataset:
    clients: list[tuple[Any, ...]] = field(default_factory=list)
    portfolios: list[tuple[Any, ...]] = field(default_factory=list)
    accounts: list[tuple[Any, ...]] = field(default_factory=list)
    securities: list[tuple[Any, ...]] = field(default_factory=list)
    daily_prices: list[tuple[Any, ...]] = field(default_factory=list)
    trades: list[tuple[Any, ...]] = field(default_factory=list)
    positions_eod: list[tuple[Any, ...]] = field(default_factory=list)
    cash_flows: list[tuple[Any, ...]] = field(default_factory=list)

    TABLE_ORDER = (
        "clients", "portfolios", "accounts", "securities",
        "daily_prices", "trades", "positions_eod", "cash_flows",
    )  # fmt: skip

    def tables(self) -> Iterable[tuple[str, list[tuple[Any, ...]]]]:
        for name in self.TABLE_ORDER:
            yield name, getattr(self, name)


@dataclass(frozen=True)
class Scale:
    clients: int = 40
    second_portfolio_share: float = 0.5
    accounts_per_portfolio: int = 2
    equities: int = 240
    etfs: int = 40
    bond_funds: int = 20

    @classmethod
    def small(cls) -> Scale:
        """For fast tests."""
        return cls(clients=4, equities=24, etfs=4, bond_funds=2)


def _tickers(rng: np.random.Generator, n: int) -> list[str]:
    letters = np.array(list("ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    seen: set[str] = set()
    while len(seen) < n:
        length = int(rng.choice([3, 4], p=[0.4, 0.6]))
        seen.add("".join(rng.choice(letters, size=length)))
    return sorted(seen)


def _securities(rng: np.random.Generator, scale: Scale) -> list[tuple[Any, ...]]:
    total = scale.equities + scale.etfs + scale.bond_funds
    tickers = _tickers(rng, total)
    rng.shuffle(tickers)
    rows: list[tuple[Any, ...]] = []
    used_names: set[str] = set()
    for i, ticker in enumerate(tickers, start=1):
        if i <= scale.equities:
            asset_class, sector = "equity", SECTORS[(i - 1) % len(SECTORS)]
            name = f"{rng.choice(NAME_A)} {rng.choice(NAME_B)}"
        elif i <= scale.equities + scale.etfs:
            asset_class, sector = "etf", None
            name = f"{rng.choice(NAME_A)} {rng.choice(ETF_KINDS)} ETF"
        else:
            asset_class, sector = "fixed_income", None
            name = f"{rng.choice(NAME_A)} {rng.choice(FUND_KINDS)} Fund"
        while name in used_names:
            name = f"{name} {ticker}"
        used_names.add(name)
        rows.append((i, ticker, name, asset_class, sector))
    return rows


def _prices(
    rng: np.random.Generator, securities: Sequence[tuple[Any, ...]], days: Sequence[date]
) -> np.ndarray:
    """Matrix [security, day] of closes: market + sector factor + idiosyncratic GBM."""
    n, t = len(securities), len(days)
    dt = 1 / 252
    market = rng.normal(0.08 * dt, 0.16 * np.sqrt(dt), size=t)
    sector_shock = {s: rng.normal(0, 0.10 * np.sqrt(dt), size=t) for s in SECTORS}
    closes = np.empty((n, t))
    for i, (_, _, _, asset_class, sector) in enumerate(securities):
        if asset_class == "equity":
            beta, vol = rng.uniform(0.6, 1.5), rng.uniform(0.15, 0.35)
            drift, start = rng.normal(0.03, 0.06), rng.uniform(15, 400)
            factor = beta * market + sector_shock[sector]
        elif asset_class == "etf":
            beta, vol = rng.uniform(0.8, 1.1), rng.uniform(0.03, 0.08)
            drift, start = 0.0, rng.uniform(40, 450)
            factor = beta * market
        else:
            beta, vol, drift, start = 0.05, rng.uniform(0.03, 0.07), 0.01, rng.uniform(20, 110)
            factor = beta * market
        idio = rng.normal(drift * dt - 0.5 * vol**2 * dt, vol * np.sqrt(dt), size=t)
        closes[i] = start * np.exp(np.cumsum(factor + idio))
    return np.round(closes, 4)


@dataclass
class _Counters:
    trade_id: int = 0
    flow_id: int = 0


class _Account:
    """Running state of one simulated account: holdings, average cost, cash."""

    def __init__(
        self,
        account_id: int,
        ds: Dataset,
        rng: np.random.Generator,
        closes: np.ndarray,
        day_index: dict[date, int],
        ids: _Counters,
    ) -> None:
        self.account_id = account_id
        self.ds, self.rng, self.closes, self.day_index, self.ids = ds, rng, closes, day_index, ids
        self.qty: dict[int, Decimal] = {}
        self.cost: dict[int, Decimal] = {}
        self.cash = Decimal(0)

    def trade(self, i: int, d: date, side: str, q: Decimal) -> None:
        px = _q(self.closes[i, self.day_index[d]] * (1 + self.rng.normal(0, 0.002)), PX)
        fee = _q(float(self.rng.choice([0, 0, 0, 1, 4.95, 9.99])), CENT)
        self.ids.trade_id += 1
        security_id = self.ds.securities[i][0]
        self.ds.trades.append(
            (self.ids.trade_id, self.account_id, security_id, d, side, q, px, fee)
        )
        if side == "buy":
            self.cost[i] = self.cost.get(i, Decimal(0)) + q * px
            self.qty[i] = self.qty.get(i, Decimal(0)) + q
            self.cash -= q * px + fee
        else:
            avg = self.cost[i] / self.qty[i]
            self.qty[i] -= q
            self.cost[i] -= avg * q
            self.cash += q * px - fee
            if self.qty[i] == 0:
                del self.qty[i], self.cost[i]

    def flow(self, d: date, kind: str, amount: Decimal) -> None:
        self.ids.flow_id += 1
        self.ds.cash_flows.append((self.ids.flow_id, self.account_id, d, kind, amount))
        self.cash += amount

    def snapshot(self, j: int, d: date) -> None:
        for i in sorted(self.qty):
            mv = _q(self.qty[i] * Decimal(str(self.closes[i, j])), CENT)
            security_id = self.ds.securities[i][0]
            self.ds.positions_eod.append(
                (self.account_id, security_id, d, self.qty[i], mv, _q(self.cost[i], CENT))
            )


def generate(seed: int = 7, scale: Scale | None = None) -> Dataset:
    scale = scale or Scale()
    rng = np.random.default_rng(seed)
    ds = Dataset()
    days = business_days()
    day_index = {d: i for i, d in enumerate(days)}

    # --- reference data ---------------------------------------------------------
    for cid in range(1, scale.clients + 1):
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        ds.clients.append((
            cid,
            f"{first} {last}",
            f"{first.lower()}.{last.lower()}{cid}@example.com",
            # ITIN-like 9xx prefix: never a valid SSN
            f"9{rng.integers(10, 99)}-{rng.integers(70, 99)}-{rng.integers(1000, 9999)}",
            str(rng.choice(["conservative", "moderate", "aggressive"], p=[0.3, 0.45, 0.25])),
            FIRST_DAY - timedelta(days=int(rng.integers(30, 3000))),
        ))  # fmt: skip

    owners = [c[0] for c in ds.clients]
    n_second = int(len(owners) * scale.second_portfolio_share)
    owners += sorted(rng.choice(owners, size=n_second, replace=False).tolist())
    names: set[str] = set()
    for pid, cid in enumerate(owners, start=1):
        strategy = str(rng.choice(STRATEGIES))
        name = f"{rng.choice(TREES)} {strategy.title()} {pid:02d}"
        names.add(name)
        incepted_mid_period = rng.random() < 0.25
        inception = (
            days[int(rng.integers(20, 300))]
            if incepted_mid_period
            else FIRST_DAY - timedelta(days=int(rng.integers(1, 1500)))
        )
        ds.portfolios.append((pid, cid, name, strategy, inception))

    aid = 0
    for pid, _, _, _, inception in ds.portfolios:
        for _ in range(scale.accounts_per_portfolio):
            aid += 1
            ds.accounts.append((
                aid, pid, str(rng.choice(["taxable", "ira", "trust"], p=[0.6, 0.3, 0.1])),
                str(rng.choice(CUSTODIANS)), inception,
            ))  # fmt: skip

    ds.securities = _securities(rng, scale)
    closes = _prices(rng, ds.securities, days)
    for i, sec in enumerate(ds.securities):
        for j, d in enumerate(days):
            ds.daily_prices.append((sec[0], d, _q(closes[i, j], PX)))

    # --- trading simulation -----------------------------------------------------
    by_class: dict[str, list[int]] = {"equity": [], "etf": [], "fixed_income": []}
    sector_of: dict[int, str | None] = {}
    for idx, (_, _, _, asset_class, sector) in enumerate(ds.securities):
        by_class[asset_class].append(idx)
        sector_of[idx] = sector
    dividend_payers = {i for i in by_class["equity"] if rng.random() < 0.4}
    dividend_payers |= set(by_class["fixed_income"])
    yields = {i: rng.uniform(0.01, 0.045) for i in dividend_payers}

    strategy_of = {p[0]: p[3] for p in ds.portfolios}
    ids = _Counters()

    def universe_weights(strategy: str) -> tuple[list[int], np.ndarray]:
        if strategy == "index":
            pool = by_class["etf"] + by_class["fixed_income"][:2]
        else:
            pool = by_class["equity"] + by_class["etf"]
            if strategy in ("income", "balanced"):
                pool += by_class["fixed_income"]
        tilt = STRATEGY_TILT[strategy]
        w = np.array([tilt.get(sector_of[i] or "", 1.0) for i in pool])
        return pool, w / w.sum()

    for account_id, pid, _, _, opened in ds.accounts:
        pool, weights = universe_weights(strategy_of[pid])
        start = max(opened, FIRST_DAY)
        start_i = day_index[next(d for d in days if d >= start)]
        acct = _Account(account_id, ds, rng, closes, day_index, ids)

        # funding and initial allocation
        acct.flow(days[start_i], "deposit", _q(rng.uniform(1e5, 4e6) // 1000 * 1000, CENT))
        n_hold = int(rng.integers(8, 16))
        picks = rng.choice(pool, size=min(n_hold, len(pool)), replace=False, p=weights)
        budget = acct.cash * Decimal("0.97") / len(picks)
        for i in picks:
            q = _q(budget / Decimal(str(closes[i, start_i])), Decimal(1))
            if q > 0:
                acct.trade(int(i), days[start_i], "buy", q)

        for j in range(start_i, len(days)):
            d = days[j]
            if j > start_i:
                r = rng.random()
                if r < 0.025 and acct.qty:  # trim or exit a holding
                    i = int(rng.choice(sorted(acct.qty)))
                    frac = Decimal(str(rng.choice([0.25, 0.5, 1.0])))
                    q = _q(acct.qty[i] * frac, Decimal(1)) or acct.qty[i]
                    acct.trade(i, d, "sell", min(q, acct.qty[i]))
                elif r < 0.05 and acct.cash > 5000:  # add to a position or open a new one
                    i = int(rng.choice(pool, p=weights))
                    spend = acct.cash * Decimal(str(rng.uniform(0.2, 0.8)))
                    q = _q(spend / Decimal(str(closes[i, j])), Decimal(1))
                    if q > 0:
                        acct.trade(i, d, "buy", q)
                # monthly-ish external flows and quarterly income
                if d.day <= 7 and d.weekday() == 0 and rng.random() < 0.15:
                    if rng.random() < 0.7:
                        acct.flow(d, "deposit", _q(rng.uniform(5e3, 2e5) // 100 * 100, CENT))
                    elif acct.cash > 10_000:
                        amount = min(acct.cash / 2, Decimal(str(rng.uniform(5e3, 1e5))))
                        acct.flow(d, "withdrawal", -_q(amount, CENT))
                if d.month in (3, 6, 9, 12) and 14 <= d.day <= 18 and d.weekday() == 4:
                    for i in sorted(acct.qty):
                        if i in dividend_payers:
                            per_share = Decimal(str(closes[i, j] * yields[i] / 4))
                            amt = _q(acct.qty[i] * per_share, CENT)
                            if amt > 0:
                                acct.flow(d, "dividend", amt)
                    if rng.random() < 0.5:
                        acct.flow(d, "fee", -_q(rng.uniform(50, 2500), CENT))
            acct.snapshot(j, d)
    return ds


def load(conn: psycopg.Connection[Any], ds: Dataset) -> dict[str, int]:
    """Replace all portfolio data with `ds`. Caller owns the transaction."""
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute(
            "TRUNCATE " + ", ".join(reversed(Dataset.TABLE_ORDER)) + " RESTART IDENTITY CASCADE"
        )
        for table, rows in ds.tables():
            with cur.copy(f"COPY {table} FROM STDIN") as copy:
                for row in rows:
                    copy.write_row(row)
            counts[table] = len(rows)
        cur.execute("ANALYZE")
    return counts
