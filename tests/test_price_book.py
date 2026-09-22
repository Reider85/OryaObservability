"""Tests for PriceBook loading and validation (P13)."""

import pytest
from datetime import datetime, timezone
from pathlib import Path
from agent_obs.cost.price_book import PriceBook, PriceNotFoundError, PriceEntry, ModelPricing


class TestPriceBookLoading:
    """Test PriceBook loading and validation."""

    def test_successful_loading(self):
        """Test successful price book loading."""
        price_book = PriceBook.load("price_book.yaml")
        
        assert price_book.version == "2026-09-01"
        assert price_book.currency == "USD"
        assert len(price_book.models) == 5
        
        # Check gpt-4o pricing
        gpt4o = price_book.models["gpt-4o"]
        assert gpt4o.model == "gpt-4o"
        assert len(gpt4o.prices) == 1
        assert gpt4o.prices[0].input_per_1k == 0.0025
        assert gpt4o.prices[0].output_per_1k == 0.010
        assert gpt4o.prices[0].cached_input_per_1k == 0.00125
        assert gpt4o.prices[0].valid_from == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert gpt4o.prices[0].valid_to == datetime(2026, 12, 31, tzinfo=timezone.utc)

    def test_file_not_found(self):
        """Test error when price book file doesn't exist."""
        with pytest.raises(FileNotFoundError, match="Price book file not found"):
            PriceBook.load("nonexistent.yaml")

    def test_invalid_yaml(self, tmp_path):
        """Test error for invalid YAML content."""
        invalid_yaml = tmp_path / "invalid.yaml"
        invalid_yaml.write_text("invalid: yaml: content: [")
        
        with pytest.raises(ValueError, match="Invalid YAML"):
            PriceBook.load(str(invalid_yaml))

    def test_not_a_dict(self, tmp_path):
        """Test error when price book is not a dictionary."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("not a dict")
        
        with pytest.raises(ValueError, match="Price book must be a dictionary"):
            PriceBook.load(str(invalid_file))

    def test_empty_version(self, tmp_path):
        """Test error for empty version."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: ""
currency: USD
models:
  gpt-4o:
    input_per_1k: 0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="Version must be a non-empty string"):
            PriceBook.load(str(invalid_file))

    def test_missing_version(self, tmp_path):
        """Test error for missing version."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
currency: USD
models:
  gpt-4o:
    input_per_1k: 0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="Version must be a non-empty string"):
            PriceBook.load(str(invalid_file))

    def test_missing_currency(self, tmp_path):
        """Test error for missing currency."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
models:
  gpt-4o:
    input_per_1k: 0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="Currency must be specified"):
            PriceBook.load(str(invalid_file))

    def test_models_not_dict(self, tmp_path):
        """Test error when models is not a dictionary."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models: "not a dict"
""")
        
        with pytest.raises(ValueError, match="Models must be a dictionary"):
            PriceBook.load(str(invalid_file))

    def test_empty_model_name(self, tmp_path):
        """Test error for empty model name."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  "":
    input_per_1k: 0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="Model name must be a non-empty string"):
            PriceBook.load(str(invalid_file))

    def test_model_config_not_dict(self, tmp_path):
        """Test error when model config is not a dictionary."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o: "not a dict"
