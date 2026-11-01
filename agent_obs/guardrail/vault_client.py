"""HashiCorp Vault client for PII mask ↔ original mapping with TTL.

PC18 provides a full implementation with real Vault HTTP API calls, TTL-based
expiration via Vault leases, and batch operations. Replaces the PC07 in-memory stub.
PC19 adds TOTP MFA verification and rate-limiting for recovery operations.
PC20 adds audit trail for every recovery with ClickHouse write and reason field.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Optional, TypeVar

import hvac
import pyotp
from hvac.exceptions import VaultError

from agent_obs.guardrail.audit import (
    RecoveryAuditEvent,
    AuditReasonRequiredError,
    AuditWriteError,
)

if TYPE_CHECKING:
    from agent_obs.storage.hot import HotStore

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Cooldown per dead Vault address before it is retried (PP37, mirrors PP35)
REPLICA_COOLDOWN_SECONDS = 30.0

# Tenant identifier validation regex (same as hot.py _validate_tenant_identifier)
_TENANT_REGEX = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*[a-zA-Z0-9]$|^[a-zA-Z0-9]$')

def _validate_tenant_segment(tenant_id: str) -> None:
    """Validate tenant identifier format.
    
    Parameters
    ----------
    tenant_id: str
        Tenant identifier to validate
        
    Raises
    ------
    ValueError
        If tenant_id is invalid
    """
    if not _TENANT_REGEX.match(tenant_id):
        raise ValueError(
            f"Invalid tenant_id format: '{tenant_id}'. "
            "Must be 1-64 chars, alphanumeric, underscore, hyphen, "
            "cannot start/end with underscore/hyphen"
        )

# Prometheus metrics for vault operations
from prometheus_client import Counter, Histogram

vault_recovery_total = Counter(
    "agent_obs_vault_recovery_total",
    "Vault recovery operations by reason category",
    ["reason_category"],
)

vault_recovery_duration_seconds = Histogram(
    "agent_obs_vault_recovery_duration_seconds",
    "Vault recovery operation latency",
    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
)

vault_audit_write_total = Counter(
    "agent_obs_vault_audit_write_total",
    "Audit event write attempts by status",
    ["status"],
)


class MFARequiredError(Exception):
    """Raised when MFA verification fails or is required for vault recovery."""
    pass


@dataclass
class MFAConfig:
    """Configuration for TOTP MFA verification."""
    
    totp_secrets: dict[str, str]  # login -> totp_secret
    authorized_logins: list[str]  # list of authorized engineer emails
    max_attempts: int = 3
    cooldown_seconds: int = 300  # 5 minutes
    
    @classmethod
    def from_env(cls) -> Optional["MFAConfig"]:
        """Load MFA config from environment variables."""
        # In production, these would be loaded from secure config files
        # For CRITICAL level, we keep them in env for security
        totp_secrets = {}
        authorized_logins = []
        
        # Parse from environment: VAULT_MFA_SECRETS="alice:secret1,bob:secret2"
        if secrets_str := os.environ.get("VAULT_MFA_SECRETS"):
            for item in secrets_str.split(","):
                if ":" in item:
                    login, secret = item.split(":", 1)
                    totp_secrets[login] = secret
        
        # Parse from environment: VAULT_MFA_AUTHORIZED="alice@company.com,bob@company.com"
        if authorized_str := os.environ.get("VAULT_MFA_AUTHORIZED"):
            authorized_logins = [login.strip() for login in authorized_str.split(",")]
        
        if not totp_secrets or not authorized_logins:
            return None
            
        return cls(
            totp_secrets=totp_secrets,
            authorized_logins=authorized_logins,
            max_attempts=int(os.environ.get("VAULT_MFA_MAX_ATTEMPTS", "3")),
            cooldown_seconds=int(os.environ.get("VAULT_MFA_COOLDOWN_SECONDS", "300")),
        )


@dataclass
class _VaultEntry:
    mask: str
    original: str
    vault_key: str
    created_at: float
    ttl_seconds: int

    @property
    def expired(self) -> bool:
        return time.time() > self.created_at + self.ttl_seconds


def _resolve_vault_addrs(
    addr: str | None = None,
    addrs: list[str] | str | None = None,
) -> list[str]:
    """Build the ordered Vault address list (PP37).

    Priority: explicit ``addrs`` argument > ``VAULT_ADDRS`` env > explicit
    ``addr`` argument > ``VAULT_ADDR`` env > pre-PP37 default.

    ``VAULT_ADDRS`` is a comma-separated list of full URLs, e.g.
    ``https://vault-1:8200,https://vault-2:8200,https://vault-3:8200``.
    """
    if addrs is None:
        raw = os.environ.get("VAULT_ADDRS", "").strip()
        addrs = [part.strip() for part in raw.split(",") if part.strip()] if raw else None

    if not addrs:
        single = addr or os.environ.get("VAULT_ADDR", "http://127.0.0.1:8200")
        return [single]

    if isinstance(addrs, str):
        return [part.strip() for part in addrs.split(",") if part.strip()]
    return list(addrs)


def _vault_failover_errors() -> tuple[type[BaseException], ...]:
    """Connection-level errors that justify trying the next Vault replica.

    ``hvac.exceptions.InvalidRequest`` / ``ServerError`` / ``Forbidden`` are
    deliberately absent: a server-side error would fail on every replica and
    retrying only burns time and obscures the real error.
    """
    import requests

    return (
        ConnectionError,
        TimeoutError,
        OSError,
        requests.ConnectionError,
        requests.Timeout,
    )


class VaultClient:
    """HashiCorp Vault client for storing mask → original PII mappings with TTL.

    Uses hvac library to communicate with HashiCorp Vault via HTTP API.
    Supports TTL expiration and batch operations for performance.
    PP37 adds replica failover (mirrors HotStore._with_failover, PP35) and
    optional mTLS client certificates.

    Parameters
    ----------
    addr:
        Vault server address (e.g., "http://127.0.0.1:8200"). Used when
        ``addrs`` is not given.
    addrs:
        Ordered list of Vault addresses for failover. Overrides ``addr``
        and the ``VAULT_ADDRS`` / ``VAULT_ADDR`` env vars.
    token:
        Vault auth token for authentication.
    namespace:
        Vault namespace for enterprise (optional).
    verify_tls:
        Whether to verify TLS certificates (default True for production).
        Set to False for development with self-signed certificates.
        When True and ``VAULT_TLS_CA`` (or ``VAULT_CACERT``) points to a CA
        bundle, that bundle is used for verification.
    """

    def __init__(
        self,
        addr: str = "http://127.0.0.1:8200",
        token: str = "",
        namespace: str = "",
        verify_tls: bool = True,
        mfa_config: Optional[MFAConfig] = None,
        redis_url: Optional[str] = None,
        audit_store: Optional[HotStore] = None,
        audit_enabled: bool = True,
        jwt: str = "",
        addrs: Optional[list[str] | str] = None,
        circuit_breaker: Any | None = None,
    ) -> None:
        self._addrs = _resolve_vault_addrs(addr, addrs)
        self.addr = self._addrs[0]
        self.token = token
        self.namespace = namespace
        self.verify_tls = verify_tls
        self.mfa_config = mfa_config or MFAConfig.from_env()
        self.redis_url = redis_url or os.environ.get("VAULT_MFA_REDIS_URL")
        self._audit_store = audit_store
        self._audit_enabled = audit_enabled
        self.jwt = jwt or os.environ.get("VAULT_JWT", "")
        # mTLS material (PP37): both must be set to enable client certs.
        self._client_cert = os.environ.get("VAULT_CLIENT_CERT") or None
        self._client_key = os.environ.get("VAULT_CLIENT_KEY") or None
        self._ca_bundle = os.environ.get("VAULT_TLS_CA") or os.environ.get("VAULT_CACERT") or None
        # Failover state (PP37)
        self._active_idx = 0
        self._dead_until: dict[int, float] = {}
        self._clients: dict[int, hvac.Client] = {}
        self._health_cache: dict = {"available": False, "checked_at": 0}
        self._health_cache_ttl = 30  # Cache health check for 30 seconds
        # PP38: circuit breaker — after N consecutive all-address failures
        # (or sealed/uninitialized responses that surface as connection-level
        # errors through _with_failover), fail fast instead of re-probing every
        # address on each store()/recover() call.
        if circuit_breaker is None:
            from agent_obs.ha.health_checks import circuit_breaker_for

            circuit_breaker = circuit_breaker_for("vault")
        self._breaker = circuit_breaker

    def _hvac_verify(self) -> bool | str:
        """Resolve the ``verify`` argument for hvac/requests."""
        if not self.verify_tls:
            return False
        return self._ca_bundle or True

    def _hvac_cert(self) -> tuple[str, str] | None:
        if self._client_cert and self._client_key:
            return (self._client_cert, self._client_key)
        return None

    def _connect(self, addr: str) -> hvac.Client:
        client = hvac.Client(
            url=addr,
            token=self.token,
            namespace=self.namespace,
            verify=self._hvac_verify(),
            cert=self._hvac_cert(),
        )
        # Use JWT authentication if provided instead of static token
        if self.jwt:
            try:
                role = "default"
                if hasattr(self, "_current_tenant") and self._current_tenant:
                    role = self._current_tenant
                client.auth.jwt.login(jwt=self.jwt, role=role)
                logger.debug("JWT login successful for role: %s", role)
            except Exception as e:
                logger.error("JWT login failed: %s", str(e))
                raise ConnectionError(f"JWT authentication failed: {str(e)}")
        return client

    def _get_client(self) -> hvac.Client:
        """Return the client for the active address (no failover).

        Prefer :meth:`_with_failover` for operations that should survive a
        single node being down.
        """
        return self._client_for(self._active_idx)

    def _client_for(self, idx: int) -> hvac.Client:
        client = self._clients.get(idx)
        if client is None:
            client = self._connect(self._addrs[idx])
            self._clients[idx] = client
        self._active_idx = idx
        return client

    def _drop_client(self, idx: int) -> None:
        client = self._clients.pop(idx, None)
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001, S110 — best-effort teardown
                pass

    def _select_addr(self, exclude: set[int] | None = None) -> int:
        """Pick the next address to use, skipping cooled-down and excluded ones."""
        exclude = exclude or set()
        n = len(self._addrs)
        now = time.monotonic()
        for offset in range(n):
            idx = (self._active_idx + offset) % n
            if idx in exclude:
                continue
            if self._dead_until.get(idx, 0.0) > now:
                continue
            return idx
        for offset in range(n):
            idx = (self._active_idx + offset) % n
            if idx not in exclude:
                return idx
        return self._active_idx

    def _mark_dead(self, idx: int) -> None:
        self._dead_until[idx] = time.monotonic() + REPLICA_COOLDOWN_SECONDS

    def _with_failover(self, fn: Callable[[hvac.Client], T]) -> T:
        """Run ``fn(client)`` against the active address, failing over on
        connection-level errors (PP37).

        Each address is tried at most once per call. If every address fails,
        the last connection error propagates — callers see the same exception
        type they would have seen pre-PP37 from a single-host client.

        PP38: gated by the ``vault`` circuit breaker — while open, calls
        raise CircuitOpenError immediately (guardrail treats it like any
        other vault failure: fail-fast, recovery unavailable).
        """
        from agent_obs.ha.health_checks import CircuitOpenError

        if not self._breaker.allow_request():
            raise CircuitOpenError(self._breaker.dependency)
        tried: set[int] = set()
        last_exc: BaseException | None = None
        failover_errors = _vault_failover_errors()
        for _ in range(len(self._addrs)):
            idx = self._select_addr(exclude=tried)
            tried.add(idx)
            try:
                result = fn(self._client_for(idx))
            except failover_errors as exc:
                last_exc = exc
                self._mark_dead(idx)
                self._drop_client(idx)
                remaining = len(self._addrs) - len(tried)
                logger.warning(
                    "Vault operation failed on %s (%s: %s) — %s",
                    self._addrs[idx],
                    type(exc).__name__,
                    exc,
                    f"failing over, {remaining} address(es) left" if remaining else "no addresses left",
                )
                continue
            self._breaker.record_success()
            return result
        assert last_exc is not None  # loop always runs >= 1 iteration
        self._breaker.record_failure()
        raise last_exc
    
    def _get_audit_store(self) -> HotStore:
        """Get or create audit store instance."""
        if self._audit_store is None and self._audit_enabled:
            from agent_obs.storage.hot import HotStore
            self._audit_store = HotStore()
        return self._audit_store

    async def store(
        self,
        mask: str,
        original: str,
        ttl_seconds: int = 86400,
        tenant_id: str = "",
    ) -> str:
        """Store a mask → original mapping and return a vault_key.

        Parameters
        ----------
        mask:
            The masked token, e.g. ``"[EMAIL:5f3a]"``.
        original:
            The plaintext value that was masked.
        ttl_seconds:
            Time-to-live in seconds (default 24 h).
        tenant_id:
            Optional tenant identifier for per-tenant isolation and encryption.
            If empty, uses legacy flat path and no transit encryption.

        Returns
        -------
        str
            A vault_key that can be passed to :meth:`recover`.
            Returns None if Vault is unavailable.
        """
        # Validate tenant if provided
        if tenant_id:
            _validate_tenant_segment(tenant_id)
            # Set current tenant for JWT role selection
            self._current_tenant = tenant_id
            vault_key = f"pii/{tenant_id}/{uuid.uuid4().hex[:8]}/{int(time.time())}"
        else:
            vault_key = f"pii/{uuid.uuid4().hex[:8]}/{int(time.time())}"
        
        try:
            def _do_store(client: hvac.Client) -> str:
                if not client.is_authenticated():
                    raise ConnectionError("Vault client not authenticated")

                data = {
                    "data": {
                        "mask": mask,
                        "created_at": time.time(),
                        "ttl_seconds": ttl_seconds,
                    }
                }

                if tenant_id and os.environ.get("AGENT_OBS_VAULT_TRANSIT_ENABLED", "1") == "1":
                    transit_key = f"{tenant_id}_pii_key"
                    encrypt_response = client.secrets.transit.encrypt_data(
                        key_name=transit_key,
                        plaintext=base64.b64encode(original.encode()).decode()
                    )
                    data["data"]["ciphertext"] = encrypt_response["data"]["ciphertext"]
                    logger.debug("vault.store encrypted with key: %s", transit_key)
                else:
                    data["data"]["original"] = original

                client.secrets.kv.v2.create_or_update_secret(
                    path=vault_key,
                    secret=data,
                    cas=0,
                )
                return vault_key

            result = self._with_failover(_do_store)
            logger.debug("vault.store key=%s ttl=%d", result, ttl_seconds)
            return result

        except VaultError as e:
            logger.error("vault.store failed for mask=%s: %s", mask, str(e))
            return None
        except Exception as e:
            logger.error("vault.store unexpected error for mask=%s: %s", mask, str(e))
            return None

    async def store_batch(
        self,
        items: list[tuple[str, str]],
        ttl_seconds: int = 86400,
    ) -> dict[str, str]:
        """Store multiple mask → original mappings concurrently.

        Parameters
        ----------
        items:
            List of (mask, original) tuples to store.
        ttl_seconds:
            Time-to-live in seconds (default 24 h).

        Returns
        -------
        dict[str, str]
            Dictionary mapping mask → vault_key for successful stores.
            Failed stores are simply omitted from the result.
        """
        results = {}

        async def store_single(item: tuple[str, str]) -> tuple[str, str, Optional[str]]:
            mask, original = item
            vault_key = await self.store(mask, original, ttl_seconds)
            return mask, original, vault_key

        # Store all items concurrently
        tasks = [store_single(item) for item in items]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in batch_results:
            if isinstance(result, Exception):
                logger.warning("vault.store_batch item failed: %s", str(result))
                continue

            mask, original, vault_key = result
            if vault_key is not None:
                results[mask] = vault_key

        logger.debug("vault.store_batch stored %d/%d items", len(results), len(items))
        return results

    async def recover(
        self,
        vault_key: str,
        mfa_token: str = "",
        login: str = "",
        reason: str = "",
        trace_id: str = "",
        ip_address: str = "",
        user_agent: str = "",
        tenant_id: str = "",
    ) -> str:
        """Recover the original PII value for a given vault_key.

        Parameters
        ----------
        vault_key:
            The vault_key returned by :meth:`store`.
        mfa_token:
            TOTP token for MFA verification. Required if MFA is configured.
        login:
            Engineer's login/email for rate-limiting and MFA verification.
        reason:
            Engineer-provided reason for recovery (required).
        trace_id:
            Optional trace_id for correlation.
        ip_address:
            Client IP address (default "unknown").
        user_agent:
            Client user agent (default "cli").

        Returns
        -------
        str
            The original PII value.

        Raises
        ------
        AuditReasonRequiredError
            If reason is not provided or empty.
        KeyError
            If the vault_key does not exist or has expired.
        ConnectionError
            If Vault is unreachable.
        MFARequiredError
            If MFA is required and token is missing or invalid.
        AuditWriteError
            If writing audit event to ClickHouse fails.
        """
        # Validate reason is required
        if not reason.strip():
            raise AuditReasonRequiredError(
                "Recovery reason is required for audit compliance. "
                "Provide --reason flag in CLI or reason parameter in code."
            )
        
        # MFA verification before accessing Vault
        if self.mfa_config:
            if not login:
                raise ValueError("login is required when MFA is enabled")
            
            # Check if empty token was provided
            if not mfa_token:
                raise MFARequiredError("MFA verification required for vault recovery")
            
            await self.verify_mfa(login, mfa_token)
        
        # Record recovery start time for metrics
        start_time = time.time()
        
        try:
            # Parse tenant from vault_key path for cross-tenant validation
            # (guard runs BEFORE the read — a denied tenant must never touch the secret)
            path_parts = vault_key.split('/')
            key_tenant = ""
            if len(path_parts) >= 3 and path_parts[1] != uuid.uuid4().hex[:8]:  # Check if second part is not hex (tenant indicator)
                key_tenant = path_parts[1]

            # Cross-tenant access guard
            if tenant_id and key_tenant and tenant_id != key_tenant:
                logger.warning("Cross-tenant access denied: caller=%s, key=%s", tenant_id, key_tenant)
                # Write deny audit event before raising
                audit_store = self._get_audit_store()
                if audit_store:
                    try:
                        audit_event = RecoveryAuditEvent.for_vault_recovery(
                            vault_key=vault_key,
                            login=login or "unknown",
                            reason=reason,
                            trace_id=trace_id,
                            ip_address=ip_address,
                            user_agent=user_agent,
                            tenant_id=tenant_id,
                            mask="REDACTED"  # Mask sensitive info in deny event
                        )
                        loop = asyncio.get_running_loop()
                        await loop.run_in_executor(None, audit_store.write_audit_event, audit_event)
                        vault_audit_write_total.labels(status="deny").inc()
                    except Exception as e:
                        logger.warning("Failed to write deny audit event: %s", str(e))
                raise PermissionError(f"Cross-tenant access denied: cannot access {key_tenant} as {tenant_id}")

            def _do_read(client: hvac.Client) -> dict:
                if not client.is_authenticated():
                    raise ConnectionError("Vault client not authenticated")
                return client.secrets.kv.v2.read_secret_version(path=vault_key)["data"]["data"]

            data = self._with_failover(_do_read)

            created_at = data["created_at"]
            ttl_seconds = data.get("ttl_seconds", 86400)

            # Check TTL expiration
            if time.time() > created_at + ttl_seconds:
                logger.warning("vault.recover key=%s expired", vault_key)
                raise KeyError(f"vault_key expired: {vault_key}")

            # Decrypt original if ciphertext is present (tenant-scored with transit)
            if "ciphertext" in data:
                if key_tenant and os.environ.get("AGENT_OBS_VAULT_TRANSIT_ENABLED", "1") == "1":
                    try:
                        transit_key = f"{key_tenant}_pii_key"
                        def _do_decrypt(client: hvac.Client) -> str:
                            resp = client.secrets.transit.decrypt_data(
                                key_name=transit_key,
                                ciphertext=data["ciphertext"]
                            )
                            return base64.b64decode(resp["data"]["plaintext"]).decode()
                        original = self._with_failover(_do_decrypt)
                        logger.debug("vault.recover decrypted with key: %s", transit_key)
                    except Exception as e:
                        logger.error("Transit decryption failed for key %s: %s", transit_key, str(e))
                        raise KeyError(f"Decryption failed for vault_key: {vault_key}")
                else:
                    raise KeyError(f"Ciphertext found but no transit key available for {key_tenant}")
            else:
                # Legacy plaintext original
                original = data.get("original", "")

            logger.debug("vault.recover key=%s", vault_key)
            
            # Create and write audit event
            audit_store = self._get_audit_store()
            if audit_store:
                audit_event = RecoveryAuditEvent.for_vault_recovery(
                    vault_key=vault_key,
                    login=login or "unknown",
                    reason=reason,
                    trace_id=trace_id,
                    ip_address=ip_address,
                    user_agent=user_agent,
                    tenant_id=tenant_id,
                    mask=data.get("mask", "")
                )
                
                try:
                    # Run in executor to avoid blocking event loop
                    loop = asyncio.get_running_loop()
                    await loop.run_in_executor(None, audit_store.write_audit_event, audit_event)
                    vault_audit_write_total.labels(status="success").inc()
                    logger.debug(
                        "Audit event written: audit_id=%s reason=%s",
                        audit_event.audit_id,
                        reason,
                    )
                except Exception as e:
                    vault_audit_write_total.labels(status="failed").inc()
                    logger.error("Audit write failed: %s", str(e))
                    raise AuditWriteError(f"Failed to write audit event: {str(e)}")
            
            # Record metrics
            duration = time.time() - start_time
            if audit_store and audit_event:
                vault_recovery_total.labels(reason_category=audit_event.reason_category).inc()
            else:
                vault_recovery_total.labels(reason_category="unknown").inc()
            vault_recovery_duration_seconds.observe(duration)
            
            # PP23: после восстановления PII инвалидировать drill-down кеш
            # для (login, trace_id) — восстановленные данные могут отличаться
            # от закешированных. Best-effort: ошибка не должна ломать recovery.
            if trace_id:
                try:
                    from agent_obs.storage.drill_down import invalidate_drill_down_cache
                    import redis.asyncio as redis_async
                    if self.redis_url:
                        r = redis_async.from_url(self.redis_url)
                        await invalidate_drill_down_cache(r, trace_id, user=login)
                        await r.aclose()
                except Exception as e:
                    logger.warning(
                        "drill-down cache invalidation skipped (best-effort): "
                        "trace=%s user=%s err=%s", trace_id, login, e,
                    )
            
            return original

        except AuditWriteError:
            # Domain error from the audit sink — do not wrap as Vault connectivity.
            raise
        except MFARequiredError:
            raise
        except KeyError as e:
            # Re-raise KeyError as-is (for expired or not found)
            raise e
        except Exception as e:
            if "not found" in str(e).lower():
                raise KeyError(f"vault_key not found: {vault_key}")
            logger.error("vault.recover failed for key=%s: %s", vault_key, str(e))
            raise ConnectionError(f"Vault error: {str(e)}")

    async def verify_mfa(self, login: str, mfa_token: str) -> bool:
        """Verify TOTP token for the given login.
        
        Parameters
        ----------
        login:
            Engineer's login/email
        mfa_token:
            TOTP token to verify
            
        Returns
        -------
        bool
            True if verification successful
            
        Raises
        ------
        MFARequiredError
            If token is invalid or rate limit exceeded
        """
        if not self.mfa_config:
            return True
            
        # Check if login is authorized
        if login not in self.mfa_config.authorized_logins:
            raise MFARequiredError(f"Login '{login}' not authorized for MFA")
        
        # Check rate limit first
        if await self._check_rate_limit(login):
            raise MFARequiredError(f"Rate limit exceeded for login '{login}'")
        
        # Get TOTP secret for this login
        if login not in self.mfa_config.totp_secrets:
            raise MFARequiredError(f"No TOTP secret found for login '{login}'")
        
        totp_secret = self.mfa_config.totp_secrets[login]
        totp = pyotp.TOTP(totp_secret)
        
        # Verify with window=1 (±30 seconds tolerance)
        if not totp.verify(mfa_token, valid_window=1):
            # Rate limit on failed attempt
            await self._record_failed_attempt(login)
            raise MFARequiredError("Invalid MFA token")
        
        # Reset rate limit on successful verification
        await self._reset_rate_limit(login)
        return True

    async def _check_rate_limit(self, login: str) -> bool:
        """Check if rate limit would be exceeded for this login."""
        if not self.redis_url:
            return False
            
        try:
            import redis.asyncio as redis
            r = redis.from_url(self.redis_url)
            key = f"rate_limit:vault:{login}"
            
            # Get current count
            count = await r.incr(key)
            if count == 1:
                # Set expiration on first increment
                await r.expire(key, self.mfa_config.cooldown_seconds)
            
            return count > self.mfa_config.max_attempts
            
        except Exception as e:
            logger.warning("Rate limit check failed for %s: %s", login, str(e))
            # If Redis fails, we allow the attempt but log the failure
            return False

    async def _record_failed_attempt(self, login: str) -> None:
        """Record a failed MFA attempt."""
        if not self.redis_url:
            return
            
        try:
            import redis.asyncio as redis
            r = redis.from_url(self.redis_url)
            key = f"rate_limit:vault:{login}"
            
            await r.incr(key)
            await r.expire(key, self.mfa_config.cooldown_seconds)
            
        except Exception as e:
            logger.warning("Failed to record failed attempt for %s: %s", login, str(e))

    async def _reset_rate_limit(self, login: str) -> None:
        """Reset rate limit for a successful login."""
        if not self.redis_url:
            return
            
        try:
            import redis.asyncio as redis
            r = redis.from_url(self.redis_url)
            key = f"rate_limit:vault:{login}"
            
            await r.delete(key)
            
        except Exception as e:
            logger.warning("Failed to reset rate limit for %s: %s", login, str(e))

    def is_available(self) -> bool:
        """Return True if Vault backend is reachable (any address, PP37).

        Uses a cached health check result for 30 seconds to avoid hammering Vault.
        """
        now = time.time()
        if now - self._health_cache["checked_at"] < self._health_cache_ttl:
            return self._health_cache["available"]

        try:
            def _health(client: hvac.Client) -> bool:
                response = client.sys.health_check()
                return response.get("initialized", False) and response.get("sealed", False) is False

            available = self._with_failover(_health)

            self._health_cache = {
                "available": available,
                "checked_at": now,
            }
            
            return available
        except Exception as e:
            logger.debug("vault health check failed: %s", str(e))
            self._health_cache = {
                "available": False,
                "checked_at": now,
            }
            return False

    async def __aenter__(self) -> VaultClient:
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """Async context manager exit."""
        for idx in list(self._clients):
            self._drop_client(idx)


# ---------------------------------------------------------------------------
# CLI functionality for PC19
# ---------------------------------------------------------------------------

def generate_totp_secret(login: str) -> str:
    """Generate TOTP secret for an engineer and print QR code."""
    totp_secret = pyotp.random_base32()
    
    print(f"\n=== TOTP Secret Generation for {login} ===")
    print(f"Login: {login}")
    print(f"TOTP Secret: {totp_secret}")
    
    # Generate QR code for Google Authenticator
    totp = pyotp.TOTP(totp_secret)
    provisioning_url = totp.provisioning_uri(name=login, issuer_name="Agent-Obs")
    
    print(f"\nQR Code URL (for manual setup):")
    print(provisioning_url)
    
    print(f"\nSetup Instructions:")
    print("1. Open Google Authenticator app on your phone")
    print("2. Tap '+' and select 'Scan a barcode' (or 'Enter a setup key')")
    print("3. Scan the QR code or manually enter the secret above")
    print("4. Test with the current time-based code")
    print("5. Save the secret securely (this is the only time it will be shown)")
    
    return totp_secret


async def main() -> None:
    """CLI entry point for vault client operations."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="HashiCorp Vault client with MFA support for agent-obs"
    )
    
    subparsers = parser.add_subparsers(dest="command", required=True)
    
    # issue-mfa command (existing)
    issue_mfa_parser = subparsers.add_parser("issue-mfa", help="Generate TOTP secret for engineer")
    issue_mfa_parser.add_argument(
        "--login",
        help="Engineer's login/email for MFA operations",
        required=True
    )
    
    # health command (existing)
    health_parser = subparsers.add_parser("health", help="Check vault availability")
    
    # recover command (PC20)
    recover_parser = subparsers.add_parser("recover", help="Recover PII from vault")
    recover_parser.add_argument(
        "--key",
        help="Vault key for the PII to recover",
        required=True
    )
    recover_parser.add_argument(
        "--mfa",
        help="TOTP token for MFA verification",
        default=""
    )
    recover_parser.add_argument(
        "--login",
        help="Engineer's login/email for MFA verification",
        required=True
    )
    recover_parser.add_argument(
        "--reason",
        help="Reason for recovery (required for audit compliance)",
        required=True
    )
    recover_parser.add_argument(
        "--trace-id",
        help="Trace ID for correlation (optional)",
        default=""
    )
    recover_parser.add_argument(
        "--ip",
        help="Client IP address (optional)",
        default=""
    )
    recover_parser.add_argument(
        "--ua",
        help="Client user agent (optional)",
        default=""
    )
    recover_parser.add_argument(
        "--tenant",
        help="Tenant ID for cross-tenant access validation (optional)",
        default=""
    )
    
    args = parser.parse_args()
    
    if args.command == "issue-mfa":
        # Generate and display TOTP secret
        secret = generate_totp_secret(args.login)
        
        # In production, this would be stored securely
        print(f"\nEnvironment Variable Setup:")
        print(f"Add to your .env: VAULT_MFA_SECRETS='{args.login}:{secret}'")
        print(f"Add to your .env: VAULT_MFA_AUTHORIZED='{args.login}'")
        
    elif args.command == "health":
        # Simple health check for vault
        client = VaultClient()
        if client.is_available():
            print("Vault is healthy and available")
        else:
            print("Vault is not available")
            exit(1)
    
    elif args.command == "recover":
        # Create client with audit enabled
        client = VaultClient()
        
        try:
            # Perform recovery
            original = await client.recover(
                vault_key=args.key,
                mfa_token=args.mfa,
                login=args.login,
                reason=args.reason,
                trace_id=args.trace_id,
                ip_address=args.ip,
                user_agent=args.ua,
                tenant_id=args.tenant,
            )
            
            print(f"Recovered PII: {original}")
            
        except AuditReasonRequiredError as e:
            print(f"Error: {e}")
            exit(1)
        except MFARequiredError as e:
            print(f"Error: {e}")
            exit(1)
        except KeyError as e:
            print(f"Error: {e}")
            exit(1)
        except Exception as e:
            print(f"Error: {e}")
            exit(1)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())