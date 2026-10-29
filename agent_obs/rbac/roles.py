"""PP30 (T3.7.1): RBAC role model with 6 roles and discrete permission sets.

Six roles (admin / sre / developer / dpo / auditor / agent), each holding a
``frozenset[str]`` of permissions. The permission assignment is canonical
(PRODUCTION-PROMPTS.md PP30) with one documented deviation: ``compressed:expand``
is added to admin / sre / developer so the PP19 trace-viewer endpoints can be
migrated onto this model in PP31 without a permission gap.

"Discrete" (no-overlap) means: no two roles share an *identical* permission
set — every role is distinguishable by its permissions. Role sets do share
individual permissions (e.g. ``trace:read`` belongs to four roles); that is
intended in the canonical model.

Mapping of users to roles lives in the Postgres ``user_roles`` table
(schema: infra/storage/postgres_warm.sql). ``RBAC.check`` itself is synchronous
and reads from an in-memory cache (``MemoryRoleStore``) so the SDK's
fire-and-forget invariant is never violated; ``PostgresRoleStore.load()``
refreshes that cache asynchronously and is fail-closed (empty cache = deny).

Usage:
    rbac = RBAC(MemoryRoleStore.from_env())          # AGENT_OBS_RBAC_ROLES
    rbac.check("alice", "trace:read")                # True
    rbac.check("bob", "pii:recover")                 # False (developer)

    store = PostgresRoleStore(dsn="postgresql://...")
    await store.load()
    rbac = RBAC(store)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

logger = logging.getLogger(__name__)

# Environment variable with the user -> role mapping, format "alice:admin,bob:developer".
# Shared with the PP19 trace-viewer RBAC (agent_obs/trace_viewer/access_control.py).
RBAC_ROLES_ENV = "AGENT_OBS_RBAC_ROLES"

# Postgres DSN for the user_roles table. Falls back to the warm-tier DSN because
# user_roles lives in the warm_store database.
RBAC_PG_DSN_ENV = "RBAC_PG_DSN"
WARM_PG_DSN_ENV = "WARM_PG_DSN"
DEFAULT_WARM_DSN = "postgresql://warm:warm@localhost:5433/warm_store"


# Every permission known to the model. check() denies anything not listed here
# implicitly (a permission absent from every role can never be granted).
ALL_PERMISSIONS: frozenset[str] = frozenset(
    {
        "trace:read",
        "trace:write",
        "span:write",
        "metrics:write",
        "pii:recover",
        "policy:write",
        "config:write",
        "deploy:gate",
        "audit:read",
        "slo:read",
        "slo:write",
        "compliance:export",
        # PP19 extension (documented deviation from the canonical PP30 list):
        # required by the compressed-expand / drill-down endpoints.
        "compressed:expand",
    }
)

# Canonical role -> permissions mapping (PP30 ticket + compressed:expand for PP19
# endpoints; see module docstring).
ROLES: dict[str, frozenset[str]] = {
    "admin": frozenset(
        {
            "trace:read",
            "pii:recover",
            "policy:write",
            "config:write",
            "deploy:gate",
            "audit:read",
            "slo:write",
            "compressed:expand",
        }
    ),
    "sre": frozenset(
        {
            "trace:read",
            "pii:recover",
            "config:write",
            "deploy:gate",
            "audit:read",
            "slo:read",
            "compressed:expand",
        }
    ),
    "developer": frozenset(
        {
            "trace:read",
            "audit:read",
            "slo:read",
            "compressed:expand",
        }
    ),
    "dpo": frozenset(
        {
            "trace:read",
            "audit:read",
            "compliance:export",
        }
    ),
    "auditor": frozenset(
        {
            "audit:read",
            "compliance:export",
        }
    ),
    "agent": frozenset(
        {
            "trace:write",
            "span:write",
            "metrics:write",
        }
    ),
}

# Roles the SDK itself may act as (write-only telemetry producer). Used by
# callers to sanity-check deployments; not enforced inside check().
AGENT_ROLES: frozenset[str] = frozenset({"agent"})


@dataclass(frozen=True)
class Role:
    """A named, immutable permission set (PP0 contract)."""

    name: str
    permissions: frozenset[str]


def validate_roles(roles: Mapping[str, frozenset[str]] | None = None) -> None:
    """Validate the role model. Raises ValueError on any violation.

    Checks:
      * exactly the six canonical roles are present;
      * every permission set is a non-empty frozenset[str];
      * every permission is a member of ALL_PERMISSIONS;
      * no two roles share an identical permission set (no-overlap);
      * the agent role is write-only (no read/audit/PII permissions).
    """
    roles = ROLES if roles is None else roles

    expected = {"admin", "sre", "developer", "dpo", "auditor", "agent"}
    actual = set(roles)
    if actual != expected:
        raise ValueError(
            f"role model must contain exactly {sorted(expected)}, got {sorted(actual)}"
        )

    seen: dict[frozenset[str], str] = {}
    for name, permissions in roles.items():
        if not isinstance(permissions, frozenset) or not permissions:
            raise ValueError(f"role {name!r}: permissions must be a non-empty frozenset")
        unknown = permissions - ALL_PERMISSIONS
        if unknown:
            raise ValueError(f"role {name!r}: unknown permissions {sorted(unknown)}")
        if permissions in seen:
            raise ValueError(
                f"roles {seen[permissions]!r} and {name!r} share an identical "
                "permission set (no-overlap violated)"
            )
        seen[permissions] = name

    agent_perms = roles["agent"]
    read_side = agent_perms & (
        ALL_PERMISSIONS - {"trace:write", "span:write", "metrics:write"}
    )
    if read_side:
        raise ValueError(
            f"agent role must be write-only, but has {sorted(read_side)}"
        )


# Fail the import loudly if the shipped model is broken — a silently broken role
# model would turn every RBAC check into a coin flip.
validate_roles()


class RoleStore(Protocol):
    """Resolves a user login to a role name. Fail-closed: None = unknown."""

    def get_role(self, user: str) -> str | None: ...


def parse_env_role_mapping(raw: str) -> dict[str, str]:
    """Parse "alice:admin,bob:developer" into {user: role}.

    Unknown roles are kept as-is (the caller's get_role validates them) so a
    misconfigured role name surfaces as a deny, not as a parsing crash.
    """
    mapping: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        login, role = chunk.split(":", 1)
        login, role = login.strip(), role.strip()
        if login and role:
            mapping[login] = role
    return mapping


class MemoryRoleStore:
    """In-memory user -> role mapping. The SDK-side default (no I/O)."""

    def __init__(self, mapping: Mapping[str, str] | None = None) -> None:
        self._mapping: dict[str, str] = dict(mapping or {})

    @classmethod
    def from_env(cls, env_var: str = RBAC_ROLES_ENV) -> MemoryRoleStore:
        """Build from AGENT_OBS_RBAC_ROLES ("alice:admin,bob:developer").

        Unset/empty env yields an empty store — with fail-closed semantics that
        denies every user, matching the PP19 behaviour.
        """
        return cls(parse_env_role_mapping(os.environ.get(env_var, "")))

    def get_role(self, user: str) -> str | None:
        role = self._mapping.get(user)
        if role is None:
            return None
        if role not in ROLES:
            # Configured but unknown role name: fail closed.
            return None
        return role

    def set_user_role(self, user: str, role: str) -> None:
        if role not in ROLES:
            raise ValueError(f"unknown role {role!r}; valid: {sorted(ROLES)}")
        self._mapping[user] = role

    def remove_user(self, user: str) -> None:
        self._mapping.pop(user, None)


class PostgresRoleStore:
    """user_roles table backed store; refreshes a MemoryRoleStore cache.

    ``load()`` is the only async entry point and must be called explicitly
    (background refresh cadence is a deployment concern, not the SDK's).
    ``get_role`` reads purely from the in-memory cache — RBAC.check stays
    synchronous and never touches the event loop.

    Fail-closed: asyncpg/DSN unavailable or any load error leaves the cache
    empty (deny all) and logs a warning; ``has_backend`` reports whether a
    Postgres round-trip is even possible.
    """

    def __init__(
        self,
        dsn: str | None = None,
        cache: MemoryRoleStore | None = None,
    ) -> None:
        self._dsn = dsn or os.environ.get(RBAC_PG_DSN_ENV) or os.environ.get(
            WARM_PG_DSN_ENV
        ) or DEFAULT_WARM_DSN
        self._cache = cache if cache is not None else MemoryRoleStore()
        self._loaded = False

    @property
    def has_backend(self) -> bool:
        """True if asyncpg is importable (the DSN itself is only proven by load)."""
        try:
            import asyncpg  # noqa: F401
        except ImportError:
            return False
        return True

    @property
    def loaded(self) -> bool:
        return self._loaded

    def get_role(self, user: str) -> str | None:
        return self._cache.get_role(user)

    async def load(self) -> bool:
        """Refresh the cache from Postgres. Returns True on success.

        On any failure (asyncpg missing, connection error, bad rows) the cache
        is cleared — stale grants must not survive a failed refresh.
        """
        try:
            import asyncpg
        except ImportError:
            logger.warning(
                "PostgresRoleStore: asyncpg unavailable; RBAC cache stays empty (deny-all)"
            )
            self._cache = MemoryRoleStore()
            self._loaded = False
            return False

        try:
            conn = await asyncpg.connect(self._dsn)
        except Exception as exc:  # noqa: BLE001 - fail-closed: any backend error means deny-all
            logger.warning("PostgresRoleStore: connect failed: %s; deny-all", exc)
            self._cache = MemoryRoleStore()
            self._loaded = False
            return False

        try:
            rows = await conn.fetch("SELECT user_login, role FROM user_roles")
        except Exception as exc:  # noqa: BLE001 - fail-closed: any backend error means deny-all
            logger.warning("PostgresRoleStore: load failed: %s; deny-all", exc)
            self._cache = MemoryRoleStore()
            self._loaded = False
            return False
        finally:
            await conn.close()

        mapping = {row["user_login"]: row["role"] for row in rows}
        bad_roles = sorted(set(mapping.values()) - set(ROLES))
        if bad_roles:
            # Rows with unknown roles are dropped; valid rows still apply.
            logger.warning(
                "PostgresRoleStore: dropping rows with unknown roles %s", bad_roles
            )
            mapping = {u: r for u, r in mapping.items() if r in ROLES}

        self._cache = MemoryRoleStore(mapping)
        self._loaded = True
        logger.info("PostgresRoleStore: loaded %d user_roles rows", len(mapping))
        return True


@dataclass
class RBAC:
    """Role-based access control (PP0 contract: RBAC.check(user, action, resource)).

    ``resource`` is accepted for contract compatibility; resource-scoped rules
    (tenant isolation, MFA gating) arrive with the PP31 policy engine and
    PP32 tenant isolation — in PP30 every decision is role -> permission only.
    """

    store: RoleStore = field(default_factory=lambda: MemoryRoleStore.from_env())

    # PP0 contract nests the Role dataclass under RBAC; expose the module-level
    # one under the same name so both import paths work.
    Role = Role

    def check(
        self, user: str, action: str, resource: str | None = None
    ) -> bool:
        """True if ``user``'s role grants ``action``. Fail-closed on everything."""
        if not user or not action:
            return False
        role = self.store.get_role(user)
        if role is None:
            return False
        permissions = ROLES.get(role)
        if permissions is None:
            return False
        return action in permissions

    def permissions_for(self, user: str) -> frozenset[str]:
        """Effective permissions of ``user`` (empty frozenset when unknown)."""
        role = self.store.get_role(user)
        if role is None:
            return frozenset()
        return ROLES.get(role, frozenset())

    def role_for(self, user: str) -> str | None:
        """Role name of ``user`` or None when unknown (fail-closed)."""
        return self.store.get_role(user)


def default_rbac() -> RBAC:
    """RBAC instance wired to the AGENT_OBS_RBAC_ROLES env mapping."""
    return RBAC(MemoryRoleStore.from_env())
