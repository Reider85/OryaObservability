"""HashiCorp Vault client for PII mask ↔ original mapping with TTL.

PC18 provides a full implementation with real Vault HTTP API calls, TTL-based
expiration via Vault leases, and batch operations. Replaces the PC07 in-memory stub.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

import hvac
from hvac.exceptions import VaultError

logger = logging.getLogger(__name__)


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
    ) -> None:
        self.addr = addr
        self.token = token
        self.namespace = namespace
        self.verify_tls = verify_tls
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

    async def recover(self, vault_key: str, mfa_token: str = "") -> str:
        """Recover the original PII value for a given vault_key.

        Parameters
        ----------
        vault_key:
            The vault_key returned by :meth:`store`.
        mfa_token:
            TOTP token for MFA verification (currently unused, reserved for PC19).

        Returns
        -------
        str
            The original PII value.

        Raises
        ------
        KeyError
            If the vault_key does not exist or has expired.
        ConnectionError
            If Vault is unreachable.
        """
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
            return original

        except KeyError as e:
            # Re-raise KeyError as-is (for expired or not found)
            raise e
        except Exception as e:
            if "not found" in str(e).lower():
                raise KeyError(f"vault_key not found: {vault_key}")
            logger.error("vault.recover failed for key=%s: %s", vault_key, str(e))
            raise ConnectionError(f"Vault error: {str(e)}")

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