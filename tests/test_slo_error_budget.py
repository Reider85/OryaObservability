"""Tests for PP27 (T3.6.3) — error budget tracking + policy actions.

Validates:
- ErrorBudget math (budget_total, consumed, remaining) for each catalog SLO,
  the empty-window convention and the target=1.0 edge case
- ErrorBudgetPolicy fires soft actions at <20% remaining and hard actions at
  <=0 remaining, exactly once per transition (idempotent on repeat ticks)
- Default wiring: Redis policy-state sink, freeze_deploy audit row, the
  slo_error_budget_actions_taken_total counter
- BudgetEnforcer halves its effective budget while the policy state is soft
  (PP27 soft action on the eval pipeline)
- evaluate_rate drops the sampler to 5% when the error budget is low, with
  correct precedence against cpu_high/error_high
- SLORefresher computes the budget from the same (good, total) counts as the
  SLI and feeds the policy when one is attached
- infra/prometheus-rules.yml carries the two slo-error-budget alerts with
  page/ticket severities; scripts/cicd/error_budget_gate.py blocks a deploy
  on an exhausted budget and allows a healthy one
"""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from agent_obs.metrics import (
    slo_error_budget_actions_taken_total,
    slo_error_budget_remaining,
)
from agent_obs.slo import SLO_CATALOG, SLORefresher
from agent_obs.slo.definitions import DEFAULT_WINDOW
from agent_obs.slo.error_budget import (
    ACTION_HARD,
    ACTION_RECOVER,
    ACTION_SOFT,
    DEFAULT_SOFT_THRESHOLD,
    EVAL_BUDGET_SOFT_MULTIPLIER,
    POLICY_STATE_REDIS_KEY,
    STATE_HARD,
    STATE_OK,
    STATE_SOFT,
    ErrorBudget,
    ErrorBudgetPolicy,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RULES_PATH = REPO_ROOT / "infra" / "prometheus-rules.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class FakeHotStore:
    """Records audit events; get_sli_stats returns canned counts."""

    def __init__(self, stats: dict[tuple[str, float], tuple[int, int]]):
        self.stats = stats
        self.audit_events: list = []

    def get_sli_stats(self, sli_kind, window: timedelta, good_threshold=None):
        return self.stats.get((sli_kind, window.total_seconds()), (0, 0))

    def write_audit_event(self, event) -> None:
        self.audit_events.append(event)


class RecordingSink:
    """Stands in for RedisPolicyStateSink: records published states."""

    def __init__(self):
        self.calls: list[tuple[str, str, ErrorBudget]] = []

    def __call__(self, state: str, slo_name: str, budget: ErrorBudget) -> None:
        self.calls.append((state, slo_name, budget))


def _budget(slo_name: str, remaining_ratio: float, target: float = 0.999) -> ErrorBudget:
    """Build a budget with a precisely prescribed remaining ratio.

    Constructed directly (not via from_counts) so policy/classify tests pin
    the exact ratio without depending on integer event-count rounding.
    """
    total = 10000
    budget_total = (1 - target) * total
    consumed = budget_total * (1 - remaining_ratio)
    return ErrorBudget(
        slo_name=slo_name,
        target=target,
        total_events=total,
        error_events=round(total - consumed),
        budget_total=budget_total,
        budget_consumed=consumed,
        remaining_ratio=remaining_ratio,
    )


@pytest.fixture(autouse=True)
def _reset_slo_metrics():
    slo_error_budget_remaining._metrics.clear()
    slo_error_budget_actions_taken_total._metrics.clear()
    yield
    slo_error_budget_remaining._metrics.clear()
    slo_error_budget_actions_taken_total._metrics.clear()


def _counter_value(name: str, action: str) -> float:
    child = slo_error_budget_actions_taken_total._metrics.get((name, action))
    return 0.0 if child is None else child._value.get()  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ErrorBudget math
# ---------------------------------------------------------------------------

class TestErrorBudgetMath:
    def test_availability_budget_from_counts(self):
        # target .999, 10000 events -> budget = 10 error events.
        # 5 errors burned -> half the budget gone.
        b = ErrorBudget.from_counts("availability", 0.999, good_events=9995, total_events=10000)
        assert b.budget_total == pytest.approx(10.0)
        assert b.error_events == 5
        assert b.remaining_ratio == pytest.approx(0.5)
        assert b.consumed_ratio == pytest.approx(0.5)

    def test_perfect_sli_spends_nothing(self):
        b = ErrorBudget.from_counts("availability", 0.999, good_events=10000, total_events=10000)
        assert b.remaining_ratio == pytest.approx(1.0)
        assert b.error_events == 0

    def test_empty_window_is_perfect(self):
        b = ErrorBudget.from_counts("quality", 0.90, good_events=0, total_events=0)
        assert b.remaining_ratio == 1.0
        assert b.budget_total == 0.0
        assert b.error_events == 0

    def test_negative_remaining_when_overspent(self):
        # 100 errors against a 10-event budget -> remaining -9.0
        b = ErrorBudget.from_counts("availability", 0.999, good_events=9900, total_events=10000)
        assert b.remaining_ratio == pytest.approx(-9.0)
        assert b.clamped_remaining == 0.0

    def test_latency_target_budget(self):
        # target .95, 1000 events -> budget = 50; 10 bad -> 40/50 remaining
        b = ErrorBudget.from_counts("latency_p95", 0.95, good_events=990, total_events=1000)
        assert b.budget_total == pytest.approx(50.0)
        assert b.remaining_ratio == pytest.approx(0.8)

    def test_quality_target_budget(self):
        # target .90, 100 evals -> budget = 10; 8 unfaithful -> 2/10 remaining
        b = ErrorBudget.from_counts("quality", 0.90, good_events=92, total_events=100)
        assert b.budget_total == pytest.approx(10.0)
        assert b.remaining_ratio == pytest.approx(0.2)

    def test_cost_target_budget(self):
        # target .95, 1000 requests -> budget = 50; 25 over-cost -> 25/50 left
        b = ErrorBudget.from_counts("cost_p99", 0.95, good_events=975, total_events=1000)
        assert b.budget_total == pytest.approx(50.0)
        assert b.remaining_ratio == pytest.approx(0.5)

    def test_target_of_one_any_error_exhausts(self):
        b = ErrorBudget.from_counts("availability", 1.0, good_events=99, total_events=100)
        assert b.budget_total == 0.0
        assert b.remaining_ratio < 0  # exhausted by the single error
        # but no errors at all stays perfect
        b2 = ErrorBudget.from_counts("availability", 1.0, good_events=100, total_events=100)
        assert b2.remaining_ratio == 1.0

    def test_invalid_target_rejected(self):
        with pytest.raises(ValueError):
            ErrorBudget.from_counts("bad", 0.0, good_events=1, total_events=10)
        with pytest.raises(ValueError):
            ErrorBudget.from_counts("bad", 1.5, good_events=1, total_events=10)


# ---------------------------------------------------------------------------
# classify / state mapping
# ---------------------------------------------------------------------------

class TestClassify:
    def test_ok_at_full_budget(self):
        assert _budget("availability", 1.0).classify(DEFAULT_SOFT_THRESHOLD, 0.0) == STATE_OK

    def test_ok_just_above_soft_threshold(self):
        assert _budget("availability", 0.2).classify(DEFAULT_SOFT_THRESHOLD, 0.0) == STATE_OK

    def test_soft_below_threshold(self):
        assert _budget("availability", 0.199).classify(DEFAULT_SOFT_THRESHOLD, 0.0) == STATE_SOFT

    def test_soft_at_85pct_consumed(self):
        # spec test: 85% consumed -> remaining 0.15 -> soft
        assert _budget("availability", 0.15).classify(DEFAULT_SOFT_THRESHOLD, 0.0) == STATE_SOFT

    def test_hard_at_zero_remaining(self):
        assert _budget("availability", 0.0).classify(DEFAULT_SOFT_THRESHOLD, 0.0) == STATE_HARD

    def test_hard_when_negative(self):
        assert _budget("availability", -0.01).classify(DEFAULT_SOFT_THRESHOLD, 0.0) == STATE_HARD


# ---------------------------------------------------------------------------
# ErrorBudgetPolicy — transition behaviour
# ---------------------------------------------------------------------------

class TestPolicyTransitions:
    def test_healthy_budget_is_ok_no_actions(self):
        sink = RecordingSink()
        policy = ErrorBudgetPolicy(catalog={"availability": SLO_CATALOG["availability"]}, state_sink=sink)
        state = policy.evaluate("availability", _budget("availability", 0.9))
        assert state == STATE_OK
        assert sink.calls == []
        assert policy.state("availability") == STATE_OK

    def test_soft_action_fires_once(self):
        sink = RecordingSink()
        policy = ErrorBudgetPolicy(catalog={"availability": SLO_CATALOG["availability"]}, state_sink=sink)
        # 85% consumed -> soft
        policy.evaluate("availability", _budget("availability", 0.15))
        assert [c[:2] for c in sink.calls] == [("soft", "availability")]
        # repeat tick with the same state -> no second action (idempotent)
        policy.evaluate("availability", _budget("availability", 0.14))
        assert len(sink.calls) == 1
        assert _counter_value("availability", ACTION_SOFT) == 1.0

    def test_hard_action_fires_once(self):
        sink = RecordingSink()
        policy = ErrorBudgetPolicy(catalog={"availability": SLO_CATALOG["availability"]}, state_sink=sink)
        policy.evaluate("availability", _budget("availability", -0.05))
        assert [c[:2] for c in sink.calls] == [("hard", "availability")]
        policy.evaluate("availability", _budget("availability", -0.10))
        assert len(sink.calls) == 1
        assert _counter_value("availability", ACTION_HARD) == 1.0

    def test_ok_to_hard_skips_soft(self):
        # a fast burn can jump ok -> hard in one refresh; only hard fires
        sink = RecordingSink()
        policy = ErrorBudgetPolicy(catalog={"availability": SLO_CATALOG["availability"]}, state_sink=sink)
        policy.evaluate("availability", _budget("availability", -1.0))
        assert [c[:2] for c in sink.calls] == [("hard", "availability")]
        assert _counter_value("availability", ACTION_SOFT) == 0.0
        assert _counter_value("availability", ACTION_HARD) == 1.0

    def test_soft_to_hard_then_recover(self):
        sink = RecordingSink()
        policy = ErrorBudgetPolicy(catalog={"availability": SLO_CATALOG["availability"]}, state_sink=sink)
        policy.evaluate("availability", _budget("availability", 0.15))   # -> soft
        policy.evaluate("availability", _budget("availability", -0.02))  # -> hard
        policy.evaluate("availability", _budget("availability", 0.5))    # -> ok
        assert [c[0] for c in sink.calls] == ["soft", "hard", "ok"]
        assert _counter_value("availability", ACTION_RECOVER) == 1.0
        assert policy.worst_state == STATE_OK

    def test_worst_state_tracks_hardest_slo(self):
        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"], "quality": SLO_CATALOG["quality"]},
            state_sink=RecordingSink(),
        )
        policy.evaluate("availability", _budget("availability", 0.5))
        policy.evaluate("quality", _budget("quality", 0.1, target=0.90))
        assert policy.worst_state == STATE_SOFT
        policy.evaluate("quality", _budget("quality", -0.1, target=0.90))
        assert policy.worst_state == STATE_HARD

    def test_broken_action_does_not_raise(self):
        def boom(slo_name, budget):
            raise RuntimeError("action sink down")

        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"]},
            soft_action=boom,
            state_sink=RecordingSink(),
        )
        # must not raise — observability never blocks business logic
        policy.evaluate("availability", _budget("availability", 0.1))

    def test_evaluate_all(self):
        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"], "quality": SLO_CATALOG["quality"]},
            state_sink=RecordingSink(),
        )
        states = policy.evaluate_all({
            "availability": _budget("availability", 0.9),
            "quality": _budget("quality", 0.05, target=0.90),
        })
        assert states == {"availability": STATE_OK, "quality": STATE_SOFT}


