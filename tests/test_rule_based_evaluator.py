"""Tests for RuleBasedEvaluator (PC09) — synchronous rule-based evaluation."""

from __future__ import annotations

import dataclasses
import json
import time
from unittest.mock import MagicMock, patch

import pytest

from agent_obs.eval.base import EvalResult
from agent_obs.eval.rule_based import Rule, RuleBasedEvaluator
from agent_obs.observability import Span, SpanContext, SpanType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_span(
    text: str,
    field_path: str = "llm.output_text",
    trace_id: str = "test-trace-123",
) -> Span:
    """Create a test span with text content."""
    # Create context with specified trace_id by using parent
    parent_context = SpanContext(
        trace_id=trace_id,
        span_id="parent-span-456",
    )
    context = SpanContext.new(
        agent_id="test-agent",
        parent=parent_context,
    )
    
    span = Span(
        name="test_span",
        span_type=SpanType.LLM_CALL,
        context=context,
    )
    span.attributes[field_path] = text
    
    return span


def _make_rule(name: str, rule_type: str, field: str, params: dict | None = None) -> Rule:
    """Create a test rule."""
    return Rule(
        name=name,
        field=field,
        rule_type=rule_type,
        params=params or {},
    )


def _make_eval_result(
    trace_id: str = "test-trace-123",
    eval_name: str = "rule_based",
    rules_passed: int = 0,
    rules_failed: int = 0,
    flags: list[str] | None = None,
) -> EvalResult:
    """Create a test EvalResult."""
    return EvalResult(
        trace_id=trace_id,
        eval_id="test-eval-456",
        eval_name=eval_name,
        eval_version="1.0.0",
        eval_timestamp=time.time(),
        eval_latency_seconds=0.001,
        scores={
            "rules_passed": rules_passed,
            "rules_failed": rules_failed,
            "rule_results": {},
        },
        flags=flags or [],
    )


# ---------------------------------------------------------------------------
# Test EvalResult Dataclass
# ---------------------------------------------------------------------------


class TestEvalResult:
    """Test EvalResult dataclass contract."""
    
    def test_creation_minimal(self):
        """Test EvalResult creation with minimal required fields."""
        result = EvalResult(
            trace_id="trace-123",
            eval_id="eval-456",
            eval_name="test_eval",
            eval_version="1.0.0",
            eval_timestamp=time.time(),
            eval_latency_seconds=0.001,
            scores={"test": 1.0},
        )
        
        assert result.trace_id == "trace-123"
        assert result.eval_id == "eval-456"
        assert result.eval_name == "test_eval"
        assert result.scores == {"test": 1.0}
        assert result.flags == []
        assert result.judge_model == ""
    
    def test_creation_with_flags(self):
        """Test EvalResult creation with flags."""
        result = EvalResult(
            trace_id="trace-123",
            eval_id="eval-456",
            eval_name="test_eval",
            eval_version="1.0.0",
            eval_timestamp=time.time(),
            eval_latency_seconds=0.001,
            scores={"test": 1.0},
            flags=["pii_leak", "invalid_json"],
        )
        
        assert result.flags == ["pii_leak", "invalid_json"]
    
    def test_creation_with_optional_fields(self):
        """Test EvalResult creation with optional fields."""
        result = EvalResult(
            trace_id="trace-123",
            eval_id="eval-456",
            eval_name="test_eval",
            eval_version="1.0.0",
            eval_timestamp=time.time(),
            eval_latency_seconds=0.001,
            scores={"test": 1.0},
            judge_model="gpt-4",
            judge_prompt_sha256="abc123",
            reasoning="Test reasoning",
            flags=["test_flag"],
        )
        
        assert result.judge_model == "gpt-4"
        assert result.judge_prompt_sha256 == "abc123"
        assert result.reasoning == "Test reasoning"


# ---------------------------------------------------------------------------
# Test RuleBasedEvaluator
# ---------------------------------------------------------------------------


