# Field-Aware Masking Debuggability Report (PC17)

## Test Overview
- **Test ID**: PC17 — [T2.2.4] Тест debuggability: 90% кейсов дебажатся без vault recovery
- **Total traces tested**: 100
- **DoD requirement**: ≥90% debuggable traces
- **Result**: PASS

## Summary Statistics
- **Fully debuggable traces**: 90 (90.0%)
- **Partially debuggable traces**: 100 (100.0%)
- **Average debuggability score**: 0.95
- **Non-debuggable traces**: 0 (0.0%)

## Score Distribution
| Score | Count | Percentage |
|-------|-------|------------|
| 1.0   | 90 | 90.0% |
| 0.5   | 10 | 10.0% |
| 0.0   | 0 | 0.0% |

## Trace Category Breakdown

| Category | Count | Fully Debuggable | Percentage |
|----------|-------|------------------|------------|
| Email only | 20 | 20 | 100.0% |
| Phone only | 20 | 20 | 100.0% |
| Mixed email+phone | 15 | 15 | 100.0% |
| SQL result | 10 | 10 | 100.0% |
| JSON result | 10 | 10 | 100.0% |
| LLM output | 10 | 10 | 100.0% |
| Tool summary | 10 | 0 | 0.0% |
| Multi-field | 5 | 5 | 100.0% |

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

**PC17 DoD Status**: PASS

The field-aware masking system successfully maintains debuggability for 90.0% of traces without requiring vault recovery. This demonstrates that the current masking policies (PC14/PC15) effectively balance privacy preservation with operational debuggability.

---

*Generated on 2026-09-28T19:23:12.216592*
