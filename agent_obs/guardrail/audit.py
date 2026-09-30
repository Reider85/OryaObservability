"""Audit events and contracts for vault recovery and compliance logging.

PC20: Audit trail для каждого vault recovery (§3.4 ARCHITECT.md, retention 1 год).
PC21: Audit trail для истёкших TTL-lease'ов, которые находит cron-cleanup.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

# PC30: bounded set of reasons a sampling rate can change to. Used as the
# ``policy_reason`` label on agent_obs_tail_sampler_current_rate and as the
# ``new_reason``/``prev_reason`` labels on the change counter, so metric
# cardinality never grows with the float value of the rate itself.
SAMPLER_POLICY_REASONS = ("cpu_high", "error_high", "default")

# PC30 rule thresholds, quoted into the audit reason so a row is self-contained.
# Kept here (not in the policy engine) because the audit layer must be able to
# render a reason without importing the sampler package.
CPU_HIGH_THRESHOLD = 0.8
ERROR_RATE_THRESHOLD = 0.05

# PC30: the rate each rule selects for "normal" traces. cpu_high wins over
# error_high — a saturated host is the more immediate threat to the exporter,
# and PC30 lists the cpu check first.
SAMPLER_RATES = {
    "cpu_high": 0.05,
    "error_high": 0.30,
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
            resource={"type": "sampler_policy", "id": "normal-trace"},
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
            }
        )
        return (
            self.audit_id,
            datetime.fromtimestamp(self.timestamp, tz=timezone.utc),
            self.trace_id,
            json.dumps(self.actor),
            self.action,
            self.decision,
            json.dumps(resource),
            self.reason,
            self.ip_address,
            self.user_agent,
        )