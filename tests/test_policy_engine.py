"""PP31 (T3.7.2): tests for the OPA policy engine — agent_obs/rbac/policy_engine.py.

Coverage (ticket DoD):
  * cross-tenant access denied;
  * MFA enforcement for PII actions (pii:recover without mfa_verified = deny);
  * valid allow (role grants action, same tenant);
  * Redis decision cache: sha256 key, 60s TTL, hit skips OPA, best-effort
    when Redis is down;
  * fail-closed on OPA errors (unreachable, 500, non-JSON, missing result);
  * unknown user denied without an OPA round-trip;
  * env config wiring.

External services are mocked (project convention: unittest.mock) — the fake
HTTP client evaluates the same three rules as observability.rego in Python, so
the tests exercise realistic request/response shapes.

TestRegoPolicy runs `opa eval` against the real .rego file and skips when the
opa binary is not installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from agent_obs.rbac.policy_engine import (
    CACHE_KEY_PREFIX,
    DEFAULT_CACHE_TTL,
    DEFAULT_OPA_URL,
    OPA_POLICY_PATH,
    PolicyEngine,
    Resource,
    User,
)
from agent_obs.rbac.roles import ROLES, MemoryRoleStore

REPO_ROOT = Path(__file__).resolve().parents[1]
REGO_FILE = REPO_ROOT / "infra" / "rbac" / "policies" / "observability.rego"

# Python mirror of observability.rego — keep in sync with the Rego rules.
MFA_ACTIONS = {"pii:recover", "audit:read:raw"}


def _rego_allow(inp: dict) -> bool:
    user = inp.get("user") or {}
    resource = inp.get("resource") or {}
    action = inp.get("action")
    role = user.get("role")
    if role not in ROLES:
        return False
    if action not in ROLES[role]:
        return False
    if user.get("tenant_id") != resource.get("tenant_id"):
        return False
    if action in MFA_ACTIONS:
        return user.get("mfa_verified") is True
    return True


class _FakeOPAClient:
    """httpx.AsyncClient stand-in answering with the mirrored Rego rules."""

    def __init__(self, response: httpx.Response | None = None, error: Exception | None = None):
        self.calls: list[dict] = []
        self.urls: list[str] = []
        self._response = response
        self._error = error

    async def post(self, url: str, json: dict | None = None, timeout: float | None = None) -> httpx.Response:
        self.calls.append(json or {})
        self.urls.append(url)
        if self._error is not None:
            raise self._error
        if self._response is not None:
            return self._response
        result = _rego_allow((json or {}).get("input", {}))
        return httpx.Response(
            200,
            json={"result": result},
            request=httpx.Request("POST", url),
        )


def _engine(client, store=None, cache_url: str = "", **kwargs) -> PolicyEngine:
    return PolicyEngine(
        store=store if store is not None else MemoryRoleStore({"alice": "admin"}),
        opa_url="http://opa.test:8181",
        cache_url=cache_url,
        http_client=client,
        **kwargs,
    )


def _input_of(call: dict) -> dict:
    return call["input"]


# ---------------------------------------------------------------------------
# Valid allow
# ---------------------------------------------------------------------------


class TestAllow:
    async def test_valid_allow(self):
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme")
        resource = Resource(id="trace:123", tenant_id="acme", type="trace")
        assert await engine.check(user, "trace:read", resource) is True

    @pytest.mark.parametrize(
        "role",
        sorted(ROLES),
    )
    async def test_every_role_allows_its_own_permissions(self, role):
        store = MemoryRoleStore({"tester": role})
        client = _FakeOPAClient()
        engine = _engine(client, store=store)
        # mfa_verified=True so MFA-gated permissions (pii:recover) are not
        # tripped here — the no-MFA deny is TestMFAGate.
        user = User(id="tester", tenant_id="t1", mfa_verified=True)
        for action in sorted(ROLES[role]):
            resource = Resource(id="r:1", tenant_id="t1")
            assert await engine.check(user, action, resource) is True, (role, action)

    async def test_pii_recover_with_mfa_allowed(self):
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme", mfa_verified=True)
        resource = Resource(id="trace:123", tenant_id="acme")
        assert await engine.check(user, "pii:recover", resource) is True

    async def test_role_resolved_from_store_not_from_caller(self):
        # The caller never asserts its own role: unknown user -> deny even if a
        # role-looking value were available elsewhere.
        store = MemoryRoleStore({"alice": "admin"})
        client = _FakeOPAClient()
        engine = _engine(client, store=store)
        user = User(id="mallory", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False
        assert client.calls == []  # denied without an OPA round-trip

    async def test_resourceless_action_defaults_to_own_tenant(self):
        # No resource = contract-compatible degeneration to the PP30 role check.
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme")
        assert await engine.check(user, "slo:write") is True

    async def test_input_payload_shape(self):
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme", mfa_verified=True)
        resource = Resource(id="trace:9", tenant_id="acme", type="trace")
        await engine.check(user, "pii:recover", resource)
        payload = _input_of(client.calls[0])
        assert payload["user"] == {
            "id": "alice",
            "role": "admin",
            "tenant_id": "acme",
            "mfa_verified": True,
        }
        assert payload["action"] == "pii:recover"
        assert payload["resource"] == {"id": "trace:9", "type": "trace", "tenant_id": "acme"}
        # URL targets the allow rule in observability.rego.
        assert client.urls == ["http://opa.test:8181" + OPA_POLICY_PATH]


# ---------------------------------------------------------------------------
# Cross-tenant deny (ticket rule 1)
# ---------------------------------------------------------------------------


class TestCrossTenantDeny:
    @pytest.mark.parametrize("role", sorted(ROLES))
    async def test_foreign_tenant_denied_for_every_role(self, role):
        store = MemoryRoleStore({"tester": role})
        client = _FakeOPAClient()
        engine = _engine(client, store=store)
        user = User(id="tester", tenant_id="tenant-a")
        resource = Resource(id="trace:1", tenant_id="tenant-b", type="trace")
        # Actions the role legitimately holds in its own tenant must be denied
        # when the resource belongs to another tenant.
        for action in sorted(ROLES[role]):
            assert await engine.check(user, action, resource) is False, (role, action)

    async def test_empty_tenant_on_resource_denied(self):
        # Missing tenant field != user tenant in Rego (undefined comparison
        # fails the rule) -> deny.
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme")
        resource = Resource(id="trace:1", tenant_id="")
        assert await engine.check(user, "trace:read", resource) is False

    async def test_same_tenant_allowed(self):
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme")
        resource = Resource(id="trace:1", tenant_id="acme")
        assert await engine.check(user, "trace:read", resource) is True


# ---------------------------------------------------------------------------
# MFA enforcement (ticket rule 2)
# ---------------------------------------------------------------------------


class TestMFAGate:
    @pytest.mark.parametrize("role", ["admin", "sre"])  # roles holding pii:recover
    async def test_pii_recover_without_mfa_denied(self, role):
        store = MemoryRoleStore({"tester": role})
        client = _FakeOPAClient()
        engine = _engine(client, store=store)
        user = User(id="tester", tenant_id="acme", mfa_verified=False)
        resource = Resource(id="trace:1", tenant_id="acme")
        assert await engine.check(user, "pii:recover", resource) is False

    async def test_pii_recover_with_mfa_denied_for_foreign_tenant(self):
        # MFA does not bypass tenant isolation.
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="tenant-a", mfa_verified=True)
        resource = Resource(id="trace:1", tenant_id="tenant-b")
        assert await engine.check(user, "pii:recover", resource) is False

    async def test_audit_read_raw_never_allowed(self):
        # audit:read:raw requires MFA and is not in any role's permissions.
        client = _FakeOPAClient()
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme", mfa_verified=True)
        resource = Resource(id="audit:1", tenant_id="acme")
        assert await engine.check(user, "audit:read:raw", resource) is False

    async def test_plain_audit_read_needs_no_mfa(self):
        store = MemoryRoleStore({"charlie": "auditor"})
        client = _FakeOPAClient()
        engine = _engine(client, store=store)
        user = User(id="charlie", tenant_id="acme", mfa_verified=False)
        resource = Resource(id="audit:1", tenant_id="acme")
        assert await engine.check(user, "audit:read", resource) is True


# ---------------------------------------------------------------------------
# Fail-closed behaviour
# ---------------------------------------------------------------------------


class TestFailClosed:
    async def test_unknown_user_denied_without_opa_call(self):
        client = _FakeOPAClient()
        engine = _engine(client)  # store has only "alice"
        user = User(id="nobody", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False
        assert client.calls == []

    async def test_empty_user_id_denied(self):
        client = _FakeOPAClient()
        engine = _engine(client)
        assert await engine.check(User(id="", tenant_id="acme"), "trace:read") is False
        assert client.calls == []

    async def test_empty_action_denied(self):
        client = _FakeOPAClient()
        engine = _engine(client)
        assert await engine.check(User(id="alice", tenant_id="acme"), "") is False
        assert client.calls == []

    async def test_opa_unreachable_denies(self):
        client = _FakeOPAClient(error=httpx.ConnectError("connection refused"))
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False

    async def test_opa_timeout_denies(self):
        client = _FakeOPAClient(error=httpx.ReadTimeout("slow"))
        engine = _engine(client)
        user = User(id="alice", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False

    async def test_opa_500_denies(self):
        response = httpx.Response(
            500, text="boom", request=httpx.Request("POST", "http://opa.test/x")
        )
        engine = _engine(_FakeOPAClient(response=response))
        user = User(id="alice", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False

    async def test_opa_non_json_denies(self):
        response = httpx.Response(
            200, text="<html>not json</html>", request=httpx.Request("POST", "http://opa.test/x")
        )
        engine = _engine(_FakeOPAClient(response=response))
        user = User(id="alice", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False

    async def test_missing_result_field_denies(self):
        response = httpx.Response(
            200, json={}, request=httpx.Request("POST", "http://opa.test/x")
        )
        engine = _engine(_FakeOPAClient(response=response))
        user = User(id="alice", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False

    async def test_null_result_denies(self):
        response = httpx.Response(
            200, json={"result": None}, request=httpx.Request("POST", "http://opa.test/x")
        )
        engine = _engine(_FakeOPAClient(response=response))
        user = User(id="alice", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False

    async def test_role_outside_model_denied_without_opa_call(self):
        # A store that somehow yields a non-canonical role must deny locally.
        store = MemoryRoleStore()
        store._mapping["eve"] = "superuser"  # bypass set_user_role validation
        client = _FakeOPAClient()
        engine = _engine(client, store=store)
        user = User(id="eve", tenant_id="acme")
        assert await engine.check(user, "trace:read", Resource(id="r", tenant_id="acme")) is False
        assert client.calls == []


# ---------------------------------------------------------------------------
# Redis decision cache (ticket: sha256(user+action+resource_id), 60s)
# ---------------------------------------------------------------------------


def _mock_redis(get_value=None, get_error=None, set_error=None):
    client = MagicMock()
    if get_error is not None:
        client.get = AsyncMock(side_effect=get_error)
    else:
        client.get = AsyncMock(return_value=get_value)
    if set_error is not None:
        client.setex = AsyncMock(side_effect=set_error)
    else:
        client.setex = AsyncMock()
    return client


def _patched_redis(client):
    return patch("redis.asyncio.from_url", return_value=client)


class TestRedisCache:
    async def test_cache_hit_skips_opa(self):
        http = _FakeOPAClient()
        redis_client = _mock_redis(get_value=b"1")
        engine = _engine(http, cache_url="redis://localhost:6379/0")
        with _patched_redis(redis_client):
            user = User(id="alice", tenant_id="acme")
            resource = Resource(id="trace:1", tenant_id="acme")
            assert await engine.check(user, "trace:read", resource) is True
        assert http.calls == []  # served from cache, OPA untouched
        redis_client.get.assert_awaited_once()

    async def test_cache_miss_queries_opa_and_stores(self):
        http = _FakeOPAClient()
        redis_client = _mock_redis(get_value=None)
        engine = _engine(http, cache_url="redis://localhost:6379/0")
        with _patched_redis(redis_client):
            user = User(id="alice", tenant_id="acme")
            resource = Resource(id="trace:1", tenant_id="acme")
            assert await engine.check(user, "trace:read", resource) is True
        assert len(http.calls) == 1
        redis_client.setex.assert_awaited_once()
        _key, ttl, value = redis_client.setex.await_args.args
        assert ttl == DEFAULT_CACHE_TTL == 60
        assert value == "1"

    async def test_deny_is_cached_as_zero(self):
        http = _FakeOPAClient()
        redis_client = _mock_redis(get_value=None)
        engine = _engine(http, cache_url="redis://localhost:6379/0")
        with _patched_redis(redis_client):
            user = User(id="alice", tenant_id="tenant-a")
            resource = Resource(id="trace:1", tenant_id="tenant-b")
            assert await engine.check(user, "trace:read", resource) is False
        _key, _ttl, value = redis_client.setex.await_args.args
        assert value == "0"

    async def test_cached_zero_denies_without_opa(self):
        http = _FakeOPAClient()
        redis_client = _mock_redis(get_value=b"0")
        engine = _engine(http, cache_url="redis://localhost:6379/0")
        with _patched_redis(redis_client):
            user = User(id="alice", tenant_id="acme")
            resource = Resource(id="trace:1", tenant_id="acme")
            assert await engine.check(user, "trace:read", resource) is False
        assert http.calls == []

    def test_cache_key_is_sha256_of_user_action_resource(self):
        engine = _engine(_FakeOPAClient())
        expected = hashlib.sha256(b"alice|trace:read|trace:1").hexdigest()
        assert engine._cache_key("alice", "trace:read", "trace:1") == (
            f"{CACHE_KEY_PREFIX}{expected}"
        )
        # Different resource -> different key (no false sharing across traces).
        assert engine._cache_key("alice", "trace:read", "trace:2") != (
            f"{CACHE_KEY_PREFIX}{expected}"
        )

    async def test_redis_get_error_falls_through_to_opa(self):
        http = _FakeOPAClient()
        redis_client = _mock_redis(get_error=RuntimeError("redis down"))
        engine = _engine(http, cache_url="redis://localhost:6379/0")
        with _patched_redis(redis_client):
            user = User(id="alice", tenant_id="acme")
            resource = Resource(id="trace:1", tenant_id="acme")
            assert await engine.check(user, "trace:read", resource) is True
        assert len(http.calls) == 1

    async def test_redis_set_error_still_returns_decision(self):
        http = _FakeOPAClient()
        redis_client = _mock_redis(get_value=None, set_error=RuntimeError("read-only"))
        engine = _engine(http, cache_url="redis://localhost:6379/0")
        with _patched_redis(redis_client):
            user = User(id="alice", tenant_id="acme")
            resource = Resource(id="trace:1", tenant_id="acme")
            assert await engine.check(user, "trace:read", resource) is True
        assert len(http.calls) == 1

    async def test_unexpected_cached_value_treated_as_miss(self):
        http = _FakeOPAClient()
        redis_client = _mock_redis(get_value=b"garbage")
        engine = _engine(http, cache_url="redis://localhost:6379/0")
        with _patched_redis(redis_client):
            user = User(id="alice", tenant_id="acme")
            resource = Resource(id="trace:1", tenant_id="acme")
            assert await engine.check(user, "trace:read", resource) is True
        assert len(http.calls) == 1  # fell through to OPA

    async def test_cache_disabled_without_url(self):
        http = _FakeOPAClient()
        engine = _engine(http, cache_url="")
        with patch("redis.asyncio.from_url") as from_url:
            user = User(id="alice", tenant_id="acme")
            resource = Resource(id="trace:1", tenant_id="acme")
            assert await engine.check(user, "trace:read", resource) is True
        from_url.assert_not_called()
        assert len(http.calls) == 1


# ---------------------------------------------------------------------------
# Env configuration
# ---------------------------------------------------------------------------


class TestConfig:
    def test_default_opa_url(self, monkeypatch):
        monkeypatch.delenv("AGENT_OBS_OPA_URL", raising=False)
        engine = PolicyEngine(store=MemoryRoleStore())
        assert engine.opa_url == DEFAULT_OPA_URL == "http://localhost:8181"

    def test_opa_url_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_OPA_URL", "http://opa:8181/")
        engine = PolicyEngine(store=MemoryRoleStore())
        assert engine.opa_url == "http://opa:8181"  # trailing slash stripped

    def test_cache_url_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_POLICY_CACHE_URL", "redis://redis:6379/0")
        engine = PolicyEngine(store=MemoryRoleStore())
        assert engine.cache_url == "redis://redis:6379/0"

    def test_cache_ttl_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_POLICY_CACHE_TTL", "30")
        engine = PolicyEngine(store=MemoryRoleStore())
        assert engine._cache_ttl == 30

    def test_policy_path_targets_allow_rule(self):
        assert OPA_POLICY_PATH == "/v1/data/observability/rbac/allow"


# ---------------------------------------------------------------------------
# Rego file itself (optional: needs the opa binary)
# ---------------------------------------------------------------------------


def _opa_eval(inp: dict) -> bool:
    """Evaluate data.observability.rbac.allow for `inp` with the real OPA."""
    import tempfile

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as fh:
        json.dump(inp, fh)
        input_path = fh.name
    try:
        proc = subprocess.run(
            [
                "opa", "eval",
                "--data", str(REGO_FILE),
                "--input", input_path,
                "--format", "json",
                "data.observability.rbac.allow",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    finally:
        Path(input_path).unlink(missing_ok=True)
    assert proc.returncode == 0, f"opa eval failed: {proc.stderr}"
    payload = json.loads(proc.stdout)
    return payload["result"][0]["expressions"][0]["value"] is True


@pytest.mark.skipif(shutil.which("opa") is None, reason="opa binary not installed")
class TestRegoPolicy:
    def test_rego_file_exists_with_package(self):
        text = REGO_FILE.read_text(encoding="utf-8")
        assert "package observability.rbac" in text
        assert "default allow" in text

    def test_valid_allow(self):
        assert _opa_eval(
            {
                "user": {"id": "alice", "role": "admin", "tenant_id": "acme", "mfa_verified": False},
                "action": "trace:read",
                "resource": {"id": "trace:1", "type": "trace", "tenant_id": "acme"},
            }
        ) is True

    def test_cross_tenant_deny(self):
        assert _opa_eval(
            {
                "user": {"id": "alice", "role": "admin", "tenant_id": "tenant-a", "mfa_verified": True},
                "action": "trace:read",
                "resource": {"id": "trace:1", "type": "trace", "tenant_id": "tenant-b"},
            }
        ) is False

    def test_pii_without_mfa_deny(self):
        assert _opa_eval(
            {
                "user": {"id": "alice", "role": "admin", "tenant_id": "acme", "mfa_verified": False},
                "action": "pii:recover",
                "resource": {"id": "trace:1", "type": "trace", "tenant_id": "acme"},
            }
        ) is False

    def test_pii_with_mfa_allow(self):
        assert _opa_eval(
            {
                "user": {"id": "alice", "role": "admin", "tenant_id": "acme", "mfa_verified": True},
                "action": "pii:recover",
                "resource": {"id": "trace:1", "type": "trace", "tenant_id": "acme"},
            }
        ) is True

    def test_role_without_permission_deny(self):
        assert _opa_eval(
            {
                "user": {"id": "bob", "role": "developer", "tenant_id": "acme", "mfa_verified": False},
                "action": "pii:recover",
                "resource": {"id": "trace:1", "type": "trace", "tenant_id": "acme"},
            }
        ) is False

    def test_unknown_role_deny(self):
        assert _opa_eval(
            {
                "user": {"id": "eve", "role": "superuser", "tenant_id": "acme", "mfa_verified": True},
                "action": "trace:read",
                "resource": {"id": "trace:1", "type": "trace", "tenant_id": "acme"},
            }
        ) is False

    def test_missing_tenant_deny(self):
        # Undefined tenant comparison fails the rule -> deny (fail-closed).
        assert _opa_eval(
            {
                "user": {"id": "alice", "role": "admin", "tenant_id": "acme", "mfa_verified": False},
                "action": "trace:read",
                "resource": {"id": "trace:1", "type": "trace"},
            }
        ) is False
