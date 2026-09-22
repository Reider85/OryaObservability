"""Stdout exporter for local debugging and safe degradation (P19).

Used by tests and as the degradation target when the Langfuse configuration
is absent but ``AGENT_OBS_FAIL_ON_CONFIG=0``.  Never blocks the agent: spans
are rendered to the log and dropped immediately (fire-and-forget).
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from agent_obs.exporters.base import BaseExporter

if TYPE_CHECKING:
    from agent_obs.observability import Span

logger = logging.getLogger(__name__)


class StdoutExporter(BaseExporter):
    """Exporter that renders each batch to the log (debug only)."""

    def __init__(self, level: int = logging.DEBUG) -> None:
        self._level = level

    async def export(self, batch: list[Span]) -> None:
        """Log a compact JSON rendering of the batch."""
        payload = {
            "spans": [
                {
                    "trace_id": s.context.trace_id,
                    "span_id": s.context.span_id,
                    "name": s.name,
                    "span_type": s.span_type.value,
                    "attributes": s.attributes,
                }
                for s in batch
            ]
        }
        logger.log(self._level, "stdout_export %s", json.dumps(payload))

    async def flush(self) -> None:
        """No-op — nothing is buffered."""
        return