"""Tests for cost metrics export (P16, T1.4.5)."""

import re

import pytest
from prometheus_client import generate_latest

from agent_obs import ObservabilitySDK, PriceBook, Usage
from agent_obs.metrics import cost_per_request_usd, cost_total_usd
from agent_obs.observability import SpanType


def _parse_metric(exposition: bytes, name: str, labels: dict | None = None) -> list[tuple[float, dict]]:
    """Parse a Prometheus metric from exposition text.

    Returns a list of (value, labels_dict) tuples for matching samples.
    """
    text = exposition.decode("utf-8")
    results = []
    for match in re.finditer(
        rf'^{re.escape(name)}\{{([^}}]*)\}}\s+([\d.e+-]+)',
        text,
        re.MULTILINE,
    ):
        raw_labels, value = match.group(1), float(match.group(2))
        if raw_labels:
            label_dict = dict(
                item.split("=", 1) for item in raw_labels.split(",") if "=" in item
            )
            label_dict = {k: v.strip('"') for k, v in label_dict.items()}
        else:
            label_dict = {}
        if labels is None or all(label_dict.get(k) == v for k, v in labels.items()):
            results.append((value, label_dict))
    return results


@pytest.fixture(autouse=True)
def _reset_metrics():
    """Reset all cost metrics between tests to avoid cross-test leakage."""
    cost_per_request_usd._metrics.clear()
    cost_total_usd._metrics.clear()
    yield
    cost_per_request_usd._metrics.clear()
    cost_total_usd._metrics.clear()


@pytest.fixture
def book():
    return PriceBook.load("price_book.yaml")


@pytest.fixture
def sdk():
    return ObservabilitySDK(exporters=[])


def _llm_spans(sdk):
    return [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL]


class TestCostMetrics:
    async def test_metrics_appear_in_exposition(self, sdk, book):
        usage = Usage(input=1000, output=500, cached=400)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage,
            ) as span:
                return span

        await run()
        exposition = generate_latest()
        assert b"agent_obs_cost_per_request_usd" in exposition
        assert b"agent_obs_cost_total_usd" in exposition

    async def test_gauge_labels_correct(self, sdk, book):
        usage = Usage(input=1000, output=500, cached=400)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage,
            ) as span:
                return span

        await run()
        exposition = generate_latest()
        gauge_values = _parse_metric(exposition, "agent_obs_cost_per_request_usd")
        assert len(gauge_values) == 1
        value, labels = gauge_values[0]
        assert labels["agent_id"] == "test-agent"
        assert labels["model"] == "gpt-4o"
        assert value == pytest.approx(0.008)

    async def test_counter_labels_correct(self, sdk, book):
        usage = Usage(input=1000, output=500, cached=400)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage,
            ) as span:
                return span

        await run()
        exposition = generate_latest()
        counter_values = _parse_metric(
            exposition, "agent_obs_cost_total_usd_total"
        )
        assert len(counter_values) == 1
        value, labels = counter_values[0]
        assert labels["agent_id"] == "test-agent"
        assert labels["model"] == "gpt-4o"
        assert value == pytest.approx(0.008)

    async def test_counter_monotonically_increases(self, sdk, book):
        usage1 = Usage(input=1000, output=500, cached=400)  # cost = 0.008
        usage2 = Usage(input=2000, output=1000, cached=800)  # cost = 0.016

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage1,
            ):
                pass
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage2,
            ):
                pass

        await run()
        exposition = generate_latest()
        counter_values = _parse_metric(
            exposition, "agent_obs_cost_total_usd_total"
        )
        assert len(counter_values) == 1
        value, labels = counter_values[0]
        assert value == pytest.approx(0.024)  # 0.008 + 0.016

    async def test_gauge_updates_to_latest_value(self, sdk, book):
        usage1 = Usage(input=1000, output=500, cached=400)  # cost = 0.008
        usage2 = Usage(input=2000, output=1000, cached=800)  # cost = 0.016

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage1,
            ):
                pass
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage2,
            ):
                pass

        await run()
        exposition = generate_latest()
        gauge_values = _parse_metric(exposition, "agent_obs_cost_per_request_usd")
        assert len(gauge_values) == 1
        value, labels = gauge_values[0]
        assert value == pytest.approx(0.016)  # last call's cost

    async def test_different_models_get_separate_labels(self, sdk, book):
        usage = Usage(input=100, output=20)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage,
            ):
                pass
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o-mini",
                provider="openai",
                price_book=book,
                usage=usage,
            ):
                pass

        await run()
        exposition = generate_latest()
        counter_values = _parse_metric(
            exposition, "agent_obs_cost_total_usd_total"
        )
        assert len(counter_values) == 2
        models = {labels["model"] for _, labels in counter_values}
        assert models == {"gpt-4o", "gpt-4o-mini"}
