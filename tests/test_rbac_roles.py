"""PP30 (T3.7.1): tests for the RBAC role model — agent_obs/rbac/roles.py.

Coverage:
  * role model structure (6 roles, frozenset, only known permissions);
  * no-overlap: no two roles share an identical permission set;
  * RBAC.check allow/deny for every role x permission combination (~84 cases);
  * fail-closed behaviour (unknown user/role, empty env, bad args);
  * AGENT_OBS_RBAC_ROLES env parsing (PP19-compatible format);
  * PostgresRoleStore with a mocked asyncpg (convention: unittest.mock).
"""

from __future__ import annotations

import sys
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_obs.rbac import roles as roles_mod
from agent_obs.rbac.roles import (
    ALL_PERMISSIONS,
    RBAC,
    ROLES,
    MemoryRoleStore,
    PostgresRoleStore,
    Role,
    parse_env_role_mapping,
    validate_roles,
)

CANONICAL_ROLES = {"admin", "sre", "developer", "dpo", "auditor", "agent"}


# ---------------------------------------------------------------------------
# Role model structure
# ---------------------------------------------------------------------------


class TestRoleModelStructure:
    def test_exactly_six_roles(self):
        assert set(ROLES) == CANONICAL_ROLES
        assert len(ROLES) == 6

    def test_permission_sets_are_frozensets(self):
        for name, perms in ROLES.items():
            assert isinstance(perms, frozenset), name

    def test_no_empty_permission_sets(self):
        for name, perms in ROLES.items():
            assert perms, f"role {name} has no permissions"

    @pytest.mark.parametrize("role", sorted(CANONICAL_ROLES))
    def test_permissions_are_all_known(self, role):
        unknown = ROLES[role] - ALL_PERMISSIONS
        assert not unknown, f"role {role} uses unknown permissions {sorted(unknown)}"

    @pytest.mark.parametrize("role", sorted(CANONICAL_ROLES))
    def test_compressed_expand_in_pp19_roles(self, role):
        # Documented deviation from the canonical PP30 list: PP19 endpoints
        # (compressed expand / drill-down) require compressed:expand for the
        # roles that had it in the PP19 mapping.
        if role in ("admin", "sre", "developer"):
            assert "compressed:expand" in ROLES[role]
        else:
            assert "compressed:expand" not in ROLES[role]

    def test_agent_role_is_write_only(self):
        read_side = ROLES["agent"] & (
            ALL_PERMISSIONS - {"trace:write", "span:write", "metrics:write"}
        )
        assert not read_side

    def test_role_dataclass_contract(self):
        r = Role(name="admin", permissions=ROLES["admin"])
        assert r.name == "admin"
        assert r.permissions == ROLES["admin"]
        with pytest.raises(FrozenInstanceError):
            r.permissions = frozenset()  # frozen dataclass

    def test_rbac_exposes_role_under_class(self):
        # PP0 contract nests Role under RBAC.
        assert RBAC.Role is Role


# ---------------------------------------------------------------------------
# No-overlap + validate_roles
# ---------------------------------------------------------------------------


class TestNoOverlap:
    def test_no_two_roles_share_identical_set(self):
        seen: dict[frozenset, str] = {}
        for name, perms in ROLES.items():
            assert perms not in seen, f"{name} duplicates {seen.get(perms)}"
            seen[perms] = name

    def test_validate_roles_passes_for_canonical_model(self):
        validate_roles()  # must not raise

    def test_validate_roles_rejects_missing_role(self):
        broken = {k: v for k, v in ROLES.items() if k != "dpo"}
        with pytest.raises(ValueError, match="exactly"):
            validate_roles(broken)

    def test_validate_roles_rejects_duplicate_sets(self):
        broken = dict(ROLES)
        broken["auditor"] = broken["dpo"]
        with pytest.raises(ValueError, match="identical"):
            validate_roles(broken)

    def test_validate_roles_rejects_unknown_permission(self):
        broken = dict(ROLES)
        broken["auditor"] = frozenset({"audit:read", "compliance:export", "nope:x"})
        with pytest.raises(ValueError, match="unknown permissions"):
            validate_roles(broken)

    def test_validate_roles_rejects_empty_set(self):
        broken = dict(ROLES)
        broken["auditor"] = frozenset()
        with pytest.raises(ValueError, match="non-empty"):
            validate_roles(broken)

    def test_validate_roles_rejects_agent_with_read(self):
        broken = dict(ROLES)
        broken["agent"] = frozenset({"trace:write", "span:write", "metrics:write", "trace:read"})
        with pytest.raises(ValueError, match="write-only"):
            validate_roles(broken)


# ---------------------------------------------------------------------------
# RBAC.check: allow (role x every permission of that role)
# ---------------------------------------------------------------------------


