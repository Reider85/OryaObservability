"""Core telemetry objects: SpanContext, Span and (later) ObservabilitySDK.

Span Context carries the identity and lineage of a single trace.

ID generation strategy: ULID-like ids are produced from the standard library
only (no external dependency). Each id is 26 Crockford base32 characters:
10 chars encode millisecond timestamp (48 bits) and 16 chars encode 80 random
bits. The time prefix keeps ids lexicographically sortable by creation time.
"""

from __future__ import annotations

import asyncio
import atexit
import contextvars
import functools
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, AsyncIterator, Callable, Optional

from agent_obs.cost.compute_cost import Usage, compute_cost
from agent_obs.cost.price_book import PriceBook, PriceNotFoundError

logger = logging.getLogger(__name__)

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TRACE_ID_CHARS = 26


def _encode_crockford(value: int, length: int) -> str:
    """Encode an integer as a fixed-length Crockford base32 string."""
    chars = [""] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def _new_ulid() -> str:
    """Generate a ULID-like sortable id (26 chars, timestamp + random)."""
    ts_ms = int(time.time() * 1000)
    random_bits = secrets.randbits(80)
    return _encode_crockford(ts_ms, 10) + _encode_crockford(random_bits, 16)


@dataclass
class SpanContext:
    """Immutable-ish context object identifying a span within a trace.

    Matches the canonical contract from ARCHITECT.md field-for-field.
    """

    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    agent_id: str = ""
    agent_version: str = ""
    user_id: str = ""
    session_id: str = ""

    @classmethod
    def new(
        cls,
        agent_id: str,
        agent_version: str = "",
        parent: Optional["SpanContext"] = None,
        user_id: str = "",
        session_id: str = "",
    ) -> "SpanContext":
        """Create a new span context.

        A root context (no parent) gets a fresh trace_id; a child context
        inherits trace_id from its parent and records parent_span_id.
        """
        if parent is not None:
            trace_id = parent.trace_id
            parent_span_id = parent.span_id
        else:
            trace_id = _new_ulid()
            parent_span_id = None
        return cls(
            trace_id=trace_id,
            span_id=_new_ulid(),
            parent_span_id=parent_span_id,
            agent_id=agent_id,
            agent_version=agent_version,
            user_id=user_id,
            session_id=session_id,
        )

    def to_dict(self) -> dict:
        """Serialize to a plain dict (attribute names match the contract)."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "user_id": self.user_id,
            "session_id": self.session_id,
        }


def _now() -> float:
    """Current time as POSIX timestamp (seconds)."""
    return time.time()


def _to_iso8601(ts: Optional[float]) -> Optional[str]:
    """Convert a POSIX timestamp to ISO 8601 string (UTC)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


class SpanType(Enum):
    """Allowed span types on MVP (level 2 types are placeholders, must not be used)."""

    AGENT_LOOP = "agent.loop"
    LLM_CALL = "llm.call"
    TOOL_CALL = "tool.call"
    # Level 2 placeholders — not implemented in MVP
    # TOOL_INPUT = "tool.input"
    # TOOL_OUTPUT = "tool.output"
    # LLM_INPUT = "llm.input"
    # LLM_OUTPUT = "llm.output"
    # EVAL_RESULT = "eval.result"
    # AUDIT_EVENT = "audit.event"


class InvalidSpanTypeError(ValueError):
    """Raised when an invalid span_type string is provided."""

    pass


@dataclass
class Event:
    """A timestamped event within a span."""

    name: str
    timestamp: str  # ISO 8601
    attributes: dict = field(default_factory=dict)