# ---------------------------------------------------------------------------
# Default actions: Redis sink + audit + counter
# ---------------------------------------------------------------------------

class TestDefaultActionsAndAudit:
    def test_freeze_deploy_audit_row(self):
        store = FakeHotStore({})
        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"]},
            hot_store=store,
            state_sink=RecordingSink(),
        )
        policy.evaluate("availability", _budget("availability", -0.5))
        assert len(store.audit_events) == 1
        event = store.audit_events[0]
        assert event.action == "freeze_deploy"
        assert event.decision == "hard"
        assert event.resource["slo"] == "availability"
        assert event.resource["new_state"] == "hard"
        assert "remaining=" in event.reason

    def test_soft_audit_row_is_state_change(self):
        store = FakeHotStore({})
        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"]},
            hot_store=store,
            state_sink=RecordingSink(),
        )
        policy.evaluate("availability", _budget("availability", 0.1))
        assert store.audit_events[0].action == "error_budget.state_change"
        assert store.audit_events[0].decision == "soft"

    def test_audit_row_serializes_to_clickhouse_shape(self):
        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"]},
            hot_store=FakeHotStore({}),
            state_sink=RecordingSink(),
        )
        policy.evaluate("availability", _budget("availability", -0.1))
        event = FakeHotStore({}).audit_events  # noqa: F841 — clarity
        from agent_obs.guardrail.audit import ErrorBudgetActionAuditEvent

        e = ErrorBudgetActionAuditEvent.for_policy_action(
            "availability", "soft", "hard", -0.1, 10.0, 11.0
        )
        row = e.to_clickhouse_row()
        assert len(row) == 10  # audit_events_hot DDL column count
        assert row[4] == "freeze_deploy"
        assert json.loads(row[6])["budget_consumed"] == 11.0

    def test_broken_audit_write_does_not_raise(self):
        class ExplodingStore:
            def write_audit_event(self, event):
                raise ConnectionError("clickhouse down")

        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"]},
            hot_store=ExplodingStore(),
            state_sink=RecordingSink(),
        )
        policy.evaluate("availability", _budget("availability", -0.1))  # must not raise

    def test_default_sink_writes_redis_key(self, monkeypatch):
        # Exercise RedisPolicyStateSink against a fake redis client.
        from agent_obs.slo import error_budget as eb_module

        class FakeRedis:
            def __init__(self):
                self.store: dict[str, str] = {}
                self.expirations: dict[str, int] = {}

            def set(self, key, value, ex=None):
                self.store[key] = value
                self.expirations[key] = ex

            def delete(self, key):
                self.store.pop(key, None)

        fake = FakeRedis()
        sink = eb_module.RedisPolicyStateSink(redis_url="redis://unused")
        sink._client = fake

        b = _budget("availability", 0.1)
        sink(STATE_SOFT, "availability", b)
        assert POLICY_STATE_REDIS_KEY in fake.store
        payload = json.loads(fake.store[POLICY_STATE_REDIS_KEY])
        assert payload["state"] == "soft"
        assert payload["slo"] == "availability"

        sink(STATE_OK, "availability", b)
        assert POLICY_STATE_REDIS_KEY not in fake.store

    def test_default_sink_swallows_redis_errors(self):
        from agent_obs.slo.error_budget import RedisPolicyStateSink

        class BrokenRedis:
            def set(self, *a, **kw):
                raise ConnectionError("redis down")

            def delete(self, *a, **kw):
                raise ConnectionError("redis down")

        sink = RedisPolicyStateSink(redis_url="redis://unused")
        sink._client = BrokenRedis()
        # must not raise
        sink(STATE_SOFT, "availability", _budget("availability", 0.1))


