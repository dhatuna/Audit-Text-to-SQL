from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from auditable_sql.domain.results import fingerprint, results_match
from tests.fakes import result


class TestFingerprint:
    def test_row_order_does_not_matter(self) -> None:
        a = result(["t", "v"], ["A", 1], ["B", 2])
        b = result(["t", "v"], ["B", 2], ["A", 1])
        assert fingerprint(a) == fingerprint(b)

    def test_column_names_matter(self) -> None:
        assert fingerprint(result(["a"], [1])) != fingerprint(result(["b"], [1]))

    def test_numeric_representation_is_canonical(self) -> None:
        assert fingerprint(result(["v"], [Decimal("1.50")])) == fingerprint(result(["v"], [1.5]))

    def test_null_differs_from_string_null(self) -> None:
        assert fingerprint(result(["v"], [None])) != fingerprint(result(["v"], ["None"]))

    def test_dates_are_stable(self) -> None:
        assert fingerprint(result(["d"], [date(2025, 1, 2)])) == fingerprint(
            result(["d"], [date(2025, 1, 2)])
        )

    @given(st.lists(st.tuples(st.integers(), st.text(max_size=5)), max_size=20))
    def test_permutation_invariant(self, rows: list[tuple[int, str]]) -> None:
        assert fingerprint(result(["a", "b"], *rows)) == fingerprint(
            result(["a", "b"], *reversed(rows))
        )


class TestResultsMatch:
    def test_ignores_column_names_and_order(self) -> None:
        gold = result(["ticker", "mv"], ["AAA", 10], ["BBB", 20])
        pred = result(["market_value", "symbol"], [20, "BBB"], [10, "AAA"])
        assert results_match(gold, pred)

    def test_ordered_comparison(self) -> None:
        gold = result(["t"], ["A"], ["B"])
        pred = result(["t"], ["B"], ["A"])
        assert results_match(gold, pred)
        assert not results_match(gold, pred, ordered=True)

    def test_rounding_tolerance(self) -> None:
        assert results_match(result(["v"], [Decimal("0.123449")]), result(["v"], [0.12345]))
        assert not results_match(result(["v"], [Decimal("0.1234")]), result(["v"], [0.1236]))

    def test_multiset_semantics(self) -> None:
        assert not results_match(result(["v"], [1], [1], [2]), result(["v"], [1], [2], [2]))

    def test_shape_mismatch(self) -> None:
        assert not results_match(result(["a", "b"], [1, 2]), result(["a"], [1]))
        assert not results_match(result(["a"], [1]), result(["a"], [1], [1]))

    def test_extra_columns_are_tolerated_when_a_subset_matches(self) -> None:
        gold = result(["name"], ["Alpha"], ["Beta"])
        pred = result(["name", "change"], ["Beta", 5], ["Alpha", 9])
        assert results_match(gold, pred)
        assert not results_match(gold, pred, allow_extra_columns=False)

    def test_extra_columns_do_not_hide_wrong_values(self) -> None:
        gold = result(["name", "aum"], ["Alpha", 10], ["Beta", 20])
        pred = result(["name", "aum", "n"], ["Alpha", 20, 1], ["Beta", 10, 2])
        assert not results_match(gold, pred)

    def test_extra_columns_with_ordering(self) -> None:
        gold = result(["t"], ["A"], ["B"])
        assert results_match(gold, result(["t", "v"], ["A", 2], ["B", 1]), ordered=True)
        assert not results_match(gold, result(["t", "v"], ["B", 1], ["A", 2]), ordered=True)

    def test_truncated_never_matches(self) -> None:
        r = result(["a"], [1])
        assert not results_match(r, result(["a"], [1], truncated=True))

    def test_empty_results_match(self) -> None:
        assert results_match(result(["a"]), result(["b"]))
