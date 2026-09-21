"""Base exporter contract for observability spans."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_obs.observability import Span


class BaseExporter:
    """Abstract base class for span exporters.

    All exporters must implement ``export`` (receives a batch of spans) and
    ``flush`` (drains any internal buffers on shutdown).  Implementations must
    be idempotent by ``span_id`` — duplicate exports should not create
    duplicate data in the backend.
    """

    async def export(self, batch: list[Span]) -> None:
        """Export a batch of completed spans to the backend.

        Must not raise exceptions to the caller; errors should be logged and
        surfaced via metrics.
        """
        raise NotImplementedError

    async def flush(self) -> None:
        """Flush any buffered data on shutdown.

        Called once during ``ObservabilitySDK.shutdown()``.  Must be safe to
        call multiple times (idempotent).
        """
        raise NotImplementedError