# ---------------------------------------------------------------------------
# BudgetEnforcer — the eval-side soft action (PP15 component, PP27 wiring)
# ---------------------------------------------------------------------------

class TestBudgetEnforcerMultiplier:
    def _enforcer(self):
        from agent_obs.eval.adaptive_sampler import BudgetEnforcer

        return BudgetEnforcer(redis_url="redis://unused", budget_usd=20.0)

    def test_default_multiplier_is_one(self):
        enforcer = self._enforcer()
        assert enforcer.budget_multiplier == 1.0
        assert enforcer.effective_budget_usd == pytest.approx(20.0)

    def test_soft_multiplier_halves_budget(self):
        enforcer = self._enforcer()
        changed = enforcer.set_budget_multiplier(EVAL_BUDGET_SOFT_MULTIPLIER)
        assert changed is True
        assert enforcer.effective_budget_usd == pytest.approx(10.0)
        # idempotent
        assert enforcer.set_budget_multiplier(EVAL_BUDGET_SOFT_MULTIPLIER) is False

    def test_multiplier_above_one_rejected(self):
        enforcer = self._enforcer()
        with pytest.raises(ValueError):
            enforcer.set_budget_multiplier(2.0)

    def test_state_uses_effective_budget(self):
        enforcer = self._enforcer()
        enforcer.set_budget_multiplier(0.5)
        # $9 spent: 90% of the halved $10 budget -> soft; against the full
        # $20 it would be 45% -> ok
        state = asyncio.run(enforcer._determine_state({"day": 9.0}))
        assert state == 1  # soft
        enforcer.set_budget_multiplier(1.0)
        assert asyncio.run(enforcer._determine_state({"day": 9.0})) == 0  # ok

    def test_refresh_budget_reads_policy_state(self):
        enforcer = self._enforcer()

        async def fake_policy_state():
            return "soft"

        enforcer._read_error_budget_policy_state = fake_policy_state  # type: ignore[method-assign]
        enforcer._read_daily_cost = lambda: asyncio.sleep(0, result={"day": 1.0})  # type: ignore[method-assign]
        asyncio.run(enforcer.refresh_budget())
        assert enforcer.budget_multiplier == pytest.approx(0.5)

    def test_refresh_budget_ok_state_resets_multiplier(self):
        enforcer = self._enforcer()
        enforcer.set_budget_multiplier(0.5)

        async def fake_policy_state():
            return "ok"

        enforcer._read_error_budget_policy_state = fake_policy_state  # type: ignore[method-assign]
        enforcer._read_daily_cost = lambda: asyncio.sleep(0, result={"day": 1.0})  # type: ignore[method-assign]
        asyncio.run(enforcer.refresh_budget())
        assert enforcer.budget_multiplier == 1.0

    def test_policy_state_read_fails_open(self):
        enforcer = self._enforcer()
        enforcer._client = None

        class BrokenClient:
            def get(self, key):
                raise ConnectionError("redis down")

        enforcer._client = BrokenClient()
        assert asyncio.run(enforcer._read_error_budget_policy_state()) == "ok"


