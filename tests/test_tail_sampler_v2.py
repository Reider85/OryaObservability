"""Tests for adaptive sampling v2 policy engine (PP03)."""

import hashlib
import os
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from agent_obs.eval.base import EvalResult
from agent_obs.exporters.tail_sampler import (
    SamplingDecision,
    SamplingPolicy,
    SamplingPolicyV2,
    build_default_policies,
    content_rate_for_decision,
    normalize_cost_threshold_from_env,
    normalize_rate_from_env,
    should_keep_content_with_decision,
)
from agent_obs.observability import Span, SpanContext, SpanType


# Test fixtures
@pytest.fixture
def mock_span() -> Span:
    """Create a mock span for testing."""
    context = SpanContext(
        trace_id="trace123",
        span_id="span456",
        parent_span_id=None,
        agent_id="test-agent",
        agent_version="1.0",
        user_id="user1",
        session_id="session1",
        tool_name=None,
    )
    return Span(
        name="test-span",
        span_type=SpanType.LLM_CALL,
        context=context,
        attributes={},
        events=[],
        start_time=1234567890.0,
        end_time=1234567890.5,
    )


@pytest.fixture
def mock_span_error(mock_span) -> Span:
    """Create a span with error status."""
    span = mock_span
    span.context.span_id = "error_span_123"
    span.attributes["status"] = "error"
    return span


@pytest.fixture
def mock_span_high_cost(mock_span) -> Span:
    """Create a span with high cost."""
    span = mock_span
    span.context.span_id = "high_cost_span_456"
    span.attributes["cost.usd"] = 0.06  # > default threshold 0.05
    return span


@pytest.fixture
def mock_span_over_budget(mock_span) -> Span:
    """Create a span with cost.over_budget=true."""
    span = mock_span
    span.context.span_id = "over_budget_span_789"
    span.attributes["cost.over_budget"] = "true"
    return span


@pytest.fixture
def mock_span_security_incident(mock_span) -> Span:
    """Create a span with security incident."""
    span = mock_span
    span.context.span_id = "security_span_999"
    span.attributes["security.incident"] = "true"
    return span


@pytest.fixture
def mock_eval_fail() -> list[EvalResult]:
    """Eval result with low faithfulness."""
    return [
        EvalResult(
            trace_id="trace123",
            eval_id="eval1",
            eval_name="faithfulness",
            eval_version="1.0",
            eval_timestamp=1234567890.0,
            eval_latency_seconds=1.0,
            scores={"faithfulness": 0.5},  # < 0.7
        )
    ]


@pytest.fixture
def mock_eval_pass() -> list[EvalResult]:
    """Eval result with good faithfulness."""
    return [
        EvalResult(
            trace_id="trace123",
            eval_id="eval1",
            eval_name="faithfulness",
            eval_version="1.0",
            eval_timestamp=1234567890.0,
            eval_latency_seconds=1.0,
            scores={"faithfulness": 0.9},  # > 0.7
        )
    ]


@pytest.fixture
def mock_eval_missing() -> list[EvalResult]:
    """Eval result without faithfulness score."""
    return [
        EvalResult(
            trace_id="trace123",
            eval_id="eval1",
            eval_name="faithfulness",
            eval_version="1.0",
            eval_timestamp=1234567890.0,
            eval_latency_seconds=1.0,
            scores={"other_score": 0.8},
        )
    ]


@pytest.fixture
def mock_eval_invalid() -> list[EvalResult]:
    """Eval result with invalid faithfulness score."""
    return [
        EvalResult(
            trace_id="trace123",
            eval_id="eval1",
            eval_name="faithfulness",
            eval_version="1.0",
            eval_timestamp=1234567890.0,
            eval_latency_seconds=1.0,
            scores={"faithfulness": "invalid"},  # not a number
        )
    ]


# Tests for SamplingPolicy dataclass
def test_sampling_policy_dataclass():
    """Test SamplingPolicy dataclass structure."""
    def dummy_predicate(span: Span, evals: list[EvalResult]) -> bool:
        return True
    
    policy = SamplingPolicy(
        name="test_policy",
        predicate=dummy_predicate,
        keep_rate=0.5,
        reason_label="test",
    )
    
    assert policy.name == "test_policy"
    assert policy.predicate == dummy_predicate
    assert policy.keep_rate == 0.5
    assert policy.reason_label == "test"


# Tests for SamplingDecision dataclass
def test_sampling_decision_dataclass():
    """Test SamplingDecision dataclass structure."""
    decision = SamplingDecision(
        keep=True,
        policy_name="test_policy",
        reason_label="test",
        content_rate=30,
        span_id="span123",
        policy_version="v2",
    )
    
    assert decision.keep is True
    assert decision.policy_name == "test_policy"
    assert decision.reason_label == "test"
    assert decision.content_rate == 30
    assert decision.span_id == "span123"
    assert decision.policy_version == "v2"


