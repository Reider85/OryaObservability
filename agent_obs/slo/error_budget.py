"""Error budget tracking + policy actions (PP27, T3.6.3).

The error budget is the allowance of bad events an SLO target implicitly
grants: ``budget_total = (1 - target) * total_events``. Every error event
burns one unit of it. ``remaining_ratio`` is the share of the budget still
unspent on the rolling window:

    budget_total    = (1 - target) * total_events
    budget_consumed = error_events                    (total - good)
    remaining_ratio = (budget_total - budget_consumed) / budget_total

When the budget runs down, two levels of automated response fire (Google SRE
Workbook "you build it, you run it"):

soft   (remaining_ratio <  0.2, i.e. > 80% consumed)
    Cost-saving mode, reversible: the tail sampler drops normal traces to 5%
    (see ``scripts/sampler/policy_engine.py``, reason ``error_budget_low``)
    and the eval pipeline halves its daily cost budget
    (:class:`agent_obs.eval.adaptive_sampler.BudgetEnforcer` multiplier).
    Ticket alert: ``SLOErrorBudgetLow``.

hard   (remaining_ratio <= 0.0, i.e. 100% consumed)
    Deploys are frozen: the CI/CD gate (``scripts/cicd/error_budget_gate.py``)
    fails its status check while any SLO budget is exhausted, the on-call is
    paged (``SLOErrorBudgetExhausted``) and an ``action="freeze_deploy"``
    row lands in the audit trail.

Actions fire exactly once per *state transition* (ok -> soft -> hard), not on
every 5-minute refresher tick — a budget that sits at 15% must not spam the
on-call or re-log the freeze every cycle. Recovery (hard -> soft -> ok) also
fires so downstream consumers restore normal rates.

Process boundaries: the sampler proxy and the eval worker are separate
processes from wherever the SLO refresher runs. Out-of-process consumers read
their signal from Prometheus (``slo_error_budget_remaining`` gauge for the
sampler) or from Redis (policy state key for the eval budget enforcer); the
in-process defaults here publish both, best-effort and never raising —
observability must not block business logic (SDK invariant).
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from agent_obs.metrics import slo_error_budget_actions_taken_total
from agent_obs.slo.definitions import SLO, SLO_CATALOG

logger = logging.getLogger(__name__)

#: Default share of budget that must remain to avoid soft actions (spec: 80% consumed).
DEFAULT_SOFT_THRESHOLD = 0.2

#: Default share at/under which hard actions fire (spec: 100% consumed).
DEFAULT_HARD_THRESHOLD = 0.0

#: Multiplier applied to the eval daily cost budget while the policy is in
#: soft or hard state (spec: "eval cost-budget сокращается до 50%").
EVAL_BUDGET_SOFT_MULTIPLIER = 0.5

#: Redis key holding the policy state for out-of-process consumers.
#: The eval BudgetEnforcer reads it to halve its budget during soft/hard.
POLICY_STATE_REDIS_KEY = "agent_obs:error_budget:policy_state"

#: TTL for the policy-state key: generous vs. the 5-minute refresh cadence,
#: so a stopped refresher expires the flag instead of freezing evals forever.
POLICY_STATE_TTL_SECONDS = 3600

# Policy states, ordered: severity grows with the index.
STATE_OK = "ok"
STATE_SOFT = "soft"
STATE_HARD = "hard"
_STATES = (STATE_OK, STATE_SOFT, STATE_HARD)

#: Action label values for slo_error_budget_actions_taken_total (bounded set).
ACTION_SOFT = "soft"
ACTION_HARD = "hard"
ACTION_RECOVER = "recover"

#: Signature of a policy action: (slo_name, budget) -> None.
PolicyAction = Callable[[str, "ErrorBudget"], None]


@dataclass(frozen=True)
class ErrorBudget:
    """Error budget of one SLO over its rolling window.

    Attributes:
        slo_name: Catalog key of the SLO this budget belongs to.
        target: The SLO target the budget was derived from (0..1).
        total_events: Total events observed in the window.
        error_events: Bad events in the window (``total - good``).
        budget_total: Allowed error events, ``(1 - target) * total_events``.
        budget_consumed: Error events seen so far (same as ``error_events``).
        remaining_ratio: Unspent share of the budget; may go negative when
            the budget is overspent (hard state).
    """

    slo_name: str
    target: float
    total_events: int
    error_events: int
    budget_total: float
    budget_consumed: float
    remaining_ratio: float

    @classmethod
    def from_counts(
        cls,
        slo_name: str,
        target: float,
        good_events: int,
        total_events: int,
    ) -> ErrorBudget:
        """Build a budget from raw SLI counts (good, total) over a window.

        An empty window burns no budget — remaining is a perfect 1.0, the
        same convention ``sli_from_counts`` applies to the SLI itself.
        """
        if not 0.0 < target <= 1.0:
            raise ValueError(f"SLO {slo_name!r}: target must be in (0, 1], got {target}")
        if total_events <= 0:
            return cls(
                slo_name=slo_name,
                target=target,
                total_events=0,
                error_events=0,
                budget_total=0.0,
                budget_consumed=0.0,
                remaining_ratio=1.0,
            )
        error_events = max(0, total_events - good_events)
        budget_total = (1.0 - target) * total_events
        if budget_total <= 0.0:
            # target == 1.0 leaves no allowance: any error exhausts it.
            remaining = 1.0 if error_events == 0 else -float(error_events)
        else:
            remaining = (budget_total - error_events) / budget_total
        return cls(
            slo_name=slo_name,
            target=target,
            total_events=total_events,
            error_events=error_events,
            budget_total=budget_total,
            budget_consumed=float(error_events),
            remaining_ratio=remaining,
        )

    @property
    def consumed_ratio(self) -> float:
        """Share of the budget consumed so far (can exceed 1.0 when overspent)."""
        if self.budget_total <= 0.0:
            return 0.0 if self.error_events == 0 else 1.0 + self.budget_consumed
        return self.budget_consumed / self.budget_total

    @property
    def clamped_remaining(self) -> float:
        """Remaining ratio clamped to [0, 1] — gauge-safe form."""
        return max(0.0, min(1.0, self.remaining_ratio))

    def classify(self, soft_threshold: float, hard_threshold: float) -> str:
        """Map the remaining ratio onto a policy state (ok | soft | hard)."""
        if self.remaining_ratio <= hard_threshold:
            return STATE_HARD
        if self.remaining_ratio < soft_threshold:
            return STATE_SOFT
        return STATE_OK


def _state_severity(state: str) -> int:
    return _STATES.index(state)


def _load_soft_threshold() -> float:
    raw = os.environ.get("AGENT_OBS_ERROR_BUDGET_SOFT_THRESHOLD", "")
    try:
        value = float(raw) if raw else DEFAULT_SOFT_THRESHOLD
    except ValueError:
        logger.warning("invalid AGENT_OBS_ERROR_BUDGET_SOFT_THRESHOLD=%r, using default", raw)
        return DEFAULT_SOFT_THRESHOLD
    if not 0.0 < value < 1.0:
        logger.warning("AGENT_OBS_ERROR_BUDGET_SOFT_THRESHOLD=%r outside (0,1), using default", raw)
        return DEFAULT_SOFT_THRESHOLD
    return value


class RedisPolicyStateSink:
    """Publishes the policy state to Redis for out-of-process consumers.

    The eval BudgetEnforcer (a separate process from the SLO refresher) reads
    this key on its refresh cycle and halves its daily cost budget while the
    state is ``soft`` or ``hard``.

    Best-effort by design: a Redis outage is logged and swallowed — losing the
    cost-saving signal must never take down the SLO refresher.
    """

    def __init__(self, redis_url: str | None = None) -> None:
        self.redis_url = redis_url or os.environ.get(
            "AGENT_OBS_REDIS_URL", "redis://localhost:6379/1"
        )
        self._client = None

    @property
    def client(self):
        if self._client is None:
            import redis  # local import: only this sink needs the dependency

            self._client = redis.from_url(self.redis_url)
        return self._client

    def __call__(self, state: str, slo_name: str, budget: ErrorBudget) -> None:
        payload = json.dumps(
            {
                "state": state,
                "slo": slo_name,
                "remaining_ratio": budget.remaining_ratio,
                "updated_at": time.time(),
            }
        )
        try:
            if state == STATE_OK:
                self.client.delete(POLICY_STATE_REDIS_KEY)
            else:
                self.client.set(POLICY_STATE_REDIS_KEY, payload, ex=POLICY_STATE_TTL_SECONDS)
        except Exception as exc:  # noqa: BLE001 - best-effort publish must never raise
            logger.warning("failed to publish error-budget policy state to Redis: %s", exc)


class ErrorBudgetPolicy:
    """Evaluates error budgets and fires soft/hard actions on transitions.

    The policy is stateful per SLO: an action fires once when the SLO enters
    ``soft`` or ``hard``, and a recovery action fires when it returns to
    ``ok``. Calling ``evaluate`` repeatedly with an unchanged state is a
    no-op (idempotent), which is what makes the 5-minute refresher loop safe.

    Args:
        catalog: SLO catalog the budgets belong to. Defaults to ``SLO_CATALOG``.
        soft_threshold: Remaining ratio below which soft actions fire.
            Defaults to ``AGENT_OBS_ERROR_BUDGET_SOFT_THRESHOLD`` (0.2).
        hard_threshold: Remaining ratio at/below which hard actions fire.
        soft_action: Called on transition *into* soft. Defaults to publishing
            the Redis policy state (sampler + eval consumers react to it).
        hard_action: Called on transition *into* hard. Defaults to the Redis
            state publish plus a ``freeze_deploy`` audit row.
        recover_action: Called on transition back to ok. Defaults to clearing
            the Redis policy state.
        hot_store: Optional HotStore for audit writes (hard actions record a
            ``freeze_deploy`` audit event). Audit failures never raise.
    """

    def __init__(
        self,
        catalog: dict[str, SLO] | None = None,
        soft_threshold: float | None = None,
        hard_threshold: float = DEFAULT_HARD_THRESHOLD,
        soft_action: PolicyAction | None = None,
        hard_action: PolicyAction | None = None,
        recover_action: PolicyAction | None = None,
        hot_store: object | None = None,
        state_sink: RedisPolicyStateSink | None = None,
    ) -> None:
        self.catalog = SLO_CATALOG if catalog is None else catalog
        self.soft_threshold = (
            _load_soft_threshold() if soft_threshold is None else soft_threshold
        )
        self.hard_threshold = hard_threshold
        self.hot_store = hot_store
        self._sink = state_sink if state_sink is not None else RedisPolicyStateSink()
        self._soft_action = soft_action if soft_action is not None else self._default_soft
        self._hard_action = hard_action if hard_action is not None else self._default_hard
        self._recover_action = (
            recover_action if recover_action is not None else self._default_recover
        )
        #: Last observed state per SLO; starts at ok (budgets start full).
        self._states: dict[str, str] = {}
        #: Last observed budget per SLO, for introspection/tests.
        self._budgets: dict[str, ErrorBudget] = {}

    # --- introspection ------------------------------------------------------

    def state(self, slo_name: str) -> str:
        """Last evaluated state of ``slo_name`` (ok before the first evaluate)."""
        return self._states.get(slo_name, STATE_OK)

    def budget(self, slo_name: str) -> ErrorBudget | None:
        """Last evaluated budget of ``slo_name``, or None if never evaluated."""
        return self._budgets.get(slo_name)

    @property
    def worst_state(self) -> str:
        """The most severe state across every evaluated SLO."""
        if not self._states:
            return STATE_OK
        return max(self._states.values(), key=_state_severity)

    # --- evaluation ---------------------------------------------------------

    def evaluate(self, slo_name: str, budget: ErrorBudget) -> str:
        """Classify ``budget`` and fire actions on a state transition.

        Returns the new state. Exceptions from actions are logged and
        swallowed — a broken hook must not stop the refresher or the other
        SLOs' evaluations (observability must never block business logic).
        """
        new_state = budget.classify(self.soft_threshold, self.hard_threshold)
        prev_state = self._states.get(slo_name, STATE_OK)
        self._states[slo_name] = new_state
        self._budgets[slo_name] = budget

        if new_state == prev_state:
            return new_state

        logger.warning(
            "error budget state change: slo=%s %s -> %s "
            "(remaining=%.4f, consumed=%.1f/%.1f events)",
            slo_name,
            prev_state,
            new_state,
            budget.remaining_ratio,
            budget.budget_consumed,
            budget.budget_total,
        )

        try:
            if new_state == STATE_HARD:
                slo_error_budget_actions_taken_total.labels(
                    name=slo_name, action=ACTION_HARD
                ).inc()
                self._hard_action(slo_name, budget)
                self._write_audit(slo_name, prev_state, new_state, budget)
            elif new_state == STATE_SOFT:
                slo_error_budget_actions_taken_total.labels(
                    name=slo_name, action=ACTION_SOFT
                ).inc()
                self._soft_action(slo_name, budget)
                self._write_audit(slo_name, prev_state, new_state, budget)
            else:  # back to ok — restore normal rates
                slo_error_budget_actions_taken_total.labels(
                    name=slo_name, action=ACTION_RECOVER
                ).inc()
                self._recover_action(slo_name, budget)
                self._write_audit(slo_name, prev_state, new_state, budget)
        except Exception:
            logger.exception(
                "error-budget action failed for slo=%s transition=%s->%s",
                slo_name, prev_state, new_state,
            )
        return new_state

    def evaluate_all(self, budgets: dict[str, ErrorBudget]) -> dict[str, str]:
        """Evaluate several budgets at once; returns {slo_name: state}."""
        return {name: self.evaluate(name, budget) for name, budget in budgets.items()}

    # --- default actions (cross-process wiring) -----------------------------

    def _default_soft(self, slo_name: str, budget: ErrorBudget) -> None:
        # Sampler: reacts to the slo_error_budget_remaining gauge published by
        # the refresher (scripts/sampler/policy_engine.py drops to 5%) — no
        # in-process call needed. Eval: needs the Redis policy-state flag.
        self._sink(STATE_SOFT, slo_name, budget)

    def _default_hard(self, slo_name: str, budget: ErrorBudget) -> None:
        # CI/CD gate reads Prometheus directly (scripts/cicd/error_budget_gate.py)
        # and fails the deploy status check; the Redis flag additionally pauses
        # eval spend while the budget is gone.
        self._sink(STATE_HARD, slo_name, budget)

    def _default_recover(self, slo_name: str, budget: ErrorBudget) -> None:
        self._sink(STATE_OK, slo_name, budget)

    # --- audit --------------------------------------------------------------

    def _write_audit(
        self, slo_name: str, prev_state: str, new_state: str, budget: ErrorBudget
    ) -> None:
        """Record the transition in audit_events_hot. Never raises."""
        if self.hot_store is None:
            return
        try:
            from agent_obs.guardrail.audit import ErrorBudgetActionAuditEvent

            event = ErrorBudgetActionAuditEvent.for_policy_action(
                slo_name=slo_name,
                prev_state=prev_state,
                new_state=new_state,
                remaining_ratio=budget.remaining_ratio,
                budget_total=budget.budget_total,
                budget_consumed=budget.budget_consumed,
            )
            self.hot_store.write_audit_event(event)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - best-effort audit must never block the refresh
            logger.error("failed to write error-budget audit event: %s", exc)


def build_policy_from_env(
    catalog: dict[str, SLO] | None = None,
    hot_store: object | None = None,
    **overrides,
) -> ErrorBudgetPolicy:
    """Construct a policy from environment variables (refresher entrypoint)."""
    params: dict = {"catalog": catalog, "hot_store": hot_store}
    if "AGENT_OBS_ERROR_BUDGET_SOFT_THRESHOLD" in os.environ:
        params["soft_threshold"] = _load_soft_threshold()
    params.update(overrides)
    return ErrorBudgetPolicy(**params)
