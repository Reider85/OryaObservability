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
    """Unified decision router for security + obs + policy (PP44).
    
    Implements priority-based decision making with record-then-block semantics.
    Security decisions take highest priority, followed by optional policy rules,
    then observability decisions.
    """

    def __init__(
        self,
        audit_logger: Any = None,
        metrics: None = None,
        policy_engine: Any | None = None,
    ):
        """Initialize decision router.
        
        Args:
            audit_logger: Audit trail logger for decision recording
            metrics: Optional metrics client for monitoring
            policy_engine: Optional policy engine for tenant/agent restrictions
        """
        self.audit_logger = audit_logger
        self.metrics = metrics or {
            "counter": Counter("decision_router_total", "Decisions made"),
            "histogram": Histogram("decision_router_latency_seconds", "Decision latency"),
        }
        # Use unique metric names to avoid conflicts
        if not hasattr(self, '_metrics_initialized'):
            self._metrics_initialized = True
        self.policy_engine = policy_engine

    async def decide(
        self,
        security_result: SecurityScanResult,
        obs_span: Span,
        request_id: str | None = None,
        trace_id: str | None = None,
    ) -> Decision:
        """Make final decision based on security > policy > obs priority.
        
        Args:
            security_result: Security scanning result
            obs_span: Enriched observability span
            request_id: Optional request_id for correlation
            trace_id: Optional trace_id for correlation
            
        Returns:
            Final decision with response details
        """
        start_time = time.time()
        
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
                if self.policy_engine:
                    policy_decision = await self._evaluate_policy(
                        obs_span, request_id, trace_id
                    )
                    if policy_decision.action != "forward":
                        decision = policy_decision
                        # Skip obs priority if policy blocks
                        await self._record_decision(
                            decision, security_result, obs_span, request_id, trace_id
                        )
                        self.metrics["counter"].inc()
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
            
            # Record decision in audit trail
            await self._record_decision(
                decision, security_result, obs_span, request_id, trace_id
            )
            
            # Record metrics
            latency = time.time() - start_time
            self.metrics["histogram"].observe(latency)
            self.metrics["counter"].inc()
            
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

    async def _evaluate_policy(
        self,
        obs_span: Span,
        request_id: str | None,
        trace_id: str | None,
    ) -> Decision:
        """Evaluate optional policy rules.
        
        Args:
            obs_span: Enriched observability span
            request_id: Optional request_id for correlation
            trace_id: Optional trace_id for correlation
            
        Returns:
            Policy decision
        """
        try:
            # Example policy: tenants on free plan can't use gpt-4o
            if self.policy_engine:
                policy_result = await self.policy_engine.evaluate(
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
            # Create audit event
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
            
            # Log in audit trail (fire-and-forget)
            if self.audit_logger:
                try:
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