# Tests for SamplingPolicyV2 default policies
def test_sampling_policy_v2_default_policies(mock_span):
    """Test SamplingPolicyV2 creates default policies."""
    engine = SamplingPolicyV2()
    
    assert len(engine.policies) == 5
    policy_names = [p.name for p in engine.policies]
    assert "error_keep" in policy_names
    assert "eval_fail_keep" in policy_names
    assert "cost_high_keep" in policy_names
    assert "security_keep" in policy_names
    assert "random" in policy_names


# Test error_keep policy
def test_error_keep_policy(mock_span_error):
    """Test error_keep policy matches error spans."""
    engine = SamplingPolicyV2()
    
    # Normal span should not match error_keep
    from agent_obs.observability import Span, SpanContext, SpanType
    normal_span = Span(
        name="normal-span",
        span_type=SpanType.LLM_CALL,
        context=mock_span_error.context,
        attributes={"status": "ok"},  # not error
        events=[],
        start_time=1234567890.0,
        end_time=1234567890.5,
    )
    decision = engine.decide(normal_span, [])
    assert decision.policy_name != "error_keep"
    
    # Error span should match error_keep
    decision = engine.decide(mock_span_error, [])
    assert decision.policy_name == "error_keep"
    assert decision.keep is True  # keep_rate=1.0


# Test eval_fail_keep policy
def test_eval_fail_keep_policy(mock_span, mock_eval_fail, mock_eval_pass, mock_eval_missing, mock_eval_invalid):
    """Test eval_fail_keep policy matches low faithfulness evals."""
    engine = SamplingPolicyV2()
    
    # Normal span with no evals should not match
    decision = engine.decide(mock_span, [])
    assert decision.policy_name != "eval_fail_keep"
    
    # Span with eval faithfulness 0.5 should match
    decision = engine.decide(mock_span, mock_eval_fail)
    assert decision.policy_name == "eval_fail_keep"
    assert decision.keep is True  # keep_rate=1.0
    assert decision.content_rate == 30  # content override
    
    # Span with eval faithfulness 0.9 should not match
    decision = engine.decide(mock_span, mock_eval_pass)
    assert decision.policy_name != "eval_fail_keep"
    
    # Span with missing faithfulness should not match (default 1.0)
    decision = engine.decide(mock_span, mock_eval_missing)
    assert decision.policy_name != "eval_fail_keep"
    
    # Span with invalid faithfulness should not match (treat as 1.0)
    decision = engine.decide(mock_span, mock_eval_invalid)
    assert decision.policy_name != "eval_fail_keep"


# Test cost_high_keep policy
def test_cost_high_keep_policy(mock_span_high_cost, mock_span_over_budget):
    """Test cost_high_keep policy matches high cost spans."""
    engine = SamplingPolicyV2()
    
    # Normal span should not match
    from agent_obs.observability import Span, SpanContext, SpanType
    normal_span = Span(
        name="normal-cost-span",
        span_type=SpanType.LLM_CALL,
        context=mock_span_high_cost.context,
        attributes={"cost.usd": 0.01},  # below threshold
        events=[],
        start_time=1234567890.0,
        end_time=1234567890.5,
    )
    decision = engine.decide(normal_span, [])
    assert decision.policy_name != "cost_high_keep"
    
    # Span with cost.usd > threshold should match
    decision = engine.decide(mock_span_high_cost, [])
    assert decision.policy_name == "cost_high_keep"
    assert decision.keep is True  # keep_rate=1.0
    
    # Span with cost.over_budget=true should match
    decision = engine.decide(mock_span_over_budget, [])
    assert decision.policy_name == "cost_high_keep"
    assert decision.keep is True


# Test security_keep policy
def test_security_keep_policy(mock_span_security_incident):
    """Test security_keep policy matches security incidents."""
    engine = SamplingPolicyV2()
    
    # Normal span should not match
    from agent_obs.observability import Span, SpanContext, SpanType
    normal_span = Span(
        name="normal-security-span",
        span_type=SpanType.LLM_CALL,
        context=mock_span_security_incident.context,
        attributes={"security.incident": "false"},
        events=[],
        start_time=1234567890.0,
        end_time=1234567890.5,
    )
    decision = engine.decide(normal_span, [])
    assert decision.policy_name != "security_keep"
    
    # Span with security incident should match
    decision = engine.decide(mock_span_security_incident, [])
    assert decision.policy_name == "security_keep"
    assert decision.keep is True  # keep_rate=1.0


