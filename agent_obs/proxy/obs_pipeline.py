"""Span enrichment pipeline for the LLM proxy (PP41 / T3.9.3).

``ObsPipeline.enrich_span`` is the observability half of the proxy request
path: it parses the proxied request/response, extracts the key LLM attributes
(model, messages, tokens, finish_reason, latency, ttft) and computes cost
through the MVP price book, then stores bodies according to the ``log_bodies``
and content-sampling policies.

Design notes
------------
* Best-effort: every parse tolerates malformed input, and the caller wraps the
  call (``LLMProxyServer._enrich_span``) — enrichment must never break a
  proxied request (observability never blocks business logic).
* Streaming usage: OpenAI reports ``usage`` only in the final SSE chunk when
  ``stream_options.include_usage=true`` (the proxy injects it — PP40).  The
  server keeps raw usage/finish-reason chunks past the body-size cap so this
  pipeline can always parse them.
* ``headers`` drives tenant/agent/user cost attribution (PP42): missing
  values fall back to ``default``/``unknown``/``anonymous`` and the resolved
  triple is stamped on the span context, the ``cost.attribution.*``
  attributes, and reported to the :class:`CostAttributionTracker` (when
  wired) so budget enforcement and the daily rollup see the spend.
* Price book: lazily loaded once from ``AGENT_OBS_PRICE_BOOK_PATH`` (default
  ``price_book.yaml``); a missing/invalid book degrades to "tokens without
  cost" with a warning — matching the MVP ``llm_call`` fail-open semantics.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from agent_obs.content_sampling import attach_llm_content
from agent_obs.cost import PriceBook, Usage
from agent_obs.cost.attribution import attribution_from_headers
from agent_obs.observability import ObservabilitySDK, Span
from agent_obs.proxy import ProxyConfig
from agent_obs.proxy.prompt_templates import (
    extract_first_system_prompt,
    compute_prompt_sha256,
    extract_version_label,
    create_prompt_enricher,
)

if TYPE_CHECKING:  # pragma: no cover
    from agent_obs.cost.attribution import CostAttributionTracker

logger = logging.getLogger(__name__)

_PRICE_BOOK_PATH_ENV = "AGENT_OBS_PRICE_BOOK_PATH"
_DEFAULT_PRICE_BOOK_PATH = "price_book.yaml"

#: Sentinel: ``price_book`` was not supplied → lazy-load from the environment.
#: An explicit ``None`` disables cost attribution entirely.
_AUTO_PRICE_BOOK: Any = object()


@dataclass
class Timing:
    """Request/upstream timestamps captured by the proxy (PP41 §2).

    ``t_first_token`` is streaming-only; without it :attr:`ttft_ms` reports
    ``0.0`` (N/A) as required for non-streaming responses.
    """

    t_request_received: float
    t_upstream_started: float
    t_upstream_completed: float
    t_first_token: float | None = None

    @property
    def ttft_ms(self) -> float:
        """Time-to-first-token: first chunk minus upstream start."""
        if self.t_first_token is None:
            return 0.0
        return max(0.0, (self.t_first_token - self.t_upstream_started) * 1000.0)

    @property
    def latency_ms(self) -> float:
        """End-to-end latency: upstream completion minus request receipt."""
        return max(0.0, (self.t_upstream_completed - self.t_request_received) * 1000.0)


@dataclass
class _CompletionSummary:
    """What was parsed from a response body or an aggregated SSE stream."""

    saw_payload: bool = False
    content: str = ""
    has_choice: bool = False
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None


def _json_dict(raw: bytes | None) -> dict[str, Any]:
    """Best-effort JSON object parse (invalid / non-object → {})."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_sse_line(line: bytes) -> dict[str, Any] | None:
    """Parse a single SSE line; only ``data:`` lines with JSON payloads count."""
    line = line.strip()
    if not line.startswith(b"data:"):
        return None
    payload = line[len(b"data:"):].strip()
    if payload == b"[DONE]":
        return None
    parsed = _json_dict(payload)
    return parsed or None


def iter_sse_json(chunks: Iterable[bytes]) -> Iterable[dict[str, Any]]:
    """Yield JSON ``data:`` payloads from raw SSE byte-chunks.

    Handles events split across chunk boundaries: an incomplete trailing line
    is buffered until the next chunk (or flushed at the end).
    """
    buffer = b""
    for chunk in chunks:
        if not chunk:
            continue
        buffer += chunk
        while b"\n" in buffer:
            line, buffer = buffer.split(b"\n", 1)
            payload = _parse_sse_line(line)
            if payload is not None:
                yield payload
    payload = _parse_sse_line(buffer)
    if payload is not None:
        yield payload


