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

import asyncio
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import httpx

from agent_obs.rbac.roles import ROLES, MemoryRoleStore, RoleStore
from agent_obs.rbac.audit_bundle_reload import get_bundle_logger, BundleReloadEvent

logger = logging.getLogger(__name__)

# OPA server base URL (Data API lives at <url>/v1/...).
OPA_URL_ENV = "AGENT_OBS_OPA_URL"
DEFAULT_OPA_URL = "http://localhost:8181"
# OPA bundle server URL (bundle distribution API).
OPA_BUNDLE_URL_ENV = "AGENT_OBS_OPA_BUNDLE_URL"
DEFAULT_OPA_BUNDLE_URL = "http://localhost:8080"
# Full path of the policy entry point in observability.rego (package path +
# rule name).
OPA_POLICY_PATH = "/v1/data/observability/rbac/allow"
# Bundle polling interval in seconds.
OPA_BUNDLE_POLL_INTERVAL_ENV = "AGENT_OBS_OPA_BUNDLE_POLL_INTERVAL"
DEFAULT_OPA_BUNDLE_POLL_INTERVAL = 30

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
    """OPA-backed access decisions with a 60s Redis cache and bundle polling (PP56)."""

    def __init__(
        self,
        store: RoleStore | None = None,
        opa_url: str | None = None,
        cache_url: str | None = None,
        cache_ttl: int | None = None,
        timeout: float = DEFAULT_OPA_TIMEOUT,
        http_client: httpx.AsyncClient | None = None,
        bundle_url: str | None = None,
        bundle_poll_interval: int | None = None,
    ) -> None:
        # Role resolution reuses the PP30 store (env-backed by default).
        self._store = store if store is not None else MemoryRoleStore.from_env()
        self._opa_url = (
            opa_url or os.environ.get(OPA_URL_ENV) or DEFAULT_OPA_URL
        ).rstrip("/")
        self._bundle_url = (
            bundle_url or os.environ.get(OPA_BUNDLE_URL_ENV) or DEFAULT_OPA_BUNDLE_URL
        ).rstrip("/")
        self._bundle_poll_interval = int(
            bundle_poll_interval
            if bundle_poll_interval is not None
            else os.environ.get(OPA_BUNDLE_POLL_INTERVAL_ENV, DEFAULT_OPA_BUNDLE_POLL_INTERVAL)
        )
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
        # Bundle polling state
        self._bundle_version = None
        self._last_bundle_check = None
        self._bundle_lock = asyncio.Lock()
        # Initialize bundle logger
        self._bundle_logger = get_bundle_logger()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def opa_url(self) -> str:
        return self._opa_url

    @property
    def cache_url(self) -> str:
        return self._cache_url

    @property
    def bundle_url(self) -> str:
        return self._bundle_url

    @property
    def bundle_version(self) -> str | None:
        return self._bundle_version

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

        # Check for bundle updates before querying OPA
        await self._check_bundle_updates()
        
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

    # ------------------------------------------------------------------
    # Bundle polling for hot-reload (PP56)
    # ------------------------------------------------------------------

    async def _check_bundle_updates(self) -> None:
        """Poll the OPA bundle server for policy updates."""
        async with self._bundle_lock:
            now = datetime.now(timezone.utc)
            if (self._last_bundle_check is None or 
                (now - self._last_bundle_check).total_seconds() >= self._bundle_poll_interval):
                
                try:
                    old_version = self._bundle_version
                    new_version = await self._fetch_bundle_version()
                    if new_version != old_version:
                        # Fetch policy snapshots for audit logging
                        old_policies = await self._fetch_bundle_policies(old_version)
                        new_policies = await self._fetch_bundle_policies(new_version)
                        
                        # Update state
                        self._bundle_version = new_version
                        self._last_bundle_check = now
                        
                        # Log the reload event
                        try:
                            await self._bundle_logger.log_reload(
                                old_version or "unknown",
                                new_version,
                                old_policies or {},
                                new_policies or {},
                                trigger="poll",
                                triggered_by="system",
                            )
                        except Exception as audit_exc:
                            logger.debug("PolicyEngine: Audit logging failed (%s); continuing", audit_exc)
                        
                        logger.info("PolicyEngine: Bundle updated to version %s", new_version)
                except Exception as exc:
                    logger.debug("PolicyEngine: Bundle check failed (%s); continuing", exc)

    async def _fetch_bundle_version(self) -> str:
        """Fetch the current bundle version from OPA bundle server."""
        url = f"{self._bundle_url}/bundles/observability.zip"
        try:
            if self._http_client is not None:
                resp = await self._http_client.head(url, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.head(url)
            
            resp.raise_for_status()
            
            # Extract version from headers or return timestamp
            last_modified = resp.headers.get("last-modified")
            if last_modified:
                return last_modified
            return str(int(datetime.now(timezone.utc).timestamp()))
            
        except Exception as exc:
            logger.debug("PolicyEngine: Bundle fetch failed (%s); using cached version", exc)
            # Fall back to current version or timestamp
            if self._bundle_version:
                return self._bundle_version
            return str(int(datetime.now(timezone.utc).timestamp()))

    async def force_bundle_reload(self) -> None:
        """Force an immediate bundle reload and clear local cache."""
        async with self._bundle_lock:
            try:
                new_version = await self._fetch_bundle_version()
                if new_version != self._bundle_version:
                    self._bundle_version = new_version
                    self._last_bundle_check = datetime.now(timezone.utc)
                    logger.info("PolicyEngine: Bundle force-reloaded to version %s", new_version)
                    
                    # Clear policy cache to ensure fresh decisions
                    if self._redis:
                        try:
                            # Pattern to clear all policy decisions
                            pattern = f"{CACHE_KEY_PREFIX}*"
                            keys = await self._redis.keys(pattern)
                            if keys:
                                await self._redis.delete(*keys)
                                logger.info("PolicyEngine: Cleared %d cached decisions", len(keys))
                        except Exception as exc:
                            logger.debug("PolicyEngine: Cache clear failed (%s); continuing", exc)
            except Exception as exc:
                logger.warning("PolicyEngine: Force bundle reload failed (%s)", exc)

    async def _fetch_bundle_policies(self, version: str | None) -> dict:
        """Fetch the current bundle policies for audit logging."""
        try:
            url = f"{self._bundle_url}/bundles/observability.zip"
            if self._http_client is not None:
                resp = await self._http_client.get(url, timeout=self._timeout)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    resp = await client.get(url)
            
            resp.raise_for_status()
            
            # For audit purposes, we'll store a summary of the policies
            # In a real implementation, you might extract the actual Rego content
            return {
                "version": version,
                "size": len(resp.content),
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "endpoint": url,
            }
        except Exception as exc:
            logger.debug("PolicyEngine: Failed to fetch bundle policies (%s); returning empty", exc)
            return {}
