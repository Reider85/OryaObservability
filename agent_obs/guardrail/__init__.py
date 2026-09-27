"""PII detection, injection classification, and masking for agent observability.

This module provides regex-based and ML-based detection of personally identifiable
information in text content, supporting Russian and international PII types,
as well as prompt-injection classification via DeBERTa-v3.
"""

from agent_obs.guardrail.injection_classifier import InjectionClassifier, InjectionScore
from agent_obs.guardrail.pii_detector import PIIDetector
from agent_obs.guardrail.presidio_engine import PresidioPIIEngine, get_presidio_engine
from agent_obs.guardrail.pii_types import PIIMatch

__all__ = [
    "InjectionClassifier",
    "InjectionScore",
    "PIIDetector",
    "PIIMatch",
    "PresidioPIIEngine",
    "get_presidio_engine",
]