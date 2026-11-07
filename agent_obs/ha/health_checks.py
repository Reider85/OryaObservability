"""Active health probes and circuit breakers for SDK dependencies (PP38).

Provides:

- :class:`HealthChecker` — periodic active probes of the six SDK
  dependencies (Langfuse, ClickHouse, Postgres, Vault, Redis, MinIO) that
  publish the ``agent_obs_dependency_health`` Prometheus gauge.
- :class:`CircuitBreaker` — per-dependency call gate (closed → open →
  half-open) that lets clients fail fast once a dependency has failed
  ``failure_threshold`` times in a row, instead of burning connection
  timeouts on every operation.

The circuit breaker registry (:func:`circuit_breaker_for`) is process-wide
and always on: storage clients and exporters pull their breaker by canonical
dependency name (``langfuse`` / ``ch`` / ``pg`` / ``vault`` / ``redis`` /
``minio``) unless a test injects a fresh instance. A breaker only trips
after *real* operation failures, so a healthy (or simply unused) dependency
never gates traffic.

Active probes are opt-in (``AGENT_OBS_HEALTH_PROBES_ENABLED=true``) and only
ping dependencies that are actually configured — an unconfigured dependency
produces no gauge series and therefore no alerts.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Any, ClassVar, TypeVar, cast

import httpx

from agent_obs.metrics import (
    circuit_breaker_failures_total,
    circuit_breaker_state,
    dependency_health,
    # PP51: Component dependency metrics
    component_dependency_health,
    component_dependency_latency_seconds,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_FAILURE_THRESHOLD = 5
DEFAULT_RECOVERY_SECONDS = 60.0
DEFAULT_PROBE_INTERVAL_SECONDS = 30.0
DEFAULT_PROBE_TIMEOUT_SECONDS = 5.0

#: Canonical dependency names — the ``dependency`` label on every PP38
#: metric. Keep in sync with the DependencyUnhealthy alert and AGENTS.md.
DEPENDENCIES = ("langfuse", "ch", "pg", "vault", "redis", "minio")


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@dataclass
class Health:
    """Result of one dependency probe.

    ``status`` is ``"healthy"`` or ``"unhealthy"``; ``latency_ms`` is the
    wall-clock duration of the probe; ``last_checked`` is ``time.time()``
    at probe completion.
    """

    status: str
    latency_ms: float
    last_checked: float

    @property
    def healthy(self) -> bool:
        return self.status == "healthy"


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------


class CircuitState(Enum):
    """Circuit breaker states (PP38 spec: closed / open / half-open)."""

    CLOSED = "closed"
    HALF_OPEN = "half-open"
    OPEN = "open"

    @property
    def gauge_value(self) -> float:
        """Numeric encoding published on ``circuit_breaker_state``."""
        return {
            CircuitState.CLOSED: 0.0,
            CircuitState.HALF_OPEN: 1.0,
            CircuitState.OPEN: 2.0,
        }[self]


class CircuitOpenError(RuntimeError):
    """Raised (or returned to) callers when the breaker is open.

    Clients either propagate this (HotStore, VaultClient — callers already
    handle unavailability) or swallow it (LangfuseExporter, catalog writer
    — they degrade to stashing / buffering).
    """

    def __init__(self, dependency: str) -> None:
        super().__init__(
            f"circuit breaker for dependency {dependency!r} is open — failing fast"
        )
        self.dependency = dependency


class CircuitBreaker:
    """Per-dependency call gate.

    State machine:

    - **closed** — normal operation. ``record_failure`` increments the
      consecutive-failure counter; at ``failure_threshold`` the breaker opens.
    - **open** — calls are rejected (:class:`CircuitOpenError` /
      ``allow_request()`` → False). After ``recovery_time`` seconds the next
      ``allow_request()`` transitions to half-open and admits a single probe
      request (single-flight: concurrent callers are still rejected).
    - **half-open** — one probe in flight. ``record_success`` closes the
      breaker and resets the counter; ``record_failure`` re-opens it with a
      fresh recovery timer.

    Thread-safe (``threading.Lock``): storage clients are sync and may be
    called from worker threads. Metrics (``circuit_breaker_state`` gauge,
    ``circuit_breaker_failures_total`` counter) are updated on every
    transition / failure.
    """

    def __init__(
        self,
        dependency: str,
        failure_threshold: int | None = None,
        recovery_time: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.dependency = dependency
        if failure_threshold is None:
            failure_threshold = int(
                os.environ.get(
                    "AGENT_OBS_CB_FAILURE_THRESHOLD", str(DEFAULT_FAILURE_THRESHOLD)
                )
            )
        if recovery_time is None:
            recovery_time = float(
                os.environ.get("AGENT_OBS_CB_RECOVERY_SECONDS", str(DEFAULT_RECOVERY_SECONDS))
            )
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_time = max(0.0, recovery_time)
        # Injectable monotonic clock (tests: travel through the recovery
        # window without sleeping).
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._probe_in_flight = False
        circuit_breaker_state.labels(dependency=dependency).set(
            CircuitState.CLOSED.gauge_value
        )

    @property
    def state(self) -> CircuitState:
        """Current state (read-only snapshot)."""
        with self._lock:
            return self._effective_state_locked()

    @property
    def failures(self) -> int:
        with self._lock:
            return self._failures

    def _effective_state_locked(self) -> CircuitState:
        # Surface "half-open is available" only after allow_request has
        # actually transitioned — the raw state is the source of truth for
        # tests; open → half-open happens exclusively in allow_request so a
        # single probe can be admitted.
        return self._state

    def allow_request(self) -> bool:
        """True if a call may proceed right now."""
        with self._lock:
            if self._state is CircuitState.CLOSED:
                return True
            if self._state is CircuitState.OPEN:
                if self._clock() - self._opened_at >= self.recovery_time:
                    self._transition_locked(CircuitState.HALF_OPEN)
                    self._probe_in_flight = True
                    return True
                return False
            # HALF_OPEN: admit only the single probe.
            if self._probe_in_flight:
                return False
            self._probe_in_flight = True
            return True

    def record_success(self) -> None:
        """Record a successful operation; closes the breaker if it was open
        or half-open and resets the failure counter."""
        with self._lock:
            self._failures = 0
            self._probe_in_flight = False
            if self._state is not CircuitState.CLOSED:
                logger.info(
                    "circuit breaker for %s closed (dependency recovered)",
                    self.dependency,
                )
                self._transition_locked(CircuitState.CLOSED)

    def record_failure(self) -> None:
        """Record a failed operation; may open (or re-open) the breaker."""
        circuit_breaker_failures_total.labels(dependency=self.dependency).inc()
        with self._lock:
            self._failures += 1
            if self._state is CircuitState.HALF_OPEN:
                # Probe failed — back to open with a fresh recovery timer.
                self._open_locked()
                return
            if (
                self._state is CircuitState.CLOSED
                and self._failures >= self.failure_threshold
            ):
                self._open_locked()

    def call(self, fn: Callable[[], T]) -> T:
        """Synchronous convenience wrapper: gate, run, record.

        Raises :class:`CircuitOpenError` without invoking ``fn`` when the
        breaker is open. Any exception from ``fn`` is recorded as a failure
        and re-raised.
        """
        if not self.allow_request():
            raise CircuitOpenError(self.dependency)
        try:
            result = fn()
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def _open_locked(self) -> None:
        self._state = CircuitState.OPEN
        self._opened_at = self._clock()
        self._probe_in_flight = False
        circuit_breaker_state.labels(dependency=self.dependency).set(
            CircuitState.OPEN.gauge_value
        )
        logger.warning(
            "circuit breaker for %s opened after %d consecutive failure(s) "
            "(recovery in %.0fs)",
            self.dependency,
            self._failures,
            self.recovery_time,
        )

    def _transition_locked(self, state: CircuitState) -> None:
        self._state = state
        circuit_breaker_state.labels(dependency=self.dependency).set(
            state.gauge_value
        )


# --- process-wide registry -------------------------------------------------

_BREAKERS: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def circuit_breaker_for(dependency: str) -> CircuitBreaker:
    """Return the process-wide breaker for ``dependency``, creating it on
    first use."""
    if dependency not in DEPENDENCIES:
        raise ValueError(
            f"unknown dependency {dependency!r}; expected one of {DEPENDENCIES}"
        )
    with _REGISTRY_LOCK:
        breaker = _BREAKERS.get(dependency)
        if breaker is None:
            breaker = CircuitBreaker(dependency)
            _BREAKERS[dependency] = breaker
        return breaker


def reset_circuit_breakers() -> None:
    """Drop all registry breakers (tests: restore clean state)."""
    with _REGISTRY_LOCK:
        _BREAKERS.clear()


# ---------------------------------------------------------------------------
# Health checker
# ---------------------------------------------------------------------------


def _env_any(*names: str) -> str | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


class HealthChecker:
    """Active health probes for the six SDK dependencies (PP38).

    Each ``check_*`` method returns a :class:`Health` and never raises — a
    probe failure is data, not an exception. Unconfigured dependencies are
    skipped by :meth:`targets` / :meth:`check_all` so a deployment without
    (say) Postgres never publishes a ``pg`` series and never fires
    ``DependencyUnhealthy`` for it.

    Dependency → probe:

    - ``langfuse`` — ``GET {LANGFUSE_HOST}/api/public/health`` (2xx = up)
    - ``ch``       — ``HotStore.is_available()`` (``SELECT 1`` over failover)
    - ``pg``       — ``asyncpg.connect(dsn)`` + ``SELECT 1``
    - ``vault``    — ``GET {addr}/v1/sys/health`` (200 active, 429 standby = up)
    - ``redis``    — ``redis-cli ping`` equivalent (``redis-py`` ``PING``)
    - ``minio``    — ``mc admin info`` equivalent (boto3 ``ListBuckets``)

    Args:
        interval_seconds: probe cadence for :meth:`run_forever`.
        timeout_seconds: per-probe timeout.
        langfuse_url / hotstore / pg_dsn / vault_addrs / redis_url /
            s3_endpoint: explicit overrides; fall back to env vars.
        http_client: injectable ``httpx.AsyncClient`` (tests).
    """

    def __init__(
        self,
        *,
        interval_seconds: float | None = None,
        timeout_seconds: float | None = None,
        langfuse_url: str | None = None,
        hotstore: Any | None = None,
        pg_dsn: str | None = None,
        vault_addrs: str | list[str] | None = None,
        redis_url: str | None = None,
        s3_endpoint: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if interval_seconds is None:
            interval_seconds = float(
                os.environ.get(
                    "AGENT_OBS_HEALTH_PROBE_INTERVAL",
                    str(DEFAULT_PROBE_INTERVAL_SECONDS),
                )
            )
        if timeout_seconds is None:
            timeout_seconds = float(
                os.environ.get(
                    "AGENT_OBS_HEALTH_PROBE_TIMEOUT",
                    str(DEFAULT_PROBE_TIMEOUT_SECONDS),
                )
            )
        self.interval_seconds = max(0.1, interval_seconds)
        self.timeout_seconds = max(0.1, timeout_seconds)

        self._langfuse_url = (langfuse_url or _env_any("LANGFUSE_HOST") or "").rstrip("/")
        self._hotstore = hotstore
        self._pg_dsn = pg_dsn or _env_any("COMPLIANCE_PG_DSN")
        raw_vault = vault_addrs or _env_any("VAULT_ADDRS", "VAULT_ADDR")
        if isinstance(raw_vault, list):
            self._vault_addr = (raw_vault[0] if raw_vault else "").rstrip("/")
        else:
            self._vault_addr = (raw_vault or "").split(",")[0].strip().rstrip("/")
        self._redis_url = redis_url or _env_any(
            "AGENT_OBS_REDIS_URL", "REDIS_URL", "EVAL_REDIS_URL"
        )
        self._s3_endpoint = s3_endpoint or _env_any("S3_ENDPOINT")
        self._http = http_client

    # --- configuration detection ------------------------------------------

    def targets(self) -> list[str]:
        """Dependencies that are configured and will be probed."""
        found: list[str] = []
        if self._langfuse_url:
            found.append("langfuse")
        if self._hotstore is not None or _env_any("CLICKHOUSE_HOSTS", "CLICKHOUSE_HOST"):
            found.append("ch")
        if self._pg_dsn:
            found.append("pg")
        if self._vault_addr:
            found.append("vault")
        if self._redis_url:
            found.append("redis")
        if self._s3_endpoint:
            found.append("minio")
        return found

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.timeout_seconds)
        return self._http

    async def _timed(
        self, dependency: str, fn: Callable[[], Any]
    ) -> Health:
        """Run sync probe ``fn`` off-loop with the configured timeout."""
        start = time.monotonic()
        try:
            await asyncio.wait_for(asyncio.to_thread(fn), self.timeout_seconds)
        except Exception as exc:  # noqa: BLE001 — probe failures are data
            latency = (time.monotonic() - start) * 1000.0
            logger.warning(
                "health probe for %s failed after %.1fms: %s",
                dependency,
                latency,
                exc,
            )
            return Health(
                status="unhealthy", latency_ms=latency, last_checked=time.time()
            )
        latency = (time.monotonic() - start) * 1000.0
        return Health(
            status="healthy", latency_ms=latency, last_checked=time.time()
        )

    # --- individual probes -------------------------------------------------

    async def check_langfuse(self) -> Health:
        """``GET /api/public/health`` — any 2xx marks Langfuse healthy."""
        client = await self._get_http()
        start = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                client.get(f"{self._langfuse_url}/api/public/health"),
                self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            latency = (time.monotonic() - start) * 1000.0
            logger.warning("health probe for langfuse failed: %s", exc)
            return Health("unhealthy", latency, time.time())
        latency = (time.monotonic() - start) * 1000.0
        status = "healthy" if resp.status_code < 300 else "unhealthy"
        if status != "healthy":
            logger.warning(
                "health probe for langfuse: HTTP %d", resp.status_code
            )
        return Health(status, latency, time.time())

    async def check_clickhouse(self) -> Health:
        """``SELECT 1`` through the (failover-aware) HotStore client."""
        store = self._hotstore
        if store is None:
            from agent_obs.storage.hot import HotStore

            store = HotStore()
            self._hotstore = store

        def _probe() -> None:
            if not store.is_available():
                raise RuntimeError("SELECT 1 failed on every replica")

        return await self._timed("ch", _probe)

    async def check_postgres(self) -> Health:
        """``asyncpg.connect(dsn)`` + ``SELECT 1`` (asyncpg is optional)."""
        dsn = self._pg_dsn
        start = time.monotonic()
        try:
            import asyncpg  # type: ignore[import-untyped]

            conn = await asyncio.wait_for(
                asyncpg.connect(dsn=dsn, timeout=self.timeout_seconds),
                self.timeout_seconds + 1.0,
            )
            try:
                await asyncio.wait_for(
                    conn.fetchval("SELECT 1"), self.timeout_seconds
                )
            finally:
                await conn.close()
        except Exception as exc:  # noqa: BLE001 — probe failures are data
            latency = (time.monotonic() - start) * 1000.0
            logger.warning(
                "health probe for pg failed after %.1fms: %s", latency, exc
            )
            return Health("unhealthy", latency, time.time())
        latency = (time.monotonic() - start) * 1000.0
        return Health("healthy", latency, time.time())

    async def check_vault(self) -> Health:
        """``GET /v1/sys/health`` — 200 (active) and 429 (unsealed standby)
        both count as healthy; 501 (uninitialized) / 503 (sealed) do not."""
        client = await self._get_http()
        start = time.monotonic()
        try:
            resp = await asyncio.wait_for(
                client.get(f"{self._vault_addr}/v1/sys/health"),
                self.timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            latency = (time.monotonic() - start) * 1000.0
            logger.warning("health probe for vault failed: %s", exc)
            return Health("unhealthy", latency, time.time())
        latency = (time.monotonic() - start) * 1000.0
        healthy = resp.status_code in (200, 429)
        if not healthy:
            logger.warning(
                "health probe for vault: HTTP %d (uninitialized/sealed?)",
                resp.status_code,
            )
        return Health("healthy" if healthy else "unhealthy", latency, time.time())

    async def check_redis(self) -> Health:
        """Redis ``PING`` (sync redis-py off-loop, same as the SDK clients)."""

        def _probe() -> None:
            import redis as _redis

            client = _redis.from_url(self._redis_url, socket_connect_timeout=self.timeout_seconds)
            try:
                client.ping()
            finally:
                client.close()

        return await self._timed("redis", _probe)

    async def check_minio(self) -> Health:
        """S3/MinIO ``ListBuckets`` (``mc admin info`` equivalent via boto3)."""

        def _probe() -> None:
            import boto3  # type: ignore[import-untyped]
            from botocore.config import Config  # type: ignore[import-untyped]

            client = boto3.client(
                "s3",
                endpoint_url=self._s3_endpoint,
                aws_access_key_id=os.environ.get("S3_ACCESS_KEY", ""),
                aws_secret_access_key=os.environ.get("S3_SECRET_KEY", ""),
                region_name="us-east-1",
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=self.timeout_seconds,
                    retries={"max_attempts": 1},
                ),
            )
            client.list_buckets()

        return await self._timed("minio", _probe)

    # --- aggregate ---------------------------------------------------------

    _PROBES: ClassVar[dict[str, Callable[..., Any]]] = {
        "langfuse": check_langfuse,
        "ch": check_clickhouse,
        "pg": check_postgres,
        "vault": check_vault,
        "redis": check_redis,
        "minio": check_minio,
    }

    async def check_all(self) -> dict[str, Health]:
        """Probe every configured dependency concurrently."""
        deps = self.targets()
        if not deps:
            return {}
        results = cast(
            "list[Health]",
            await asyncio.gather(*(self._PROBES[dep](self) for dep in deps)),
        )
        return dict(zip(deps, results, strict=True))

    def apply_gauges(self, results: dict[str, Health]) -> None:
        """Publish ``dependency_health`` gauges for the probed dependencies."""
        for dep, health in results.items():
            dependency_health.labels(dependency=dep).set(
                1.0 if health.healthy else 0.0
            )

    async def run_forever(self, stop_event: asyncio.Event | None = None) -> None:
        """Probe loop for :func:`asyncio.create_task`.

        One probe cycle per interval; every exception is contained so the
        loop survives any dependency being down (that is the point).
        """
        stop_event = stop_event or asyncio.Event()
        logger.info(
            "health probes started (interval=%.0fs, timeout=%.0fs, targets=%s)",
            self.interval_seconds,
            self.timeout_seconds,
            self.targets(),
        )
        while not stop_event.is_set():
            try:
                results = await self.check_all()
                self.apply_gauges(results)
            except Exception:
                logger.exception("health probe cycle failed; gauges unchanged")
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                pass
        logger.info("health probes stopped")

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
