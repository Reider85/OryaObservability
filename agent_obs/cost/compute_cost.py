"""LLM call cost computation (P14)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from agent_obs.cost.price_book import PriceBook


@dataclass(frozen=True)
class Usage:
    """Token usage for a single LLM call."""
    input: int
    output: int
    cached: int = 0


@dataclass(frozen=True)
class CostResult:
    """Computed cost with the price book version used."""
    cost_usd: float
    price_book_version: str


def compute_cost(
    usage: Usage,
    model: str,
    book: PriceBook,
    at: datetime | None = None,
) -> CostResult:
    """Compute the cost of an LLM call (formula §3.5).

    cost = input * price_in / 1000
         + output * price_out / 1000
         + cached * price_cached / 1000 (if cached price is set;
           otherwise cached tokens are priced at the input rate).

    The result is NOT rounded here; rounding to 8 decimal places
    happens only at the span-attribute write boundary.

    Raises:
        PriceNotFoundError: if model is unknown or has no active price.
    """
    price = book.price_for(model, at)

    cached_rate = (
        price.cached_input_per_1k
        if price.cached_input_per_1k is not None
        else price.input_per_1k
    )

    cost = (
        usage.input * price.input_per_1k / 1000
        + usage.output * price.output_per_1k / 1000
        + usage.cached * cached_rate / 1000
    )

    return CostResult(cost_usd=cost, price_book_version=book.version)
