"""Pilot agent: full OpenAI-compatible LLM agent instrumented with agent_obs.

Each ``llm.call`` span carries the five P15 cost/token attributes:
``tokens.input``, ``tokens.output``, ``tokens.cached``, ``cost.usd`` and
``cost.price_book_version``.  The SDK's ``llm_call`` sugar fills them
automatically from a ``Usage`` object and a ``PriceBook``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from openai import AsyncOpenAI

from agent_obs import (
    ObservabilitySDK,
    PriceBook,
    Usage,
)
from agent_obs.cost import compute_cost


@dataclass
class PilotAgentConfig:
    """Runtime configuration for the pilot agent."""

    model: str = "gpt-4o"
    provider: str = "openai"
    max_tokens: int = 512
    temperature: float = 0.2
    price_book_path: str | Path = "price_book.yaml"


class PilotAgent:
    """Simple OpenAI-compatible agent that answers a user query.

    ``run`` (wrapped on init in an ``agent.loop`` root span) answers the query;
    every LLM call is a child ``llm.call`` span with token/cost attributes.
    """

    def __init__(
        self,
        sdk: ObservabilitySDK,
        client: AsyncOpenAI,
        config: PilotAgentConfig | None = None,
    ) -> None:
        self.sdk = sdk
        self.client = client
        self.config = config or PilotAgentConfig()
        self.price_book = PriceBook.load(self.config.price_book_path)
        self.run = self.sdk.agent_observed("pilot-agent", version="1.0.0")(
            self._run
        )

    @staticmethod
    def _usage_from(response) -> Usage:
        """Extract a Usage object from an OpenAI chat-completion response."""
        return Usage(
            input=response.usage.prompt_tokens,
            output=response.usage.completion_tokens,
            cached=getattr(response.usage, "cached_tokens", 0) or 0,
        )

    async def _run(self, user_query: str, _obs_ctx=None) -> str:
        """Answer *user_query* through the LLM, with one instrumented call."""
        config = self.config
        async with self.sdk.llm_call(
            _obs_ctx,
            model=config.model,
            provider=config.provider,
        ) as span:
            span.attributes["llm.temperature"] = config.temperature
            response = await self.client.chat.completions.create(
                model=config.model,
                messages=[{"role": "user", "content": user_query}],
                max_tokens=config.max_tokens,
                temperature=config.temperature,
            )
            usage = self._usage_from(response)
            result = compute_cost(
                usage, model=config.model, book=self.price_book
            )
            span.attributes.update({
                "tokens.input": usage.input,
                "tokens.output": usage.output,
                "tokens.cached": usage.cached,
                "cost.usd": round(result.cost_usd, 8),
                "cost.price_book_version": result.price_book_version,
            })
            return response.choices[0].message.content or ""