def parse_sse_chunk(data: bytes) -> list[dict[str, Any]]:
    """Parse one complete SSE byte-chunk into its JSON ``data:`` payloads."""
    return list(iter_sse_json([data]))


def _aggregate_stream(payloads: Iterable[dict[str, Any]]) -> _CompletionSummary:
    """Fold streaming chunks: concatenated delta content + latest usage/finish."""
    summary = _CompletionSummary()
    parts: list[str] = []
    for payload in payloads:
        summary.saw_payload = True
        if isinstance(payload.get("usage"), dict):
            summary.usage = payload["usage"]
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        first = choices[0]
        if not isinstance(first, dict):
            continue
        summary.has_choice = True
        delta = first.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            parts.append(delta["content"])
        if isinstance(first.get("finish_reason"), str):
            summary.finish_reason = first["finish_reason"]
    summary.content = "".join(parts)
    return summary


def _parse_response(resp: dict[str, Any]) -> _CompletionSummary:
    """Extract content / finish_reason / usage from a non-streaming response."""
    summary = _CompletionSummary(saw_payload=True)
    choices = resp.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        summary.has_choice = True
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            summary.content = message["content"]
        if isinstance(choice.get("finish_reason"), str):
            summary.finish_reason = choice["finish_reason"]
    if isinstance(resp.get("usage"), dict):
        summary.usage = resp["usage"]
    return summary


def _usage_from(usage: dict[str, Any] | None) -> Usage | None:
    """Build an MVP :class:`Usage` from an OpenAI-shaped usage dict."""
    if not isinstance(usage, dict):
        return None
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0
    cached = usage.get("cached_tokens", 0) or 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict) and details.get("cached_tokens") is not None:
        cached = details["cached_tokens"]
    try:
        return Usage(input=int(prompt), output=int(completion), cached=int(cached))
    except (TypeError, ValueError):
        return None


