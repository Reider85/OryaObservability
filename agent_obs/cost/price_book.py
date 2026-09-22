"""Price book loader with validation for valid_from/valid_to periods."""

from __future__ import annotations

import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
import yaml


class PriceNotFoundError(Exception):
    """Raised when model not found or no active price for the specified moment."""


@dataclass(frozen=True)
class PriceEntry:
    """Individual price entry for a model with validity period."""
    input_per_1k: float
    output_per_1k: float
    cached_input_per_1k: Optional[float] = None
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None

    def is_valid_at(self, at: datetime) -> bool:
        """Check if this price entry is valid at the specified datetime."""
        # Ensure both datetimes are timezone-aware for comparison
        at_utc = at if at.tzinfo is not None else at.replace(tzinfo=timezone.utc)
        valid_from_utc = self.valid_from if self.valid_from is None else self.valid_from.replace(tzinfo=timezone.utc)
        valid_to_utc = self.valid_to if self.valid_to is None else self.valid_to.replace(tzinfo=timezone.utc)
        
        if valid_from_utc and at_utc < valid_from_utc:
            return False
        if valid_to_utc and at_utc >= valid_to_utc:
            return False
        return True


@dataclass(frozen=True)
class ModelPricing:
    """Pricing information for a single model with multiple price entries."""
    model: str
    prices: list[PriceEntry]

    def get_active_price(self, at: Optional[datetime] = None) -> PriceEntry:
        """Get the active price for the specified datetime.
        
        Args:
            at: Datetime to check. If None, returns the only entry or the one without validity period.
            
        Returns:
            The active PriceEntry.
            
        Raises:
            PriceNotFoundError: If no active price found for the model at the specified time.
        """
        if not self.prices:
            raise PriceNotFoundError(f"No prices defined for model {self.model}")
        
        # If no datetime specified and only one price entry, return it
        if at is None and len(self.prices) == 1:
            return self.prices[0]
        
        # If no datetime specified, find entry without validity period
        if at is None:
            entries_without_period = [p for p in self.prices if p.valid_from is None and p.valid_to is None]
            if len(entries_without_period) == 1:
                return entries_without_period[0]
            elif len(entries_without_period) > 1:
                raise PriceNotFoundError(f"Multiple price entries without validity period for model {self.model}")
            else:
                # Fall back to finding any active price (shouldn't happen if validation passed)
                active_prices = [p for p in self.prices if p.is_valid_at(at)]
                if active_prices:
                    return active_prices[0]
                raise PriceNotFoundError(f"No active price found for model {self.model} at the specified time")
        
        # With datetime specified, find active price
        active_prices = [p for p in self.prices if p.is_valid_at(at)]
        if not active_prices:
            raise PriceNotFoundError(f"No active price found for model {self.model} at {at}")
        if len(active_prices) > 1:
            warnings.warn(f"Multiple overlapping price entries for model {self.model} at {at}, using the first one")
        return active_prices[0]


