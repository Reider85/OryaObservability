"""Unified decision router for security + observability + policy (PP44 / T3.10.2).

DecisionRouter implements priority-based decision making where security decisions
override observability and policy decisions. It implements record-then-block semantics
to ensure auditability even when requests are blocked.

Design notes
------------
* Priority order: Security > Policy > Observability
* Record-then-block: Even blocked requests create spans and audit events
* Single transaction: All decisions are batched and idempotent
* Policy engine: Optional policy evaluation for tenant/agent restrictions
* Decision metadata: Detailed reasons for audit trail
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# AuditTrailLogger will be implemented separately
from prometheus_client import Counter, Histogram
from agent_obs.observability import Span
from agent_obs.proxy.security_client import SecurityScanResult

logger = logging.getLogger(__name__)

# Module-level metrics to avoid duplicate registration
_decisions_total = Counter("decisions_total", "Decision-router outcomes", ["action", "reason"])
_decision_router_latency_seconds = Histogram("decision_router_latency_seconds", "Decision latency")


@dataclass
class DecisionRecord:
    """Record for single-transaction batched write."""
    request_id: str
    trace_id: str
    decision: Decision
    security_result: SecurityScanResult
    obs_span: Span
    timestamp: float


class DecisionRecorder:
    """Batched async writer for decision records with idempotency."""
    
    def __init__(self, audit_sink=None, span_sink=None, batch_size=50, flush_interval=1.0):
        self.audit_sink = audit_sink
        self.span_sink = span_sink
        self.batch_size = batch_size
        self.flush_interval = flush_interval
        
        self._queue: asyncio.Queue[DecisionRecord] = asyncio.Queue(maxsize=1000)
        self._seen_request_ids: set[str] = set()
        self._flush_task: asyncio.Task | None = None
        self._running = False
    
    async def start(self):
        """Start the flush task."""
        if not self._running:
            self._running = True
            self._flush_task = asyncio.create_task(self._flush_loop())
    
    async def stop(self):
        """Stop the flush task and flush remaining records."""
        if self._flush_task:
            self._running = False
            self._flush_task.cancel()
            try:
                await self._flush_task
            except asyncio.CancelledError:
                pass
            self._flush_task = None
        
        # Flush any remaining records
        await self.flush()
    
    async def record(self, request_id: str, trace_id: str, decision: Decision, 
                    security_result: SecurityScanResult, obs_span: Span) -> None:
        """Record a decision for batched write (idempotent by request_id)."""
        if request_id in self._seen_request_ids:
            logger.debug("Duplicate request_id %s, skipping", request_id)
            return
        
        record = DecisionRecord(
            request_id=request_id,
            trace_id=trace_id,
            decision=decision,
            security_result=security_result,
            obs_span=obs_span,
            timestamp=time.time()
        )
        
        try:
            self._queue.put_nowait(record)
            self._seen_request_ids.add(request_id)
        except asyncio.QueueFull:
            logger.warning("Decision recorder queue full, dropping record for %s", request_id)
    
    async def flush(self) -> None:
        """Flush all pending records."""
        records = []
        while not self._queue.empty():
            try:
                records.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        
        if records:
            await self._write_batch(records)
    
    async def _flush_loop(self):
        """Background loop to flush records periodically."""
        while self._running:
            await asyncio.sleep(self.flush_interval)
            await self.flush()
    
    async def _write_batch(self, records: list[DecisionRecord]) -> None:
        """Write a batch of records to audit sink and span sink."""
        if not records:
            return
        
        # Write audit events
        if self.audit_sink:
            audit_events = []
            for record in records:
                # Create audit event with action based on decision
                action = self._map_audit_action(record.decision.action)
                audit_event = {
                    "timestamp": record.timestamp,
                    "request_id": record.request_id,
                    "trace_id": record.trace_id,
                    "security_audit_id": record.security_result.audit_event_id,
                    "obs_span_id": record.obs_span.context.span_id,
                    "decision": record.decision.action,
                    "reason": record.decision.reason,
                    "security_verdict": record.security_result.verdict,
                    "security_score": record.security_result.score,
                    "redacted_fields_count": len(record.security_result.redacted_fields),
                    "policy_rule": record.decision.policy_rule,
                    "action": action,  # PP45: "blocked_request" for block
                }
                audit_events.append(audit_event)
            
            try:
                if hasattr(self.audit_sink, 'write_batch'):
                    await self.audit_sink.write_batch(audit_events)
                else:
                    # Fallback: write one by one
                    for event in audit_events:
                        await self.audit_sink.write_audit_event(event)
            except Exception as e:
                logger.warning("Failed to write audit batch: %s", e)
        
        # Enqueue spans
        if self.span_sink:
            for record in records:
                # Apply decision attributes to span
                record.obs_span.attributes.update(record.decision.span_attributes)
                try:
                    await self.span_sink(record.obs_span)
                except Exception as e:
                    logger.warning("Failed to enqueue span for %s: %s", record.request_id, e)
    
    def _map_audit_action(self, decision_action: str) -> str:
        """Map decision action to audit action."""
        if decision_action == "block_response":
            return "blocked_request"  # PP45: record-then-block semantics
        elif decision_action == "forward_with_flag":
            return "flagged_request"
        else:
            return "forwarded_request"


@dataclass
class Decision:
    """Final decision from the router.
    
    Fields:
        action: "forward" | "flag" | "block" - final decision
        status_code: int - HTTP status code for response
        body: dict - response body for blocked requests
        span_attributes: dict - attributes to add to span
        reason: str - human-readable reason for decision
        policy_rule: str | None - policy rule that triggered decision (if any)
    """
    action: str  # "forward" | "flag" | "block"
    status_code: int
    body: dict[str, Any]
    span_attributes: dict[str, Any]
    reason: str
    policy_rule: str | None = None


class DecisionRouter:
    """Unified decision router for security + obs + policy (PP45).
    
    Implements priority-based decision making with record-then-block semantics.
    Security decisions take highest priority, followed by optional policy rules,
    then observability decisions.
    """

    def __init__(
        self,
        audit_logger: Any = None,
        span_sink: Any = None,
        policy_engine: Any | None = None,
    ):
        """Initialize decision router.
        
        Args:
            audit_logger: Audit trail logger for decision recording
            span_sink: Async function to enqueue spans
            policy_engine: Optional policy engine for tenant/agent restrictions
        """
        self.audit_logger = audit_logger
        self.span_sink = span_sink
        self.policy_engine = policy_engine
        
        # Initialize recorder for single-transaction writes
        self.recorder = DecisionRecorder(
            audit_sink=audit_logger,
            span_sink=span_sink
        )

    async def decide(
        self,
        security_result: SecurityScanResult,
        obs_span: Span,
        request_id: str | None = None,
        trace_id: str | None = None,
        policy_engine: Any | None = None,
    ) -> Decision:
        """Make final decision based on security > policy > obs priority.
        
        Args:
            security_result: Security scanning result
            obs_span: Enriched observability span
            request_id: Optional request_id for correlation
            trace_id: Optional trace_id for correlation
            policy_engine: Optional policy engine for this decision (overrides constructor)
            
        Returns:
            Final decision with response details
        """
        start_time = time.time()
        
        # Use per-call policy_engine if provided, otherwise use constructor's
        effective_policy_engine = policy_engine or self.policy_engine
        
        try:
            # Priority 1: Security decisions
            if security_result.verdict == "block":
                decision = Decision(
                    action="block_response",
                    status_code=403,
                    body={
                        "error": "blocked_by_security",
                        "reason": security_result.security_metadata.get(
                            "block_reason", "Security policy violation"
                        ),
                        "security_score": security_result.score,
                        "redacted_fields": security_result.redacted_fields,
                    },
                    span_attributes={
                        "security.decision": "block",
                        "security.verdict": "block",
                        "security.score": security_result.score,
                        "security.redacted_fields": len(security_result.redacted_fields),
                        "security.audit_event_id": security_result.audit_event_id,
                    },
                    reason=f"Security block: {security_result.verdict}",
                    policy_rule="security_block",
                )
                
            elif security_result.verdict == "flag":
                decision = Decision(
                    action="forward_with_flag",
                    status_code=200,
                    body={},  # Forward normally
                    span_attributes={
                        "security.decision": "flag",
                        "security.verdict": "flag",
                        "security.score": security_result.score,
                        "security.flag": True,
                        "security.audit_event_id": security_result.audit_event_id,
                    },
                    reason=f"Security flag: {security_result.verdict}",
                    policy_rule="security_flag",
                )
                
            else:  # security_result.verdict == "clean"
                # Priority 2: Policy evaluation (optional)
                if effective_policy_engine:
                    policy_decision = await self._evaluate_policy(
                        obs_span, request_id, trace_id, effective_policy_engine
                    )
                    if policy_decision.action != "forward":
                        decision = policy_decision
                        # Record-then-block: even policy violations create audit records
                        await self._record_decision(
                            decision, security_result, obs_span, request_id, trace_id
                        )
                        return decision
                
                # Priority 3: Observability decisions
                decision = Decision(
                    action="forward",
                    status_code=200,
                    body={},  # Forward normally
                    span_attributes={
                        "security.decision": "clean",
                        "security.verdict": "clean",
                        "security.score": security_result.score,
                        "security.audit_event_id": security_result.audit_event_id,
                    },
                    reason="Security clean, forward normally",
                    policy_rule=None,
                )
            
            # Record decision in audit trail (record-then-block semantics)
            await self._record_decision(
                decision, security_result, obs_span, request_id, trace_id
            )
            
            # Record metrics
            latency = time.time() - start_time
            _decision_router_latency_seconds.observe(latency)
            
            # Map reason for metric
            reason = self._map_reason_for_metric(security_result, decision)
            _decisions_total.labels(action=decision.action, reason=reason).inc()
            
            logger.debug(
                "Decision made: %s, reason=%s, latency=%.3fs",
                decision.action, decision.reason, latency
            )
            
            return decision
            
        except Exception as e:
            logger.error("Decision router failed: %s", e)
            # Fallback: allow forward on errors
            fallback_decision = Decision(
                action="forward",
                status_code=200,
                body={},
                span_attributes={"security.decision": "fallback"},
                reason=f"Fallback on error: {e}",
                policy_rule=None,
            )
            await self._record_decision(
                fallback_decision, security_result, obs_span, request_id, trace_id
            )
            return fallback_decision

    def _map_reason_for_metric(
        self, security_result: SecurityScanResult, decision: Decision
    ) -> str:
        """Map decision reason to metric labels: clean|pii|injection|policy."""
        if decision.policy_rule:
            return "policy"
        
        if security_result.verdict == "clean":
            return "clean"
        
        # Check for specific threat types in security metadata
        threat_types = security_result.security_metadata.get("threat_types", [])
        if "injection" in threat_types or "prompt_injection" in threat_types:
            return "injection"
        elif "pii" in threat_types or "personal_identifiable_information" in threat_types:
            return "pii"
        
        # Default: redacted fields suggest PII
        if security_result.redacted_fields:
            return "pii"
        
        return "clean"

    async def start(self):
        """Start the decision recorder."""
        await self.recorder.start()

    async def stop(self):
        """Stop the decision recorder."""
        await self.recorder.stop()

    def create_audit_sink(self, sdk):
        """Create an audit sink from the SDK for the recorder."""
        if sdk is None:
            return None
        
        # Try to get audit trail from SDK
        audit_trail = getattr(sdk, "_audit_trail", None)
        if audit_trail is not None:
            return audit_trail
        
        # Fallback: create a simple sink that writes to hot store
        try:
            hot_store = sdk._resolve_hot_store()
            if hot_store is not None:
                return hot_store
        except Exception:
            pass
        
        return None

    async def _evaluate_policy(
        self,
        obs_span: Span,
        request_id: str | None,
        trace_id: str | None,
        policy_engine: Any,
    ) -> Decision:
        """Evaluate optional policy rules.
        
        Args:
            obs_span: Enriched observability span
            request_id: Optional request_id for correlation
            trace_id: Optional trace_id for correlation
            policy_engine: Policy engine to use for evaluation
            
        Returns:
            Policy decision
        """
        try:
            # Example policy: tenants on free plan can't use gpt-4o
            policy_result = await policy_engine.evaluate(
                obs_span.attributes, obs_span.context
            )
            
            if not policy_result.allowed:
                return Decision(
                    action="block_response",
                    status_code=403,
                    body={
                        "error": "policy_violation",
                        "rule": policy_result.rule,
                        "message": policy_result.message,
                    },
                    span_attributes={
                        "policy.decision": "block",
                        "policy.rule": policy_result.rule,
                        "policy.reason": policy_result.message,
                    },
                    reason=f"Policy violation: {policy_result.rule}",
                    policy_rule=policy_result.rule,
                )
            
            # Default: allow forward
            return Decision(
                action="forward",
                status_code=200,
                body={},
                span_attributes={"policy.decision": "allow"},
                reason="Policy allows forward",
                policy_rule=None,
            )
            
        except Exception as e:
            logger.warning("Policy evaluation failed, allowing forward: %s", e)
            return Decision(
                action="forward",
                status_code=200,
                body={},
                span_attributes={"policy.decision": "eval_error"},
                reason=f"Policy evaluation error: {e}",
                policy_rule=None,
            )

    async def _record_decision(
        self,
        decision: Decision,
        security_result: SecurityScanResult,
        obs_span: Span,
        request_id: str | None,
        trace_id: str | None,
    ) -> None:
        """Record decision in audit trail (single transaction).
        
        Args:
            decision: Final decision
            security_result: Security scanning result
            obs_span: Enriched observability span
            request_id: Optional request_id for correlation
            trace_id: Optional trace_id for correlation
        """
        try:
            # Use recorder for single-transaction batched write
            if request_id and trace_id:
                await self.recorder.record(
                    request_id=request_id,
                    trace_id=trace_id,
                    decision=decision,
                    security_result=security_result,
                    obs_span=obs_span
                )
            
            # Fallback: direct logging if recorder not available
            else:
                audit_event = {
                    "timestamp": time.time(),
                    "request_id": request_id or "",
                    "trace_id": trace_id or "",
                    "security_audit_id": security_result.audit_event_id,
                    "obs_span_id": obs_span.span_id,
                    "decision": decision.action,
                    "reason": decision.reason,
                    "security_verdict": security_result.verdict,
                    "security_score": security_result.score,
                    "redacted_fields_count": len(security_result.redacted_fields),
                    "policy_rule": decision.policy_rule,
                }
                
                if self.audit_logger:
                    try:
                        if hasattr(self.audit_logger, 'write_audit_event'):
                            await self.audit_logger.write_audit_event(audit_event)
                        elif hasattr(self.audit_logger, 'log_security_decision'):
                            await self.audit_logger.log_security_decision(audit_event)
                    except Exception as e:
                        logger.warning("Audit logging failed: %s", e)
                
                # Add security metadata to span
                obs_span.attributes.update(decision.span_attributes)
            
            logger.debug(
                "Decision recorded in audit trail: %s", decision.action
            )
            
        except Exception as e:
            logger.warning("Failed to record decision in audit trail: %s", e)
            # Don't fail the request if audit logging fails