# ---------------------------------------------------------------------------
# Sampler policy: evaluate_rate with the error-budget signal (PP27 soft action)
# ---------------------------------------------------------------------------

class TestSamplerErrorBudgetRule:
    def test_low_budget_drops_rate_to_five_percent(self):
        from scripts.sampler.policy_engine import evaluate_rate

        rate, reason = evaluate_rate(
            cpu_ratio=0.1, error_rate_5m=0.0, error_budget_remaining=0.15
        )
        assert reason == "error_budget_low"
        assert rate == pytest.approx(0.05)

    def test_budget_rule_loses_to_cpu_high(self):
        from scripts.sampler.policy_engine import evaluate_rate

        rate, reason = evaluate_rate(
            cpu_ratio=0.95, error_rate_5m=0.0, error_budget_remaining=0.1
        )
        assert reason == "cpu_high"
        assert rate == pytest.approx(0.05)

    def test_budget_rule_beats_error_high(self):
        # budget low + error rate high: conserve storage (0.05), errors are
        # kept unconditionally by the always-keep attributes anyway
        from scripts.sampler.policy_engine import evaluate_rate

        rate, reason = evaluate_rate(
            cpu_ratio=0.1, error_rate_5m=0.5, error_budget_remaining=0.05
        )
        assert reason == "error_budget_low"
        assert rate == pytest.approx(0.05)

    def test_healthy_budget_keeps_default_behaviour(self):
        from scripts.sampler.policy_engine import evaluate_rate

        rate, reason = evaluate_rate(
            cpu_ratio=0.1, error_rate_5m=0.07, error_budget_remaining=0.9
        )
        assert reason == "error_high"  # budget rule did not fire
        assert rate == pytest.approx(0.30)

    def test_unknown_budget_is_no_signal(self):
        from scripts.sampler.policy_engine import evaluate_rate

        rate, reason = evaluate_rate(
            cpu_ratio=0.1, error_rate_5m=0.0, error_budget_remaining=None
        )
        assert reason == "default"
        assert rate == pytest.approx(0.10)

    def test_budget_exactly_at_threshold_is_not_low(self):
        from scripts.sampler.policy_engine import evaluate_rate

        _, reason = evaluate_rate(
            cpu_ratio=0.1, error_rate_5m=0.0, error_budget_remaining=0.2
        )
        assert reason != "error_budget_low"

    def test_engine_evaluate_carries_budget_signal(self):
        from scripts.sampler.policy_engine import PolicyEngine

        engine = PolicyEngine()
        decision = engine.evaluate(0.1, 0.0, error_budget_remaining=0.1)
        assert decision.reason == "error_budget_low"
        assert decision.error_budget_remaining == pytest.approx(0.1)
        assert "error_budget_remaining=0.1000" in decision.reason_text

    def test_audit_reason_mentions_threshold(self):
        from scripts.sampler.policy_engine import PolicyEngine

        engine = PolicyEngine()
        decision = engine.evaluate(0.1, 0.0, error_budget_remaining=0.05)
        event = engine.build_audit_event(decision)
        assert "slo_error_budget_remaining=0.0500" in event.reason
        assert "error_budget_low" in event.reason

    def test_read_error_budget_returns_min_series(self, monkeypatch):
        from scripts.sampler.policy_engine import PolicyEngine

        engine = PolicyEngine()

        async def fake_query(query):
            return [("availability", 0.15), ("quality", 0.9)]

        engine._query = fake_query  # type: ignore[method-assign]
        assert asyncio.run(engine.read_error_budget()) == pytest.approx(0.15)

    def test_read_error_budget_none_when_no_series(self):
        from scripts.sampler.policy_engine import PolicyEngine

        engine = PolicyEngine()

        async def fake_query(query):
            return []

        engine._query = fake_query  # type: ignore[method-assign]
        assert asyncio.run(engine.read_error_budget()) is None


