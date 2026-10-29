"""Audit events and contracts for vault recovery and compliance logging.

PC20: Audit trail для каждого vault recovery (§3.4 ARCHITECT.md, retention 1 год).
PC21: Audit trail для истёкших TTL-lease'ов, которые находит cron-cleanup.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone, UTC
from typing import Any

logger = logging.getLogger(__name__)

# Bounded cardinality for Prometheus vault_recovery_total{reason_category}
REASON_CATEGORIES = frozenset(
    {
        "debugging",      # "debug", "отлад", "hallucination"
        "incident",       # "inc-", "incident", "outage", "alert"
        "support",        # "support", "ticket", "user complaint"
        "compliance",     # "audit", "compliance", "gdpr", "152-фз"
        "other",          # anything else
    }
)

# PP03-PP04: bounded set of reasons a sampling rate can change to. Used as the
# ``policy_reason`` label on agent_obs_tail_sampler_current_rate and as the
# ``new_reason``/``prev_reason`` labels on the change counter, so metric
# cardinality never grows with the float value of the rate itself.
SAMPLER_POLICY_REASONS = (
    "cpu_high", "error_high", "default",  # PP0 (PC30)
    "error_budget_low",  # PP27 — error-budget soft action
    "eval_feedback_normal", "eval_feedback_degradation", "eval_feedback_heavy_degradation",  # PP04
    "error_keep", "cost_high_keep", "security_keep", "random",  # PP03
)

# PC30 rule thresholds, quoted into the audit reason so a row is self-contained.
# Kept here (not in the policy engine) because the audit layer must be able to
# render a reason without importing the sampler package.
CPU_HIGH_THRESHOLD = 0.8
ERROR_RATE_THRESHOLD = 0.05

# PP27: soft error-budget threshold — when slo_error_budget_remaining drops
# below this, the sampler cuts the normal-trace rate to save storage for the
# degraded period. Mirrors agent_obs.slo.error_budget.DEFAULT_SOFT_THRESHOLD
# (the sampler process cannot import the SDK-side policy module cheaply, and
# the audit layer must render the reason without it).
ERROR_BUDGET_SOFT_THRESHOLD = 0.2

# PC30: the rate each rule selects for "normal" traces. cpu_high wins over
# error_high — a saturated host is the more immediate threat to the exporter,
# and PC30 lists the cpu check first. error_budget_low (PP27) also wins over
# error_high: while the budget lasts, saving storage beats keeping extra
# normal traces; error traces are kept unconditionally anyway (PC0 rule 1).
SAMPLER_RATES = {
    "cpu_high": 0.05,
    "error_high": 0.30,
    "error_budget_low": 0.05,
    "default": 0.10,
}


def classify_reason_category(reason: str) -> str:
    """Classify recovery reason into bounded set for Prometheus label cardinality.
    
    Returns one of REASON_CATEGORIES.
    """
    reason_lower = reason.lower()
    
    if any(keyword in reason_lower for keyword in ["debug", "отлад", "hallucination"]):
        return "debugging"
    elif any(keyword in reason_lower for keyword in ["inc-", "incident", "outage", "alert"]):
        return "incident"
    elif any(keyword in reason_lower for keyword in ["support", "ticket", "user complaint"]):
        return "support"
    elif any(keyword in reason_lower for keyword in ["audit", "compliance", "gdpr", "152-фз"]):
        return "compliance"
    else:
        return "other"


class AuditReasonRequiredError(Exception):
    """Raised when vault recovery is attempted without a reason."""
    pass


class AuditWriteError(Exception):
    """Raised when writing audit event to ClickHouse fails."""
    pass


@dataclass
class RecoveryAuditEvent:
    """Audit event for vault recovery operations.
    
    Maps to ClickHouse audit_events_hot table DDL:
    - audit_id String
    - timestamp DateTime64(3)
    - trace_id String
    - actor JSON
    - action String
    - decision String
    - resource JSON
    - reason String
    - ip_address String
    - user_agent String
    
    Note: 'created_at' is DEFAULT now() in DDL, not stored here.
    """
    
    audit_id: str
    timestamp: float  # Unix timestamp
    trace_id: str     # Optional, empty if not trace-related
    actor: dict[str, str]
    action: str
    resource: dict[str, str]
    reason: str
    ip_address: str
    user_agent: str
    decision: str = "allow"  # "allow" | "deny" — for vault recovery, always "allow"
    
    @property
    def reason_category(self) -> str:
        """Reason category for Prometheus metrics."""
        return classify_reason_category(self.reason)
    
    @classmethod
    def for_vault_recovery(
        cls,
        vault_key: str,
        login: str,
        reason: str,
        trace_id: str = "",
        ip_address: str = "",
        user_agent: str = "",
    ) -> "RecoveryAuditEvent":
        """Create audit event for vault recovery operation.
        
        Parameters
        ----------
        vault_key:
            Vault secret path (e.g., "pii/5f3a/2026-09-21/abc123")
        login:
            Engineer's login/email (maps to actor.id)
        reason:
            Engineer-provided reason for recovery
        trace_id:
            Optional trace_id for correlation
        ip_address:
            Client IP address (default "unknown")
        user_agent:
            Client user agent (default "cli")
        """
        return cls(
            audit_id=f"rae-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            trace_id=trace_id,
            actor={"type": "engineer", "id": login},
            action="vault.recover",
            resource={"type": "pii_vault_key", "id": vault_key},
            reason=reason,
            ip_address=ip_address or "unknown",
            user_agent=user_agent or "cli",
            decision="allow",
        )
    
    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse row format."""
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=UTC),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class CrossTenantAccessAuditEvent:
    """Audit event for cross-tenant access attempt (PP32 tenant isolation).
    
    Records failed cross-tenant access attempts for security monitoring.
    Severity warning in resource JSON to maintain 10-column DDL contract.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    actor: dict[str, str]  # {"type": "user", "id": login}
    resource: dict[str, Any]  # {"type":"trace","trace_id","caller_tenant","resource_tenant","tier","severity":"warning"}
    reason: str  # "cross_tenant_denied"
    action: str = "tenant.cross_tenant_access"
    decision: str = "deny"  # always deny for cross-tenant
    trace_id: str = ""
    ip_address: str = "unknown"
    user_agent: str = "trace-viewer"

    @classmethod
    def for_attempt(
        cls,
        trace_id: str,
        user: str,
        caller_tenant: str,
        resource_tenant: str,
        tier: str = "unknown",
        ip_address: str = "unknown",
    ) -> "CrossTenantAccessAuditEvent":
        """Create an audit event for cross-tenant access attempt.
        
        Args:
            trace_id: Trace ID being accessed
            user: User login making the request
            caller_tenant: Tenant of the requesting user
            resource_tenant: Tenant of the requested trace
            tier: Storage tier where attempt was detected
            ip_address: Client IP address
        """
        return cls(
            audit_id=f"cta-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            actor={"type": "user", "id": user},
            resource={
                "type": "trace",
                "trace_id": trace_id,
                "caller_tenant": caller_tenant,
                "resource_tenant": resource_tenant,
                "tier": tier,
                "severity": "warning",
            },
            reason="cross_tenant_denied",
            action="tenant.cross_tenant_access",
            decision="deny",
            trace_id=trace_id,
            ip_address=ip_address,
            user_agent="trace-viewer",
        )

    def to_dict(self) -> dict[str, Any]:
        """Flatten the access metadata for the Query API."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "decision": self.decision,
            "resource": self.resource,
            "reason": self.reason,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to a ClickHouse INSERT row matching the audit_events_hot DDL."""
        resource = dict(self.resource)
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=UTC),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )
    
    @property
    def vault_api_path(self) -> str:
        """Get the vault API path for this recovery."""
        return f"secret/data/{self.resource['id']}"
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "decision": self.decision,
            "reason_category": self.reason_category,
        }
    
def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row tuple (positional parameters).
        
        Matches audit_events_hot DDL columns:
        (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
        """
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


