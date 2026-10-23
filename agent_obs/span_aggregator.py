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