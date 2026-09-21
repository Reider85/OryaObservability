"""Core telemetry objects: SpanContext, Span and (later) ObservabilitySDK.

Span Context carries the identity and lineage of a single trace.

ID generation strategy: ULID-like ids are produced from the standard library
only (no external dependency). Each id is 26 Crockford base32 characters:
10 chars encode millisecond timestamp (48 bits) and 16 chars encode 80 random
bits. The time prefix keeps ids lexicographically sortable by creation time.
"""

from __future__ import annotations

import contextvars
import functools
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional

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


_obs_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_obs_user_id", default=""
)
_obs_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_obs_session_id", default=""
)


class ObservabilitySDK:
    """SDK entry point for agent observability.

    Instrumentation happens via the ``agent_observed`` decorator, which wraps an
    async agent ``run()`` method into an ``agent.loop`` root span. Completed
    spans are enqueued (a temporary in-memory list on MVP; replaced by the ring
    buffer + export worker in P07-P08).
    """

    def __init__(self, exporters: list, enabled: bool = True):
        self._exporters = exporters
        self.enabled = enabled
        self.last_spans: list[Span] = []

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
        """Temporary stub: append span to ``last_spans`` for tests.

        P07-P08 replaces this with the ring buffer + export worker. The public
        contract of ``_enqueue`` stays unchanged.
        """
        self.last_spans.append(span)

    def agent_observed(self, agent_id: str, version: str = ""):
        """Decorate an async agent ``run()`` method into an ``agent.loop`` root span.

        On success the span gets ``status=ok``; on exception the span gets
        ``status=error`` and ``error.type``, and the exception is re-raised.
        The wrapped function receives the trace context as the ``_obs_ctx`` kwarg.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                ctx = self._build_context(agent_id=agent_id, version=version)
                span = Span(
                    name=f"agent.loop:{agent_id}",
                    span_type=SpanType.AGENT_LOOP,
                    context=ctx,
                )
                span.start_time = _now()
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
                    await self._enqueue(span)

            return wrapper

        return decorator