# ---------------------------------------------------------------------------
# SLORefresher integration
# ---------------------------------------------------------------------------

class TestRefresherIntegration:
    def _stats(self, good: int, total: int) -> dict:
        thirty_d = DEFAULT_WINDOW.total_seconds()
        return {
            ("availability", thirty_d): (good, total),
            ("availability", 300.0): (good, total),
            ("availability", 3600.0): (good, total),
            ("availability", 6 * 3600.0): (good, total),
        }

    def test_refresher_computes_budget_from_counts(self):
        # 9995/10000 -> budget 10, consumed 5 -> 0.5 remaining (matches the
        # old placeholder formula, which was 1 - burn_rate for the same counts)
        store = FakeHotStore(self._stats(9995, 10000))
        SLORefresher(store, catalog={"availability": SLO_CATALOG["availability"]}).refresh()
        from prometheus_client import generate_latest

        expo = generate_latest().decode()
        line = next(l for l in expo.splitlines() if l.startswith("slo_error_budget_remaining"))
        assert float(line.split()[-1]) == pytest.approx(0.5)

    def test_refresher_feeds_policy(self):
        sink = RecordingSink()
        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"]}, state_sink=sink
        )
        # 0 good out of 100 -> budget 0.1, consumed 100 -> hard
        store = FakeHotStore(self._stats(0, 100))
        SLORefresher(
            store, catalog={"availability": SLO_CATALOG["availability"]}, policy=policy
        ).refresh()
        assert policy.state("availability") == STATE_HARD
        assert sink.calls and sink.calls[0][0] == "hard"

    def test_refresher_without_policy_still_works(self):
        store = FakeHotStore(self._stats(9999, 10000))
        report = SLORefresher(
            store, catalog={"availability": SLO_CATALOG["availability"]}
        ).refresh()
        # 1 error against a 10-event budget -> 0.9 remaining
        assert report["availability"]["error_budget_remaining"] == pytest.approx(0.9)

    def test_hotstore_failure_does_not_break_policy(self):
        class ExplodingHotStore:
            def get_sli_stats(self, *a, **kw):
                raise ConnectionError("clickhouse down")

        sink = RecordingSink()
        policy = ErrorBudgetPolicy(
            catalog={"availability": SLO_CATALOG["availability"]}, state_sink=sink
        )
        report = SLORefresher(
            ExplodingHotStore(),
            catalog={"availability": SLO_CATALOG["availability"]},
            policy=policy,
        ).refresh()
        assert report == {}
        assert sink.calls == []  # no budget -> no policy evaluation