@dataclass
class Span:
    """Core telemetry object representing a unit of work within a trace.

    Matches the canonical contract from ARCHITECT.md §7.1.
    """

    name: str
    span_type: SpanType
    context: SpanContext
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    start_time: float = 0.0
    end_time: Optional[float] = None

    def __post_init__(self) -> None:
        if isinstance(self.span_type, str):
            try:
                self.span_type = SpanType(self.span_type)
            except ValueError:
                raise InvalidSpanTypeError(
                    f"Invalid span_type: {self.span_type!r}. "
                    f"Allowed: {[st.value for st in SpanType]}"
                )

    @property
    def duration_ms(self) -> float:
        """Duration of the span in milliseconds."""
        if self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time) * 1000.0

    def set_attribute(self, key: str, value: object) -> None:
        """Set a key-value attribute on the span."""
        self.attributes[key] = value

    def add_event(self, name: str, attrs: Optional[dict] = None) -> None:
        """Append a timestamped event to the span."""
        self.events.append(
            Event(
                name=name,
                timestamp=_to_iso8601(_now()) or "",
                attributes=attrs or {},
            )
        )

    def to_dict(self) -> dict:
        """Serialize to the §7.1 ARCHITECT.md schema."""
        return {
            "trace_id": self.context.trace_id,
            "span_id": self.context.span_id,
            "parent_span_id": self.context.parent_span_id,
            "name": self.name,
            "span_type": self.span_type.value,
            "start_time": _to_iso8601(self.start_time),
            "end_time": _to_iso8601(self.end_time),
            "status": self.attributes.get("status", "unset"),
            "attributes": dict(self.attributes),
            "events": [
                {
                    "name": e.name,
                    "timestamp": e.timestamp,
                    "attributes": dict(e.attributes),
                }
                for e in self.events
            ],
        }


class NullSpan:
    """Zero-overhead no-op span for disabled observability mode."""

    __slots__ = ()

    def set_attribute(self, key: str, value: object) -> None:
        """No-op: does nothing."""
        pass

    def add_event(self, name: str, attrs: Optional[dict] = None) -> None:
        """No-op: does nothing."""
        pass


_obs_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_obs_user_id", default=""
)
_obs_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_obs_session_id", default=""
)
_active_span_context: contextvars.ContextVar[Optional[SpanContext]] = (
    contextvars.ContextVar("_active_span_context", default=None)
)


