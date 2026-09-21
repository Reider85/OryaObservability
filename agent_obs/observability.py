"""Core telemetry objects: SpanContext, Span and (later) ObservabilitySDK.

Span Context carries the identity and lineage of a single trace.

ID generation strategy: ULID-like ids are produced from the standard library
only (no external dependency). Each id is 26 Crockford base32 characters:
10 chars encode millisecond timestamp (48 bits) and 16 chars encode 80 random
bits. The time prefix keeps ids lexicographically sortable by creation time.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

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