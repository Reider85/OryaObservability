"""LLM-proxy mode configuration (PP39 / T3.9.1).

Level-3 observability mode (PRODUCTION-PROMPTS §0.4): the SDK can run an
in-process HTTP proxy in front of the LLM provider so that agents we do not
control still produce spans and cost attributes.  This module owns only the
*configuration* — env-toggle, mode resolution and fail-fast validation.  The
FastAPI server itself is PP40 (``agent_obs/proxy/server.py``); the shared
observability+security pipeline is PP44 (Mode B, ``SHARED_PROXY_ENABLED``).

Modes (§0.4 compatibility table):

===============================  =========================================
``AGENT_OBS_PROXY_MODE``         Behaviour
===============================  =========================================
``off`` (default) + shared off   In-process SDK (MVP/CRITICAL behaviour).
``on`` + shared off              Mode A: proxy all LLM traffic.
``on`` + shared on               Mode B: shared obs+security proxy.
``off`` + shared on              **Invalid** — ``ConfigError`` at startup.
``auto``                         Proxy only when an SDK dependency's
                                 circuit breaker is open (failover).
===============================  =========================================
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: Upstream providers the proxy knows how to forward to (PP40 wires the
#: actual HTTP client per provider).
SUPPORTED_PROVIDERS = ("openai", "anthropic", "azure_openai", "custom")

_DEFAULT_BASE_URL = "https://api.openai.com/v1"

_TRUTHY = ("true", "1", "yes", "on")
_FALSY = ("false", "0", "no", "off")

_MODE_ALIASES = {
    "on": "on", "true": "on", "1": "on", "yes": "on",
    "off": "off", "false": "off", "0": "off", "no": "off",
    "auto": "auto",
}


class ConfigError(ValueError):
    """Invalid proxy-mode combination — raised at SDK construction (fail-fast)."""


def _parse_bool(raw: Any, default: bool) -> bool:
    """Parse an env-style boolean; unknown values fall back to ``default``.

    Already-typed values (bool from config-dict overrides, or the ``default``
    itself) pass through unchanged.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return raw
    value = str(raw).strip().lower()
    if value in _TRUTHY:
        return True
    if value in _FALSY:
        return False
    return default