class ObservabilitySDK:
    """SDK entry point for agent observability.

    Instrumentation happens via the ``agent_observed`` decorator, which wraps an
    async agent ``run()`` method into an ``agent.loop`` root span. Completed
    spans are enqueued (a temporary in-memory list on MVP; replaced by the ring
    buffer + export worker in P07-P08).
    """

    def __init__(self, exporters: list, enabled: bool = None, config: dict | None = None, ring_buffer_maxsize: int = 100_000):
        self._exporters = exporters

        # Priority: explicit enabled parameter > config > environment variable
        if config is not None and "enabled" in config:
            self.enabled = config["enabled"]
        else:
            # Use explicit enabled parameter if provided, otherwise check environment
            if enabled is not None:
                self.enabled = enabled
            else:
                env_enabled = os.environ.get("AGENT_OBS_ENABLED", "true").strip().lower()
                self.enabled = env_enabled in ("true", "1", "yes", "on")

        self.last_spans: list[Span] = []
        self._ring_buffer: asyncio.Queue[Span] = asyncio.Queue(
            maxsize=ring_buffer_maxsize
        )
        self._shutdown: asyncio.Event = asyncio.Event()
        self._last_drop_warning: float = 0.0
        self._worker_task: asyncio.Task | None = None
        if self.enabled and self._exporters:
            self._worker_task = asyncio.create_task(self._export_worker())
            atexit.register(self._atexit_handler)

    def _build_context(
        self,
        agent_id: str,
        version: str = "",
        user_id: str = "",
        session_id: str = "",
    ) -> SpanContext:
        """Create a root SpanContext for an agent loop.

        user_id/session_id fall back to values stored in the current
        contextvars (``_obs_user_id`` / ``_obs_session_id``).
        """
        return SpanContext.new(
            agent_id=agent_id,
            agent_version=version,
            user_id=user_id or _obs_user_id.get(),
            session_id=session_id or _obs_session_id.get(),
        )

    async def _enqueue(self, span: Span) -> None:
        """Enqueue a span into the ring buffer for async export.

        This is a fire-and-forget operation — never blocks the caller.
        If the buffer is full, the span is dropped and counted.
        """
        try:
            self._ring_buffer.put_nowait(span)
            self.last_spans.append(span)
            from agent_obs.metrics import spans_total

            spans_total.labels(agent_id=span.context.agent_id).inc()
        except asyncio.QueueFull:
            from agent_obs.metrics import dropped_spans_total

            dropped_spans_total.labels(agent_id=span.context.agent_id).inc()
            now = _now()
            if now - self._last_drop_warning >= 5.0:
                self._last_drop_warning = now
                logger.warning(
                    "Ring buffer full, dropping span %s (trace=%s)",
                    span.context.span_id,
                    span.context.trace_id,
                )

    async def _export_worker(self) -> None:
        """Background worker that drains the ring buffer and exports spans.

        Batches spans with a 50ms drain window and max batch size of 512.
        Fan-outs to all registered exporters via asyncio.gather.

        When ``_shutdown`` is signalled the worker finishes the current batch
        then continues draining any remaining spans until the queue is empty
        before exiting.
        """
        while True:
            batch: list[Span] = []
            try:
                first = await asyncio.wait_for(
                    self._ring_buffer.get(), timeout=0.5
                )
                batch.append(first)
            except asyncio.TimeoutError:
                if self._shutdown.is_set() and self._ring_buffer.empty():
                    break
                continue

            deadline = _now() + 0.05  # 50ms drain window
            while _now() < deadline and len(batch) < 512:
                try:
                    batch.append(self._ring_buffer.get_nowait())
                except asyncio.QueueEmpty:
                    break

            try:
                await asyncio.gather(
                    *[exp.export(batch) for exp in self._exporters],
                    return_exceptions=True,
                )
            except Exception:
                logger.exception("Export worker encountered an unexpected error")

            if self._shutdown.is_set() and self._ring_buffer.empty():
                break

    async def shutdown(self) -> None:
        """Gracefully shut down: drain buffer, flush exporters, cancel worker.

        The entire procedure is bounded by a 5-second deadline.  If the deadline
        is exceeded the worker is cancelled and a warning is logged with the
        count of undelivered spans.
        """
        self._shutdown.set()

        if self._worker_task is None:
            return

        try:
            await asyncio.wait_for(self._worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            remaining = self._ring_buffer.qsize()
            logger.warning(
                "Shutdown timed out after 5s, cancelling worker (%d spans undelivered)",
                remaining,
            )
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass

        self._worker_task = None

        # Flush all exporters (best-effort within remaining time, max 3s)
        for exporter in self._exporters:
            try:
                await asyncio.wait_for(exporter.flush(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Exporter %s flush failed or timed out", type(exporter).__name__)

    async def __aenter__(self) -> "ObservabilitySDK":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.shutdown()

    def agent_observed(self, agent_id: str, version: str = ""):
        """Decorate an async agent ``run()`` method into an ``agent.loop`` root span.

        On success the span gets ``status=ok``; on exception the span gets
        ``status=error`` and ``error.type``, and the exception is re-raised.
        The wrapped function receives the trace context as the ``_obs_ctx`` kwarg.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if not self.enabled:
                # Zero-overhead mode: return a decorator that returns the function unchanged
                @functools.wraps(fn)
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    return fn(*args, **kwargs)
                return wrapper
            
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                ctx = self._build_context(agent_id=agent_id, version=version)
                span = Span(
                    name=f"agent.loop:{agent_id}",
                    span_type=SpanType.AGENT_LOOP,
                    context=ctx,
                )
                span.start_time = _now()
                token = _active_span_context.set(ctx)
                try:
                    result = await fn(*args, _obs_ctx=ctx, **kwargs)
                    span.attributes["status"] = "ok"
                    return result
                except Exception as e:
                    span.attributes["status"] = "error"
                    span.attributes["error.type"] = type(e).__name__
                    raise
                finally:
                    span.end_time = _now()
                    _active_span_context.reset(token)
                    await self._enqueue(span)

            return wrapper

        return decorator

    def _atexit_handler(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            # No running loop — we can create one for sync scripts
            asyncio.run(self.shutdown())
        else:
            logger.warning(
                "Event loop is running at exit; async shutdown not possible. "
                "Use 'async with sdk:' or call sdk.shutdown() explicitly."
            )

    def _atexit_handler(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            # No running loop — we can create one for sync scripts
            asyncio.run(self.shutdown())
        else:
            logger.warning(
                "Event loop is running at exit; async shutdown not possible. "
                "Use 'async with sdk:' or call sdk.shutdown() explicitly."
            )

    @asynccontextmanager
    async def llm_call(
        self,
        ctx: SpanContext,
        *,
        model: str,
        provider: str,
        price_book: PriceBook | None = None,
        usage: Usage | None = None,
    ) -> AsyncIterator[Span]:
        """Create a child ``llm.call`` span for an LLM invocation.

        The span inherits ``trace_id`` from *ctx* and gets its own ``span_id``
        with ``parent_span_id`` set to the active span.  On exit the span is
        enqueued regardless of exceptions.

        When both *usage* and *price_book* are provided, token and cost
        attributes are auto-filled on exit (SDK sugar, P15):
        ``tokens.input``, ``tokens.output``, ``tokens.cached``,
        ``cost.usd`` and ``cost.price_book_version``.
        """
        if not self.enabled:
            # Zero-overhead mode: yield a no-op NullSpan
            yield NullSpan()
            return

        parent_ctx = _active_span_context.get()
        child_ctx = SpanContext.new(
            agent_id=ctx.agent_id,
            agent_version=ctx.agent_version,
            parent=parent_ctx if parent_ctx is not None else ctx,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
        )
        span = Span(
            name=f"llm.call:{model}",
            span_type=SpanType.LLM_CALL,
            context=child_ctx,
            attributes={"llm.model": model, "llm.provider": provider},
        )
        span.start_time = _now()
        token = _active_span_context.set(child_ctx)
        try:
            yield span
        finally:
            span.end_time = _now()
            _active_span_context.reset(token)
            self._attach_cost_attributes(span, model, usage, price_book)
            await self._enqueue(span)

    @staticmethod
    def _attach_cost_attributes(
        span: Span,
        model: str,
        usage: Usage | None,
        price_book: PriceBook | None,
    ) -> None:
        """Attach token and cost attributes to a span (P15).

        If *usage* is provided, token counts are always recorded.  Cost
        attributes additionally require *price_book*: a missing model raises
        ``PriceNotFoundError`` which is swallowed with a warning so that
        observability never blocks the agent.
        """
        if usage is None:
            return
        span.attributes["tokens.input"] = usage.input
        span.attributes["tokens.output"] = usage.output
        span.attributes["tokens.cached"] = usage.cached
        if price_book is None:
            return
        try:
            result = compute_cost(usage, model, price_book)
        except PriceNotFoundError:
            logger.warning(
                "No price for model %r in price book %s; cost attributes skipped",
                model,
                price_book.version,
            )
            return
        span.attributes["cost.usd"] = round(result.cost_usd, 8)
        span.attributes["cost.price_book_version"] = result.price_book_version

    @asynccontextmanager
    async def tool_call(self, ctx: SpanContext, *, tool_name: str) -> AsyncIterator[Span]:
        """Create a child ``tool.call`` span for an instrumented tool.

        The span inherits ``trace_id`` from *ctx* and gets its own ``span_id``
        with ``parent_span_id`` set to the active span.  On exit the span is
        enqueued regardless of exceptions.
        """
        if not self.enabled:
            # Zero-overhead mode: yield a no-op NullSpan
            yield NullSpan()
            return
            
        parent_ctx = _active_span_context.get()
        child_ctx = SpanContext.new(
            agent_id=ctx.agent_id,
            agent_version=ctx.agent_version,
            parent=parent_ctx if parent_ctx is not None else ctx,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
        )
        span = Span(
            name=f"tool.call:{tool_name}",
            span_type=SpanType.TOOL_CALL,
            context=child_ctx,
            attributes={"tool.name": tool_name},
        )
        span.start_time = _now()
        token = _active_span_context.set(child_ctx)
        try:
            yield span
        finally:
            span.end_time = _now()
            _active_span_context.reset(token)
            await self._enqueue(span)