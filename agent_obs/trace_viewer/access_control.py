"""
Minimal RBAC and MFA access control for PP19 compressed-node expand.

This module provides placeholder RBAC and MFA validation until PP30 Wave 7
when agent_obs/rbac/ will be implemented. Do not extend this module beyond
PP19 scope.

Usage:
    from agent_obs.trace_viewer.access_control import check_permission, verify_mfa_header

    # Check if user has permission
    if not check_permission(user, "compressed:expand"):
        raise HTTPException(status_code=403, detail="Permission denied")

    # Check MFA header if requesting PII fields
    if fields_intersect_pii(requested_fields):
        if not verify_mfa_header(user, mfa_token):
            raise HTTPException(status_code=403, detail="MFA required")
"""

import os
from typing import Set, Optional, List
from agent_obs.guardrail.vault_client import VaultClient, MFARequiredError


# PII field names that require MFA when requested via `fields` query param
PII_FIELD_NAMES: Set[str] = {
    "llm.input_text",
    "llm.output_text", 
    "tool.input",
    "tool.output",
    "input_text",
    "output_text",
    "input",
    "output",
}

# Role -> permissions mapping (PP19 minimal set)
# PP30 will replace this with agent_obs.rbac.policy_engine
ROLE_PERMISSIONS = {
    "admin": {
        "trace:read",
        "compressed:expand", 
        "pii:recover",
        "audit:read",
        "policy:write",
        "*",
    },
    "sre": {
        "trace:read",
        "compressed:expand",
        "audit:read",
    },
    "developer": {
        "trace:read",
        "compressed:expand",
    },
    "auditor": {
        "trace:read",
        "audit:read",
    },
    "dpo": {
        "trace:read", 
        "pii:recover",
    },
}


def resolve_role(user: str) -> Optional[str]:
    """Resolve user role from environment.
    
    Args:
        user: User login name (from X-OryaObs-User header)
        
    Returns:
        Role string or None if unknown user (fail-closed)
    """
    # Environment mapping: "alice:admin,bob:developer,charlie:auditor"
    env_mapping = os.environ.get("AGENT_OBS_RBAC_ROLES", "")
    print(f"DEBUG: RBAC_ROLES env var: {env_mapping}")
    
    if not env_mapping:
        # No mapping configured -> all users unknown (fail-closed)
        return None
    
    # Parse mapping
    for mapping in env_mapping.split(","):
        mapping = mapping.strip()
        print(f"DEBUG: Checking mapping: {mapping}")
        if ":" in mapping:
            login, role = mapping.split(":", 1)
            print(f"DEBUG: Comparing '{login.strip()}' with '{user}'")
            if login.strip() == user:
                return role.strip()
    
    # User not found in mapping
    return None


def check_permission(user: str, action: str) -> bool:
    """Check if user has permission for action.
    
    Args:
        user: User login name
        action: Action string (e.g., "trace:read", "compressed:expand")
        
    Returns:
        True if permission granted, False otherwise
    """
    role = resolve_role(user)
    if not role:
        return False
    
    # Check if role has permission
    permissions = ROLE_PERMISSIONS.get(role, set())
    return action in permissions or "*" in permissions


def fields_intersect_pii(requested_fields: List[str]) -> bool:
    """Check if requested fields include any PII fields.
    
    Args:
        requested_fields: List of field names from query param
        
    Returns:
        True if any field is in PII_FIELD_NAMES
    """
    if not requested_fields:
        return False
    
    return any(field in PII_FIELD_NAMES for field in requested_fields)


async def verify_mfa_header(user: str, mfa_token: str) -> bool:
    """Verify MFA token for user.
    
    Args:
        user: User login name (for audit, not permission check)
        mfa_token: MFA token from X-OryaObs-MFA header
        
    Returns:
        True if MFA verification successful
        
    Raises:
        HTTPException: 403 if MFA required but invalid/missing
        (This function returns False instead for caller to handle)
    """
    # If no MFA token provided, fail
    if not mfa_token:
        return False
    
    # Try to verify MFA (fail-closed on errors)
    try:
        # Create VaultClient with MFA config from environment
        vault_client = VaultClient()
        
        # Verify the token
        if vault_client.mfa_config:
            # MFA configured - verify the token
            is_valid = await vault_client.verify_mfa(user, mfa_token)
            return is_valid
        else:
            # MFA not configured - treat as valid (fail-open for dev)
            # In production, AGENT_OBS_MFA_SECRETS should be set
            return True
            
    except (MFARequiredError, Exception):
        # Any verification error (invalid token, rate limit, vault down) -> fail
        return False