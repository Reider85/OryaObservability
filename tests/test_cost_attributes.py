"""Tests for cost attributes on llm.call spans (P15, T1.4.4)."""

import pytest

from agent_obs import ObservabilitySDK, PriceBook, Usage
from agent_obs.observability import SpanType
from pilot_agent import PilotAgent, PilotAgentConfig


class FakeUsage:
    """Minimal stand-in for an OpenAI CompletionUsage object."""

    def __init__(self, prompt_tokens, completion_tokens, cached_tokens=0):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.cached_tokens = cached_tokens


class FakeChatMessage:
    def __init__(self, content):
        self.content = content
        self.tool_calls = None


class FakeChoice:
    def __init__(self, content):
        self.message = FakeChatMessage(content)


class FakeResponse:
    def __init__(self, usage, content="hello"):
        self.usage = usage
        self.choices = [FakeChoice(content)]


class FakeOpenAIClient:
    """Fake AsyncOpenAI client: returns a canned response, counts calls."""

    def __init__(self, response: FakeResponse):
        self.response = response
        self.calls = []

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


@pytest.fixture
def book():
    return PriceBook.load("price_book.yaml")


@pytest.fixture
def sdk():
    return ObservabilitySDK(exporters=[])


def _llm_spans(sdk):
    return [s for s in sdk.last_spans if s.span_type == SpanType.LLM_CALL]


class TestSdkLlmCallSugar:
    async def test_attributes_set_with_usage_and_price_book(self, sdk, book):
        # gpt-4o: in=0.0025, out=0.010, cached=0.00125
        # 1000*0.0025/1000 + 500*0.010/1000 + 400*0.00125/1000 = 0.008
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
        span = _llm_spans(sdk)[0]
        assert span.attributes["tokens.input"] == 1000
        assert span.attributes["tokens.output"] == 500
        assert span.attributes["tokens.cached"] == 400
        assert span.attributes["cost.usd"] == pytest.approx(0.008)
        assert span.attributes["cost.price_book_version"] == book.version

    async def test_no_attributes_without_usage(self, sdk):
        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx, model="gpt-4o", provider="openai"
            ):
                pass

        await run()
        span = _llm_spans(sdk)[0]
        assert "tokens.input" not in span.attributes
        assert "cost.usd" not in span.attributes

    async def test_tokens_without_price_book(self, sdk):
        # Tokens recorded, cost attributes absent when price_book omitted.
        usage = Usage(input=100, output=20, cached=5)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="gpt-4o",
                provider="openai",
                usage=usage,
            ):
                pass

        await run()
        span = _llm_spans(sdk)[0]
        assert span.attributes["tokens.input"] == 100
        assert span.attributes["tokens.output"] == 20
        assert span.attributes["tokens.cached"] == 5
        assert "cost.usd" not in span.attributes
        assert "cost.price_book_version" not in span.attributes

    async def test_unknown_model_skips_cost_but_keeps_tokens(self, sdk, book):
        usage = Usage(input=50, output=10)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            async with sdk.llm_call(
                _obs_ctx,
                model="unknown-model",
                provider="openai",
                price_book=book,
                usage=usage,
            ):
                pass

        await run()
        span = _llm_spans(sdk)[0]
        assert span.attributes["tokens.input"] == 50
        assert span.attributes["tokens.output"] == 10
        assert "cost.usd" not in span.attributes
        assert "cost.price_book_version" not in span.attributes


class TestPilotAgent:
    """Integration test: pilot agent + fake OpenAI client (P15)."""

    async def test_every_llm_call_carries_all_five_attributes(self, sdk, book):
        usage = FakeUsage(prompt_tokens=1000, completion_tokens=500, cached_tokens=400)
        client = FakeOpenAIClient(FakeResponse(usage=usage))
        agent = PilotAgent(
            sdk=sdk,
            client=client,
            config=PilotAgentConfig(price_book_path="price_book.yaml"),
        )

        answer = await agent.run("hello")
        assert answer == "hello"
        assert len(_llm_spans(sdk)) == 1

        span = _llm_spans(sdk)[0]
        # All 5 attributes from P15 are present on every llm.call.
        assert span.attributes["tokens.input"] == 1000
        assert span.attributes["tokens.output"] == 500
        assert span.attributes["tokens.cached"] == 400
        assert span.attributes["cost.usd"] == pytest.approx(0.008)
        assert span.attributes["cost.price_book_version"] == book.version

    async def test_cost_matches_manual_calculation(self, sdk):
        usage = FakeUsage(prompt_tokens=10_000, completion_tokens=2000, cached_tokens=8000)
        client = FakeOpenAIClient(FakeResponse(usage=usage))
        config = PilotAgentConfig(model="gpt-4o-mini", price_book_path="price_book.yaml")
        agent = PilotAgent(sdk=sdk, client=client, config=config)

        await agent.run("hi")
        span = _llm_spans(sdk)[0]
        # gpt-4o-mini: in=0.00015, out=0.0006, cached=0.000075
        # 10000*0.00015/1000 + 2000*0.0006/1000 + 8000*0.000075/1000
        # = 0.0015 + 0.0012 + 0.0006 = 0.0033
        assert span.attributes["cost.usd"] == pytest.approx(0.0033)

    async def test_agent_loop_and_llm_call_in_same_trace(self, sdk):
        client = FakeOpenAIClient(
            FakeResponse(usage=FakeUsage(prompt_tokens=10, completion_tokens=5))
        )
        agent = PilotAgent(sdk=sdk, client=client)
        await agent.run("q")

        agent_span = next(
            s for s in sdk.last_spans if s.span_type == SpanType.AGENT_LOOP
        )
        llm_span = _llm_spans(sdk)[0]
        assert llm_span.context.trace_id == agent_span.context.trace_id
        assert llm_span.context.parent_span_id == agent_span.context.span_id