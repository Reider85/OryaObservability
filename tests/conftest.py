"""Shared test fixtures.

PP38: circuit breakers live in a process-wide registry
(``agent_obs.ha.circuit_breaker_for``). Without a reset, failure counts
accumulated by one test (e.g. a failover test that kills every replica)
would leak into the next test through the shared instance and trip its
breaker open. Reset before and after every test; tests that need an open
breaker build their own via ``CircuitBreaker(...)`` or open the registry
instance explicitly.
"""

from __future__ import annotations

import pytest

from agent_obs.ha import reset_circuit_breakers


@pytest.fixture(autouse=True)
def _reset_circuit_breakers():
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()