class TestRuleBasedEvaluator:
    """Test RuleBasedEvaluator class."""
    
    def test_creation_empty_rules(self):
        """Test evaluator creation with no rules."""
        evaluator = RuleBasedEvaluator(rules=[])
        
        assert evaluator.rules == []
        assert evaluator.version == "1.0.0"
    
    def test_creation_with_rules(self):
        """Test evaluator creation with rules."""
        rules = [
            _make_rule("test_rule", "regex", "llm.output_text"),
        ]
        evaluator = RuleBasedEvaluator(rules=rules)
        
        assert len(evaluator.rules) == 1
        assert evaluator.rules[0].name == "test_rule"
    
    def test_evaluate_empty_rules(self):
        """Test evaluation with no rules."""
        evaluator = RuleBasedEvaluator(rules=[])
        span = _make_span("test text")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.eval_name == "rule_based"
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 0
        assert result.flags == []
    
    def test_evaluate_field_not_found(self):
        """Test evaluation when field doesn't exist."""
        rule = _make_rule("test_rule", "regex", "nonexistent.field")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("test text")
        
        result = evaluator.evaluate(span, [span])
        
        # Rule should be skipped, not counted in pass/fail
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 0
        assert result.scores["rule_results"]["test_rule"]["skipped"] == True


# ---------------------------------------------------------------------------
# Test NoPIIInOutput Rule
# ---------------------------------------------------------------------------