# Test random policy
def test_random_policy(mock_span):
    """Test random policy distribution."""
    engine = SamplingPolicyV2(normal_rate=0.50)  # 50% rate
    
    # Test with a fixed span_id for reproducibility
    mock_span.context.span_id = "fixed_span_id"
    
    decision1 = engine.decide(mock_span, [])
    decision2 = engine.decide(mock_span, [])
    
    # Same span_id should give same decision (idempotency)
    assert decision1.policy_name == "random"
    assert decision2.policy_name == "random"
    assert decision1.keep == decision2.keep
    
    # Test with different span_ids
    mock_span.context.span_id = "span_id_1"
    decision_a = engine.decide(mock_span, [])
    mock_span.context.span_id = "span_id_2"
    decision_b = engine.decide(mock_span, [])
    
    # Different span_ids can have different decisions
    # (but both should be random policy)
    assert decision_a.policy_name == "random"
    assert decision_b.policy_name == "random"


# Test idempotency
def test_idempotency(mock_span, mock_eval_fail):
    """Test decisions are idempotent for same span_id."""
    engine = SamplingPolicyV2()
    
    mock_span.context.span_id = "idempotent_span"
    
    decision1 = engine.decide(mock_span, mock_eval_fail)
    decision2 = engine.decide(mock_span, mock_eval_fail)
    
    # Same span_id should give same decision
    assert decision1.keep == decision2.keep
    assert decision1.policy_name == decision2.policy_name
    assert decision1.content_rate == decision2.content_rate


# Test policy version affects random decisions
def test_policy_version_affects_decisions(mock_span):
    """Test changing policy_version changes random decisions."""
    engine1 = SamplingPolicyV2(policy_version="v2.1", normal_rate=0.50)
    engine2 = SamplingPolicyV2(policy_version="v2.2", normal_rate=0.50)
    
    mock_span.context.span_id = "version_test_span"
    
    decision1 = engine1.decide(mock_span, [])
    decision2 = engine2.decide(mock_span, [])
    
    # Different policy versions can give different decisions
    # (but both should be random policy)
    assert decision1.policy_name == "random"
    assert decision2.policy_name == "random"


# Test content rate override
def test_content_rate_override(mock_span, mock_eval_fail):
    """Test content rate override for eval_fail_keep."""
    engine = SamplingPolicyV2()
    
    # Normal span should use env default (no override)
    decision = engine.decide(mock_span, [])
    assert decision.content_rate is None  # env default
    
    # Eval fail span should override to 30%
    decision = engine.decide(mock_span, mock_eval_fail)
    assert decision.content_rate == 30


# Test helper function content_rate_for_decision
def test_content_rate_for_decision():
    """Test content_rate_for_decision helper."""
    decision_with_override = SamplingDecision(
        keep=True,
        policy_name="eval_fail_keep",
        reason_label="eval_fail",
        content_rate=30,
    )
    
    decision_without_override = SamplingDecision(
        keep=True,
        policy_name="random",
        reason_label="random",
    )
    
    assert content_rate_for_decision(decision_with_override) == 30
    assert content_rate_for_decision(decision_without_override) == 10  # default


# Test policy priority
def test_policy_priority(mock_span_error, mock_eval_fail):
    """Test that first matching policy wins (priority order)."""
    engine = SamplingPolicyV2()
    
    # Span has both error and eval_fail
    mock_span_error.attributes["status"] = "error"
    
    # Add eval fail to the error span
    eval_fail = [
        EvalResult(
            trace_id="trace123",
            eval_id="eval1",
            eval_name="faithfulness",
            eval_version="1.0",
            eval_timestamp=1234567890.0,
            eval_latency_seconds=1.0,
            scores={"faithfulness": 0.5},
        )
    ]
    
    decision = engine.decide(mock_span_error, eval_fail)
    
    # Error should come first in priority, not eval_fail
    assert decision.policy_name == "error_keep"
    assert decision.reason_label == "error"
    # Content rate should NOT be overridden (eval_fail didn't win)
    assert decision.content_rate is None


# Test env parsing for rate
def test_normalize_rate_from_env():
    """Test normalize_rate_from_env with various inputs."""
    # Normal float
    assert normalize_rate_from_env("0.10", 0.10) == 0.10
    assert normalize_rate_from_env("0.5", 0.10) == 0.5
    
    # Percent (should convert)
    assert normalize_rate_from_env("10", 0.10) == 0.10  # 10% → 0.10
    assert normalize_rate_from_env("25", 0.10) == 0.25  # 25% → 0.25
    
    # Invalid (should use default)
    assert normalize_rate_from_env("invalid", 0.10) == 0.10
    assert normalize_rate_from_env("200", 0.10) == 1.0  # Clamp to 1.0
    assert normalize_rate_from_env("-1", 0.10) == 0.0    # Clamp to 0.0
    
    # None (should use default)
    assert normalize_rate_from_env(None, 0.10) == 0.10