def _rbac_for(role: str) -> RBAC:
    return RBAC(MemoryRoleStore({"tester": role}))


class TestCheckAllow:
    @pytest.mark.parametrize(
        "role,permission",
        [(role, perm) for role in sorted(CANONICAL_ROLES) for perm in sorted(ROLES[role])],
    )
    def test_role_grants_its_own_permissions(self, role, permission):
        assert _rbac_for(role).check("tester", permission) is True


# ---------------------------------------------------------------------------
# RBAC.check: deny (role x every permission it does NOT hold)
# ---------------------------------------------------------------------------


class TestCheckDeny:
    @pytest.mark.parametrize(
        "role,permission",
        [
            (role, perm)
            for role in sorted(CANONICAL_ROLES)
            for perm in sorted(ALL_PERMISSIONS - ROLES[role])
        ],
    )
    def test_role_denies_foreign_permissions(self, role, permission):
        assert _rbac_for(role).check("tester", permission) is False


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


class TestFailClosed:
    def test_unknown_user_denied(self):
        rbac = RBAC(MemoryRoleStore({"alice": "admin"}))
        assert rbac.check("mallory", "trace:read") is False

    def test_empty_store_denies_everything(self):
        rbac = RBAC(MemoryRoleStore())
        for perm in ALL_PERMISSIONS:
            assert rbac.check("alice", perm) is False

    def test_configured_unknown_role_denied(self):
        # A role name that is not in ROLES must deny, not crash.
        store = MemoryRoleStore({"eve": "superuser"})
        assert store.get_role("eve") is None
        assert RBAC(store).check("eve", "trace:read") is False

    def test_empty_user_or_action_denied(self):
        rbac = _rbac_for("admin")
        assert rbac.check("", "trace:read") is False
        assert rbac.check("tester", "") is False

    def test_resource_argument_is_accepted(self):
        # PP0 contract: check(user, action, resource). PP30 ignores resource.
        rbac = _rbac_for("admin")
        assert rbac.check("tester", "trace:read", resource="trace:123") is True
        assert rbac.check("tester", "pii:recover", "trace:123") is True


# ---------------------------------------------------------------------------
# Helper methods
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_permissions_for(self):
        assert _rbac_for("dpo").permissions_for("tester") == ROLES["dpo"]

    def test_permissions_for_unknown_user_is_empty(self):
        assert _rbac_for("admin").permissions_for("nobody") == frozenset()

    def test_role_for(self):
        assert _rbac_for("sre").role_for("tester") == "sre"
        assert _rbac_for("sre").role_for("nobody") is None


# ---------------------------------------------------------------------------
# MemoryRoleStore + env parsing (PP19 format)
# ---------------------------------------------------------------------------


class TestMemoryRoleStore:
    def test_parse_env_format(self):
        assert parse_env_role_mapping("alice:admin,bob:developer") == {
            "alice": "admin",
            "bob": "developer",
        }

    def test_parse_env_tolerates_whitespace_and_bad_chunks(self):
        raw = " alice : admin , broken , bob:developer , : , charlie:auditor "
        assert parse_env_role_mapping(raw) == {
            "alice": "admin",
            "bob": "developer",
            "charlie": "auditor",
        }

    def test_parse_env_empty(self):
        assert parse_env_role_mapping("") == {}

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin,bob:developer")
        store = MemoryRoleStore.from_env()
        assert store.get_role("alice") == "admin"
        assert store.get_role("bob") == "developer"
        assert store.get_role("charlie") is None

    def test_from_env_unset_is_deny_all(self, monkeypatch):
        monkeypatch.delenv("AGENT_OBS_RBAC_ROLES", raising=False)
        store = MemoryRoleStore.from_env()
        assert store.get_role("alice") is None
        assert not RBAC(store).check("alice", "trace:read")

    def test_set_user_role_and_remove(self):
        store = MemoryRoleStore()
        store.set_user_role("alice", "admin")
        assert store.get_role("alice") == "admin"
        store.remove_user("alice")
        assert store.get_role("alice") is None

    def test_set_user_role_rejects_unknown_role(self):
        store = MemoryRoleStore()
        with pytest.raises(ValueError, match="unknown role"):
            store.set_user_role("alice", "root")

    def test_default_rbac_uses_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        assert roles_mod.default_rbac().check("alice", "policy:write") is True


# ---------------------------------------------------------------------------
# PostgresRoleStore (mocked asyncpg)
# ---------------------------------------------------------------------------