def _parse_int(raw: Any, default: int, *, name: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using default %d", name, raw, default)
        return default


def _parse_float(raw: Any, default: float, *, name: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r, using default %s", name, raw, default)
        return default


def _parse_downgrade_targets(raw: str) -> dict[str, list[str]]:
    """Parse downgrade_targets JSON configuration."""
    if not raw or raw == "":
        return {}
    
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ConfigError(
                f"downgrade_targets must be a JSON object, "
                f"got {type(parsed).__name__}"
            )
        
        # Validate structure and convert to list[str]
        result = {}
        for model, alternatives in parsed.items():
            if not isinstance(alternatives, list):
                raise ConfigError(
                    f"downgrade_targets['{model}'] must be a list, "
                    f"got {type(alternatives).__name__}"
                )
            result[model] = [str(alt) for alt in alternatives]
        
        return result
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in downgrade_targets: {e}")


def _default_auto_probe() -> bool:
    """Failover probe for ``AGENT_OBS_PROXY_MODE=auto``.

    Returns True when at least one SDK dependency's circuit breaker (PP38)
    is open — i.e. a component is unavailable and the proxy should take over
    the tap.  Reads the process-wide breaker registry synchronously (no
    network I/O).  Full runtime failover with retries/cache lands in PP43;
    this probe only answers "is anything broken right now?".
    """
    from agent_obs.ha.health_checks import (  # lazy: avoid import cost/cycles
        DEPENDENCIES,
        CircuitState,
        circuit_breaker_for,
    )

    return any(
        circuit_breaker_for(dep).state is CircuitState.OPEN
        for dep in DEPENDENCIES
    )


@dataclass
class ProxyConfig:
    """Resolved proxy-mode configuration (PP39).

    ``enabled`` is the single flag the SDK acts on: it is True for
    ``mode="on"``, and for ``mode="auto"`` only when the failover probe
    reports an unavailable component.  Validation of provider / API key is
    performed only when ``enabled`` is True — an ``off`` proxy must never
    force users to configure upstream credentials.
    """

    mode: str = "off"
    enabled: bool = False
    listen_host: str = "127.0.0.1"
    listen_port: int = 8765
    upstream_provider: str = "openai"
    upstream_base_url: str = _DEFAULT_BASE_URL
    upstream_api_key_env: str = "OPENAI_API_KEY"
    request_timeout_seconds: float = 120.0
    upstream_timeout_seconds: float = 5.0
    stream_timeout_seconds: float = 10.0
    streaming_enabled: bool = True
    log_bodies: bool = True
    log_bodies_max_bytes: int = 100_000
    cache_enabled: bool = False
    cache_ttl_seconds: float = 300.0
    
    # PP54: Semantic cache configuration
    semantic_cache_enabled: bool = True
    semantic_cache_redis_url: str = "redis://localhost:6379/0"
    semantic_cache_ttl_seconds: int = 300
    semantic_cache_max_connections: int = 10
    
    # PP44: Shared proxy mode configuration
    shared_proxy_enabled: bool = False
    shared_proxy_security_url: str = ""
    shared_proxy_security_timeout: float = 0.05
    shared_proxy_security_fail_open: bool = True
    
    # PP55: Cost-aware model routing configuration
    cost_aware_routing_enabled: bool = False
    cost_aware_routing_threshold: float = 0.8
    downgrade_targets: dict[str, list[str]] = field(default_factory=dict)
    max_downgrade_attempts: int = 1

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        overrides: Mapping[str, Any] | None = None,
        shared_enabled: bool | None = None,
        auto_probe: Callable[[], bool] | None = None,
    ) -> ProxyConfig:
        """Build a ProxyConfig from env vars + optional SDK-config overrides.

        Priority: ``overrides`` (the ``config["proxy"]`` dict) > env > defaults.
        ``shared_enabled`` mirrors ``config["shared_proxy_enabled"]``; when not
        given it is read from ``SHARED_PROXY_ENABLED``.  ``auto_probe``
        overrides the default circuit-breaker failover probe (tests).
        """
        environ = os.environ if env is None else env

        def pick(key: str, env_var: str, default: Any) -> Any:
            if overrides is not None and overrides.get(key) is not None:
                return overrides[key]  # type: ignore[no-any-return]
            raw = environ.get(env_var)
            if raw is None or raw == "":
                return default
            return raw

        raw_mode = str(pick("mode", "AGENT_OBS_PROXY_MODE", "off")).strip().lower()
        mode = _MODE_ALIASES.get(raw_mode)
        if mode is None:
            raise ConfigError(
                f"invalid AGENT_OBS_PROXY_MODE={raw_mode!r}; "
                "expected one of: off, on, auto"
            )

        if shared_enabled is None:
            shared_enabled = _parse_bool(environ.get("SHARED_PROXY_ENABLED"), False)
        if overrides is not None and "shared_proxy_enabled" in overrides:
            shared_enabled = bool(overrides["shared_proxy_enabled"])

        # §0.4 compatibility table: off + shared on is forbidden.
        if mode == "off" and shared_enabled:
            raise ConfigError(
                "SHARED_PROXY_ENABLED=on requires AGENT_OBS_PROXY_MODE=on "
                "(invalid combination per §0.4)"
            )
        
        # Parse downgrade_targets JSON configuration (PP55)
        downgrade_targets_json = str(
            pick("downgrade_targets", "AGENT_OBS_PROXY_DOWNGRADE_TARGETS", "")
        ).strip()
        if downgrade_targets_json:
            try:
                downgrade_targets = json.loads(downgrade_targets_json)
                if not isinstance(downgrade_targets, dict):
                    raise ConfigError(
                        f"AGENT_OBS_PROXY_DOWNGRADE_TARGETS must be a JSON object, "
                        f"got {type(downgrade_targets).__name__}"
                    )
            except json.JSONDecodeError as e:
                raise ConfigError(
                    f"Invalid JSON in AGENT_OBS_PROXY_DOWNGRADE_TARGETS: {e}"
                )
        else:
            downgrade_targets = {}

        if mode == "on":
            enabled = True
        elif mode == "auto":
            probe = auto_probe or _default_auto_probe
            enabled = bool(probe())
        else:
            enabled = False

        provider = str(
            pick("upstream_provider", "AGENT_OBS_PROXY_UPSTREAM_PROVIDER", "openai")
        ).strip().lower()

        cfg = cls(
            mode=mode,
            enabled=enabled,
            listen_host=str(pick("listen_host", "AGENT_OBS_PROXY_HOST", "127.0.0.1")),
            listen_port=_parse_int(
                pick("listen_port", "AGENT_OBS_PROXY_PORT", 8765),
                8765,
                name="AGENT_OBS_PROXY_PORT",
            ),
            upstream_provider=provider,
            upstream_base_url=str(
                pick(
                    "upstream_base_url",
                    "AGENT_OBS_PROXY_UPSTREAM_BASE_URL",
                    _DEFAULT_BASE_URL,
                )
            ),
            upstream_api_key_env=str(
                pick(
                    "upstream_api_key_env",
                    "AGENT_OBS_PROXY_UPSTREAM_API_KEY_ENV",
                    "OPENAI_API_KEY",
                )
            ),
            request_timeout_seconds=_parse_float(
                pick(
                    "request_timeout_seconds",
                    "AGENT_OBS_PROXY_REQUEST_TIMEOUT_SECONDS",
                    120.0,
                ),
                120.0,
                name="AGENT_OBS_PROXY_REQUEST_TIMEOUT_SECONDS",
            ),
            upstream_timeout_seconds=_parse_float(
                pick(
                    "upstream_timeout_seconds",
                    "AGENT_OBS_PROXY_UPSTREAM_TIMEOUT_SECONDS",
                    5.0,
                ),
                5.0,
                name="AGENT_OBS_PROXY_UPSTREAM_TIMEOUT_SECONDS",
            ),
            stream_timeout_seconds=_parse_float(
                pick(
                    "stream_timeout_seconds",
                    "AGENT_OBS_PROXY_STREAM_TIMEOUT_SECONDS",
                    10.0,
                ),
                10.0,
                name="AGENT_OBS_PROXY_STREAM_TIMEOUT_SECONDS",
            ),
            streaming_enabled=_parse_bool(
                pick(
                    "streaming_enabled",
                    "AGENT_OBS_PROXY_STREAMING_ENABLED",
                    True,
                ),
                True,
            ),
            log_bodies=_parse_bool(
                pick("log_bodies", "AGENT_OBS_PROXY_LOG_BODIES", True),
                True,
            ),
            log_bodies_max_bytes=_parse_int(
                pick(
                    "log_bodies_max_bytes",
                    "AGENT_OBS_PROXY_LOG_BODIES_MAX_BYTES",
                    100_000,
                ),
                100_000,
                name="AGENT_OBS_PROXY_LOG_BODIES_MAX_BYTES",
            ),
            cache_enabled=_parse_bool(
                pick("cache_enabled", "AGENT_OBS_PROXY_CACHE_ENABLED", False),
                False,
            ),
            cache_ttl_seconds=_parse_float(
                pick(
                    "cache_ttl_seconds",
                    "AGENT_OBS_PROXY_CACHE_TTL_SECONDS",
                    300.0,
                ),
                300.0,
                name="AGENT_OBS_PROXY_CACHE_TTL_SECONDS",
            ),
            # PP54: Semantic cache configuration
            semantic_cache_enabled=_parse_bool(
                pick("semantic_cache_enabled", "SEMANTIC_CACHE_ENABLED", True),
                True,
            ),
            semantic_cache_redis_url=str(
                pick(
                    "semantic_cache_redis_url",
                    "SEMANTIC_CACHE_REDIS_URL",
                    "redis://localhost:6379/0",
                )
            ),
            semantic_cache_ttl_seconds=int(
                pick(
                    "semantic_cache_ttl_seconds",
                    "SEMANTIC_CACHE_TTL",
                    300,
                )
            ),
            semantic_cache_max_connections=int(
                pick(
                    "semantic_cache_max_connections",
                    "SEMANTIC_CACHE_MAX_CONNECTIONS",
                    10,
                )
            ),
            # PP44: Shared proxy configuration
            shared_proxy_enabled=shared_enabled,
            shared_proxy_security_url=str(
                pick(
                    "shared_proxy_security_url",
                    "SHARED_PROXY_SECURITY_URL",
                    "",
                )
            ),
            shared_proxy_security_timeout=_parse_float(
                pick(
                    "shared_proxy_security_timeout",
                    "SHARED_PROXY_SECURITY_TIMEOUT",
                    0.05,
                ),
                0.05,
                name="SHARED_PROXY_SECURITY_TIMEOUT",
            ),
            shared_proxy_security_fail_open=_parse_bool(
                pick(
                    "shared_proxy_security_fail_open",
                    "SHARED_PROXY_SECURITY_FAIL_OPEN",
                    True,
                ),
                True,
            ),
            # PP55: Cost-aware routing configuration
            cost_aware_routing_enabled=_parse_bool(
                pick(
                    "cost_aware_routing_enabled",
                    "COST_AWARE_ROUTING_ENABLED",
                    False,
                ),
                False,
            ),
            cost_aware_routing_threshold=_parse_float(
                pick(
                    "cost_aware_routing_threshold",
                    "COST_AWARE_ROUTING_THRESHOLD",
                    0.8,
                ),
                0.8,
                name="COST_AWARE_ROUTING_THRESHOLD",
            ),
            max_downgrade_attempts=_parse_int(
                pick(
                    "max_downgrade_attempts",
                    "MAX_DOWNGRADE_ATTEMPTS",
                    1,
                ),
                1,
                name="MAX_DOWNGRADE_ATTEMPTS",
            ),
            downgrade_targets=_parse_downgrade_targets(
                pick("downgrade_targets", "AGENT_OBS_PROXY_DOWNGRADE_TARGETS", ""),
            ),
        )

        # Fail-fast validation only when the proxy will actually run.
        if cfg.enabled:
            if cfg.upstream_provider not in SUPPORTED_PROVIDERS:
                raise ConfigError(
                    f"unsupported AGENT_OBS_PROXY_UPSTREAM_PROVIDER="
                    f"{cfg.upstream_provider!r}; expected one of "
                    f"{', '.join(SUPPORTED_PROVIDERS)}"
                )
            key_value = environ.get(cfg.upstream_api_key_env, "").strip()
            if not key_value:
                raise ConfigError(
                    f"missing API key: set {cfg.upstream_api_key_env} to the "
                    f"upstream {cfg.upstream_provider} key"
                )
        return cfg


def load_proxy_config(
    config: dict | None = None,
    *,
    env: Mapping[str, str] | None = None,
    auto_probe: Callable[[], bool] | None = None,
) -> ProxyConfig:
    """Build a ProxyConfig from the SDK ``config`` dict + environment (PP39).

    Reads ``config["proxy"]`` (field-name overrides) and
    ``config["shared_proxy_enabled"]``; everything else comes from env.
    Raises :class:`ConfigError` on invalid combinations (fail-fast at SDK
    construction).
    """
    overrides = config.get("proxy") if config is not None else None
    shared = (
        config.get("shared_proxy_enabled") if config is not None else None
    )
    return ProxyConfig.from_env(
        env,
        overrides=overrides,
        shared_enabled=bool(shared) if shared is not None else None,
        auto_probe=auto_probe,
    )


__all__ = [
    "SUPPORTED_PROVIDERS",
    "ConfigError",
    "ProxyConfig",
    "load_proxy_config",
]
