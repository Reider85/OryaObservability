"""PP56: Audit logging for OPA bundle reloads.

BundleReloadEvent tracks all bundle reload operations with old vs new policy
diffs for compliance and debugging purposes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from agent_obs.storage.hot import HotStore
from prometheus_client import Counter, Histogram

logger = logging.getLogger(__name__)

# Module-level metrics to avoid duplicate registration
_bundle_reload_total = Counter("policy_bundle_reload_total", "Bundle reload events")
_bundle_reload_latency_seconds = Histogram("policy_bundle_reload_latency_seconds", "Bundle reload latency")


@dataclass
class BundleReloadEvent:
    """Audit event for OPA bundle reload operations.
    
    Fields:
        audit_id: str - unique audit event ID
        timestamp: float - event timestamp
        old_version: str - previous bundle version
        new_version: str - new bundle version
        old_policies: dict - previous policy content snapshot
        new_policies: dict - new policy content snapshot
        policy_diff: dict - diff between old and new policies
        trigger: str - what triggered the reload ("file_change" | "manual" | "poll")
        triggered_by: str - who/what triggered the reload
        success: bool - whether reload was successful
        error_message: str - error message if reload failed
    """
    
    audit_id: str
    timestamp: float
    old_version: str
    new_version: str
    old_policies: dict
    new_policies: dict
    policy_diff: dict
    trigger: str
    triggered_by: str
    success: bool
    error_message: str | None = None


class BundleReloadLogger:
    """Logs bundle reload events to audit trail for PP56 compliance."""
    
    def __init__(self, hot_store: HotStore | None = None):
        self._hot_store = hot_store
        self._metrics = {
            "total": _bundle_reload_total,
            "latency": _bundle_reload_latency_seconds,
        }
    
    async def log_reload(
        self,
        old_version: str,
        new_version: str,
        old_policies: dict,
        new_policies: dict,
        trigger: str = "file_change",
        triggered_by: str = "system",
        error_message: str | None = None,
    ) -> str:
        """Log a bundle reload event.
        
        Args:
            old_version: Previous bundle version
            new_version: New bundle version
            old_policies: Previous policy content snapshot
            new_policies: New policy content snapshot
            trigger: What triggered the reload
            triggered_by: Who/what triggered the reload
            error_message: Error message if reload failed
            
        Returns:
            audit_id of the logged event
        """
        start_time = time.time()
        
        # Generate unique audit ID
        audit_data = f"{old_version}:{new_version}:{trigger}:{int(start_time)}"
        audit_id = hashlib.sha256(audit_data.encode()).hexdigest()
        
        # Calculate policy diff
        policy_diff = self._calculate_policy_diff(old_policies, new_policies)
        
        # Create audit event
        event = BundleReloadEvent(
            audit_id=audit_id,
            timestamp=start_time,
            old_version=old_version,
            new_version=new_version,
            old_policies=old_policies,
            new_policies=new_policies,
            policy_diff=policy_diff,
            trigger=trigger,
            triggered_by=triggered_by,
            success=error_message is None,
            error_message=error_message,
        )
        
        # Log to audit trail
        await self._write_audit_event(event)
        
        # Update metrics
        latency = time.time() - start_time
        self._metrics["total"].inc()
        self._metrics["latency"].observe(latency)
        
        logger.info(
            "Bundle reload logged: %s -> %s (trigger: %s, success: %s)",
            old_version[:8] if old_version else "none",
            new_version[:8] if new_version else "none",
            trigger,
            event.success,
        )
        
        return audit_id
    
    def _calculate_policy_diff(self, old_policies: dict, new_policies: dict) -> dict:
        """Calculate the difference between old and new policies."""
        diff = {
            "added": [],
            "removed": [],
            "modified": [],
        }
        
        old_keys = set(old_policies.keys())
        new_keys = set(new_policies.keys())
        
        # Added policies
        for key in new_keys - old_keys:
            diff["added"].append({
                "key": key,
                "value": new_policies[key],
            })
        
        # Removed policies
        for key in old_keys - new_keys:
            diff["removed"].append({
                "key": key,
                "value": old_policies[key],
            })
        
        # Modified policies
        for key in old_keys & new_keys:
            if old_policies[key] != new_policies[key]:
                diff["modified"].append({
                    "key": key,
                    "old_value": old_policies[key],
                    "new_value": new_policies[key],
                })
        
        return diff
    
    async def _write_audit_event(self, event: BundleReloadEvent) -> None:
        """Write audit event to ClickHouse."""
        if self._hot_store is None:
            logger.debug("No hot store configured; skipping audit write")
            return
        
        try:
            # Convert to ClickHouse row format
            row = {
                "audit_id": event.audit_id,
                "timestamp": event.timestamp,
                "action": "policy_reload",
                "resource_type": "opa_bundle",
                "resource_id": "observability",
                "old_version": event.old_version,
                "new_version": event.new_version,
                "policy_diff": json.dumps(event.policy_diff),
                "trigger": event.trigger,
                "triggered_by": event.triggered_by,
                "success": event.success,
                "error_message": event.error_message or "",
                "severity": "info" if event.success else "warning",
            }
            
            # Write to audit_events_hot table
            await self._hot_store.write_audit_events([row])
            
        except Exception as exc:
            logger.warning("Failed to write bundle reload audit event: %s", exc)
            # Don't fail the reload operation due to audit logging issues


# Global instance for convenience
_bundle_logger: BundleReloadLogger | None = None


def get_bundle_logger(hot_store: HotStore | None = None) -> BundleReloadLogger:
    """Get the global bundle reload logger instance."""
    global _bundle_logger
    if _bundle_logger is None:
        _bundle_logger = BundleReloadLogger(hot_store)
    return _bundle_logger


async def log_bundle_reload(
    old_version: str,
    new_version: str,
    old_policies: dict,
    new_policies: dict,
    trigger: str = "file_change",
    triggered_by: str = "system",
    error_message: str | None = None,
) -> str:
    """Convenience function to log a bundle reload event."""
    logger = get_bundle_logger()
    return await logger.log_reload(
        old_version,
        new_version,
        old_policies,
        new_policies,
        trigger,
        triggered_by,
        error_message,
    )