def _mock_asyncpg(rows=None, connect_error=None, fetch_error=None):
    """Build a MagicMock asyncpg module: connect() -> conn with fetch()."""
    conn = MagicMock()
    if fetch_error is not None:
        conn.fetch = AsyncMock(side_effect=fetch_error)
    else:
        conn.fetch = AsyncMock(return_value=rows or [])
    conn.close = AsyncMock()

    module = MagicMock()
    if connect_error is not None:
        module.connect = AsyncMock(side_effect=connect_error)
    else:
        module.connect = AsyncMock(return_value=conn)
    return module, conn


class TestPostgresRoleStore:
    async def test_load_success(self):
        rows = [
            {"user_login": "alice", "role": "admin"},
            {"user_login": "bob", "role": "developer"},
        ]
        module, conn = _mock_asyncpg(rows=rows)
        store = PostgresRoleStore(dsn="postgresql://x/y")
        with patch.dict(sys.modules, {"asyncpg": module}):
            assert await store.load() is True
        assert store.loaded is True
        assert store.get_role("alice") == "admin"
        assert store.get_role("bob") == "developer"
        assert store.get_role("carol") is None
        conn.close.assert_awaited()

    async def test_load_without_asyncpg_is_deny_all(self):
        store = PostgresRoleStore(dsn="postgresql://x/y")
        store._cache.set_user_role("alice", "admin")  # stale grant before refresh
        with patch.dict(sys.modules, {"asyncpg": None}):
            assert await store.load() is False
        assert store.loaded is False
        assert store.get_role("alice") is None  # stale grants cleared

    async def test_connect_error_is_deny_all(self):
        module, _ = _mock_asyncpg(connect_error=RuntimeError("refused"))
        store = PostgresRoleStore(dsn="postgresql://x/y")
        with patch.dict(sys.modules, {"asyncpg": module}):
            assert await store.load() is False
        assert store.get_role("alice") is None

    async def test_fetch_error_is_deny_all(self):
        module, _ = _mock_asyncpg(fetch_error=RuntimeError("table missing"))
        store = PostgresRoleStore(dsn="postgresql://x/y")
        with patch.dict(sys.modules, {"asyncpg": module}):
            assert await store.load() is False
        assert store.get_role("alice") is None

    async def test_load_drops_unknown_roles_keeps_valid(self):
        rows = [
            {"user_login": "alice", "role": "admin"},
            {"user_login": "eve", "role": "superuser"},
        ]
        module, _ = _mock_asyncpg(rows=rows)
        store = PostgresRoleStore(dsn="postgresql://x/y")
        with patch.dict(sys.modules, {"asyncpg": module}):
            assert await store.load() is True
        assert store.get_role("alice") == "admin"
        assert store.get_role("eve") is None  # unknown role dropped, fail-closed

    async def test_get_role_before_load_is_deny_all(self):
        store = PostgresRoleStore(dsn="postgresql://x/y")
        assert store.loaded is False
        assert store.get_role("alice") is None

    def test_has_backend_reflects_asyncpg_availability(self):
        store = PostgresRoleStore(dsn="postgresql://x/y")
        with patch.dict(sys.modules, {"asyncpg": MagicMock()}):
            assert store.has_backend is True
        with patch.dict(sys.modules, {"asyncpg": None}):
            assert store.has_backend is False

    def test_dsn_env_fallback(self, monkeypatch):
        monkeypatch.delenv("RBAC_PG_DSN", raising=False)
        monkeypatch.setenv("WARM_PG_DSN", "postgresql://warm-dsn/warm_store")
        store = PostgresRoleStore()
        assert store._dsn == "postgresql://warm-dsn/warm_store"

    def test_dsn_explicit_wins(self, monkeypatch):
        monkeypatch.setenv("RBAC_PG_DSN", "postgresql://env/d")
        store = PostgresRoleStore(dsn="postgresql://explicit/d")
        assert store._dsn == "postgresql://explicit/d"


# ---------------------------------------------------------------------------
# RBAC instance wiring
# ---------------------------------------------------------------------------


class TestRBACWiring:
    def test_default_store_is_env_backed(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        rbac = RBAC()
        assert rbac.check("alice", "audit:read") is True

    def test_cross_role_access_denied(self):
        """Each role has access only to its own permissions (ticket DoD)."""
        rbac_admin = _rbac_for("admin")
        for role in CANONICAL_ROLES - {"admin"}:
            for perm in ROLES[role] - ROLES["admin"]:
                assert rbac_admin.check("tester", perm) is False, (role, perm)

    def test_developer_cannot_write_policy(self):
        assert _rbac_for("developer").check("tester", "policy:write") is False

    def test_auditor_cannot_read_traces(self):
        assert _rbac_for("auditor").check("tester", "trace:read") is False

    def test_agent_cannot_read_traces(self):
        assert _rbac_for("agent").check("tester", "trace:read") is False

    def test_agent_can_write_spans(self):
        assert _rbac_for("agent").check("tester", "span:write") is True