def _load_price_book() -> PriceBook | None:
    """Load the proxy price book; failure degrades to "no cost attributes"."""
    path = os.environ.get(_PRICE_BOOK_PATH_ENV) or _DEFAULT_PRICE_BOOK_PATH
    try:
        return PriceBook.load(path)
    except (OSError, ValueError) as exc:
        logger.warning(
            "Proxy price book %r unavailable (%s); cost attributes will be skipped",
            path,
            exc,
        )
        return None


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ObsPipeline:
    """Attribute enrichment for ``llm.proxy.call`` spans (PP41).

    Stateless per request; the only cached piece is the lazily loaded price
    book.  Construct with the proxy :class:`ProxyConfig` and (optionally) an
    explicit ``price_book`` — ``None`` disables cost attribution, omission
    triggers the lazy env-based load.  An optional ``cost_tracker`` (PP42)
    receives every attributed ``cost.usd`` so budget enforcement and the
    daily tenant rollup stay current; without it enrichment is pure.
    
    PP53: Includes system prompt template versioning with SHA256 mapping.
    """

    def __init__(
        self,
        config: ProxyConfig,
        price_book: PriceBook | None | object = _AUTO_PRICE_BOOK,
        cost_tracker: CostAttributionTracker | None = None,
        hot_store=None,  # Optional HotStore for prompt template operations
    ) -> None:
        self.config = config
        self._price_book = price_book
        self.cost_tracker = cost_tracker
        self.hot_store = hot_store
        self._prompt_enricher = None

    @property
    def price_book(self) -> PriceBook | None:
        if self._price_book is _AUTO_PRICE_BOOK:
            self._price_book = _load_price_book()
        return self._price_book  # type: ignore[return-value]
    
    @property
    def prompt_enricher(self):
        """Lazy initialization of prompt template enricher."""
        if self._prompt_enricher is None and self.hot_store is not None:
            self._prompt_enricher = create_prompt_enricher(self.hot_store)
        return self._prompt_enricher

    async def enrich_span(
        self,
        span: Span,
        request_body: bytes,
        response_body: bytes | None,
        stream_chunks: list[bytes] | None,
        headers: Mapping[str, str],
        timing: Timing,
    ) -> Span:
        """Attach request/response-derived attributes to *span* (PP41 §1, PP42 §1).

        Non-streaming calls pass ``response_body`` (``stream_chunks=None``);
        streaming calls pass the retained ``stream_chunks`` (``response_body``
        is then ignored).  *headers* resolves the tenant/agent/user cost
        attribution (PP42).
        """
        tenant_id, agent_id, user_id = attribution_from_headers(headers)
        # PP42 §1: attribution defaults ride on the span context (SpanContext
        # has a tenant_id field) and the explicit cost.attribution.* keys so
        # reports can group by either.
        span.context.tenant_id = tenant_id
        span.attributes["cost.attribution.tenant_id"] = tenant_id
        span.attributes["cost.attribution.agent_id"] = agent_id
        span.attributes["cost.attribution.user_id"] = user_id
        
        # PP53: Enrich span with system prompt template information
        if self.prompt_enricher is not None:
            try:
                await self.prompt_enricher.enrich_span_with_prompt(
                    span=span,
                    request_body=request_body,
                    trace_id=span.context.trace_id,
                )
            except Exception as e:
                logger.warning("Failed to enrich span with prompt template: %s", e)

        req = _json_dict(request_body)
        messages = req.get("messages")
        message_list = messages if isinstance(messages, list) else []
        model = str(req.get("model") or "")

        if stream_chunks is not None:
            summary = _aggregate_stream(iter_sse_json(stream_chunks))
            if summary.saw_payload:
                output_text = summary.content or None
            else:
                # Non-SSE body in stream mode (e.g. upstream error JSON):
                # keep the raw text rather than nothing.
                raw = b"".join(stream_chunks).decode("utf-8", errors="replace")
                output_text = raw or None
        else:
            summary = _parse_response(_json_dict(response_body))
            if summary.content:
                output_text = summary.content
            elif response_body:
                output_text = response_body.decode("utf-8", errors="replace")
            else:
                output_text = None

        input_text = (
            json.dumps(message_list, ensure_ascii=False)
            if message_list
            else request_body.decode("utf-8", errors="replace")
        )

        # --- request/response structure (PP41 §1 attribute list) -----------
        span.attributes["llm.provider"] = self.config.upstream_provider
        if model:
            span.attributes["llm.model"] = model
        if req.get("temperature") is not None:
            span.attributes["llm.temperature"] = req["temperature"]
        span.attributes["llm.input_messages_count"] = len(message_list)
        span.attributes["llm.output_messages_count"] = 1 if summary.has_choice else 0
        span.attributes["llm.stream"] = bool(req.get("stream"))
        if summary.finish_reason is not None:
            span.attributes["llm.finish_reason"] = summary.finish_reason
        span.attributes["llm.ttft_ms"] = round(timing.ttft_ms, 3)
        span.attributes["llm.latency_ms"] = round(timing.latency_ms, 3)

        # --- tokens + cost (MVP _compute_cost semantics) --------------------
        usage = _usage_from(summary.usage)
        if usage is not None:
            ObservabilitySDK._attach_cost_attributes(
                span, model, usage, self.price_book if model else None
            )
            # PP42 §1: attribute the spend to tenant+agent for budget/rollup.
            if self.cost_tracker is not None:
                cost_usd = float(span.attributes.get("cost.usd") or 0.0)
                self.cost_tracker.record(tenant_id, agent_id, cost_usd)

        # --- bodies: log_bodies policy + content sampling (P22) -------------
        self._attach_bodies(span, input_text=input_text, output_text=output_text)
        return span

    def _attach_bodies(
        self, span: Span, *, input_text: str | None, output_text: str | None
    ) -> None:
        """Store request/response bodies per the log_bodies + sampling policy.

        ``log_bodies=False`` → sha256 + char count only (never plaintext).
        Otherwise bodies are truncated to ``log_bodies_max_bytes`` and go
        through ``attach_llm_content`` so the ``AGENT_OBS_CONTENT_RATE``
        sampling policy decides full-text vs hash storage.  Char counts
        always describe the full logical text (spec: ``llm.input_chars`` =
        ``len(json.dumps(messages))``).
        """
        cfg = self.config

        def cap(text: str) -> tuple[str, bool]:
            raw = text.encode("utf-8", errors="replace")
            if len(raw) <= cfg.log_bodies_max_bytes:
                return text, False
            return raw[: cfg.log_bodies_max_bytes].decode("utf-8", errors="replace"), True

        if not cfg.log_bodies:
            if input_text is not None:
                span.attributes["llm.input_sha256"] = _sha256(input_text)
                span.attributes["llm.input_chars"] = len(input_text)
            if output_text is not None:
                span.attributes["llm.output_sha256"] = _sha256(output_text)
                span.attributes["llm.output_chars"] = len(output_text)
            return

        in_text: str | None = None
        out_text: str | None = None
        if input_text is not None:
            in_text, in_trunc = cap(input_text)
            if in_trunc:
                span.attributes["llm.input_truncated"] = True
        if output_text is not None:
            out_text, out_trunc = cap(output_text)
            if out_trunc:
                span.attributes["llm.output_truncated"] = True
        attach_llm_content(span, input_text=in_text, output_text=out_text)
        if input_text is not None:
            span.attributes["llm.input_chars"] = len(input_text)
        if output_text is not None:
            span.attributes["llm.output_chars"] = len(output_text)


__all__ = [
    "ObsPipeline",
    "Timing",
    "iter_sse_json",
    "parse_sse_chunk",
]
