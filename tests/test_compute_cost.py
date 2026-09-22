"""Tests for compute_cost (P14)."""

import pytest
from datetime import datetime, timezone

from agent_obs.cost import Usage, CostResult, compute_cost, PriceBook, PriceNotFoundError


class TestComputeCostArithmetic:
    """Arithmetic tests against hand-calculated reference values."""

    @pytest.fixture(autouse=True)
    def _book(self):
        self.book = PriceBook.load("price_book.yaml")
        self.at = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_gpt4o_no_cached(self):
        # gpt-4o: in=0.0025, out=0.010
        # 1000*0.0025/1000 + 500*0.010/1000 = 0.0025 + 0.005 = 0.0075
        result = compute_cost(Usage(input=1000, output=500), "gpt-4o", self.book, self.at)
        assert result.cost_usd == pytest.approx(0.0075)
        assert result.price_book_version == "2026-09-01"

    def test_gpt4o_with_cached(self):
        # gpt-4o: in=0.0025, out=0.010, cached=0.00125
        # 1000*0.0025/1000 + 500*0.010/1000 + 400*0.00125/1000
        # = 0.0025 + 0.005 + 0.0005 = 0.008
        result = compute_cost(
            Usage(input=1000, output=500, cached=400), "gpt-4o", self.book, self.at
        )
        assert result.cost_usd == pytest.approx(0.008)

    def test_gpt4o_mini_with_cached(self):
        # gpt-4o-mini: in=0.00015, out=0.0006, cached=0.000075
        # 10000*0.00015/1000 + 2000*0.0006/1000 + 8000*0.000075/1000
        # = 0.0015 + 0.0012 + 0.0006 = 0.0033
        result = compute_cost(
            Usage(input=10000, output=2000, cached=8000),
            "gpt-4o-mini",
            self.book,
            self.at,
        )
        assert result.cost_usd == pytest.approx(0.0033)

    def test_claude_cached_falls_back_to_input_price(self):
        # claude-3.5-sonnet has no cached_input_per_1k → cached priced at input rate
        # in=0.003, out=0.015, cached uses 0.003
        # 1000*0.003/1000 + 500*0.015/1000 + 400*0.003/1000
        # = 0.003 + 0.0075 + 0.0012 = 0.0117
        result = compute_cost(
            Usage(input=1000, output=500, cached=400),
            "claude-3.5-sonnet",
            self.book,
            self.at,
        )
        assert result.cost_usd == pytest.approx(0.0117)

    def test_claude_haiku_no_cached(self):
        # claude-3-haiku: in=0.00025, out=0.00125
        # 5000*0.00025/1000 + 1000*0.00125/1000 = 0.00125 + 0.00125 = 0.0025
        result = compute_cost(
            Usage(input=5000, output=1000), "claude-3-haiku", self.book, self.at
        )
        assert result.cost_usd == pytest.approx(0.0025)

    def test_zero_usage(self):
        result = compute_cost(Usage(input=0, output=0), "gpt-4o", self.book, self.at)
        assert result.cost_usd == 0.0

    def test_no_rounding_inside(self):
        # Result must be a plain float, not rounded to 8 decimals internally.
        # 1*0.0025/1000 = 0.0000025 → would still survive 8 decimals,
        # so verify with a value that would be truncated: use 1 token output.
        # 1*0.010/1000 = 0.00001 exactly; pick input instead:
        # 3*0.0025/1000 = 0.0000075 (7 decimal places, fine),
        # but the important assertion is the raw float equality.
        result = compute_cost(Usage(input=1, output=1), "gpt-4o", self.book, self.at)
        assert result.cost_usd == 0.0025 / 1000 + 0.010 / 1000


class TestComputeCostErrors:
    """Error paths."""

    @pytest.fixture(autouse=True)
    def _book(self):
        self.book = PriceBook.load("price_book.yaml")

    def test_unknown_model_raises(self):
        with pytest.raises(PriceNotFoundError, match="not found"):
            compute_cost(Usage(input=100, output=10), "nonexistent-model", self.book)

    def test_expired_price_raises(self):
        expired = datetime(2027, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(PriceNotFoundError, match="No active price"):
            compute_cost(Usage(input=100, output=10), "gpt-4o", self.book, expired)


class TestComputeCostResult:
    """CostResult shape and price book version."""

    @pytest.fixture(autouse=True)
    def _book(self):
        self.book = PriceBook.load("price_book.yaml")
        self.at = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_result_is_cost_result(self):
        result = compute_cost(Usage(input=100, output=50), "gpt-4o", self.book, self.at)
        assert isinstance(result, CostResult)

    def test_price_book_version_attribute(self):
        result = compute_cost(Usage(input=100, output=50), "gpt-4o", self.book, self.at)
        assert result.price_book_version == self.book.version
        assert result.price_book_version == "2026-09-01"

    def test_cost_is_float(self):
        result = compute_cost(Usage(input=100, output=50), "gpt-4o", self.book, self.at)
        assert isinstance(result.cost_usd, float)