class TestNoPiiInOutput:
    """Test no_pii_in_output rule implementation."""
    
    def test_clean_text_passes(self):
        """Test that clean text passes the rule."""
        rule = _make_rule("no_pii_in_output", "regex", "llm.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("Hello, how are you?")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
        assert result.flags == []
    
    def test_email_detected_fails(self):
        """Test that email detection fails the rule."""
        rule = _make_rule("no_pii_in_output", "regex", "llm.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("My email is test@example.com")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "pii_leak" in result.flags
        assert "detected_pii_types" in result.scores["rule_results"]["no_pii_in_output"]
        assert "email" in result.scores["rule_results"]["no_pii_in_output"]["detected_pii_types"]
    
    def test_phone_detected_fails(self):
        """Test that phone number detection fails the rule."""
        rule = _make_rule("no_pii_in_output", "regex", "llm.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("Call me at +7 (999) 123-45-67")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "pii_leak" in result.flags
        assert "phone" in result.scores["rule_results"]["no_pii_in_output"]["detected_pii_types"]
    
    def test_multiple_pii_detected(self):
        """Test detection of multiple PII types."""
        rule = _make_rule("no_pii_in_output", "regex", "llm.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("Email: test@example.com, Phone: +7 (999) 123-45-67")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "pii_leak" in result.flags
        detected_types = result.scores["rule_results"]["no_pii_in_output"]["detected_pii_types"]
        assert "email" in detected_types
        assert "phone" in detected_types
        assert result.scores["rule_results"]["no_pii_in_output"]["matches"] == 2
    
    def test_inn_detected_fails(self):
        """Test that INN detection fails the rule."""
        rule = _make_rule("no_pii_in_output", "regex", "llm.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("My INN is 1234567890")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "pii_leak" in result.flags
        assert "inn" in result.scores["rule_results"]["no_pii_in_output"]["detected_pii_types"]


# ---------------------------------------------------------------------------
# Test JsonOutputValid Rule
# ---------------------------------------------------------------------------


class TestJsonOutputValid:
    """Test json_output_valid rule implementation."""
    
    def test_valid_json_passes(self):
        """Test that valid JSON passes the rule."""
        rule = _make_rule("json_output_valid", "json_schema", "tool.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span('{"result": "success", "data": [1, 2, 3]}', "tool.output_text")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
        assert result.flags == []
    
    def test_invalid_json_fails(self):
        """Test that invalid JSON fails the rule."""
        rule = _make_rule("json_output_valid", "json_schema", "tool.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span('{"result": "success", "data": [1, 2, 3', "tool.output_text")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "invalid_json" in result.flags
        assert "Invalid JSON format" in result.scores["rule_results"]["json_output_valid"]["error"]
    
    def test_non_json_text_passes(self):
        """Test that non-JSON text passes the rule."""
        rule = _make_rule("json_output_valid", "json_schema", "tool.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("Regular text output", "tool.output_text")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
        assert result.flags == []
    
    def test_empty_json_passes(self):
        """Test that empty JSON passes the rule."""
        rule = _make_rule("json_output_valid", "json_schema", "tool.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("{}", "tool.output_text")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
        assert result.flags == []


# ---------------------------------------------------------------------------
# Test NoBlacklistedWords Rule
# ---------------------------------------------------------------------------


class TestNoBlacklistedWords:
    """Test no_blacklisted_words rule implementation."""
    
    def test_no_blacklisted_words_passes(self):
        """Test that text without blacklisted words passes."""
        rule = _make_rule("no_blacklisted_words", "blacklist", "llm.output_text", {
            "words": ["bad", "evil", "hate"]
        })
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("Hello, how are you today?")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
        assert result.flags == []
    
    def test_blacklisted_word_fails(self):
        """Test that blacklisted words fail the rule."""
        rule = _make_rule("no_blacklisted_words", "blacklist", "llm.output_text", {
            "words": ["bad", "evil", "hate"]
        })
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("This is a bad example")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "blacklisted_word" in result.flags
        assert "bad" in result.scores["rule_results"]["no_blacklisted_words"]["found_words"]
    
    def test_case_insensitive_detection(self):
        """Test that blacklisted words are detected case-insensitively."""
        rule = _make_rule("no_blacklisted_words", "blacklist", "llm.output_text", {
            "words": ["BAD"]
        })
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("This is a bad example")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "blacklisted_word" in result.flags
        assert "BAD" in result.scores["rule_results"]["no_blacklisted_words"]["found_words"]
    
    def test_multiple_blacklisted_words(self):
        """Test detection of multiple blacklisted words."""
        rule = _make_rule("no_blacklisted_words", "blacklist", "llm.output_text", {
            "words": ["bad", "evil", "hate"]
        })
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("This is a bad and evil example")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "blacklisted_word" in result.flags
        found_words = result.scores["rule_results"]["no_blacklisted_words"]["found_words"]
        assert "bad" in found_words
        assert "evil" in found_words


# ---------------------------------------------------------------------------
# Test MaxResponseChars Rule
# ---------------------------------------------------------------------------


class TestMaxResponseChars:
    """Test max_response_chars rule implementation."""
    
    def test_short_text_passes(self):
        """Test that short text passes the rule."""
        rule = _make_rule("max_response_chars", "max_length", "llm.output_text", {
            "max_chars": 1000
        })
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("Hello, how are you?")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
        assert result.flags == []
    
    def test_long_text_fails(self):
        """Test that long text fails the rule."""
        rule = _make_rule("max_response_chars", "max_length", "llm.output_text", {
            "max_chars": 10
        })
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("This is a very long text that exceeds the limit")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "response_too_long" in result.flags
        assert result.scores["rule_results"]["max_response_chars"]["actual_length"] == 47
        assert result.scores["rule_results"]["max_response_chars"]["max_length"] == 10
    
    def test_exact_length_passes(self):
        """Test that text at exact limit passes the rule."""
        rule = _make_rule("max_response_chars", "max_length", "llm.output_text", {
            "max_chars": 10
        })
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("1234567890")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
        assert result.flags == []


# ---------------------------------------------------------------------------
# Test Integration
# ---------------------------------------------------------------------------


class TestIntegration:
    """Test integration of multiple rules."""
    
    def test_all_rules_pass(self):
        """Test evaluation when all rules pass."""
        rules = [
            _make_rule("no_pii_in_output_llm", "regex", "llm.output_text"),
            _make_rule("json_output_valid_tool", "json_schema", "tool.output_text"),
            _make_rule("no_blacklisted_words_llm", "blacklist", "llm.output_text", {
                "words": ["bad", "evil"]
            }),
            _make_rule("max_response_chars_llm", "max_length", "llm.output_text", {
                "max_chars": 10000
            }),
        ]
        evaluator = RuleBasedEvaluator(rules=rules)
        
        # Create spans for different rules
        span1 = _make_span("Hello, how are you?")
        span2 = _make_span('{"result": "success"}', "tool.output_text")
        
        result = evaluator.evaluate(span1, [span1, span2])
        
        assert result.scores["rules_passed"] == 3
        assert result.scores["rules_failed"] == 0
        assert result.flags == []
    
    def test_mixed_rule_results(self):
        """Test evaluation with mixed pass/fail results."""
        rules = [
            _make_rule("no_pii_in_output_llm", "regex", "llm.output_text"),
            _make_rule("json_output_valid_tool", "json_schema", "tool.output_text"),
            _make_rule("no_blacklisted_words_llm", "blacklist", "llm.output_text", {
                "words": ["bad"]
            }),
            _make_rule("max_response_chars_llm", "max_length", "llm.output_text", {
                "max_chars": 10
            }),
        ]
        evaluator = RuleBasedEvaluator(rules=rules)
        
        # Create spans with issues
        span1 = _make_span("This is bad")  # blacklisted word + short
        span2 = _make_span('{"result": "success"}', "tool.output_text")
        
        result = evaluator.evaluate(span1, [span1, span2])
        
        assert result.scores["rules_passed"] == 1  # only pii (json skipped, blacklist+maxlen fail)
        assert result.scores["rules_failed"] == 2
        assert "blacklisted_word" in result.flags
        assert "response_too_long" in result.flags
    
    def test_field_path_resolution(self):
        """Test field path resolution for different span attributes."""
        rules = [
            _make_rule("no_pii_in_output_llm", "regex", "llm.output_text"),
            _make_rule("no_pii_in_output_tool", "regex", "tool.output_text"),
        ]
        evaluator = RuleBasedEvaluator(rules=rules)
        
        # Create spans with different field paths
        span_llm = _make_span("LLM output with email test@example.com")
        span_tool = _make_span("Tool output with phone +7 (999) 123-45-67", "tool.output_text")
        
        # Evaluate each span separately
        result_llm = evaluator.evaluate(span_llm, [span_llm, span_tool])
        result_tool = evaluator.evaluate(span_tool, [span_llm, span_tool])
        
        # Both spans should fail their respective rules
        assert result_llm.scores["rules_passed"] == 0
        assert result_llm.scores["rules_failed"] == 1
        assert "pii_leak" in result_llm.flags
        
        assert result_tool.scores["rules_passed"] == 0
        assert result_tool.scores["rules_failed"] == 1
        assert "pii_leak" in result_tool.flags


# ---------------------------------------------------------------------------
# Test Error Handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Test error handling in evaluator."""
    
    def test_unknown_rule_type(self):
        """Test handling of unknown rule types."""
        rule = _make_rule("unknown_rule", "unknown_type", "llm.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        span = _make_span("test text")
        
        result = evaluator.evaluate(span, [span])
        
        # Unknown rule type should be skipped (pass)
        assert result.scores["rules_passed"] == 1
        assert result.scores["rules_failed"] == 0
    
    def test_invalid_json_schema_rule(self):
        """Test handling of invalid JSON in schema rule."""
        rule = _make_rule("json_output_valid", "json_schema", "tool.output_text")
        evaluator = RuleBasedEvaluator(rules=[rule])
        
        # Test with JSON that looks valid but is actually invalid
        span = _make_span('{"result": "success", "data": [1, 2, 3', "tool.output_text")
        
        result = evaluator.evaluate(span, [span])
        
        assert result.scores["rules_passed"] == 0
        assert result.scores["rules_failed"] == 1
        assert "invalid_json" in result.flags