class EvalBudgetStateChangeAuditEvent:
    """Audit event for budget state changes (PP15).
    
    Records when budget state transitions occur (ok/soft/paused).
    """
    
    def __init__(
        self,
        audit_id: str,
        timestamp: float,
        trace_id: str,
        actor: dict,
        action: str,
        decision: str,
        resource: dict,
        reason: str,
        ip_address: str,
        user_agent: str,
    ):
        self.audit_id = audit_id
        self.timestamp = timestamp
        self.trace_id = trace_id
        self.actor = actor
        self.action = action
        self.decision = decision
        self.resource = resource
        self.reason = reason
        self.ip_address = ip_address
        self.user_agent = user_agent
    
    @classmethod
    def for_budget_state_change(
        cls,
        old_state: str,
        new_state: str,
        current_cost: float,
        budget: float,
        actor_id: str,
    ) -> "EvalBudgetStateChangeAuditEvent":
        """Factory for budget state change audit events."""
        from agent_obs.util import generate_audit_id
        from datetime import datetime, UTC
        
        return cls(
            audit_id=generate_audit_id(),
            timestamp=datetime.now(UTC).timestamp(),
            trace_id="system",  # System-level event, not trace-specific
            actor={"id": actor_id, "type": "service"},
            action="eval_budget.state_change",
            decision="system",
            resource={
                "type": "budget_state",
                "old_state": old_state,
                "new_state": new_state,
                "current_cost_usd": current_cost,
                "budget_usd": budget,
                "utilization_ratio": current_cost / budget if budget > 0 else 0.0,
            },
            reason=f"Budget state changed from {old_state} to {new_state}",
            ip_address="127.0.0.1",
            user_agent="agent-obs/eval",
        )
    
    def to_dict(self) -> dict:
        """Serialize to dict for ClickHouse insertion."""
        from datetime import UTC
        from agent_obs.util import serialize_audit_event
        
        return serialize_audit_event(
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=UTC),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )
    
    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row tuple (positional parameters).
        
        Matches audit_events_hot DDL columns:
        (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
        """
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )
    
    @property
    def vault_api_path(self) -> str:
        """Vault API path for cross-validation with native audit log.
        
        Example: "secret/data/pii/5f3a/2026-09-21/abc123"
        """
        # Extract vault_key from resource.id
        vault_key = self.resource.get("id", "")
        return f"secret/data/{vault_key}"


@dataclass
class LeaseExpiryAuditEvent:
    """Audit event for a PII vault entry whose TTL has expired (PC21).

    Vault reclaims the secret lease on its own, but the metadata has to be
    recorded before it disappears (§3.4 ARCHITECT.md, retention 1 год).
    Maps to the same ``audit_events_hot`` DDL columns as
    :class:`RecoveryAuditEvent`.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    trace_id: str     # always "" — a lease has no originating trace
    actor: dict[str, str]
    action: str
    resource: dict[str, str]
    reason: str
    ip_address: str
    user_agent: str
    decision: str = "allow"  # expiry is a system action, never denied

    @property
    def reason_category(self) -> str:
        """Reason category for Prometheus metrics."""
        return classify_reason_category(self.reason)

    @classmethod
    def for_expired_lease(
        cls,
        vault_key: str,
        expired_at: float,
        source: str = "kv_metadata",
    ) -> "LeaseExpiryAuditEvent":
        """Create an audit event for an expired vault entry.

        Parameters
        ----------
        vault_key:
            Vault secret path (e.g. ``"pii/5f3a/1758326400"``).
        expired_at:
            Unix timestamp at which the TTL elapsed.
        source:
            How the cron job found the entry: ``"kv_metadata"`` (LIST of
            ``secret/metadata/pii`` + ``created_at + ttl_seconds``) or
            ``"lease"`` (``sys/leases`` list with a past ``expire_time``).
        """
        return cls(
            audit_id=f"lee-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            trace_id="",
            actor={"type": "system", "id": "cron-cleanup"},
            action="vault.lease_expired",
            resource={"type": "pii_vault_key", "id": vault_key},
            reason=f"ttl_expired source={source} expired_at={int(expired_at)}",
            ip_address="unknown",
            user_agent="cron",
            decision="allow",
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "decision": self.decision,
            "reason_category": self.reason_category,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row tuple (positional parameters).

        Matches audit_events_hot DDL columns:
        (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
        """
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class SamplerRateChangeAuditEvent:
    """Audit event for a tail-sampler rate change (PC30).

    The adaptive policy engine raises the sampling rate when the agent's error
    rate climbs (errors are the signal we least want to lose) and drops it when
    the host is CPU-saturated.  Every transition is recorded so a missing trace
    can later be explained by "the sampler was at 5% because CPU was 0.92".

    The ``audit_events_hot`` schema has no dedicated numeric columns for the
    rates, so prev/new rate and reason plus the two driving metrics are packed
    into the ``resource`` JSON blob.  ``to_dict`` flattens them for the Query
    API added in PC31.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    actor: dict[str, str]
    resource: dict[str, str]
    reason: str
    prev_rate: float
    new_rate: float
    prev_reason: str
    new_reason: str
    system_cpu_ratio: float
    agent_error_rate_5m: float
    agent_id: str = "*"  # agent_id from env AGENT_OBS_SAMPLER_AGENT_ID
    triggering_agent_id: str = "*"  # triggering agent from Prometheus (if available)
    trace_id: str = ""  # always "" — a rate change is not trace-scoped
    action: str = "sampler.rate_change"
    ip_address: str = "unknown"
    user_agent: str = "policy-engine"
    decision: str = "applied"  # a change is either applied or not recorded

    @classmethod
    def for_rate_change(
        cls,
        prev_rate: float,
        new_rate: float,
        prev_reason: str,
        new_reason: str,
        system_cpu_ratio: float,
        agent_error_rate_5m: float,
        actor_id: str = "policy-engine",
        agent_id: str = "*",
        triggering_agent_id: str = "*",
        eval_score: float | None = None,
        error_budget_remaining: float | None = None,
    ) -> "SamplerRateChangeAuditEvent":
        """Create an audit event for one sampling-rate transition.

        The human-readable ``reason`` embeds the numbers that caused the
        decision, e.g. ``"policy changed from 0.10 to 0.05, reason=cpu_high
        (system_cpu_ratio=0.92 > 0.80)"`` — PC30 requires the reason to be
        legible in the audit trail without cross-referencing other rows.
        """
        if new_reason == "cpu_high":
            detail = (
                f"system_cpu_ratio={system_cpu_ratio:.2f} > "
                f"{CPU_HIGH_THRESHOLD:.2f}"
            )
        elif new_reason == "error_high":
            detail = (
                f"agent_error_rate_5m={agent_error_rate_5m:.3f} > "
                f"{ERROR_RATE_THRESHOLD:.3f}"
            )
        elif new_reason == "error_budget_low":
            # PP27 soft action: the numeric remaining ratio is optional (the
            # caller may only know the verdict), the threshold is always shown.
            remaining = (
                f"{error_budget_remaining:.4f}"
                if error_budget_remaining is not None
                else "<threshold"
            )
            detail = (
                f"slo_error_budget_remaining={remaining} < "
                f"{ERROR_BUDGET_SOFT_THRESHOLD:.2f}"
            )
        elif new_reason.startswith("eval_feedback_"):
            eval_type = new_reason.split("_", 2)[-1]
            if eval_score is not None:
                detail = f"eval_score={eval_score:.2f}"
            else:
                detail = f"eval_feedback_{eval_type}"
        elif new_reason in ("error_keep", "cost_high_keep", "security_keep", "random"):
            detail = f"policy={new_reason}"
        else:
            detail = (
                f"system_cpu_ratio={system_cpu_ratio:.2f} <= {CPU_HIGH_THRESHOLD:.2f} "
                f"and agent_error_rate_5m={agent_error_rate_5m:.3f} <= "
                f"{ERROR_RATE_THRESHOLD:.3f}"
            )

        return cls(
            audit_id=f"sra-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            actor={"type": "system", "id": actor_id},
            resource={"type": "sampler_policy", "id": "normal-trace", "eval_score": eval_score if eval_score is not None else 0.0},
            reason=(
                f"policy changed from {prev_rate:.2f} to {new_rate:.2f}, "
                f"reason={new_reason} ({detail})"
            ),
            prev_rate=prev_rate,
            new_rate=new_rate,
            prev_reason=prev_reason,
            new_reason=new_reason,
            system_cpu_ratio=system_cpu_ratio,
            agent_error_rate_5m=agent_error_rate_5m,
            agent_id=agent_id,
            triggering_agent_id=triggering_agent_id,
        )

    def to_dict(self) -> dict[str, Any]:
        """Flatten the rate metadata out of ``resource`` for the Query API."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "decision": self.decision,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "prev_rate": self.prev_rate,
            "new_rate": self.new_rate,
            "prev_reason": self.prev_reason,
            "new_reason": self.new_reason,
            "system_cpu_ratio": self.system_cpu_ratio,
            "agent_error_rate_5m": self.agent_error_rate_5m,
            "agent_id": self.agent_id,
            "triggering_agent_id": self.triggering_agent_id,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to a ClickHouse INSERT row matching the audit_events_hot DDL.

        The sampler-specific numbers ride along inside the ``resource`` JSON so
        the DDL stays unchanged and no migration is needed for PC30.
        """
        resource = dict(self.resource)
        resource.update(
            {
                "prev_rate": self.prev_rate,
                "new_rate": self.new_rate,
                "prev_reason": self.prev_reason,
                "new_reason": self.new_reason,
                "system_cpu_ratio": self.system_cpu_ratio,
                "agent_error_rate_5m": self.agent_error_rate_5m,
                "agent_id": self.agent_id,
                "triggering_agent_id": self.triggering_agent_id,
            }
        )
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=UTC),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class CompressedExpandAuditEvent:
    """Audit event for compressed node expand (PP19).

    Records each compressed node expand request for security and compliance.
    Includes RBAC decision, MFA usage, and requested fields for audit trail.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    actor: dict[str, str]  # {"type": "user", "id": login}
    resource: dict[str, Any]  # {"type": "compressed_node", "trace_id", "node_id", "requested_fields", "mfa_used"}
    reason: str  # "ok", "rbac_denied", "mfa_missing", "mfa_invalid", "trace_not_found", "node_not_found"
    action: str = "tree.compressed_expand"
    decision: str = "allow"  # "allow" or "deny"
    trace_id: str = ""
    ip_address: str = "unknown"
    user_agent: str = "trace-viewer-expand-api"

    @classmethod
    def for_expand(
        cls,
        trace_id: str,
        node_id: str,
        user: str,
        decision: str,
        reason: str,
        requested_fields: list[str] | None = None,
        mfa_used: bool = False,
        ip_address: str = "unknown",
    ) -> "CompressedExpandAuditEvent":
        """Create an audit event for compressed node expand request.

        Args:
            trace_id: Trace ID containing the compressed node
            node_id: Span ID of the compressed node being expanded
            user: User login making the request
            decision: "allow" or "deny"
            reason: Reason for decision (see class docstring)
            requested_fields: Optional list of requested fields (for PIA check)
            mfa_used: Whether MFA was used and valid
            ip_address: Client IP address
        """
        return cls(
            audit_id=f"cde-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            actor={"type": "user", "id": user},
            resource={
                "type": "compressed_node",
                "trace_id": trace_id,
                "node_id": node_id,
                "requested_fields": requested_fields or [],
                "mfa_used": mfa_used,
            },
            reason=reason,
            action="tree.compressed_expand",
            decision=decision,
            trace_id=trace_id,
            ip_address=ip_address,
            user_agent="trace-viewer-expand-api",
        )

    def to_dict(self) -> dict[str, Any]:
        """Flatten the expand metadata for the Query API."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "decision": self.decision,
            "resource": self.resource,
            "reason": self.reason,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to a ClickHouse INSERT row matching the audit_events_hot DDL."""
        resource = dict(self.resource)
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=UTC),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class EvalSamplerModeChangeAuditEvent:
    """Audit event for eval sampler mode change (PP12).

    Records when the adaptive eval sampler changes between day/night/incident
    modes, allowing SRE to correlate eval frequency changes with time/incidents.
    Maps to the same ``audit_events_hot`` DDL columns as other audit events.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    trace_id: str     # always "" — a mode change is not trace-scoped
    actor: dict[str, str]
    action: str
    resource: dict[str, str]
    reason: str
    ip_address: str
    user_agent: str
    decision: str = "changed"  # "changed" or "no_change" (if mode didn't actually change)

    @property
    def reason_category(self) -> str:
        """Reason category for Prometheus metrics."""
        return classify_reason_category(self.reason)

    @classmethod
    def for_mode_change(
        cls,
        old_mode: str,
        new_mode: str,
        actor_id: str = "adaptive-eval-sampler",
    ) -> "EvalSamplerModeChangeAuditEvent":
        """Create audit event for eval sampler mode change.

        Parameters
        ----------
        old_mode:
            Previous mode ("day" | "night" | "incident")
        new_mode:
            New mode ("day" | "night" | "incident")
        actor_id:
            System component that detected the mode change
        """
        return cls(
            audit_id=f"esm-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            trace_id="",
            actor={"type": "system", "id": actor_id},
            action="eval_sampler.mode_change",
            resource={
                "type": "eval_sampler_mode",
                "old_mode": old_mode,
                "new_mode": new_mode,
            },
            reason=f"sampler mode changed from {old_mode} to {new_mode}",
            ip_address="unknown",
            user_agent="adaptive-eval-sampler",
            decision="changed",
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "decision": self.decision,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "reason_category": self.reason_category,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row tuple (positional parameters).

        Matches audit_events_hot DDL columns:
        (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
        """
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class EvalIncidentEvalAuditEvent:
    """Audit event for incident eval decision (PP14).
    
    Records when the adaptive eval sampler grants an eval job for a security
    or prompt injection trace, allowing SRE to correlate incident evals
    with security alerts.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    trace_id: str     # The trace being evaluated
    actor: dict[str, str]
    action: str
    resource: dict[str, str]
    reason: str
    ip_address: str
    user_agent: str
    decision: str = "evaluated"  # evaluated or rate_limited

    @property
    def reason_category(self) -> str:
        """Reason category for Prometheus metrics."""
        return classify_reason_category(self.reason)

    @classmethod
    def for_incident_eval(
        cls,
        trace_id: str,
        reason: str,
        reason_label: str,
        mode: str,
        trigger: str,
        actor_id: str = "adaptive-eval-sampler",
    ) -> "EvalIncidentEvalAuditEvent":
        """Create audit event for incident eval decision.

        Parameters
        ----------
        trace_id:
            The trace identifier being evaluated
        reason:
            Original reason from sampler (incident_security, incident_injection, etc.)
        reason_label:
            Metric label version (security, injection, cost, other)
        mode:
            Current eval sampler mode (day/night/incident)
        trigger:
            Which predicate triggered the decision (security.incident or error.type=prompt_injection)
        actor_id:
            System component that made the decision
        """
        return cls(
            audit_id=f"eie-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            trace_id=trace_id,
            actor={"type": "system", "id": actor_id},
            action="eval_sampler.incident_eval",
            resource={
                "type": "incident_eval",
                "reason": reason,
                "reason_label": reason_label,
                "mode": mode,
                "trigger": trigger,
            },
            reason=f"incident eval granted: {reason} in {mode} mode",
            ip_address="unknown",
            user_agent="adaptive-eval-sampler",
            decision="evaluated",
        )

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "decision": self.decision,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
            "reason_category": self.reason_category,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row tuple (positional parameters).

        Matches audit_events_hot DDL columns:
        (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
        """
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class AggregationFlushAuditEvent:
    """Audit event for span aggregation flush (PP11).
    
    Records when the SDK aggregates micro-spans into one span with events,
    tracking the efficiency of the aggregation process.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    trace_id: str     # Optional, empty if not trace-related
    actor: dict[str, str]
    action: str
    resource: dict[str, str]
    reason: str
    ip_address: str
    user_agent: str
    decision: str = "aggregated"  # aggregated or dropped (if buffer full)
    
    @classmethod
    def for_aggregation_flush(
        cls,
        children_count: int,
        bytes_saved: int,
        span_type: str,
        agent_id: str,
        trace_id: str = "",
        ip_address: str = "",
        user_agent: str = "sdk",
    ) -> "AggregationFlushAuditEvent":
        """Create audit event for aggregation flush.
        
        Parameters
        ----------
        children_count:
            Number of micro-spans aggregated
        bytes_saved:
            Total bytes saved by this aggregation
        span_type:
            Type of span (e.g., "agent.reasoning")
        agent_id:
            Agent identifier
        trace_id:
            Optional trace_id for correlation
        ip_address:
            Client IP address (default "unknown")
        user_agent:
            Client user agent (default "sdk")
        """
        return cls(
            audit_id=f"afe-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            trace_id=trace_id,
            actor={"type": "system", "id": "span_aggregator"},
            action="span.aggregate_flush",
            resource={
                "type": "span_aggregation",
                "span_type": span_type,
                "children_count": children_count,
                "bytes_saved": bytes_saved,
                "agent_id": agent_id,
            },
            reason=f"aggregated {children_count} micro-spans, bytes_saved={bytes_saved}",
            ip_address=ip_address or "unknown",
            user_agent=user_agent,
            decision="aggregated",
        )
    
    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "decision": self.decision,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }
    
    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row tuple (positional parameters).
        
        Matches audit_events_hot DDL columns:
        (audit_id, timestamp, trace_id, actor, action, decision, resource, reason, ip_address, user_agent)
        """
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class ErrorBudgetActionAuditEvent:
    """Audit event for an error-budget policy action (PP27, T3.6.3).

    Recorded once per state transition of an SLO's error budget:

    - soft (remaining < 0.2):  ``action="error_budget.state_change"`` —
      cost-saving mode engaged (sampler to 5%, eval budget halved).
    - hard (remaining <= 0.0): ``action="freeze_deploy"`` — deploys are
      frozen by the CI/CD gate and the on-call is paged.
    - recover (back to ok):    ``action="error_budget.state_change"`` —
      normal rates restored.

    The numeric budget detail rides inside ``resource`` so the audit_events_hot
    DDL stays unchanged.
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    actor: dict[str, str]
    resource: dict[str, Any]
    reason: str
    action: str = "error_budget.state_change"
    decision: str = "soft"  # "soft" | "hard" | "recover" — the transition kind
    trace_id: str = ""  # always "" — a budget is not trace-scoped
    ip_address: str = "127.0.0.1"
    user_agent: str = "agent-obs/slo"

    @classmethod
    def for_policy_action(
        cls,
        slo_name: str,
        prev_state: str,
        new_state: str,
        remaining_ratio: float,
        budget_total: float,
        budget_consumed: float,
    ) -> ErrorBudgetActionAuditEvent:
        """Create an audit event for one error-budget state transition.

        The reason embeds the numbers so a row is self-contained without
        cross-referencing other rows — the same rule PC30 applied to sampler
        rate-change reasons.
        """
        decision = new_state if new_state != "ok" else "recover"
        # Freeze deploys is the hard action's defining effect (PP27 spec).
        action = "freeze_deploy" if new_state == "hard" else "error_budget.state_change"
        reason = (
            f"error budget {prev_state} -> {new_state} for SLO {slo_name} "
            f"(remaining={remaining_ratio:.4f}, consumed={budget_consumed:.1f}"
            f"/{budget_total:.1f} events)"
        )
        return cls(
            audit_id=f"eb-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            actor={"type": "system", "id": "error-budget-policy"},
            resource={
                "type": "error_budget",
                "slo": slo_name,
                "prev_state": prev_state,
                "new_state": new_state,
                "remaining_ratio": remaining_ratio,
                "budget_total": budget_total,
                "budget_consumed": budget_consumed,
            },
            reason=reason,
            action=action,
            decision=decision,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON consumers (Query API)."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "decision": self.decision,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row matching the audit_events_hot DDL."""
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=UTC),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )


@dataclass
class CanaryRollbackAuditEvent:
    """Audit event for an SLO-gated canary rollback (PP29, T3.6.5).

    Recorded once per rollback decision of the canary deploy gate:

    - ``decision="rolled_back"``       — canary unhealthy, revert command ran.
    - ``decision="rollback_failed"``   — canary unhealthy, but the revert
      command (or the audit write itself) failed; the on-call is still paged.
    - ``decision="rollback_simulated"``— ``--dry-run``: no command was run.

    The burn-rate numbers that triggered the gate ride inside ``resource`` so
    the audit_events_hot DDL stays unchanged and a row is self-contained
    without cross-referencing (the same rule PC30 applied to sampler rate
    changes and PP27 applied to error-budget transitions).
    """

    audit_id: str
    timestamp: float  # Unix timestamp
    actor: dict[str, str]
    resource: dict[str, Any]
    reason: str
    action: str = "canary_rollback"
    decision: str = "rolled_back"  # "rolled_back" | "rollback_failed" | "rollback_simulated"
    trace_id: str = ""  # always "" — a deploy is not trace-scoped
    ip_address: str = "127.0.0.1"
    user_agent: str = "agent-obs/canary"

    @classmethod
    def for_rollback(
        cls,
        new_version: str,
        old_version: str,
        slo_name: str,
        burn_rates: dict[str, float],
        threshold: float,
        window: str = "30m",
        decision: str = "rolled_back",
        error: str | None = None,
    ) -> CanaryRollbackAuditEvent:
        """Create an audit event for one canary rollback.

        Args:
            new_version: Version that was being canaried.
            old_version: Version traffic is rolled back to.
            slo_name: SLO whose burn rate breached the gate ("" if unknown).
            burn_rates: Per-SLO burn rates measured on ``new_version``.
            threshold: Gate threshold that was exceeded.
            window: Measurement window spec (e.g. "30m").
            decision: "rolled_back" | "rollback_failed" | "rollback_simulated".
            error: Optional failure detail when the revert command failed.
        """
        breached = sorted(
            name for name, value in burn_rates.items() if value >= threshold
        ) or sorted(burn_rates)
        reason = (
            f"canary {new_version} rolled back to {old_version}: "
            f"burn rate on {slo_name or breached[0] if breached else 'unknown SLO'} "
            f"exceeded {threshold} over {window}"
        )
        if error:
            reason = f"{reason} ({error})"
        return cls(
            audit_id=f"cy-{uuid.uuid4().hex[:12]}",
            timestamp=time.time(),
            actor={"type": "system", "id": "canary-gate"},
            resource={
                "type": "canary_deploy",
                "new_version": new_version,
                "old_version": old_version,
                "slo": slo_name,
                "window": window,
                "threshold": threshold,
                "burn_rates": dict(burn_rates),
                "breached_slos": breached,
                **({"error": error} if error else {}),
            },
            reason=reason,
            action="canary_rollback",
            decision=decision,
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for JSON consumers (Query API)."""
        return {
            "audit_id": self.audit_id,
            "timestamp": self.timestamp,
            "trace_id": self.trace_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "reason": self.reason,
            "decision": self.decision,
            "ip_address": self.ip_address,
            "user_agent": self.user_agent,
        }

    def to_clickhouse_row(self) -> tuple:
        """Convert to ClickHouse INSERT row matching the audit_events_hot DDL."""
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=UTC),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(self.resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )