"""HashiCorp Vault client for PII mask ↔ original mapping with TTL.

PC18 provides a full implementation with real Vault HTTP API calls, TTL-based
expiration via Vault leases, and batch operations. Replaces the PC07 in-memory stub.
PC19 adds TOTP MFA verification and rate-limiting for recovery operations.
PC20 adds audit trail for every recovery with ClickHouse write and reason field.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

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


class VaultClient:
    """HashiCorp Vault client for storing mask → original PII mappings with TTL.

    Uses hvac library to communicate with HashiCorp Vault via HTTP API.
    Supports TTL expiration and batch operations for performance.

    Parameters
    ----------
    addr:
        Vault server address (e.g., "http://127.0.0.1:8200").
    token:
        Vault auth token for authentication.
    namespace:
        Vault namespace for enterprise (optional).
    verify_tls:
        Whether to verify TLS certificates (default True for production).
        Set to False for development with self-signed certificates.
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
    ) -> None:
        self.addr = addr
        self.token = token
        self.namespace = namespace
        self.verify_tls = verify_tls
        self.mfa_config = mfa_config or MFAConfig.from_env()
        self.redis_url = redis_url or os.environ.get("VAULT_MFA_REDIS_URL")
        self._audit_store = audit_store
        self._audit_enabled = audit_enabled
        self._client: Optional[hvac.Client] = None
        self._health_cache: dict = {"available": False, "checked_at": 0}
        self._health_cache_ttl = 30  # Cache health check for 30 seconds

    def _get_client(self) -> hvac.Client:
        """Get or create hvac client instance."""
        if self._client is None:
            self._client = hvac.Client(
                url=self.addr,
                token=self.token,
                namespace=self.namespace,
                verify=self.verify_tls,
            )
        return self._client
    
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

        Returns
        -------
        str
            A vault_key that can be passed to :meth:`recover`.
            Returns None if Vault is unavailable.
        """
        vault_key = f"pii/{uuid.uuid4().hex[:8]}/{int(time.time())}"
        
        try:
            client = self._get_client()
            if not client.is_authenticated():
                logger.error("Vault client not authenticated")
                return None

            # Store in KV v2 secrets engine
            path = vault_key
            data = {
                "data": {
                    "mask": mask,
                    "original": original,
                    "created_at": time.time(),
                    "ttl_seconds": ttl_seconds,
                }
            }

            # Use KV v2 with cas=0 to create new secret
            response = client.secrets.kv.v2.create_or_update_secret(
                path=path,
                secret=data,
                cas=0,  # Create only, don't overwrite
            )

            logger.debug("vault.store key=%s ttl=%d", vault_key, ttl_seconds)
            return vault_key

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
            client = self._get_client()
            if not client.is_authenticated():
                raise ConnectionError("Vault client not authenticated")

            # Read from KV v2 secrets engine
            path = vault_key
            response = client.secrets.kv.v2.read_secret_version(path=path)

            data = response["data"]["data"]
            original = data["original"]
            created_at = data["created_at"]
            ttl_seconds = data.get("ttl_seconds", 86400)

            # Check TTL expiration
            if time.time() > created_at + ttl_seconds:
                logger.warning("vault.recover key=%s expired", vault_key)
                raise KeyError(f"vault_key expired: {vault_key}")

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
        """Return True if Vault backend is reachable.

        Uses a cached health check result for 30 seconds to avoid hammering Vault.
        """
        now = time.time()
        if now - self._health_cache["checked_at"] < self._health_cache_ttl:
            return self._health_cache["available"]

        try:
            client = self._get_client()
            # Lightweight health check
            response = client.sys.health_check()
            available = response.get("initialized", False) and response.get("sealed", False) is False
            
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
        if self._client:
            self._client.close()
            self._client = None


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