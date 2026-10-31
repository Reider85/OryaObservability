"""PP37 — VaultClient multi-address failover.

Covers address-list parsing (``VAULT_ADDRS``), failover on connection-level
errors, sticky behaviour after failover, cooldown, and the critical invariant
that server-side errors are NEVER retried on another address.

The hvac client is mocked throughout — no real Vault is involved, so this
file runs in the fast suite.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from agent_obs.guardrail.vault_client import (
    REPLICA_COOLDOWN_SECONDS,
    VaultClient,
    _resolve_vault_addrs,
    _vault_failover_errors,
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("VAULT_ADDRS", raising=False)
    monkeypatch.delenv("VAULT_ADDR", raising=False)
    monkeypatch.delenv("VAULT_CLIENT_CERT", raising=False)
    monkeypatch.delenv("VAULT_CLIENT_KEY", raising=False)
    monkeypatch.delenv("VAULT_TLS_CA", raising=False)
    monkeypatch.delenv("VAULT_CACERT", raising=False)


@pytest.fixture
def make_client(request):
    """Factory: ``make_client({addr: mock_client, ...})`` → VaultClient.

    hvac.Client is patched for the whole test so lazy connects are covered.
    """

    def _make(addr_clients: dict[str, MagicMock]) -> VaultClient:
        def factory(url=None, **kwargs):
            if url not in addr_clients:
                raise AssertionError(f"unexpected connect to {url}")
            return addr_clients[url]

        patcher = patch("hvac.Client", side_effect=factory)
        patcher.start()
        request.addfinalizer(patcher.stop)
        return VaultClient(addrs=list(addr_clients), token="test-token", verify_tls=False)

    return _make


class TestAddrParsing:
    def test_single_addr_fallback(self):
        with patch.dict(os.environ, {"VAULT_ADDR": "http://vault-a:8200"}, clear=False):
            assert _resolve_vault_addrs() == ["http://vault-a:8200"]

    def test_env_addr_list(self):
        env = {"VAULT_ADDRS": "https://v-1:8200, https://v-2:8200,https://v-3:8200"}
        with patch.dict(os.environ, env, clear=False):
            assert _resolve_vault_addrs() == [
                "https://v-1:8200",
                "https://v-2:8200",
                "https://v-3:8200",
            ]

    def test_explicit_addrs_arg_beats_env(self):
        with patch.dict(os.environ, {"VAULT_ADDRS": "http://env:8200"}, clear=False):
            assert _resolve_vault_addrs(addrs=["http://arg:8200"]) == ["http://arg:8200"]

    def test_explicit_addr_beats_default(self):
        assert _resolve_vault_addrs(addr="http://explicit:8200") == ["http://explicit:8200"]

    def test_default_when_nothing_set(self):
        assert _resolve_vault_addrs() == ["http://127.0.0.1:8200"]

    def test_client_defaults_to_single_addr(self):
        client = VaultClient()
        assert client._addrs == ["http://127.0.0.1:8200"]
        assert client.addr == "http://127.0.0.1:8200"

    def test_client_takes_addrs_from_env(self):
        with patch.dict(os.environ, {"VAULT_ADDRS": "http://a:8200,http://b:8200"}, clear=False):
            client = VaultClient()
        assert client._addrs == ["http://a:8200", "http://b:8200"]


class TestFailoverOnOp:
    def test_op_fails_over_to_second_addr(self, make_client):
        """addr-1 dies → op succeeds via addr-2 (PP37 DoD)."""
        dead = MagicMock()
        dead.is_authenticated.side_effect = requests.ConnectionError("refused")
        alive = MagicMock()
        alive.is_authenticated.return_value = True

        client = make_client({"http://v-1:8200": dead, "http://v-2:8200": alive})

        result = client._with_failover(lambda c: c.is_authenticated())

        assert result is True
        dead.is_authenticated.assert_called_once()
        alive.is_authenticated.assert_called_once()
        # Sticky: subsequent ops go straight to addr-2 without touching addr-1.
        client._with_failover(lambda c: c.is_authenticated())
        assert dead.is_authenticated.call_count == 1
        assert alive.is_authenticated.call_count == 2

    def test_failover_marks_addr_dead_with_cooldown(self, make_client):
        dead = MagicMock()
        dead.is_authenticated.side_effect = requests.ConnectionError("refused")
        alive = MagicMock()
        alive.is_authenticated.return_value = True
        client = make_client({"http://v-1:8200": dead, "http://v-2:8200": alive})

        client._with_failover(lambda c: c.is_authenticated())

        assert 0 in client._dead_until
        assert client._dead_until[0] > 0

    def test_dead_addr_retried_after_cooldown(self, make_client):
        """After the cooldown expires the address is eligible again."""
        v1 = MagicMock()
        v1.is_authenticated.side_effect = requests.ConnectionError("refused")
        v2 = MagicMock()
        v2.is_authenticated.return_value = True
        client = make_client({"http://v-1:8200": v1, "http://v-2:8200": v2})

        client._with_failover(lambda c: c.is_authenticated())  # v-1 dies
        assert 0 in client._dead_until

        # v-1 recovers: clear the error and expire the cooldown.
        v1.is_authenticated.side_effect = None
        v1.is_authenticated.return_value = True
        client._dead_until[0] = 0.0
        client._active_idx = 0
        client._drop_client(0)

        result = client._with_failover(lambda c: c.is_authenticated())
        assert result is True
        assert v1.is_authenticated.call_count == 2

    def test_all_addrs_down_raises_last_error(self, make_client):
        first = MagicMock()
        first.is_authenticated.side_effect = requests.ConnectionError("refused")
        second = MagicMock()
        second.is_authenticated.side_effect = TimeoutError("timed out")
        client = make_client({"http://v-1:8200": first, "http://v-2:8200": second})

        with pytest.raises((requests.ConnectionError, TimeoutError)):
            client._with_failover(lambda c: c.is_authenticated())

        first.is_authenticated.assert_called_once()
        second.is_authenticated.assert_called_once()

    def test_failover_does_not_loop_more_than_addr_count(self, make_client):
        flaky = MagicMock()
        flaky.is_authenticated.side_effect = requests.ConnectionError("refused")
        client = make_client({"http://v-1:8200": flaky})

        with pytest.raises(requests.ConnectionError):
            client._with_failover(lambda c: c.is_authenticated())
        assert flaky.is_authenticated.call_count == 1

    def test_timeout_triggers_failover(self, make_client):
        dead = MagicMock()
        dead.is_authenticated.side_effect = requests.Timeout("timeout")
        alive = MagicMock()
        alive.is_authenticated.return_value = True
        client = make_client({"http://v-1:8200": dead, "http://v-2:8200": alive})

        assert client._with_failover(lambda c: c.is_authenticated()) is True
        alive.is_authenticated.assert_called_once()


class TestNoFailoverOnServerError:
    def test_vault_error_does_not_failover(self, make_client):
        """A server-side error must surface immediately, not hop addresses."""
        from hvac.exceptions import InvalidRequest

        broken = MagicMock()
        broken.is_authenticated.side_effect = InvalidRequest("no active node")
        healthy = MagicMock()
        client = make_client({"http://v-1:8200": broken, "http://v-2:8200": healthy})

        with pytest.raises(InvalidRequest):
            client._with_failover(lambda c: c.is_authenticated())

        healthy.is_authenticated.assert_not_called()
        assert 0 not in client._dead_until

    def test_forbidden_does_not_failover(self, make_client):
        from hvac.exceptions import Forbidden

        broken = MagicMock()
        broken.is_authenticated.side_effect = Forbidden("permission denied")
        healthy = MagicMock()
        client = make_client({"http://v-1:8200": broken, "http://v-2:8200": healthy})

        with pytest.raises(Forbidden):
            client._with_failover(lambda c: c.is_authenticated())

        healthy.is_authenticated.assert_not_called()


class TestAvailability:
    def test_available_when_any_addr_up(self, make_client):
        dead = MagicMock()
        dead.sys.health_check.side_effect = requests.ConnectionError("refused")
        alive = MagicMock()
        alive.sys.health_check.return_value = {"initialized": True, "sealed": False}
        client = make_client({"http://v-1:8200": dead, "http://v-2:8200": alive})

        assert client.is_available() is True

    def test_unavailable_when_all_down(self, make_client):
        a = MagicMock()
        a.sys.health_check.side_effect = requests.ConnectionError("refused")
        b = MagicMock()
        b.sys.health_check.side_effect = requests.ConnectionError("refused")
        client = make_client({"http://v-1:8200": a, "http://v-2:8200": b})

        assert client.is_available() is False

    def test_unavailable_when_sealed(self, make_client):
        sealed = MagicMock()
        sealed.sys.health_check.return_value = {"initialized": True, "sealed": True}
        client = make_client({"http://v-1:8200": sealed})

        assert client.is_available() is False


class TestMTLS:
    def test_client_cert_from_env(self, monkeypatch):
        monkeypatch.setenv("VAULT_CLIENT_CERT", "/certs/vault.pem")
        monkeypatch.setenv("VAULT_CLIENT_KEY", "/certs/vault-key.pem")
        with patch("hvac.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = VaultClient()
            client._get_client()
        _, kwargs = mock_cls.call_args
        assert kwargs["cert"] == ("/certs/vault.pem", "/certs/vault-key.pem")

    def test_no_client_cert_when_env_unset(self):
        with patch("hvac.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = VaultClient()
            client._get_client()
        _, kwargs = mock_cls.call_args
        assert kwargs["cert"] is None

    def test_ca_bundle_from_env(self, monkeypatch):
        monkeypatch.setenv("VAULT_TLS_CA", "/certs/ca.pem")
        with patch("hvac.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = VaultClient()
            client._get_client()
        _, kwargs = mock_cls.call_args
        assert kwargs["verify"] == "/certs/ca.pem"

    def test_verify_false_overrides_ca(self, monkeypatch):
        monkeypatch.setenv("VAULT_TLS_CA", "/certs/ca.pem")
        with patch("hvac.Client") as mock_cls:
            mock_cls.return_value = MagicMock()
            client = VaultClient(verify_tls=False)
            client._get_client()
        _, kwargs = mock_cls.call_args
        assert kwargs["verify"] is False


class TestCooldownConstant:
    def test_cooldown_is_thirty_seconds(self):
        assert REPLICA_COOLDOWN_SECONDS == 30.0


class TestFailoverErrors:
    def test_connection_errors_included(self):
        errs = _vault_failover_errors()
        assert ConnectionError in errs
        assert TimeoutError in errs
        assert OSError in errs
        assert requests.ConnectionError in errs
        assert requests.Timeout in errs

    def test_server_errors_excluded(self):
        from hvac.exceptions import Forbidden, InternalServerError, InvalidRequest

        errs = _vault_failover_errors()
        assert InvalidRequest not in errs
        assert InternalServerError not in errs
        assert Forbidden not in errs
