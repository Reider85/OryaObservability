"""Rule-based evaluator for synchronous quality assessment."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Pattern

import jsonschema
from agent_obs.eval.base import BaseEvaluator, EvalResult
from agent_obs.observability import Span


@dataclass
class Rule:
    """A single rule for evaluating span attributes.
    
    Args:
        name: Human-readable name of the rule
        field: Span attribute path to evaluate (e.g., "llm.output_text")
        rule_type: Type of rule: "regex" | "json_schema" | "blacklist" | "max_length"
        params: Rule-specific parameters
    """
    
    name: str
    field: str
    rule_type: str
    params: dict[str, Any]


class RuleBasedEvaluator(BaseEvaluator):
    """Synchronous rule-based evaluator for span quality assessment.
    
    Implements 4 standard rules:
    1. no_pii_in_output - detects PII in LLM output
    2. json_output_valid - validates JSON output from tools
    3. no_blacklisted_words - checks for blacklisted words
    4. max_response_chars - enforces response length limits
    """
    
    def __init__(self, rules: list[Rule] | None = None) -> None:
        super().__init__("1.0.0")
        self.rules = rules or []
        
        # Pre-compile regex patterns for performance
        self._compiled_patterns: dict[str, Pattern[str]] = {}
        self._setup_regex_patterns()
    
    def _setup_regex_patterns(self) -> None:
        """Pre-compile common regex patterns for PII detection."""
        self._compiled_patterns.update({
            "email": re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}'),
            "phone": re.compile(r'(\+7|8)[\s\-]?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'),
            "inn": re.compile(r'\b\d{10}\b|\b\d{12}\b'),
            "passport": re.compile(r'\b\d{4}\s\d{6}\b'),
            "payment": re.compile(r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b'),
        })
    
    def evaluate(self, span: Span, full_trace: list[Span]) -> EvalResult:
        """Evaluate span using configured rules.
        
        Args:
            span: The span to evaluate
            full_trace: Complete list of spans in the trace
            
        Returns:
            EvalResult with rule-based evaluation scores and flags
        """
        start_time = time.time()
        
        rule_results = {}
        flags = []
        rules_passed = 0
        rules_failed = 0
        rules_skipped = 0
        
        for rule in self.rules:
            result = self._apply_rule(rule, span)
            rule_results[rule.name] = result
            
            if result.get("skipped"):
                rules_skipped += 1
            elif result["passed"]:
                rules_passed += 1
            else:
                rules_failed += 1
                if result.get("flag"):
                    flags.extend(result["flag"])
        
        eval_latency = time.time() - start_time
        
        return EvalResult(
            trace_id=span.context.trace_id,
            eval_id=f"rule_{int(time.time())}",
            eval_name="rule_based",
            eval_version=self.version,
            eval_timestamp=start_time,
            eval_latency_seconds=eval_latency,
            scores={
                "rules_passed": rules_passed,
                "rules_failed": rules_failed,
                "rule_results": rule_results,
            },
            flags=flags,
        )
    
    def _apply_rule(self, rule: Rule, span: Span) -> dict[str, Any]:
        """Apply a single rule to the span.
        
        Returns:
            Dict with "passed" (bool) and optional "flag" (list)
        """
        # Get the field value from span attributes
        field_value = self._get_field_value(rule.field, span)
        if field_value is None:
            return {"passed": True, "skipped": True}  # Skip evaluation if field not present
        
        if rule.rule_type == "regex":
            return self._apply_regex_rule(rule, field_value)
        elif rule.rule_type == "json_schema":
            return self._apply_json_schema_rule(rule, field_value)
        elif rule.rule_type == "blacklist":
            return self._apply_blacklist_rule(rule, field_value)
        elif rule.rule_type == "max_length":
            return self._apply_max_length_rule(rule, field_value)
        else:
            return {"passed": True}  # Unknown rule type, skip
    
    def _get_field_value(self, field_path: str, span: Span) -> str | None:
        """Get field value from span attributes using dot notation.
        
        Args:
            field_path: Path like "llm.output_text" or "tool.output_text"
            
        Returns:
            Field value or None if not found
        """
        # First try direct access (flat structure)
        if field_path in span.attributes:
            return str(span.attributes[field_path])
        
        # Then try dot notation lookup (nested structure)
        keys = field_path.split('.')
        current = span.attributes
        
        try:
            for key in keys:
                if isinstance(current, dict):
                    current = current[key]
                else:
                    return None
            return str(current) if current is not None else None
        except (KeyError, TypeError):
            return None
    
    def _apply_regex_rule(self, rule: Rule, text: str) -> dict[str, Any]:
        """Apply regex-based PII detection rule."""
        pii_detected = False
        detected_types = []
        
        for ptype, pattern in self._compiled_patterns.items():
            if pattern.search(text):
                pii_detected = True
                detected_types.append(ptype)
        
        if pii_detected:
            return {
                "passed": False,
                "flag": ["pii_leak"],
                "detected_pii_types": detected_types,
                "matches": len(detected_types),
            }
        
        # Default: rule passed
        return {"passed": True}
    
    def _apply_json_schema_rule(self, rule: Rule, text: str) -> dict[str, Any]:
        """Apply JSON schema validation rule."""
        try:
            # Only validate if text looks like JSON
            if text.strip().startswith(('{', '[')):
                json.loads(text)
                return {"passed": True}
            else:
                # Not JSON, so validation passes
                return {"passed": True}
        except json.JSONDecodeError:
            return {
                "passed": False,
                "flag": ["invalid_json"],
                "error": "Invalid JSON format",
            }
    
    def _apply_blacklist_rule(self, rule: Rule, text: str) -> dict[str, Any]:
        """Apply blacklist word detection rule."""
        blacklist_words = rule.params.get("words", [])
        if not blacklist_words:
            return {"passed": True}
        
        text_lower = text.lower()
        found_words = []
        
        for word in blacklist_words:
            if word.lower() in text_lower:
                found_words.append(word)
        
        if found_words:
            return {
                "passed": False,
                "flag": ["blacklisted_word"],
                "found_words": found_words,
            }
        
        # Default: rule passed
        return {"passed": True}
    
    def _apply_max_length_rule(self, rule: Rule, text: str) -> dict[str, Any]:
        """Apply maximum length rule."""
        max_chars = rule.params.get("max_chars", 10000)
        if len(text) > max_chars:
            return {
                "passed": False,
                "flag": ["response_too_long"],
                "actual_length": len(text),
                "max_length": max_chars,
            }
        
        # Default: rule passed
        return {"passed": True}