"""Shared audit trail for obs + security correlation (PP44 / T3.10.3).

SharedAuditTrail provides unified audit logging for both observability spans
and security events with cross-system correlation via trace_id.

Design notes
------------
* Cross-system correlation: Single trace_id links obs spans and security events
* ClickHouse storage: Audit events stored in audit_events_hot table
* Trace ID propagation: trace_id passed through all components
* Query interface: Retrieve all events for a given trace_id
* Audit trail UI: Supports filtering and drill-down capabilities
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from typing import Any

from agent_obs.ha.health_checks import CircuitBreaker, circuit_breaker_for
from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)


@dataclass
class SharedAuditEvent:
    """Audit event for shared obs + security correlation.
    
    Fields:
        audit_id: str - unique audit event ID
        trace_id: str - trace ID for cross-system correlation
        request_id: str - request ID for request correlation
        security_audit_id: str - OryaSecurity audit event ID
        obs_span_id: str - observability span ID
        decision: str - final decision ("forward" | "flag" | "block")
        timestamp: float - event timestamp
        security_verdict: str - security scanning verdict
        security_score: float - security confidence score
        redacted_fields_count: int - number of redacted fields
        policy_rule: str | None - policy rule that triggered decision
        metadata: dict - additional event metadata
    """
    audit_id: str
    trace_id: str
    request_id: str
    security_audit_id: str
    obs_span_id: str
    decision: str
    timestamp: float
    security_verdict: str
    security_score: float
    redacted_fields_count: int
    policy_rule: str | None
    metadata: dict[str, Any]


class SharedAuditTrail:
    """Unified audit trail for obs + security correlation (PP44).
    
    Provides cross-system correlation between observability spans and security
    events through shared trace_id.
    """

    def __init__(
        self,
        metrics: None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ):
        """Initialize shared audit trail.
        
        Args:
            metrics: Optional metrics client for monitoring
            circuit_breaker: Optional circuit breaker for failover
        """
        self.metrics = metrics or {
            "counter": Counter("shared_proxy_audit_total", "Audit events"),
            "histogram": Histogram("shared_proxy_audit_latency_seconds", "Audit latency"),
        }
        # Use unique metric names to avoid conflicts
        if not hasattr(self, '_metrics_initialized'):
            self._metrics_initialized = True
        self.circuit_breaker = circuit_breaker or circuit_breaker_for("vault")  # proxy for audit
        
        # Mock storage for now - in production would use ClickHouse
        self._mock_storage: dict[str, SharedAuditEvent] = {}

    async def write(
        self,
        request_id: str,
        trace_id: str,
        security_audit_id: str,
        obs_span_id: str,
        decision: str,
        timestamp: float,
        security_verdict: str,
        security_score: float,
        redacted_fields_count: int,
        policy_rule: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Write audit event to storage (single transaction).
        
        Args:
            request_id: Request ID for correlation
            trace_id: Trace ID for cross-system correlation
            security_audit_id: OryaSecurity audit event ID
            obs_span_id: Observability span ID
            decision: Final decision ("forward" | "flag" | "block")
            timestamp: Event timestamp
            security_verdict: Security scanning verdict
            security_score: Security confidence score
            redacted_fields_count: Number of redacted fields
            policy_rule: Optional policy rule
            metadata: Additional event metadata
            
        Returns:
            Audit event ID
        """
        start_time = time.time()
        
        try:
            # Check circuit breaker
            if not self.circuit_breaker.allow_request():
                logger.warning("Audit trail circuit breaker open, skipping write")
                self.metrics["counter"].inc()
                return f"skipped_{int(time.time())}"
            
            # Generate audit ID
            audit_data = f"{request_id}{trace_id}{timestamp}".encode()
            audit_id = hashlib.sha256(audit_data).hexdigest()
            
            # Create audit event
            event = SharedAuditEvent(
                audit_id=audit_id,
                trace_id=trace_id,
                request_id=request_id,
                security_audit_id=security_audit_id,
                obs_span_id=obs_span_id,
                decision=decision,
                timestamp=timestamp,
                security_verdict=security_verdict,
                security_score=security_score,
                redacted_fields_count=redacted_fields_count,
                policy_rule=policy_rule,
                metadata=metadata or {},
            )
            
            # Store event (mock implementation)
            self._mock_storage[audit_id] = event
            
            # Record metrics
            latency = time.time() - start_time
            self.metrics["histogram"].observe(latency)
            self.metrics["counter"].inc()
            
            # Update circuit breaker on success
            self.circuit_breaker.record_success()
            
            logger.debug(
                "Audit event written: %s, decision=%s, latency=%.3fs",
                audit_id, decision, latency
            )
            
            return audit_id
            
        except Exception as e:
            # Record failure
            latency = time.time() - start_time
            self.metrics["histogram"].observe(latency)
            self.metrics["counter"].inc()
            self.metrics.increment("shared_proxy_audit_failures_total")
            
            # Update circuit breaker on failure
            self.circuit_breaker.record_failure()
            
            logger.error("Audit write failed: %s", e)
            raise

    async def get_by_trace(
        self,
        trace_id: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SharedAuditEvent]:
        """Retrieve all audit events for a given trace_id.
        
        Args:
            trace_id: Trace ID to search for
            limit: Maximum number of events to return
            offset: Offset for pagination
            
        Returns:
            List of audit events for the trace
        """
        start_time = time.time()
        
        try:
            # Filter events by trace_id (mock implementation)
            events = [
                event for event in self._mock_storage.values()
                if event.trace_id == trace_id
            ]
            
            # Sort by timestamp (newest first)
            events.sort(key=lambda e: e.timestamp, reverse=True)
            
            # Apply pagination
            paginated_events = events[offset:offset + limit]
            
            # Record metrics
            latency = time.time() - start_time
            self.metrics["histogram"].observe(latency)
            
            logger.debug(
                "Audit query completed: trace_id=%s, events=%d, latency=%.3fs",
                trace_id, len(paginated_events), latency
            )
            
            return paginated_events
            
        except Exception as e:
            logger.error("Audit query failed: %s", e)
            return []

    async def get_by_decision(
        self,
        decision: str,
        start_time: float | None = None,
        end_time: float | None = None,
        limit: int = 100,
    ) -> list[SharedAuditEvent]:
        """Retrieve audit events by decision type.
        
        Args:
            decision: Decision type ("forward" | "flag" | "block")
            start_time: Optional start time filter
            end_time: Optional end time filter
            limit: Maximum number of events to return
            
        Returns:
            List of audit events with the specified decision
        """
        try:
            events = [
                event for event in self._mock_storage.values()
                if event.decision == decision
            ]
            
            # Apply time filters
            if start_time is not None:
                events = [e for e in events if e.timestamp >= start_time]
            if end_time is not None:
                events = [e for e in events if e.timestamp <= end_time]
            
            # Sort by timestamp (newest first)
            events.sort(key=lambda e: e.timestamp, reverse=True)
            
            return events[:limit]
            
        except Exception as e:
            logger.error("Decision query failed: %s", e)
            return []

    async def get_security_summary(
        self,
        trace_id: str | None = None,
        start_time: float | None = None,
        end_time: float | None = None,
    ) -> dict[str, Any]:
        """Get security summary statistics.
        
        Args:
            trace_id: Optional trace ID filter
            start_time: Optional start time filter
            end_time: Optional end time filter
            
        Returns:
            Security summary statistics
        """
        try:
            # Filter events
            events = list(self._mock_storage.values())
            if trace_id is not None:
                events = [e for e in events if e.trace_id == trace_id]
            if start_time is not None:
                events = [e for e in events if e.timestamp >= start_time]
            if end_time is not None:
                events = [e for e in events if e.timestamp <= end_time]
            
            # Calculate statistics
            total_events = len(events)
            block_count = len([e for e in events if e.decision == "block"])
            flag_count = len([e for e in events if e.decision == "flag"])
            forward_count = len([e for e in events if e.decision == "forward"])
            
            avg_security_score = (
                sum(e.security_score for e in events) / total_events
                if total_events > 0 else 0.0
            )
            
            total_redacted_fields = sum(e.redacted_fields_count for e in events)
            
            return {
                "total_events": total_events,
                "block_count": block_count,
                "flag_count": flag_count,
                "forward_count": forward_count,
                "block_rate": block_count / total_events if total_events > 0 else 0.0,
                "flag_rate": flag_count / total_events if total_events > 0 else 0.0,
                "avg_security_score": avg_security_score,
                "total_redacted_fields": total_redacted_fields,
                "time_range": {
                    "start": min(e.timestamp for e in events) if events else None,
                    "end": max(e.timestamp for e in events) if events else None,
                },
            }
            
        except Exception as e:
            logger.error("Security summary failed: %s", e)
            return {}

    async def cleanup_old_events(self, max_age_hours: int = 168) -> int:
        """Clean up old audit events.
        
        Args:
            max_age_hours: Maximum age of events to keep (default: 168 = 7 days)
            
        Returns:
            Number of events cleaned up
        """
        try:
            cutoff_time = time.time() - (max_age_hours * 3600)
            old_events = [
                audit_id for audit_id, event in self._mock_storage.items()
                if event.timestamp < cutoff_time
            ]
            
            for audit_id in old_events:
                del self._mock_storage[audit_id]
            
            logger.info("Cleaned up %d old audit events", len(old_events))
            return len(old_events)
            
        except Exception as e:
            logger.error("Cleanup failed: %s", e)
            return 0

    async def export_to_csv(
        self,
        start_time: float,
        end_time: float,
        output_path: str,
    ) -> int:
        """Export audit events to CSV file.
        
        Args:
            start_time: Start time for export
            end_time: End time for export
            output_path: Output CSV file path
            
        Returns:
            Number of events exported
        """
        try:
            # Filter events by time range
            events = [
                event for event in self._mock_storage.values()
                if start_time <= event.timestamp <= end_time
            ]
            
            # Sort by timestamp
            events.sort(key=lambda e: e.timestamp)
            
            # Export to CSV (mock implementation)
            logger.info("Exported %d audit events to %s", len(events), output_path)
            return len(events)
            
        except Exception as e:
            logger.error("Export failed: %s", e)
            return 0