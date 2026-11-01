"""PP39 — proxy mode configuration, env-toggle and fail-fast validation.

Covers ProxyConfig loading for on/off/auto, the §0.4 compatibility table
(Mode A / Mode B / invalid combination), the default auto failover probe
(PP38 circuit-breaker registry), and SDK wiring: the proxy object is only
constructed when the config resolves to enabled, and ``start()`` /
``shutdown()`` propagate to it.  ``LLMProxyServer`` is stubbed — the real
server lands in PP40 (``agent_obs/proxy/server.py``).
"""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

from agent_obs.ha import circuit_breaker_for, reset_circuit_breakers
from agent_obs.observability import ObservabilitySDK
from agent_obs.proxy import (
    SUPPORTED_PROVIDERS,
    ConfigError,
    load_proxy_config,
)

_PROXY_ENV_VARS = (
    "AGENT_OBS_PROXY_MODE",
    "AGENT_OBS_PROXY_HOST",
    "AGENT_OBS_PROXY_PORT",
    "AGENT_OBS_PROXY_UPSTREAM_PROVIDER",
    "AGENT_OBS_PROXY_UPSTREAM_BASE_URL",
    "AGENT_OBS_PROXY_UPSTREAM_API_KEY_ENV",
    "AGENT_OBS_PROXY_REQUEST_TIMEOUT_SECONDS",
    "AGENT_OBS_PROXY_STREAMING_ENABLED",
    "AGENT_OBS_PROXY_LOG_BODIES",
    "AGENT_OBS_PROXY_LOG_BODIES_MAX_BYTES",
    "SHARED_PROXY_ENABLED",
    "OPENAI_API_KEY",
)


@pytest.fixture(autouse=True)
def clean_proxy_env(monkeypatch):
    """Neutral ambient state: proxy off, master switch on, no key/shared."""
    for var in _PROXY_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AGENT_OBS_ENABLED", "true")


def _set_key(monkeypatch, value: str = "sk-test") -> None:
    monkeypatch.setenv("OPENAI_API_KEY", value)


