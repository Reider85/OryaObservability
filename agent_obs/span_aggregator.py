"""Span aggregation for micro-spans into aggregated spans with events."""

import time
from dataclasses import dataclass, field
from typing import Optional

from agent_obs.observability import Span


@dataclass
class SpanAggregate:
    """Aggregated span containing micro-spans as events.

    Canonical contract from PP0 PRODUCTION-PROMPTS.md — cannot be modified.
    """

    parent_span_id: str
    span_type: str
    events: list[dict]
    children_count: int
    bytes_saved: int

    def to_dict(self) -> dict:
        """Serialize including computed name field for bytes_saved calculation."""
        return {
            "parent_span_id": self.parent_span_id,
            "span_type": self.span_type,
            "events": self.events,
            "children_count": self.children_count,
            "bytes_saved": self.bytes_saved,
            "name": f"agent.reasoning:aggregated:{self.children_count}",
        }

    def to_span(self, context: SpanContext) -> Span:
        """Convert SpanAggregate to a Span (PP09 wiring).
        
        Args:
            context: SpanContext for the aggregate (trace_id, agent_id, etc.)
        """
        from agent_obs.observability import Span, SpanType, Event, _to_iso8601

        # Build events with sorted timestamps and ISO format
        events = []
        for event_data in sorted(self.events, key=lambda e: e["ts"]):
            events.append(
                Event(
                    name=event_data["name"],
                    timestamp=_to_iso8601(event_data["ts"]),
                    attributes=dict(event_data["attrs"]),
                )
            )

        return Span(
            name=f"agent.reasoning:aggregated:{self.children_count}",
            span_type=SpanType.AGENT_REASONING,
            context=context,
            start_time=self.events[0]["ts"] if self.events else 0.0,
            end_time=0.0,  # Will be set when span ends
            attributes={
                "status": "ok",
                "aggregation.children_count": self.children_count,
                "aggregation.bytes_saved": self.bytes_saved,
                "aggregation.source": "span_aggregator",
            },
            events=events,
        )

    @classmethod
    def from_span(cls, span: Span) -> "SpanAggregate":
        """Convert Span back to SpanAggregate (PP09 reversibility).
        
        Requires span to have aggregation metadata and events representing micro-spans.
        """
        children_count = span.attributes.get("aggregation.children_count", 0)
        bytes_saved = span.attributes.get("aggregation.bytes_saved", 0)
        
        # Convert events back to PP0 internal format {name, ts, attrs}
        events = []
        for event in span.events:
            # Parse timestamp back to float (ISO -> POSIX)
            ts = None
            if event.timestamp:
                from agent_obs.observability import _parse_iso8601
                try:
                    ts = _parse_iso8601(event.timestamp)
                except (ValueError, AttributeError):
                    # Fallback to current time if parsing fails
                    ts = 0.0
            events.append({
                "name": event.name,
                "ts": ts,
                "attrs": dict(event.attributes),
            })
        
        # Get parent_span_id from span context
        parent_span_id = span.context.parent_span_id or ""
        
        return cls(
            parent_span_id=parent_span_id,
            span_type=span.span_type.value,
            events=events,
            children_count=children_count,
            bytes_saved=bytes_saved,
        )


class SpanAggregator:
    """Accumulates micro-spans and flushes them into aggregated spans.

    Buffers spans until max_count or max_window_seconds is reached.
    """

    def __init__(
        self,
        max_count: int = 50,
        max_window_seconds: float = 1.0,
        span_type: str = "agent.reasoning",
        clock: Optional[callable] = None,
    ):
        self.max_count = max_count
        self.max_window_seconds = max_window_seconds
        self.span_type = span_type
        self.clock = clock or time.time
        self._buffer: list[Span] = []
        self._first_span_time: Optional[float] = None
        self._lock = None

    async def add(self, span: Span) -> Optional[SpanAggregate]:
        """Add a span to the buffer.

        Returns:
            SpanAggregate if buffer should be flushed (condition met), None otherwise.
        """
        if self._lock is None:
            self._lock = __import__("asyncio").Lock()

        async with self._lock:
            self._buffer.append(span)
            if not self._first_span_time:
                self._first_span_time = span.start_time

            # Check flush condition: count >= max_count OR window expired
            if (
                len(self._buffer) >= self.max_count
                or (
                    self._first_span_time
                    and (self.clock() - self._first_span_time) >= self.max_window_seconds
                )
            ):
                return await self.flush()
            return None

    async def flush(self) -> Optional[SpanAggregate]:
        """Flush buffer into aggregated span. Returns None if buffer is empty."""
        if not self._buffer:
            self._buffer = []
            self._first_span_time = None
            return None

        # Build aggregate from buffer
        first_span = self._buffer[0]
        parent_span_id = first_span.context.parent_span_id or ""

        events = []
        for micro_span in self._buffer:
            event = {
                "name": micro_span.name,
                "ts": micro_span.start_time,
                "attrs": dict(micro_span.attributes),
            }
            events.append(event)

        # Calculate bytes saved
        total_micro_bytes = sum(len(str(s.to_dict())) for s in self._buffer)
        aggregate_bytes = len(str(SpanAggregate(
            parent_span_id=parent_span_id,
            span_type=self.span_type,
            events=events,
            children_count=len(self._buffer),
            bytes_saved=0,  # placeholder for calculation
        ).to_dict()))
        
        bytes_saved = total_micro_bytes - aggregate_bytes

        # Create aggregate
        aggregate = SpanAggregate(
            parent_span_id=parent_span_id,
            span_type=self.span_type,
            events=events,
            children_count=len(self._buffer),
            bytes_saved=bytes_saved,
        )

        # Clear buffer
        self._buffer = []
        self._first_span_time = None

        return aggregate