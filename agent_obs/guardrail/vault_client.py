"""HashiCorp Vault client for PII mask ↔ original mapping with TTL.

PC07 provides a minimal in-memory stub so that GuardrailEngine can be tested
end-to-end.  PC18 replaces the storage backend with real Vault HTTP API
calls, TOTP MFA verification, and batch operations.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

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
    """Minimal Vault client storing mask → original PII mappings with TTL.

    In PC07 this is an **in-memory stub**.  The real implementation (PC18)
    talks to HashiCorp Vault over HTTP and requires TOTP MFA for recovery.

    Parameters
    ----------
    addr:
        Vault server address (unused in stub, reserved for PC18).
    token:
        Vault auth token (unused in stub, reserved for PC18).
    namespace:
        Vault namespace for enterprise (unused in stub).
    """

    def __init__(
        self,
        addr: str = "http://127.0.0.1:8200",
        token: str = "",
        namespace: str = "",
    ) -> None:
        self.addr = addr
        self.token = token
        self.namespace = namespace
        self._store: dict[str, _VaultEntry] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

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
        """
        vault_key = f"pii/{uuid.uuid4().hex[:8]}/{int(time.time())}"
        entry = _VaultEntry(
            mask=mask,
            original=original,
            vault_key=vault_key,
            created_at=time.time(),
            ttl_seconds=ttl_seconds,
        )
        self._store[vault_key] = entry
        logger.debug("vault.store key=%s ttl=%d", vault_key, ttl_seconds)
        return vault_key

    async def recover(self, vault_key: str, mfa_token: str = "") -> str:
        """Recover the original PII value for a given vault_key.

        In PC07 this is a stub that always succeeds (no real MFA check).
        PC18 adds TOTP verification and rate-limiting.

        Raises
        ------
        KeyError
            If the vault_key does not exist or has expired.
        """
        entry = self._store.get(vault_key)
        if entry is None:
            raise KeyError(f"vault_key not found: {vault_key}")
        if entry.expired:
            del self._store[vault_key]
            raise KeyError(f"vault_key expired: {vault_key}")
        logger.debug("vault.recover key=%s", vault_key)
        return entry.original

    def is_available(self) -> bool:
        """Return True if Vault backend is reachable (always True for stub)."""
        return True