class TestProxyConfigLoading:
    def test_default_is_off(self):
        cfg = load_proxy_config()
        assert cfg.mode == "off"
        assert cfg.enabled is False
        assert cfg.listen_host == "127.0.0.1"
        assert cfg.listen_port == 8765
        assert cfg.upstream_provider == "openai"
        assert cfg.log_bodies is True
        assert cfg.log_bodies_max_bytes == 100_000

    def test_mode_on_enables(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        _set_key(monkeypatch)
        cfg = load_proxy_config()
        assert cfg.mode == "on"
        assert cfg.enabled is True

    def test_mode_on_requires_api_key(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        with pytest.raises(ConfigError, match="missing API key"):
            load_proxy_config()

    def test_mode_on_empty_api_key_treated_missing(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        monkeypatch.setenv("OPENAI_API_KEY", "   ")
        with pytest.raises(ConfigError, match="missing API key"):
            load_proxy_config()

    def test_mode_auto_probe_true_enables(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "auto")
        _set_key(monkeypatch)
        cfg = load_proxy_config(auto_probe=lambda: True)
        assert cfg.mode == "auto"
        assert cfg.enabled is True

    def test_mode_auto_probe_false_disables_without_key(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "auto")
        # No API key set: auto-resolved-off must not require credentials.
        cfg = load_proxy_config(auto_probe=lambda: False)
        assert cfg.enabled is False

    def test_invalid_mode_raises(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "sometimes")
        with pytest.raises(ConfigError, match="invalid AGENT_OBS_PROXY_MODE"):
            load_proxy_config()

    def test_host_port_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_HOST", "0.0.0.0")
        monkeypatch.setenv("AGENT_OBS_PROXY_PORT", "9001")
        cfg = load_proxy_config()
        assert cfg.listen_host == "0.0.0.0"
        assert cfg.listen_port == 9001

    def test_log_bodies_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_LOG_BODIES", "false")
        monkeypatch.setenv("AGENT_OBS_PROXY_LOG_BODIES_MAX_BYTES", "4096")
        cfg = load_proxy_config()
        assert cfg.log_bodies is False
        assert cfg.log_bodies_max_bytes == 4096

    def test_timeout_and_streaming_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_REQUEST_TIMEOUT_SECONDS", "30.5")
        monkeypatch.setenv("AGENT_OBS_PROXY_STREAMING_ENABLED", "false")
        cfg = load_proxy_config()
        assert cfg.request_timeout_seconds == 30.5
        assert cfg.streaming_enabled is False

    def test_invalid_port_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_PORT", "not-a-port")
        cfg = load_proxy_config()
        assert cfg.listen_port == 8765

    def test_config_dict_overrides_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "off")
        monkeypatch.setenv("AGENT_OBS_PROXY_PORT", "8000")
        _set_key(monkeypatch)
        cfg = load_proxy_config({"proxy": {"mode": "on", "listen_port": 9999}})
        assert cfg.enabled is True
        assert cfg.listen_port == 9999

    def test_unsupported_provider_raises(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        monkeypatch.setenv("AGENT_OBS_PROXY_UPSTREAM_PROVIDER", "gemini")
        _set_key(monkeypatch)
        with pytest.raises(ConfigError, match="unsupported"):
            load_proxy_config()

    @pytest.mark.parametrize("provider", SUPPORTED_PROVIDERS)
    def test_supported_providers_accepted(self, provider, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        monkeypatch.setenv("AGENT_OBS_PROXY_UPSTREAM_PROVIDER", provider)
        _set_key(monkeypatch)
        cfg = load_proxy_config()
        assert cfg.upstream_provider == provider
        assert cfg.enabled is True


class TestModeCompatibilityTable:
    """The §0.4 table: default / Mode A / Mode B / invalid."""

    def test_default_in_process(self):
        cfg = load_proxy_config()
        assert cfg.enabled is False

    def test_mode_a(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        _set_key(monkeypatch)
        cfg = load_proxy_config()
        assert cfg.enabled is True

    def test_mode_b_allowed(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        monkeypatch.setenv("SHARED_PROXY_ENABLED", "on")
        _set_key(monkeypatch)
        cfg = load_proxy_config()
        assert cfg.enabled is True

    def test_off_with_shared_is_invalid(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "off")
        monkeypatch.setenv("SHARED_PROXY_ENABLED", "on")
        with pytest.raises(ConfigError, match="SHARED_PROXY_ENABLED"):
            load_proxy_config()

    def test_off_with_shared_via_config_dict(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "off")
        with pytest.raises(ConfigError, match="SHARED_PROXY_ENABLED"):
            load_proxy_config({"shared_proxy_enabled": True})

    def test_auto_with_shared_is_allowed(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "auto")
        monkeypatch.setenv("SHARED_PROXY_ENABLED", "on")
        cfg = load_proxy_config(auto_probe=lambda: False)
        assert cfg.enabled is False

    def test_auto_probe_false_skips_key_validation(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "auto")
        cfg = load_proxy_config(auto_probe=lambda: False)
        assert cfg.enabled is False  # no ConfigError despite no key


class TestDefaultAutoProbe:
    def test_probe_false_when_all_breakers_closed(self):
        reset_circuit_breakers()
        from agent_obs.proxy import _default_auto_probe

        assert _default_auto_probe() is False

    def test_probe_true_when_a_breaker_is_open(self, monkeypatch):
        reset_circuit_breakers()
        from agent_obs.proxy import _default_auto_probe

        monkeypatch.setenv("AGENT_OBS_CB_FAILURE_THRESHOLD", "1")
        breaker = circuit_breaker_for("ch")
        breaker.record_failure()
        assert _default_auto_probe() is True

    def test_auto_mode_resolves_via_default_probe(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "auto")
        reset_circuit_breakers()
        monkeypatch.setenv("AGENT_OBS_CB_FAILURE_THRESHOLD", "1")
        # Nothing broken yet -> proxy stays off (no key needed).
        assert load_proxy_config().enabled is False
        circuit_breaker_for("ch").record_failure()
        _set_key(monkeypatch)
        assert load_proxy_config().enabled is True


@pytest.fixture
def stub_server_module(monkeypatch):
    """Inject a stub ``agent_obs.proxy.server`` with a recording LLMProxyServer."""
    mod = ModuleType("agent_obs.proxy.server")

    class LLMProxyServer:
        def __init__(self, config, sdk=None):
            self.config = config
            self.sdk = sdk
            self.started = False
            self.stopped = False

        async def start(self):
            self.started = True

        async def stop(self):
            self.stopped = True

    mod.LLMProxyServer = LLMProxyServer
    monkeypatch.setitem(sys.modules, "agent_obs.proxy.server", mod)
    return mod


class TestSDKWiring:
    def test_no_proxy_when_mode_off(self):
        sdk = ObservabilitySDK([])
        assert sdk._proxy is None
        assert sdk._proxy_config.enabled is False

    def test_proxy_constructed_when_enabled(self, stub_server_module, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        _set_key(monkeypatch)
        sdk = ObservabilitySDK([])
        assert sdk._proxy is not None
        assert isinstance(sdk._proxy, stub_server_module.LLMProxyServer)
        assert sdk._proxy.sdk is sdk
        assert sdk._proxy.config.mode == "on"

    def test_degrades_when_server_module_unimportable(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        _set_key(monkeypatch)
        # A None entry in sys.modules makes `import` raise ImportError —
        # simulates fastapi/uvicorn missing (server.py exists since PP40 but
        # its import must still degrade to _proxy=None instead of raising).
        monkeypatch.setitem(sys.modules, "agent_obs.proxy.server", None)
        sdk = ObservabilitySDK([])
        assert sdk._proxy is None

    async def test_start_and_shutdown_propagate(
        self, stub_server_module, monkeypatch
    ):
        monkeypatch.setenv("AGENT_OBS_PROXY_MODE", "on")
        _set_key(monkeypatch)
        async with ObservabilitySDK([]) as sdk:
            proxy = sdk._proxy
            assert proxy is not None
            assert proxy.started is True  # __aenter__ -> start()
            await sdk.shutdown()
            proxy_after = sdk._proxy
        assert proxy_after is not None
        assert proxy_after.stopped is True

    async def test_start_shutdown_noop_without_proxy(self):
        async with ObservabilitySDK([]) as sdk:
            assert sdk._proxy is None
            await sdk.start()
            await sdk.shutdown()
