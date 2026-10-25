"""Utility functions for the agent_obs package."""

import json
import uuid
from datetime import datetime
from typing import Any


def generate_audit_id() -> str:
    """Generate a unique audit ID using UUID4.
    
    Returns a 12-character hex string from a UUID4 random value.
    This provides a good balance between uniqueness and brevity for audit IDs.
    """
    return uuid.uuid4().hex[:12]


def serialize_audit_event(
    audit_id: str,
    timestamp: datetime,
    trace_id: str,
    actor: str,
    action: str,
    decision: str,
    resource: str,
    reason: str,
    ip_address: str,
    user_agent: str,
) -> dict[str, Any]:
    """Serialize audit event to dictionary for ClickHouse insertion.
    
    Args:
        audit_id: Unique audit event identifier
        timestamp: Event timestamp
        trace_id: Trace identifier (empty for system events)
        actor: Actor information as JSON string
        action: Action performed
        decision: Decision made
        resource: Resource information as JSON string
        reason: Reason for the action
        ip_address: Client IP address
        user_agent: Client user agent
        
    Returns:
        Dictionary representation of the audit event
    """
    return {
        "audit_id": audit_id,
        "timestamp": timestamp,
        "trace_id": trace_id,
        "actor": json.loads(actor),
        "action": action,
        "decision": decision,
        "resource": json.loads(resource),
        "reason": reason,
        "ip_address": ip_address,
        "user_agent": user_agent,
    }