# ---------------------------------------------------------------------------
# CI/CD gate script
# ---------------------------------------------------------------------------

class TestCicdGate:
    def _gate(self, budgets: dict[str, float] | None, error: str | None = None):
        sys_path_gate = REPO_ROOT / "scripts" / "cicd" / "error_budget_gate.py"

        import importlib.util

        spec = importlib.util.spec_from_file_location("error_budget_gate", sys_path_gate)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_healthy_budgets_allow(self, capsys):
        module = self._gate(None)
        result = module.GateResult({"availability": 0.8, "quality": 0.5})
        assert module.evaluate_gate(result) == 0
        assert "allowed" in capsys.readouterr().out

    def test_exhausted_budget_blocks(self, capsys):
        module = self._gate(None)
        result = module.GateResult({"availability": 0.0, "quality": 0.6})
        assert module.evaluate_gate(result) == 1
        assert "BLOCKED" in capsys.readouterr().err

    def test_negative_budget_blocks(self):
        module = self._gate(None)
        assert module.evaluate_gate(module.GateResult({"cost_p99": -0.1})) == 1

    def test_no_series_allows(self, capsys):
        module = self._gate(None)
        assert module.evaluate_gate(module.GateResult({})) == 0
        assert "no slo_error_budget_remaining series" in capsys.readouterr().out

    def test_prometheus_error_fails_closed(self, capsys):
        module = self._gate(None)
        result = module.GateResult(None, error="prometheus down")
        assert module.evaluate_gate(result) == 2
        assert "prometheus down" in capsys.readouterr().err

    def test_prometheus_error_fail_open(self, capsys):
        module = self._gate(None)
        result = module.GateResult(None, error="prometheus down")
        assert module.evaluate_gate(result, fail_open=True) == 0


