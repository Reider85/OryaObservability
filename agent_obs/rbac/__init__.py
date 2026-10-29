"""PP30 (T3.7.1) + PP31 (T3.7.2): RBAC — role model and OPA policy engine.

Public surface:
    from agent_obs.rbac import RBAC, ROLES, Role, MemoryRoleStore, PostgresRoleStore
    from agent_obs.rbac import PolicyEngine, User, Resource
"""

from agent_obs.rbac.policy_engine import PolicyEngine, Resource, User
from agent_obs.rbac.roles import (
    ALL_PERMISSIONS,
    RBAC,
    ROLES,
    MemoryRoleStore,
    PostgresRoleStore,
    Role,
    RoleStore,
    parse_env_role_mapping,
    validate_roles,
)

__all__ = [
    "ALL_PERMISSIONS",
    "RBAC",
    "ROLES",
    "MemoryRoleStore",
    "PolicyEngine",
    "PostgresRoleStore",
    "Resource",
    "Role",
    "RoleStore",
    "User",
    "parse_env_role_mapping",
    "validate_roles",
]
