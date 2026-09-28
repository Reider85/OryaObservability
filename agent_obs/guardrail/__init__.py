"""PII detection, injection classification, and masking for agent observability.

This module provides regex-based and ML-based detection of personally identifiable
information in text content, supporting Russian and international PII types,
as well as prompt-injection classification via DeBERTa-v3.
"""

from agent_obs.guardrail.audit import (
    RecoveryAuditEvent,
    AuditReasonRequiredError,
    AuditWriteError,
    classify_reason_category,
)
from agent_obs.guardrail.engine import (
    AuditEvent,
    GuardrailBlockException,
    GuardrailConfig,
    GuardrailEngine,
    GuardrailVerdict,
)
from agent_obs.guardrail.vault_client import MFARequiredError, MFAConfig
from agent_obs.guardrail.field_masker import FieldMasker
from agent_obs.guardrail.injection_classifier import InjectionClassifier, InjectionScore
from agent_obs.guardrail.pii_detector import PIIDetector
from agent_obs.guardrail.presidio_engine import PresidioPIIEngine, get_presidio_engine
from agent_obs.guardrail.pii_types import PIIMatch
from agent_obs.guardrail.vault_client import VaultClient

__all__ = [
    # PC20 Vault recovery audit
    "RecoveryAuditEvent",
    "AuditReasonRequiredError",
    "AuditWriteError",
    "classify_reason_category",
    # Existing exports
    "AuditEvent",
    "GuardrailBlockException",
    "GuardrailConfig",
    "GuardrailEngine",
    "GuardrailVerdict",
    "FieldMasker",
    "InjectionClassifier",
    "InjectionScore",
    "MFAConfig",
    "MFARequiredError",
    "PIIDetector",
    "PIIMatch",
    "PresidioPIIEngine",
    "VaultClient",
    "get_presidio_engine",
]