# ---------------------------------------------------------------------------
# infra/prometheus-rules.yml — the two alert rules
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def error_budget_rules():
    rules = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))
    groups = [g for g in rules["groups"] if g["name"] == "slo-error-budget"]
    assert groups, "slo-error-budget group missing from prometheus-rules.yml"
    return {r["alert"]: r for r in groups[0]["rules"]}


class TestPrometheusRules:
    def test_two_alerts_present(self, error_budget_rules):
        assert set(error_budget_rules) == {"SLOErrorBudgetLow", "SLOErrorBudgetExhausted"}

    def test_soft_alert_is_ticket(self, error_budget_rules):
        rule = error_budget_rules["SLOErrorBudgetLow"]
        assert rule["labels"]["severity"] == "ticket"
        assert "< 0.2" in rule["expr"]

    def test_hard_alert_is_page(self, error_budget_rules):
        rule = error_budget_rules["SLOErrorBudgetExhausted"]
        assert rule["labels"]["severity"] == "page"
        assert "<= 0" in rule["expr"]

    def test_alerts_have_runbook_links(self, error_budget_rules):
        for rule in error_budget_rules.values():
            assert "error-budget-runbook.md" in rule["annotations"]["description"]


# ---------------------------------------------------------------------------
# Catalog-wide sanity: budget math for all 4 SLOs
# ---------------------------------------------------------------------------

class TestAllCatalogSlos:
    @pytest.mark.parametrize("slo_name", ["availability", "latency_p95", "quality", "cost_p99"])
    def test_catalog_slo_budget_scales_with_target(self, slo_name):
        slo = SLO_CATALOG[slo_name]
        total = 1000
        # At target (SLI == target) the whole budget is spent: error rate is
        # exactly the allowed (1 - target) -> remaining == 0.
        good_at_target = round(slo.target * total)
        b = ErrorBudget.from_counts(slo_name, slo.target, good_at_target, total)
        assert b.remaining_ratio == pytest.approx(0.0, abs=0.05)
        # A perfect window leaves the budget untouched.
        b_perfect = ErrorBudget.from_counts(slo_name, slo.target, total, total)
        assert b_perfect.remaining_ratio == pytest.approx(1.0)
        # All bad -> far beyond exhausted.
        b_bad = ErrorBudget.from_counts(slo_name, slo.target, 0, total)
        assert b_bad.remaining_ratio < 0
