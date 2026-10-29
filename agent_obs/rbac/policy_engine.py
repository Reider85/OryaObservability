"""PP31 (T3.7.2): OPA/Rego policy engine for declarative access rules.

``PolicyEngine.check`` asks an Open Policy Agent server for an allow/deny
verdict on ``(user, action, resource)`` — the three rules live in Rego
(``infra/rbac/policies/observability.rego``): cross-tenant deny, MFA gate for
PII actions, role-permission match. The role itself is resolved from the PP30
``RoleStore`` (``MemoryRoleStore`` / ``PostgresRoleStore``), so this engine
extends PP30 rather than replacing it.

Contract vs ``RBAC.check`` (PP30):

* ``check`` is **async** (network I/O to OPA + Redis), unlike the synchronous
  PP30 check — same rationale as the guardrail engine being async-first.
* **Fail-closed everywhere**: unknown user, OPA unreachable, malformed
  response, Redis error — all end in ``False`` (deny) with a warning.
* Decisions are cached in Redis for 60s keyed by
  ``sha256(user_id + action + resource_id)`` (ticket spec), so a briefly
  unavailable OPA keeps serving the last known decisions. The cache is
  best-effort: Redis down never blocks a check, it just skips the cache.
* Direct HTTP to the OPA Data API (``POST /v1/data/observability/rbac/allow``)
  via ``httpx`` — the ticket's python-opa/opa-python suggestions are stale
  unmaintained packages and httpx is already a core dependency.

Usage:
    engine = PolicyEngine()                       # env-backed (OPA + Redis URLs)
    user = User(id="alice", tenant_id="acme", mfa_verified=True)
    resource = Resource(id="trace:123", tenant_id="acme", type="trace")

    if not await engine.check(user, "pii:recover", resource):
        raise HTTPException(status_code=403, detail="Permission denied")
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass

import httpx

from agent_obs.rbac.roles import ROLES, MemoryRoleStore, RoleStore

logger = logging.getLogger(__name__)

# OPA server base URL (Data API lives at <url>/v1/...).
OPA_URL_ENV = "AGENT_OBS_OPA_URL"
DEFAULT_OPA_URL = "http://localhost:8181"
# Full path of the policy entry point in observability.rego (package path +
# rule name).
OPA_POLICY_PATH = "/v1/data/observability/rbac/allow"

# Redis URL for the decision cache. Empty/unset = cache disabled (every check
# round-trips to OPA). Best-effort: Redis failures never block a decision.
CACHE_URL_ENV = "AGENT_OBS_POLICY_CACHE_URL"
# Cache TTL in seconds (ticket spec: 60).
CACHE_TTL_ENV = "AGENT_OBS_POLICY_CACHE_TTL"
DEFAULT_CACHE_TTL = 60

# Key prefix keeps policy decisions namespaced in a shared Redis.
CACHE_KEY_PREFIX = "agent_obs:policy:"

# Default per-request timeout against OPA — a policy decision must never hang
# a caller; fail-closed deny is the intended behaviour on timeout.
DEFAULT_OPA_TIMEOUT = 2.0


@dataclass(frozen=True)
class User:
    """Principal of a decision. Role is resolved from the RoleStore, not here.

    ``mfa_verified`` is set by the caller after a real MFA challenge (e.g.
    trace-viewer verifies the ``X-OryaObs-MFA`` header via
    ``VaultClient.verify_mfa`` before building the User).
    """

    id: str
    tenant_id: str = ""
    mfa_verified: bool = False


@dataclass(frozen=True)
class Resource:
    """Object the action targets. ``tenant_id`` drives the cross-tenant rule.

    When the caller passes no resource (non-resource-scoped actions such as
    ``slo:write``), the engine substitutes a resource in the caller's own
    tenant — the check then degenerates to the PP30 role check, as before.
    Row-level tenant filtering itself is PP32's concern.
    """

    id: str
    tenant_id: str = ""
    type: str = ""


class PolicyEngine:
    """OPA-backed access decisions with a 60s Redis cache (PP31 ticket)."""

    def __init__(
        self,
        store: RoleStore | None = None,
        opa_url: str | None = None,
        cache_url: str | None = None,
        cache_ttl: int | None = None,
        timeout: float = DEFAULT_OPA_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        # Role resolution reuses the PP30 store (env-backed by default).
        self._store = store if store is not None else MemoryRoleStore.from_env()
        self._opa_url = (
            opa_url or os.environ.get(OPA_URL_ENV) or DEFAULT_OPA_URL
        ).rstrip("/")
        self._cache_url = (
            cache_url
            if cache_url is not None
            else os.environ.get(CACHE_URL_ENV, "")
        )
        self._cache_ttl = int(
            cache_ttl
            if cache_ttl is not None
            else os.environ.get(CACHE_TTL_ENV, DEFAULT_CACHE_TTL)
        )
        self._timeout = timeout
        # Optional injected client (tests / connection sharing). When None a
        # fresh client is created per call — expand endpoints are not hot-path.
        self._http_client = http_client
        self._redis = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def opa_url(self) -> str:
        return self._opa_url

    @property
    def cache_url(self) -> str:
        return self._cache_url

    async def check(
        self, user: User, action: str, resource: Resource | None = None
    ) -> bool:
        """True if OPA allows ``user`` to perform ``action`` on ``resource``.

        Fail-closed: every error path (unknown user, OPA down, bad response,
        Redis errors) returns False. Redis errors only skip the cache.
        """
        if not user or not user.id or not action:
            return False

        # Role comes from the PP30 store — the caller never asserts its own.
        role = self._store.get_role(user.id)
        if role is None or role not in ROLES:
            # Unknown user/role: deny without an OPA round-trip (PP30 parity).
            return False

        if resource is None:
            resource = Resource(id="", tenant_id=user.tenant_id)

        payload = {
            "user": {
                "id": user.id,
                "role": role,
                "tenant_id": user.tenant_id,
                "mfa_verified": bool(user.mfa_verified),
            },
            "action": action,
            "resource": {
                "id": resource.id,
                "type": resource.type,
                "tenant_id": resource.tenant_id,
            },
        }

        cache_key = self._cache_key(user.id, action, resource.id)
        cached = await self._cache_get(cache_key)
        if cached is not None:
            return cached

        allowed = await self._query_opa(payload)
        await self._cache_set(cache_key, allowed)
        return allowed

    async def aclose(self) -> None:
        """Release the injected HTTP client / Redis pool (best-effort)."""
        if self._http_client is not None:
            try:
                await self._http_client.aclose()
            except Exception as exc:  # noqa: BLE001 - shutdown best-effort
                logger.debug("PolicyEngine: http client close failed: %s", exc)
        if self._redis is not None:
            try:
                await self._redis.aclose()
            except Exception as exc:  # noqa: BLE001 - shutdown best-effort
                logger.debug("PolicyEngine: redis close failed: %s", exc)

    # ------------------------------------------------------------------
    # OPA Data API
    # ------------------------------------------------------------------

    async def _query_opa(self, payload: dict) -> bool:
        """POST the decision input to OPA. Fail-closed on any error."""
        url = f"{self._opa_url}{OPA_POLICY_PATH}"
        try:
            if self._http_client is not None:
                resp = await self._http_client.post(
                    url, json={"input": payload}, timeout=self._timeout
                )
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.post(url, json={"input": payload})
            resp.raise_for_status()
            body = resp.json()
        except Exception as exc:  # noqa: BLE001 - any transport/shape error = deny
            logger.warning(
                "PolicyEngine: OPA query failed (%s); denying (fail-closed)", exc
            )
            return False

        # `default allow := false` makes `result` always present; accept only
        # an explicit true — null/missing/malformed all deny.
        result = body.get("result") if isinstance(body, dict) else None
        if result is not True:
            if not isinstance(body, dict) or "result" not in body:
                logger.warning(
                    "PolicyEngine: OPA response has no result field; denying"
                )
            return False
        return True

    # ------------------------------------------------------------------
    # Redis decision cache (best-effort, 60s TTL)
    # ------------------------------------------------------------------

    def _cache_key(self, user_id: str, action: str, resource_id: str) -> str:
        """sha256(user_id + action + resource_id) — ticket-specified key."""
        digest = hashlib.sha256(
            f"{user_id}|{action}|{resource_id}".encode()
        ).hexdigest()
        return f"{CACHE_KEY_PREFIX}{digest}"

    def _redis_client(self):
        """Lazy Redis client, or None when caching is disabled/unavailable."""
        if not self._cache_url:
            return None
        if self._redis is None:
            try:
                import redis.asyncio as redis_async
            except ImportError:  # pragma: no cover - redis is a core dep
                logger.warning(
                    "PolicyEngine: redis package unavailable; decision cache disabled"
                )
                return None
            try:
                self._redis = redis_async.from_url(self._cache_url)
            except Exception as exc:  # noqa: BLE001 - cache is best-effort
                logger.warning(
                    "PolicyEngine: redis init failed (%s); decision cache disabled",
                    exc,
                )
                return None
        return self._redis

    async def _cache_get(self, key: str) -> bool | None:
        """Cached verdict, or None on miss/error (caller falls through to OPA)."""
        client = self._redis_client()
        if client is None:
            return None
        try:
            raw = await client.get(key)
        except Exception as exc:  # noqa: BLE001 - Redis down = skip cache
            logger.debug("PolicyEngine: redis get failed (%s); skipping cache", exc)
            return None
        if raw is None:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode()
        if raw == "1":
            return True
        if raw == "0":
            return False
        # Unexpected value: treat as a miss rather than trusting garbage.
        return None

    async def _cache_set(self, key: str, allowed: bool) -> None:
        client = self._redis_client()
        if client is None:
            return
        try:
            await client.setex(key, self._cache_ttl, "1" if allowed else "0")
        except Exception as exc:  # noqa: BLE001 - Redis down = skip cache
            logger.debug("PolicyEngine: redis set failed (%s); skipping cache", exc)
