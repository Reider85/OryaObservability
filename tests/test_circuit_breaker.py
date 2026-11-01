"""PP38 — CircuitBreaker state machine, metrics, and registry.

Pure-unit: no network, no drivers. Time travel through the recovery window
uses the injectable ``clock`` argument — no sleeps.
"""

from __future__ import annotations

import threading

import pytest
from prometheus_client import REGISTRY

from agent_obs.ha import (
    DEPENDENCIES,
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
    circuit_breaker_for,
    reset_circuit_breakers,
)


@pytest.fixture(autouse=True)
def clean_registry():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _state_gauge(dependency: str) -> float | None:
    return REGISTRY.get_sample_value(
        "agent_obs_circuit_breaker_state", {"dependency": dependency}
    )


def _failures_counter(dependency: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "agent_obs_circuit_breaker_failures_total", {"dependency": dependency}
        )
        or 0.0
    )


class TestStateMachine:
    def test_starts_closed_and_allows(self):
        b = CircuitBreaker("ch", failure_threshold=3, recovery_time=60)
        assert b.state is CircuitState.CLOSED
        assert b.allow_request() is True

    def test_opens_after_threshold_consecutive_failures(self):
        b = CircuitBreaker("ch", failure_threshold=3, recovery_time=60)
        b.record_failure()
        b.record_failure()
        assert b.state is CircuitState.CLOSED
        b.record_failure()
        assert b.state is CircuitState.OPEN
        assert b.allow_request() is False

    def test_success_resets_failure_counter(self):
        b = CircuitBreaker("ch", failure_threshold=3, recovery_time=60)
        b.record_failure()
        b.record_failure()
        b.record_success()
        assert b.failures == 0
        # Three more failures needed — the counter was reset.
        b.record_failure()
        b.record_failure()
        assert b.state is CircuitState.CLOSED
        b.record_failure()
        assert b.state is CircuitState.OPEN

    def test_open_rejects_within_recovery_window(self):
        clock = FakeClock()
        b = CircuitBreaker("ch", failure_threshold=1, recovery_time=60, clock=clock)
        b.record_failure()
        clock.advance(59.0)
        assert b.allow_request() is False
        assert b.state is CircuitState.OPEN

    def test_half_open_admits_single_probe(self):
        clock = FakeClock()
        b = CircuitBreaker("ch", failure_threshold=1, recovery_time=60, clock=clock)
        b.record_failure()
        clock.advance(60.0)
        # First caller is the half-open probe.
        assert b.allow_request() is True
        assert b.state is CircuitState.HALF_OPEN
        # Concurrent callers are rejected while the probe is in flight.
        assert b.allow_request() is False

    def test_half_open_success_closes(self):
        clock = FakeClock()
        b = CircuitBreaker("ch", failure_threshold=1, recovery_time=60, clock=clock)
        b.record_failure()
        clock.advance(60.0)
        assert b.allow_request() is True
        b.record_success()
        assert b.state is CircuitState.CLOSED
        assert b.failures == 0
        assert b.allow_request() is True

    def test_half_open_failure_reopens_with_fresh_timer(self):
        clock = FakeClock()
        b = CircuitBreaker("ch", failure_threshold=1, recovery_time=60, clock=clock)
        b.record_failure()
        clock.advance(60.0)
        assert b.allow_request() is True  # half-open probe admitted
        b.record_failure()  # probe failed
        assert b.state is CircuitState.OPEN
        # Fresh recovery timer: 59s later still open, 60s later probe again.
        clock.advance(59.0)
        assert b.allow_request() is False
        clock.advance(1.0)
        assert b.allow_request() is True

    def test_state_gauge_encoding(self):
        assert CircuitState.CLOSED.gauge_value == 0.0
        assert CircuitState.HALF_OPEN.gauge_value == 1.0
        assert CircuitState.OPEN.gauge_value == 2.0

    def test_gauge_updated_on_transitions(self):
        b = CircuitBreaker("ch", failure_threshold=1, recovery_time=60)
        assert _state_gauge("ch") == 0.0
        b.record_failure()
        assert _state_gauge("ch") == 2.0
        b.record_success()
        assert _state_gauge("ch") == 0.0

    def test_failures_counter_increments(self):
        b = CircuitBreaker("ch", failure_threshold=10, recovery_time=60)
        before = _failures_counter("ch")
        b.record_failure()
        b.record_failure()
        assert _failures_counter("ch") == before + 2


class TestCallWrapper:
    def test_open_raises_without_invoking_fn(self):
        b = CircuitBreaker("ch", failure_threshold=1, recovery_time=60)
        b.record_failure()
        calls = []
        with pytest.raises(CircuitOpenError) as exc_info:
            b.call(lambda: calls.append(1))
        assert exc_info.value.dependency == "ch"
        assert calls == []

    def test_exception_recorded_and_propagated(self):
        b = CircuitBreaker("ch", failure_threshold=5, recovery_time=60)

        def boom():
            raise ValueError("x")

        with pytest.raises(ValueError):
            b.call(boom)
        assert b.failures == 1
        assert b.state is CircuitState.CLOSED

    def test_success_recorded(self):
        b = CircuitBreaker("ch", failure_threshold=5, recovery_time=60)
        assert b.call(lambda: 42) == 42
        assert b.failures == 0


class TestEnvOverrides:
    def test_threshold_and_recovery_from_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_CB_FAILURE_THRESHOLD", "2")
        monkeypatch.setenv("AGENT_OBS_CB_RECOVERY_SECONDS", "7")
        b = CircuitBreaker("ch")
        assert b.failure_threshold == 2
        assert b.recovery_time == 7.0

    def test_explicit_args_beat_env(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_CB_FAILURE_THRESHOLD", "99")
        b = CircuitBreaker("ch", failure_threshold=3)
        assert b.failure_threshold == 3


class TestRegistry:
    def test_singleton_per_name(self):
        a = circuit_breaker_for("ch")
        b = circuit_breaker_for("ch")
        assert a is b

    def test_different_names_different_instances(self):
        assert circuit_breaker_for("ch") is not circuit_breaker_for("vault")

    def test_known_dependencies(self):
        for name in DEPENDENCIES:
            assert circuit_breaker_for(name).dependency == name

    def test_unknown_dependency_rejected(self):
        with pytest.raises(ValueError, match="unknown dependency"):
            circuit_breaker_for("nope")

    def test_reset_clears_registry(self):
        a = circuit_breaker_for("ch")
        reset_circuit_breakers()
        b = circuit_breaker_for("ch")
        assert a is not b


class TestThreadSafety:
    def test_concurrent_failures_do_not_corrupt_state(self):
        b = CircuitBreaker("ch", failure_threshold=50, recovery_time=60)
        errors: list[BaseException] = []

        def worker():
            try:
                for _ in range(20):
                    b.record_failure()
                    b.allow_request()
                    b.record_success()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []
        # Every worker's last action is record_success → clean closed state.
        assert b.state is CircuitState.CLOSED
        assert b.failures == 0