""")
        
        with pytest.raises(ValueError, match="Model config for gpt-4o must be a dictionary"):
            PriceBook.load(str(invalid_file))

    def test_missing_input_price(self, tmp_path):
        """Test error for missing input_per_1k."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="input_per_1k is required for model gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_missing_output_price(self, tmp_path):
        """Test error for missing output_per_1k."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    input_per_1k: 0.0025
""")
        
        with pytest.raises(ValueError, match="output_per_1k is required for model gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_negative_input_price(self, tmp_path):
        """Test error for negative input_per_1k."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    input_per_1k: -0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="input_per_1k must be positive for model gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_zero_input_price(self, tmp_path):
        """Test error for zero input_per_1k."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    input_per_1k: 0
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="input_per_1k must be positive for model gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_negative_output_price(self, tmp_path):
        """Test error for negative output_per_1k."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    input_per_1k: 0.0025
    output_per_1k: -0.010
""")
        
        with pytest.raises(ValueError, match="output_per_1k must be positive for model gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_negative_cached_price(self, tmp_path):
        """Test error for negative cached_input_per_1k."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    input_per_1k: 0.0025
    output_per_1k: 0.010
    cached_input_per_1k: -0.00125
""")
        
        with pytest.raises(ValueError, match="cached_input_per_1k must be positive for model gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_invalid_valid_from_format(self, tmp_path):
        """Test error for invalid valid_from format."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    valid_from: "invalid-date"
    valid_to: "2026-12-31"
    input_per_1k: 0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="Invalid valid_from format for gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_invalid_valid_to_format(self, tmp_path):
        """Test error for invalid valid_to format."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    valid_from: "2026-01-01"
    valid_to: "invalid-date"
    input_per_1k: 0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="Invalid valid_to format for gpt-4o"):
            PriceBook.load(str(invalid_file))

    def test_valid_from_after_valid_to(self, tmp_path):
        """Test error when valid_from is after valid_to."""
        invalid_file = tmp_path / "invalid.yaml"
        invalid_file.write_text("""
version: "2026-09-01"
currency: USD
models:
  gpt-4o:
    valid_from: "2026-12-31"
    valid_to: "2026-01-01"
    input_per_1k: 0.0025
    output_per_1k: 0.010
""")
        
        with pytest.raises(ValueError, match="valid_from must be before valid_to for model gpt-4o"):
            PriceBook.load(str(invalid_file))


class TestPriceFor:
    """Test price_for method."""

    def setup_method(self):
        """Set up test fixture."""
        self.price_book = PriceBook.load("price_book.yaml")
        self.test_date = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_model_not_found(self):
        """Test error when model not found."""
        with pytest.raises(PriceNotFoundError, match="Model 'nonexistent' not found"):
            self.price_book.price_for("nonexistent")

    def test_price_for_without_datetime(self):
        """Test price_for without datetime (should return the only entry)."""
        price = self.price_book.price_for("gpt-4o")
        assert price.input_per_1k == 0.0025
        assert price.output_per_1k == 0.010
        assert price.cached_input_per_1k == 0.00125
        assert price.valid_from == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert price.valid_to == datetime(2026, 12, 31, tzinfo=timezone.utc)

    def test_price_for_with_active_datetime(self):
        """Test price_for with datetime within valid range."""
        price = self.price_book.price_for("gpt-4o", self.test_date)
        assert price.input_per_1k == 0.0025
        assert price.output_per_1k == 0.010
        assert price.cached_input_per_1k == 0.00125
        assert price.valid_from == datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert price.valid_to == datetime(2026, 12, 31, tzinfo=timezone.utc)

    def test_price_for_with_datetime_at_boundary(self):
        """Test price_for with datetime at valid_from boundary."""
        price = self.price_book.price_for("gpt-4o", datetime(2026, 1, 1, tzinfo=timezone.utc))
        assert price.input_per_1k == 0.0025
        assert price.output_per_1k == 0.010

    def test_price_for_with_datetime_at_upper_boundary(self):
        """Test price_for with datetime just before valid_to boundary."""
        price = self.price_book.price_for("gpt-4o", datetime(2026, 12, 30, 23, 59, 59, tzinfo=timezone.utc))
        assert price.input_per_1k == 0.0025
        assert price.output_per_1k == 0.010

    def test_price_for_with_expired_datetime(self):
        """Test price_for with datetime outside valid range."""
        expired_date = datetime(2027, 1, 1, tzinfo=timezone.utc)
        with pytest.raises(PriceNotFoundError, match="No active price found for model gpt-4o"):
            self.price_book.price_for("gpt-4o", expired_date)

    def test_price_for_with_future_datetime(self):
        """Test price_for with datetime before valid_from."""
        future_date = datetime(2025, 12, 31, tzinfo=timezone.utc)
        with pytest.raises(PriceNotFoundError, match="No active price found for model gpt-4o"):
            self.price_book.price_for("gpt-4o", future_date)

    def test_price_for_different_models(self):
        """Test price_for for different models."""
        # Test gpt-4o-mini
        price = self.price_book.price_for("gpt-4o-mini", self.test_date)
        assert price.input_per_1k == 0.00015
        assert price.output_per_1k == 0.0006
        assert price.cached_input_per_1k == 0.000075

        # Test claude-3.5-sonnet
        price = self.price_book.price_for("claude-3.5-sonnet", self.test_date)
        assert price.input_per_1k == 0.003
        assert price.output_per_1k == 0.015
        assert price.cached_input_per_1k is None