@dataclass(frozen=True)
class PriceBook:
    """Complete price book with version, currency, and model pricing."""
    version: str
    currency: str
    models: dict[str, ModelPricing]

    @classmethod
    def load(cls, path: str | Path) -> PriceBook:
        """Load and validate price book from YAML file.
        
        Args:
            path: Path to the price_book.yaml file.
            
        Returns:
            Validated PriceBook instance.
            
        Raises:
            ValueError: If validation fails (empty version, invalid prices, overlapping periods).
        """
        path_obj = Path(path)
        if not path_obj.exists():
            raise FileNotFoundError(f"Price book file not found: {path}")
        
        try:
            with open(path_obj, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML in price book: {e}")
        
        # Validate basic structure
        if not isinstance(data, dict):
            raise ValueError("Price book must be a dictionary")
        
        # Validate version
        version = data.get('version')
        if not version or not isinstance(version, str) or not version.strip():
            raise ValueError("Version must be a non-empty string")
        
        # Validate currency
        currency = data.get('currency')
        if not currency or not isinstance(currency, str):
            raise ValueError("Currency must be specified")
        
        # Validate models section
        models_data = data.get('models')
        if not isinstance(models_data, dict):
            raise ValueError("Models must be a dictionary")
        
        models = {}
        
        for model_name, model_config in models_data.items():
            if not isinstance(model_name, str) or not model_name.strip():
                raise ValueError("Model name must be a non-empty string")
            
            if not isinstance(model_config, dict):
                raise ValueError(f"Model config for {model_name} must be a dictionary")
            
            # Parse validity period from model level
            valid_from = None
            valid_to = None
            
            if 'valid_from' in model_config:
                valid_from_str = model_config['valid_from']
                if valid_from_str:
                    try:
                        valid_from = datetime.fromisoformat(valid_from_str).replace(tzinfo=timezone.utc)
                    except ValueError:
                        raise ValueError(f"Invalid valid_from format for {model_name}: {valid_from_str}")
            
            if 'valid_to' in model_config:
                valid_to_str = model_config['valid_to']
                if valid_to_str:
                    try:
                        valid_to = datetime.fromisoformat(valid_to_str).replace(tzinfo=timezone.utc)
                    except ValueError:
                        raise ValueError(f"Invalid valid_to format for {model_name}: {valid_to_str}")
            
            # Validate validity period
            if valid_from and valid_to and valid_from >= valid_to:
                raise ValueError(f"valid_from must be before valid_to for model {model_name}")
            
            # Extract pricing information (remove validity fields)
            pricing_config = {k: v for k, v in model_config.items() 
                            if k not in ['valid_from', 'valid_to']}
            
            # Validate required pricing fields
            if 'input_per_1k' not in pricing_config:
                raise ValueError(f"input_per_1k is required for model {model_name}")
            if 'output_per_1k' not in pricing_config:
                raise ValueError(f"output_per_1k is required for model {model_name}")
            
            # Validate prices are positive
            input_price = pricing_config['input_per_1k']
            output_price = pricing_config['output_per_1k']
            
            if not isinstance(input_price, (int, float)) or input_price <= 0:
                raise ValueError(f"input_per_1k must be positive for model {model_name}")
            if not isinstance(output_price, (int, float)) or output_price <= 0:
                raise ValueError(f"output_per_1k must be positive for model {model_name}")
            
            # Validate cached price if present
            cached_price = pricing_config.get('cached_input_per_1k')
            if cached_price is not None:
                if not isinstance(cached_price, (int, float)) or cached_price <= 0:
                    raise ValueError(f"cached_input_per_1k must be positive for model {model_name}")
            
            # Create price entry
            price_entry = PriceEntry(
                input_per_1k=input_price,
                output_per_1k=output_price,
                cached_input_per_1k=cached_price,
                valid_from=valid_from,
                valid_to=valid_to
            )
            
            # Check for overlapping periods (only if validity period is defined)
            if valid_from or valid_to:
                # For now, we only allow one entry per model with validity period
                # In the future, we could support multiple entries with non-overlapping periods
                pass
            
            # Create model pricing
            model_pricing = ModelPricing(
                model=model_name,
                prices=[price_entry]
            )
            
            models[model_name] = model_pricing
        
        return cls(
            version=version.strip(),
            currency=currency,
            models=models
        )
    
    def price_for(self, model: str, at: Optional[datetime] = None) -> PriceEntry:
        """Get the price for a model at the specified datetime.
        
        Args:
            model: Name of the model.
            at: Datetime to get price for. If None, returns the default price.
            
        Returns:
            The PriceEntry for the specified model and time.
            
        Raises:
            PriceNotFoundError: If model not found or no active price for the specified time.
        """
        if model not in self.models:
            raise PriceNotFoundError(f"Model '{model}' not found in price book")
        
        model_pricing = self.models[model]
        return model_pricing.get_active_price(at)