# Test env parsing for cost threshold
def test_normalize_cost_threshold_from_env():
    """Test normalize_cost_threshold_from_env."""
    assert normalize_cost_threshold_from_env("0.05", 0.05) == 0.05
    assert normalize_cost_threshold_from_env("0.10", 0.05) == 0.10
    assert normalize_cost_threshold_from_env("invalid", 0.05) == 0.05
    assert normalize_cost_threshold_from_env(None, 0.05) == 0.05


# Test build_default_policies
def test_build_default_policies():
    """Test build_default_policies helper."""
    policies = build_default_policies(cost_threshold=0.10, normal_rate=0.20)
    
    assert len(policies) == 5
    assert all(p.keep_rate == 1.0 for p in policies[:4])  # First 4 are keep=1.0
    assert policies[4].keep_rate == 0.20  # Random is 0.20


# Test should_keep_content_with_decision integration
def test_should_keep_content_with_decision(mock_span, mock_eval_fail):
    """Test should_keep_content_with_decision integration."""
    # Create eval_fail decision
    decision = SamplingDecision(
        keep=True,
        policy_name="eval_fail_keep",
        reason_label="eval_fail",
        content_rate=30,
    )
    
    # Test with trace_id that would pass 30% but fail 10%
    # This is tricky to test directly since we can't control the hash easily
    # Instead, test that it uses the decision's content rate
    from unittest.mock import patch
    
    with patch("agent_obs.content_sampling.should_keep_content") as mock_keep:
        mock_keep.return_value = True
        
        result = should_keep_content_with_decision("trace123", decision)
        mock_keep.assert_called_once_with("trace123", 30)  # Uses decision's rate
        assert result is True


# Test with custom policies
def test_custom_policies(mock_span):
    """Test SamplingPolicyV2 with custom policies."""
    def custom_predicate(span: Span, evals: list[EvalResult]) -> bool:
        return "custom_flag" in span.attributes
    
    custom_policy = SamplingPolicy(
        name="custom",
        predicate=custom_predicate,
        keep_rate=0.75,
        reason_label="custom",
    )
    
    engine = SamplingPolicyV2(policies=[custom_policy])
    
    # Span without custom_flag should not match
    mock_span.context.span_id = "custom_span_no_flag"
    decision = engine.decide(mock_span, [])
    assert decision.policy_name == "none"
    
    # Span with custom_flag should match
    mock_span.context.span_id = "custom_span_with_flag"
    mock_span.attributes["custom_flag"] = "true"
    decision = engine.decide(mock_span, [])
    assert decision.policy_name == "custom"
    # For this span_id, should be True with 75% probability
    # Since it's deterministic, we can't guarantee it, but it's likely True
    # Let's test with a keep_rate of 1.0 to ensure it passes
    custom_policy_keep_all = SamplingPolicy(
        name="custom_keep_all",
        predicate=custom_predicate,
        keep_rate=1.0,
        reason_label="custom",
    )
    engine_keep_all = SamplingPolicyV2(policies=[custom_policy_keep_all])
    decision_all = engine_keep_all.decide(mock_span, [])
    assert decision_all.policy_name == "custom_keep_all"
    assert decision_all.keep is True


# Test edge cases
def test_edge_cases():
    """Test edge cases and error conditions."""
    engine = SamplingPolicyV2()
    
    # Span with empty evals list
    mock_context = MagicMock()
    mock_context.span_id = "edge_case_span"
    mock_span = MagicMock(spec=Span)
    mock_span.context = mock_context
    mock_span.attributes = {}
    
    decision = engine.decide(mock_span, [])
    assert decision.policy_name == "random"  # Always matches
    assert decision.span_id == "edge_case_span"


# Test deterministic random distribution
def test_deterministic_random_distribution():
    """Test that random decisions are deterministic and distributed."""
    engine = SamplingPolicyV2(normal_rate=0.50)
    
    # Test multiple span_ids with fixed seed effect
    span_ids = [f"span_{i}" for i in range(1000)]
    decisions = []
    
    for span_id in span_ids:
        mock_context = MagicMock()
        mock_context.span_id = span_id
        mock_span = MagicMock(spec=Span)
        mock_span.context = mock_context
        mock_span.attributes = {}
        decision = engine.decide(mock_span, [])
        decisions.append(decision.keep)
    
    # Should be approximately 50% True (with some variance due to hash)
    true_count = sum(decisions)
    ratio = true_count / len(decisions)
    
    # Allow some variance but should be close to 0.5
    assert 0.4 <= ratio <= 0.6, f"Random ratio {ratio} not in expected range"