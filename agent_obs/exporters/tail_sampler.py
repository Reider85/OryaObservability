"""Adaptive sampling v2 policy engine for agent-obs (PP03).

Extends CRITICAL-сэмплер (PC29–PC32) до adaptive v2: eval-aware + dynamic rate.
Пolicies operate on SDK Span and EvalResult objects (PP0 contract).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from agent_obs.eval.base import EvalResult
from agent_obs.observability import Span

logger = logging.getLogger(__name__)

# PP0 contract: SamplingPolicy dataclass
@dataclass
class SamplingPolicy:
    """Policy for adaptive sampling (PP0 contract).
    
    A predicate evaluates a Span and associated EvalResults; if true,
    the policy may decide to keep the trace with keep_rate probability.
    """
    name: str
    predicate: Callable[[Span, list[EvalResult]], bool]
    keep_rate: float  # 0.0..1.0; 1.0 = keep 100%
    reason_label: str  # for tail_sampler_kept_ratio_by_reason metric


@dataclass
class SamplingDecision:
    """Decision from SamplingPolicyV2 for a span.
    
    - keep: True if trace should be kept, False if dropped
    - policy_name: which policy made the decision
    - reason_label: label for metrics
    - content_rate: override for content sampling (percent); None = use env default
    - span_id: for idempotency
    - policy_version: for idempotency across runs
    """
    keep: bool
    policy_name: str
    reason_label: str
    content_rate: int | None = None  # percent, 0..100; None = env default
    span_id: str = ""
    policy_version: str = "v2"


class SamplingPolicyV2:
    """Adaptive sampling v2 policy engine (PP03).
    
    Evaluates a list of policies against a Span and EvalResults, returns a decision.
    Supports content-rate override for eval_fail_keep.
    """
    
    def __init__(
        self,
        policies: list[SamplingPolicy] | None = None,
        policy_version: str = "v2",
        cost_threshold: float | None = None,
        normal_rate: float | None = None,
    ):
        self.policies = policies or self._build_default_policies(cost_threshold, normal_rate)
        self.policy_version = policy_version
    
    def _build_default_policies(
        self,
        cost_threshold: float | None,
        normal_rate: float | None,
    ) -> list[SamplingPolicy]:
        """Build the 5 default policies (PP03)."""
        cost_threshold = cost_threshold if cost_threshold is not None else 0.05
        normal_rate = normal_rate if normal_rate is not None else 0.10
        
        def error_predicate(span: Span, evals: list[EvalResult]) -> bool:
            # Adapt spec: span.status == "ERROR" → real Span attributes
            return span.attributes.get("status") == "error"
        
        def eval_fail_predicate(span: Span, evals: list[EvalResult]) -> bool:
            # PP03: any eval result with faithfulness < 0.7
            for er in evals:
                try:
                    faithfulness = float(er.scores.get("faithfulness", 1.0))
                    if faithfulness < 0.7:
                        return True
                except (ValueError, TypeError):
                    # Unparseable faithfulness → treat as 1.0 (no match)
                    continue
            return False
        
        def cost_high_predicate(span: Span, evals: list[EvalResult]) -> bool:
            # PP03: span.cost_usd > threshold (real model: cost.usd, cost.usd_sum, cost.over_budget)
            # Check per-call cost, trace total cost, and over_budget flag
            cost_usd = float(span.attributes.get("cost.usd", 0.0))
            cost_usd_sum = float(span.attributes.get("cost.usd_sum", 0.0))
            over_budget = span.attributes.get("cost.over_budget") == "true"
            
            return (
                cost_usd > cost_threshold or
                cost_usd_sum > cost_threshold or
                over_budget
            )
        
        def security_predicate(span: Span, evals: list[EvalResult]) -> bool:
            # PP03: span.attributes.get("security.incident") == "true"
            return span.attributes.get("security.incident") == "true"
        
        def random_predicate(span: Span, evals: list[EvalResult]) -> bool:
            # Always matches; decision based on keep_rate
            return True
        
        return [
            SamplingPolicy(
                name="error_keep",
                predicate=error_predicate,
                keep_rate=1.0,
                reason_label="error",
            ),
            SamplingPolicy(
                name="eval_fail_keep",
                predicate=eval_fail_predicate,
                keep_rate=1.0,
                reason_label="eval_fail",
            ),
            SamplingPolicy(
                name="cost_high_keep",
                predicate=cost_high_predicate,
                keep_rate=1.0,
                reason_label="cost",
            ),
            SamplingPolicy(
                name="security_keep",
                predicate=security_predicate,
                keep_rate=1.0,
                reason_label="security",
            ),
            SamplingPolicy(
                name="random",
                predicate=random_predicate,
                keep_rate=normal_rate,
                reason_label="random",
            ),
        ]
    
    def _should_keep(self, span_id: str, keep_rate: float) -> bool:
        """Idempotent keep decision based on span_id and keep_rate (PP03).
        
        Uses deterministic hash: same span_id + policy_version → same decision.
        """
        if keep_rate >= 1.0:
            return True
        if keep_rate <= 0.0:
            return False
        
        # Hash span_id + policy_version for stability
        hash_input = f"{span_id}:{self.policy_version}"
        digest = hashlib.sha256(hash_input.encode("utf-8")).digest()
        # Convert first 8 bytes to uniform [0,1) value
        value = int.from_bytes(digest[:8], "big") / (2**64)
        return value < keep_rate
    
    def decide(self, span: Span, evals: list[EvalResult]) -> SamplingDecision:
        """Decide sampling for a span based on policies (PP03).
        
        Iterates policies in order; first matching policy decides.
        Returns decision with content_rate override for eval_fail_keep.
        """
        span_id = span.context.span_id
        
        for policy in self.policies:
            if policy.predicate(span, evals):
                keep = self._should_keep(span_id, policy.keep_rate)
                content_rate = None  # default to env unless override
                
                # PP03: Content-sampling override for eval_fail_keep
                if policy.name == "eval_fail_keep":
                    content_rate = 30  # 30% instead of default 10%
                
                return SamplingDecision(
                    keep=keep,
                    policy_name=policy.name,
                    reason_label=policy.reason_label,
                    content_rate=content_rate,
                    span_id=span_id,
                    policy_version=self.policy_version,
                )
        
        # No policy matched (shouldn't happen with random policy)
        return SamplingDecision(
            keep=False,
            policy_name="none",
            reason_label="none",
            span_id=span_id,
            policy_version=self.policy_version,
        )


def build_default_policies(
    cost_threshold: float | None = None,
    normal_rate: float | None = None,
) -> list[SamplingPolicy]:
    """Build default policies for SamplingPolicyV2 (PP03)."""
    return SamplingPolicyV2()._build_default_policies(cost_threshold, normal_rate)


def normalize_rate_from_env(
    raw_value: str | None,
    default: float,
) -> float:
    """Parse TAIL_SAMPLER_NORMAL_RATE from env (PP03).
    
    Accepts both percent (e.g., "10") and float (e.g., "0.10").
    If >1, treat as percent and divide by 100.
    """
    if raw_value is None:
        return default
    
    try:
        rate = float(raw_value)
    except ValueError:
        logger.warning(
            "Invalid sampling rate %r, using default %.2f",
            raw_value,
            default,
        )
        return default
    
    if rate > 1.0:
        # Treat as percent
        rate = rate / 100.0
    
    return max(0.0, min(1.0, rate))


def normalize_cost_threshold_from_env(
    raw_value: str | None,
    default: float,
) -> float:
    """Parse TAIL_SAMPLER_COST_THRESHOLD from env (PP03)."""
    if raw_value is None:
        return default
    
    try:
        return float(raw_value)
    except ValueError:
        logger.warning(
            "Invalid cost threshold %r, using default %.2f",
            raw_value,
            default,
        )
        return default


def content_rate_for_decision(
    decision: SamplingDecision,
    env_default: int = 10,
) -> int:
    """Get content rate for a decision (PP03 override).
    
    If decision has content_rate override, use it; otherwise use env_default.
    """
    return decision.content_rate if decision.content_rate is not None else env_default


def should_keep_content_with_decision(
    trace_id: str,
    decision: SamplingDecision,
    env_default: int = 10,
) -> bool:
    """Determine if a trace should keep content based on decision (PP03).
    
    Uses deterministic sampling like content_sampling.should_keep_content,
    but with rate from decision (eval_fail_keep → 30%, otherwise env_default).
    """
    rate = content_rate_for_decision(decision, env_default)
    from agent_obs.content_sampling import should_keep_content
    
    return should_keep_content(trace_id, rate)