class TestPriceEntry:
    """Test PriceEntry functionality."""

    def test_is_valid_at(self):
        """Test is_valid_at method."""
        entry = PriceEntry(
            input_per_1k=0.0025,
            output_per_1k=0.010,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2026, 12, 31, tzinfo=timezone.utc)
        )

        # Within range
        assert entry.is_valid_at(datetime(2026, 6, 15, tzinfo=timezone.utc))

        # At lower boundary (inclusive)
        assert entry.is_valid_at(datetime(2026, 1, 1, tzinfo=timezone.utc))

        # Just before upper boundary (exclusive)
        assert entry.is_valid_at(datetime(2026, 12, 30, tzinfo=timezone.utc))

        # Before valid_from
        assert not entry.is_valid_at(datetime(2025, 12, 31, tzinfo=timezone.utc))

        # At or after valid_to
        assert not entry.is_valid_at(datetime(2026, 12, 31, tzinfo=timezone.utc))
        assert not entry.is_valid_at(datetime(2027, 1, 1, tzinfo=timezone.utc))

    def test_is_valid_at_no_period(self):
        """Test is_valid_at for entry without validity period."""
        entry = PriceEntry(
            input_per_1k=0.0025,
            output_per_1k=0.010
        )

        # Should always be valid
        assert entry.is_valid_at(datetime(2025, 1, 1, tzinfo=timezone.utc))
        assert entry.is_valid_at(datetime(2026, 6, 15, tzinfo=timezone.utc))
        assert entry.is_valid_at(datetime(2027, 1, 1, tzinfo=timezone.utc))


class TestModelPricing:
    """Test ModelPricing functionality."""

    def setup_method(self):
        """Set up test fixture."""
        self.test_date = datetime(2026, 6, 15, 12, 0, 0, tzinfo=timezone.utc)

    def test_get_active_price_single_entry(self):
        """Test get_active_price with single entry."""
        entry = PriceEntry(
            input_per_1k=0.0025,
            output_per_1k=0.010,
            valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2026, 12, 31, tzinfo=timezone.utc)
        )
        model_pricing = ModelPricing("gpt-4o", [entry])

        price = model_pricing.get_active_price(self.test_date)
        assert price == entry

    def test_get_active_price_without_datetime(self):
        """Test get_active_price without datetime for single entry."""
        entry = PriceEntry(
            input_per_1k=0.0025,
            output_per_1k=0.010
        )
        model_pricing = ModelPricing("gpt-4o", [entry])

        price = model_pricing.get_active_price()
        assert price == entry

    def test_get_active_price_no_prices(self):
        """Test get_active_price with no prices."""
        model_pricing = ModelPricing("gpt-4o", [])
        
        with pytest.raises(PriceNotFoundError, match="No prices defined for model gpt-4o"):
            model_pricing.get_active_price()

    def test_get_active_price_multiple_entries_without_period(self):
        """Test error with multiple entries without validity period."""
        entry1 = PriceEntry(input_per_1k=0.0025, output_per_1k=0.010)
        entry2 = PriceEntry(input_per_1k=0.0030, output_per_1k=0.015)
        model_pricing = ModelPricing("gpt-4o", [entry1, entry2])
        
        with pytest.raises(PriceNotFoundError, match="Multiple price entries without validity period"):
            model_pricing.get_active_price()

    def test_get_active_price_no_active_price(self):
        """Test error when no active price found."""
        entry = PriceEntry(
            input_per_1k=0.0025,
            output_per_1k=0.010,
            valid_from=datetime(2027, 1, 1, tzinfo=timezone.utc),
            valid_to=datetime(2027, 12, 31, tzinfo=timezone.utc)
        )
        model_pricing = ModelPricing("gpt-4o", [entry])
        
        with pytest.raises(PriceNotFoundError, match="No active price found for model gpt-4o"):
            model_pricing.get_active_price(self.test_date)