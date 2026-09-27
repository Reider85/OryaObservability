"""Debuggability test for field-aware PII masking (PC17).

Tests that ≥90% of 100 synthetic traces remain debuggable after field-aware masking
without needing vault recovery.

A trace is "debuggable" if an engineer can understand the conversation context
from the masked spans alone.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import pytest

from agent_obs.guardrail.engine import GuardrailEngine, GuardrailConfig
from agent_obs.guardrail.field_masker import FieldMasker
from agent_obs.guardrail.injection_classifier import InjectionClassifier, InjectionScore
from agent_obs.guardrail.pii_detector import PIIDetector
from agent_obs.guardrail.vault_client import VaultClient
from agent_obs.observability import Span, SpanContext, SpanType

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Synthetic Trace Data Generation
# ---------------------------------------------------------------------------

@dataclass
class SyntheticTrace:
    """A synthetic trace with PII entities for testing."""
    
    trace_id: str
    spans: List[Span]
    original_text: Dict[str, str]  # field_name -> original text
    masked_text: Dict[str, str]   # field_name -> masked text
    debuggability_score: float = 0.0


def _generate_synthetic_traces() -> List[SyntheticTrace]:
    """Generate 100 synthetic traces with various PII patterns."""
    traces = []
    
    # 1. Email only in user_message (20 traces)
    for i in range(20):
        trace_id = f"email-trace-{i:03d}"
        email = f"user{i}@example.com"
        spans = _create_spans_for_email_trace(trace_id, email)
        original_text = {"user_message": f"Hello, my email is {email}. Please contact me there."}
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    # 2. Phone only in user_message (20 traces)
    for i in range(20):
        trace_id = f"phone-trace-{i:03d}"
        phone = f"+7(9{i:02d}) {i:03d}-{i:02d}-{i:02d}"
        spans = _create_spans_for_phone_trace(trace_id, phone)
        original_text = {"user_message": f"Call me at {phone} for urgent matters."}
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    # 3. Mixed email+phone in user_message (15 traces)
    for i in range(15):
        trace_id = f"mixed-trace-{i:03d}"
        email = f"contact{i}@company.com"
        phone = f"+1(555) {i:03d}-{i:02d}-{i:02d}"
        spans = _create_spans_for_mixed_trace(trace_id, email, phone)
        original_text = {"user_message": f"My email is {email} and phone is {phone}. Please contact me."}
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    # 4. SQL result tool_output with email column (10 traces)
    for i in range(10):
        trace_id = f"sql-trace-{i:03d}"
        email = f"customer{i}@example.com"
        sql_result = {
            "columns": ["id", "name", "email", "created_at"],
            "rows": [
                {"id": 1, "name": "John Doe", "email": email, "created_at": "2023-01-01"},
                {"id": 2, "name": "Jane Smith", "email": "jane@test.com", "created_at": "2023-01-02"}
            ]
        }
        spans = _create_spans_for_sql_trace(trace_id, sql_result)
        original_text = {"tool_output": json.dumps(sql_result, ensure_ascii=False)}
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    # 5. JSON tool_output with phone field (10 traces)
    for i in range(10):
        trace_id = f"json-trace-{i:03d}"
        phone = f"+7(9{i:02d}) {i:03d}-{i:02d}-{i:02d}"
        json_result = {
            "user_id": 123,
            "contact_info": {
                "name": "User Test",
                "phone": phone,
                "email": "test@example.com"
            },
            "status": "active"
        }
        spans = _create_spans_for_json_trace(trace_id, json_result)
        original_text = {"tool_output": json.dumps(json_result, ensure_ascii=False)}
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    # 6. PII in llm_output_text (10 traces)
    for i in range(10):
        trace_id = f"llm-output-trace-{i:03d}"
        inn = f"{i:02d}{i:02d}000000{i:02d}"
        spans = _create_spans_for_llm_output_trace(trace_id, inn)
        original_text = {"llm_output_text": f"User's INN is {inn}. Please verify this information."}
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    # 7. tool_input_summary (hash_only) (10 traces)
    for i in range(10):
        trace_id = f"summary-trace-{i:03d}"
        input_text = f"Search for user with email user{i}@example.com and phone +7(9{i:02d}) {i:03d}-{i:02d}-{i:02d}"
        spans = _create_spans_for_summary_trace(trace_id, input_text)
        original_text = {"tool_input_summary": input_text}
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    # 8. Multi-field PII (5 traces)
    for i in range(5):
        trace_id = f"multi-trace-{i:03d}"
        email = f"multi{i}@example.com"
        phone = f"+7(9{i:02d}) {i:03d}-{i:02d}-{i:02d}"
        inn = f"{i:02d}{i:02d}000000{i:02d}"
        
        spans = []
        # User message with email
        user_ctx = SpanContext.new(agent_id="test-agent")
        user_span = Span(
            name="llm.call:gpt-4o",
            span_type=SpanType.LLM_CALL,
            context=user_ctx,
            attributes={"llm.model": "gpt-4o"}
        )
        spans.append(user_span)
        
        # Tool output with phone
        tool_ctx = SpanContext.new(agent_id="test-agent", parent=user_ctx)
        tool_span = Span(
            name="tool.call:search",
            span_type=SpanType.TOOL_CALL,
            context=tool_ctx,
            attributes={"tool.name": "search"}
        )
        spans.append(tool_span)
        
        # LLM output with INN
        llm_ctx = SpanContext.new(agent_id="test-agent", parent=user_ctx)
        llm_span = Span(
            name="llm.call:gpt-4o",
            span_type=SpanType.LLM_CALL,
            context=llm_ctx,
            attributes={"llm.model": "gpt-4o"}
        )
        spans.append(llm_span)
        
        original_text = {
            "user_message": f"My email is {email}. Call me at {phone}.",
            "tool_output": f"User INN: {inn}. Contact phone: {phone}.",
            "llm_output_text": f"Confirmed user details: email {email}, INN {inn}."
        }
        trace = SyntheticTrace(trace_id, spans, original_text, {})
        traces.append(trace)
    
    return traces


def _create_spans_for_email_trace(trace_id: str, email: str) -> List[Span]:
    """Create spans for email-only trace."""
    ctx = SpanContext.new(agent_id="test-agent")
    span = Span(
        name="llm.call:gpt-4o",
        span_type=SpanType.LLM_CALL,
        context=ctx,
        attributes={"llm.model": "gpt-4o"}
    )
    return [span]


def _create_spans_for_phone_trace(trace_id: str, phone: str) -> List[Span]:
    """Create spans for phone-only trace."""
    ctx = SpanContext.new(agent_id="test-agent")
    span = Span(
        name="llm.call:gpt-4o",
        span_type=SpanType.LLM_CALL,
        context=ctx,
        attributes={"llm.model": "gpt-4o"}
    )
    return [span]


def _create_spans_for_mixed_trace(trace_id: str, email: str, phone: str) -> List[Span]:
    """Create spans for mixed email+phone trace."""
    ctx = SpanContext.new(agent_id="test-agent")
    span = Span(
        name="llm.call:gpt-4o",
        span_type=SpanType.LLM_CALL,
        context=ctx,
        attributes={"llm.model": "gpt-4o"}
    )
    return [span]


def _create_spans_for_sql_trace(trace_id: str, sql_result: Dict[str, Any]) -> List[Span]:
    """Create spans for SQL result trace."""
    ctx = SpanContext.new(agent_id="test-agent")
    span = Span(
        name="tool.call:database",
        span_type=SpanType.TOOL_CALL,
        context=ctx,
        attributes={"tool.name": "database"}
    )
    return [span]


def _create_spans_for_json_trace(trace_id: str, json_result: Dict[str, Any]) -> List[Span]:
    """Create spans for JSON result trace."""
    ctx = SpanContext.new(agent_id="test-agent")
    span = Span(
        name="tool.call:api",
        span_type=SpanType.TOOL_CALL,
        context=ctx,
        attributes={"tool.name": "api"}
    )
    return [span]


def _create_spans_for_llm_output_trace(trace_id: str, inn: str) -> List[Span]:
    """Create spans for LLM output with PII trace."""
    ctx = SpanContext.new(agent_id="test-agent")
    span = Span(
        name="llm.call:gpt-4o",
        span_type=SpanType.LLM_CALL,
        context=ctx,
        attributes={"llm.model": "gpt-4o"}
    )
    return [span]


def _create_spans_for_summary_trace(trace_id: str, input_text: str) -> List[Span]:
    """Create spans for tool input summary trace."""
    ctx = SpanContext.new(agent_id="test-agent")
    span = Span(
        name="tool.call:search",
        span_type=SpanType.TOOL_CALL,
        context=ctx,
        attributes={"tool.name": "search"}
    )
    return [span]


# ---------------------------------------------------------------------------
# Masking Pipeline
# ---------------------------------------------------------------------------

def _create_guardrail_engine() -> GuardrailEngine:
    """Create a GuardrailEngine with real PII detector and mocked classifier."""
    # Mock injection classifier to always return benign
    classifier = InjectionClassifier()
    classifier.classify = lambda text: InjectionScore(
        score=0.0, label="benign", confidence=0.99
    )
    
    # Real PII detector
    pii_detector = PIIDetector(enabled_detectors={"email", "phone", "inn", "passport", "payment"})
    
    # Stub vault client
    vault_client = VaultClient()
    
    # Create minimal field masker config for testing
    field_masker_config = {
        "policies": {
            "system_prompt": "no_mask",
            "user_message": "full_mask",
            "tool_output": "partial_mask",
            "llm_output_text": "full_mask",
            "tool_input_summary": "hash_only"
        },
        "default_policy": "full_mask"
    }
    
    # Minimal PII columns config for testing
    pii_columns_config = {
        "email": ["email", "e_mail", "mail", "contact_email"],
        "phone": ["phone", "tel", "telephone", "mobile", "contact_phone"]
    }
    
    field_masker = FieldMasker(field_masker_config, pii_columns_config)
    
    return GuardrailEngine(
        pii_detector=pii_detector,
        injection_classifier=classifier,
        vault_client=vault_client,
        config=GuardrailConfig(),
        field_masker=field_masker
    )


async def _apply_masking_to_trace(trace: SyntheticTrace, engine: GuardrailEngine) -> SyntheticTrace:
    """Apply field-aware masking to a synthetic trace."""
    masked_text = {}
    
    for span in trace.spans:
        # Apply masking to each text field
        for field_name in ["user_message", "tool_output", "llm_output_text", "tool_input_summary"]:
            if field_name in trace.original_text:
                # Check if this field has text in span attributes
                original_text = trace.original_text[field_name]
                
                # Apply masking
                result = await engine.check_input(
                    original_text,
                    context=span.context,
                    field=field_name
                )
                
                # Store masked text and update span
                masked_text[field_name] = result.masked_text
                span.attributes[f"llm.input_text" if field_name == "user_message" else 
                               f"tool.output_text" if field_name == "tool_output" else 
                               f"llm.output_text" if field_name == "llm_output_text" else 
                               f"tool.input_summary"] = result.masked_text
                
                # Add redacted fields
                if result.redacted_fields:
                    span.attributes["pii.redacted_fields"] = result.redacted_fields
    
    return SyntheticTrace(
        trace_id=trace.trace_id,
        spans=trace.spans,
        original_text=trace.original_text,
        masked_text=masked_text,
        debuggability_score=0.0  # Will be computed later
    )


# ---------------------------------------------------------------------------
# Debuggability Scoring
# ---------------------------------------------------------------------------

def _compute_debuggability_score(trace: SyntheticTrace) -> float:
    """Compute debuggability score for a trace (0.0, 0.5, or 1.0)."""
    total_score = 0.0
    num_fields = 0
    
    for field_name, masked_text in trace.masked_text.items():
        if field_name not in trace.original_text:
            continue
            
        original_text = trace.original_text[field_name]
        score = _score_field_debuggability(field_name, original_text, masked_text)
        total_score += score
        num_fields += 1
    
    # Average score across all fields
    if num_fields == 0:
        return 0.0
    
    average_score = total_score / num_fields
    
    # Round to nearest 0.5 or 1.0
    if average_score >= 0.75:
        return 1.0
    elif average_score >= 0.25:
        return 0.5
    else:
        return 0.0


def _score_field_debuggability(field_name: str, original_text: str, masked_text: str) -> float:
    """Score debuggability of a single field (0.0, 0.5, or 1.0)."""
    
    # Check for guardrail failure
    if "[REDACTED:guardrail_unavailable]" in masked_text:
        return 0.0
    
    # Check for hash_only fields - inherently less debuggable but acceptable
    if field_name == "tool_input_summary":
        return 0.5  # Hash-only fields are partially debuggable
    
    # Check for proper PII token format
    pii_tokens = re.findall(r'\[[A-Z_]+:[a-f0-9]+\]', masked_text)
    if pii_tokens:
        # Verify all tokens follow the correct format
        for token in pii_tokens:
            if not re.match(r'\[[A-Z_]+:[a-f0-9]{4,}\]', token):
                return 0.0
    
    # Check word preservation (at least 30% of original words should remain)
    original_words = len(original_text.split())
    masked_words = len(masked_text.split())
    
    if original_words == 0:
        return 0.0
    
    word_ratio = masked_words / original_words
    
    if word_ratio < 0.3:
        return 0.0
    
    # Check sentence structure preservation
    if _is_sentence_structure_preserved(original_text, masked_text):
        return 1.0
    else:
        return 0.5


def _is_sentence_structure_preserved(original: str, masked: str) -> bool:
    """Check if sentence structure is preserved after masking."""
    # Count punctuation marks
    original_punct = sum(1 for c in original if c in '.!?;:,')
    masked_punct = sum(1 for c in masked if c in '.!?;:,')
    
    # Check if punctuation is preserved
    if original_punct > 0 and masked_punct == 0:
        return False
    
    # Check for reasonable length preservation
    if len(masked) < len(original) * 0.2:  # Masked text too short
        return False
    
    # Check for brackets (important for structured data)
    if '[' in original and ']' in original:
        if '[' not in masked or ']' not in masked:
            return False
    
    return True


# ---------------------------------------------------------------------------
# Main Test
# ---------------------------------------------------------------------------

async def test_debuggability_90_percent_threshold():
    """Test that ≥90% of traces are debuggable without vault recovery."""
    
    # Generate synthetic traces
    traces = _generate_synthetic_traces()
    
    # Create guardrail engine
    engine = _create_guardrail_engine()
    
    # Apply masking and compute scores
    scored_traces = []
    debuggability_scores = []
    
    for trace in traces:
        masked_trace = await _apply_masking_to_trace(trace, engine)
        score = _compute_debuggability_score(masked_trace)
        masked_trace.debuggability_score = score
        scored_traces.append(masked_trace)
        debuggability_scores.append(score)
    
    # Calculate statistics
    total_traces = len(scored_traces)
    debuggable_traces = sum(1 for t in scored_traces if t.debuggability_score >= 1.0)
    partially_debuggable_traces = sum(1 for t in scored_traces if t.debuggability_score >= 0.5)
    average_score = sum(debuggability_scores) / total_traces
    
    # Generate report
    _generate_report(scored_traces, total_traces, debuggable_traces, partially_debuggable_traces, average_score)
    
    # Assertions
    assert average_score >= 0.90, f"Average debuggability score {average_score:.2f} < 0.90 threshold"
    assert debuggable_traces / total_traces >= 0.90, f"Debuggable traces {debuggable_traces}/{total_traces} < 90%"
    
    # Log results
    logger.info(f"Debuggability test results:")
    logger.info(f"  Total traces: {total_traces}")
    logger.info(f"  Fully debuggable: {debuggable_traces} ({debuggable_traces/total_traces*100:.1f}%)")
    logger.info(f"  Partially debuggable: {partially_debuggable_traces} ({partially_debuggable_traces/total_traces*100:.1f}%)")
    logger.info(f"  Average score: {average_score:.2f}")
    
    # Per-trace breakdown for debugging
    for trace in scored_traces:
        if trace.debuggability_score < 0.5:
            logger.warning(f"Trace {trace.trace_id} not debuggable (score={trace.debuggability_score})")


def _generate_report(
    traces: List[SyntheticTrace],
    total_traces: int,
    debuggable_traces: int,
    partially_debuggable_traces: int,
    average_score: float
) -> None:
    """Generate the field_aware_masking_report.md file."""
    
    # Count by category
    category_counts = {
        "email_only": sum(1 for t in traces if t.trace_id.startswith("email-trace-")),
        "phone_only": sum(1 for t in traces if t.trace_id.startswith("phone-trace-")),
        "mixed": sum(1 for t in traces if t.trace_id.startswith("mixed-trace-")),
        "sql": sum(1 for t in traces if t.trace_id.startswith("sql-trace-")),
        "json": sum(1 for t in traces if t.trace_id.startswith("json-trace-")),
        "llm_output": sum(1 for t in traces if t.trace_id.startswith("llm-output-trace-")),
        "summary": sum(1 for t in traces if t.trace_id.startswith("summary-trace-")),
        "multi": sum(1 for t in traces if t.trace_id.startswith("multi-trace-")),
    }
    
    # Score distribution
    score_distribution = {
        "1.0": sum(1 for t in traces if t.debuggability_score == 1.0),
        "0.5": sum(1 for t in traces if t.debuggability_score == 0.5),
        "0.0": sum(1 for t in traces if t.debuggability_score == 0.0),
    }
    
    # Write report
    report_content = f"""# Field-Aware Masking Debuggability Report (PC17)

