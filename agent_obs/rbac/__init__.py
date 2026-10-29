"""PP30 (T3.7.1): RBAC role model — admin / sre / developer / dpo / auditor / agent.

Public surface:
    from agent_obs.rbac import RBAC, ROLES, Role, MemoryRoleStore, PostgresRoleStore
"""

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
    "PostgresRoleStore",
    "Role",
    "RoleStore",
    "parse_env_role_mapping",
    "validate_roles",
]
