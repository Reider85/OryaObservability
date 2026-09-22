"""Tests for per-trace aggregation onto the root span (P20/P24).

The root ``agent.loop`` span must carry ``steps.count``, ``cost.usd_sum``,
``cost.over_budget`` and ``security.incident`` (stub) so the tail sampler and
the Langfuse saved queries can filter traces without scanning children.
"""

import pytest

from agent_obs import PriceBook, Usage
from agent_obs.observability import ObservabilitySDK


@pytest.fixture
def sdk() -> ObservabilitySDK:
    return ObservabilitySDK(exporters=[], enabled=True)


def _find_agent_loop(spans):
    return [s for s in spans if s.span_type.value == "agent.loop"][0]


class TestStepsCount:
    async def test_root_counts_child_steps(self, sdk) -> None:
        @sdk.agent_observed("agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai"):
                ...
            async with sdk.tool_call(_obs_ctx, tool_name="search"):
                ...

        await run()
        root = _find_agent_loop(sdk.last_spans)
        assert root.attributes["steps.count"] == 3


class TestCostAggregation:
    async def test_root_sums_child_costs(self, sdk) -> None:
        sdk.cost_threshold = 0.001  # force over_budget=true for a $0.0025 trace
        book = PriceBook.load("price_book.yaml")
        usage = Usage(input=1000, output=0)  # gpt-4o: 1000 input tokens = $0.0025

        @sdk.agent_observed("agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage,
            ):
                ...

        await run()
        root = _find_agent_loop(sdk.last_spans)
        assert root.attributes["cost.usd_sum"] == pytest.approx(0.0025)
        assert root.attributes["cost.over_budget"] == "true"

    async def test_root_cost_below_threshold(self, sdk) -> None:
        sdk.cost_threshold = 100.0
        book = PriceBook.load("price_book.yaml")
        usage = Usage(input=1000, output=0)

        @sdk.agent_observed("agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                price_book=book,
                usage=usage,
            ):
                ...

        await run()
        root = _find_agent_loop(sdk.last_spans)
        assert root.attributes["cost.over_budget"] == "false"


class TestErrorPropagation:
    async def test_root_marked_error_when_child_fails(self, sdk) -> None:
        @sdk.agent_observed("agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(_obs_ctx, model="gpt-4o", provider="openai") as span:
                span.attributes["status"] = "error"

        await run()
        root = _find_agent_loop(sdk.last_spans)
        assert root.attributes["status"] == "error"


class TestSamplerStub:
    async def test_security_incident_stub_on_root(self, sdk) -> None:
        @sdk.agent_observed("agent")
        async def run(_obs_ctx=None):
            ...

        await run()
        root = _find_agent_loop(sdk.last_spans)
        assert root.attributes["security.incident"] == "false"