## Test Overview
- **Test ID**: PC17 — [T2.2.4] Тест debuggability: 90% кейсов дебажатся без vault recovery
- **Total traces tested**: {total_traces}
- **DoD requirement**: ≥90% debuggable traces
- **Result**: {"PASS" if debuggable_traces / total_traces >= 0.90 else "FAIL"}

## Summary Statistics
- **Fully debuggable traces**: {debuggable_traces} ({debuggable_traces/total_traces*100:.1f}%)
- **Partially debuggable traces**: {partially_debuggable_traces} ({partially_debuggable_traces/total_traces*100:.1f}%)
- **Average debuggability score**: {average_score:.2f}
- **Non-debuggable traces**: {score_distribution['0.0']} ({score_distribution['0.0']/total_traces*100:.1f}%)

## Score Distribution
| Score | Count | Percentage |
|-------|-------|------------|
| 1.0   | {score_distribution['1.0']} | {score_distribution['1.0']/total_traces*100:.1f}% |
| 0.5   | {score_distribution['0.5']} | {score_distribution['0.5']/total_traces*100:.1f}% |
| 0.0   | {score_distribution['0.0']} | {score_distribution['0.0']/total_traces*100:.1f}% |

## Trace Category Breakdown

| Category | Count | Fully Debuggable | Percentage |
|----------|-------|------------------|------------|
| Email only | {category_counts['email_only']} | {sum(1 for t in traces if t.trace_id.startswith("email-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("email-trace-") and t.debuggability_score == 1.0)/category_counts['email_only']*100:.1f}% |
| Phone only | {category_counts['phone_only']} | {sum(1 for t in traces if t.trace_id.startswith("phone-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("phone-trace-") and t.debuggability_score == 1.0)/category_counts['phone_only']*100:.1f}% |
| Mixed email+phone | {category_counts['mixed']} | {sum(1 for t in traces if t.trace_id.startswith("mixed-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("mixed-trace-") and t.debuggability_score == 1.0)/category_counts['mixed']*100:.1f}% |
| SQL result | {category_counts['sql']} | {sum(1 for t in traces if t.trace_id.startswith("sql-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("sql-trace-") and t.debuggability_score == 1.0)/category_counts['sql']*100:.1f}% |
| JSON result | {category_counts['json']} | {sum(1 for t in traces if t.trace_id.startswith("json-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("json-trace-") and t.debuggability_score == 1.0)/category_counts['json']*100:.1f}% |
| LLM output | {category_counts['llm_output']} | {sum(1 for t in traces if t.trace_id.startswith("llm-output-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("llm-output-trace-") and t.debuggability_score == 1.0)/category_counts['llm_output']*100:.1f}% |
| Tool summary | {category_counts['summary']} | {sum(1 for t in traces if t.trace_id.startswith("summary-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("summary-trace-") and t.debuggability_score == 1.0)/category_counts['summary']*100:.1f}% |
| Multi-field | {category_counts['multi']} | {sum(1 for t in traces if t.trace_id.startswith("multi-trace-") and t.debuggability_score == 1.0)} | {sum(1 for t in traces if t.trace_id.startswith("multi-trace-") and t.debuggability_score == 1.0)/category_counts['multi']*100:.1f}% |

## Debuggability Scoring Methodology

Each trace is scored based on whether an engineer can understand the conversation context from masked spans:

- **Score 1.0 (Fully debuggable)**: No `[REDACTED:guardrail_unavailable]`, all PII tokens match `[TYPE:hex4]` format, sentence structure preserved, ≥30% of original words remain
- **Score 0.5 (Partially debuggable)**: `hash_only` applied to primary text field, or sentence structure degraded but context still partially understandable
- **Score 0.0 (Not debuggable)**: `[REDACTED:guardrail_unavailable]` present, or unstructured text with excessive PII masking where meaning is lost

## Examples

### Fully Debuggable Example
**Original**: "Hello, my email is user@example.com. Please contact me there."
**Masked**: "Hello, my email is [EMAIL:5f3a]. Please contact me there."
**Score**: 1.0 - Context and intent preserved

### Partially Debuggable Example
**Original**: "Search for user with email user@example.com and phone +7(999) 123-45-67"
**Masked**: "[HASH:a1b2c3](35 chars)"
**Score**: 0.5 - Hash-only field loses specific details but preserves search intent

### Not Debuggable Example
**Original**: "My phone is +7(999) 123-45-67 and email is user@example.com"
**Masked**: "[REDACTED:guardrail_unavailable]"
**Score**: 0.0 - Guardrail failure makes trace unusable

## Conclusion

**PC17 DoD Status**: {"PASS" if debuggable_traces / total_traces >= 0.90 else "FAIL"}

The field-aware masking system successfully maintains debuggability for {debuggable_traces/total_traces*100:.1f}% of traces without requiring vault recovery. This demonstrates that the current masking policies (PC14/PC15) effectively balance privacy preservation with operational debuggability.

---

*Generated on {__import__('datetime').datetime.now().isoformat()}*
"""
    
    # Write to file
    with open("docs/field_aware_masking_report.md", "w", encoding="utf-8") as f:
        f.write(report_content)
    
    logger.info(f"Report generated: docs/field_aware_masking_report.md")