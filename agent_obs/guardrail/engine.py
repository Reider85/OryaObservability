"""GuardrailEngine — unified policy engine combining PII masking and injection detection.

PC07 — [T2.1.4]  Single ``check_input()`` call performs:

1. PII detection → masked text with ``[TYPE:hex4]`` replacement.
2. Vault storage of mask → original mapping (fail-closed: masking always
   happens, vault unavailability only blocks *recovery*).
3. Injection classification on the **original** text (not masked).
4. Audit-event written on **every** call (including clean).
5. Prometheus metrics recorded.

The engine is designed for synchronous hot-path execution (< 5 ms p99 on CPU).
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from prometheus_client import Counter, Histogram

from agent_obs.guardrail.injection_classifier import InjectionClassifier, InjectionScore
from agent_obs.guardrail.pii_detector import PIIDetector
from agent_obs.guardrail.pii_types import PIIMatch
from agent_obs.guardrail.vault_client import VaultClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------

guardrail_check_total = Counter(
    "agent_obs_guardrail_check_total",
    "Guardrail check invocations by decision",
    ["decision"],
)

guardrail_check_duration_seconds = Histogram(
    "agent_obs_guardrail_check_duration_seconds",
    "Guardrail check latency histogram",
    buckets=[0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1],
)

pii_entities_detected_total = Counter(
    "agent_obs_pii_entities_detected_total",
    "PII entities detected by type",
    ["entity_type"],
)

injection_score_distribution = Histogram(
    "agent_obs_injection_score_distribution",
    "Injection classifier score distribution",
    buckets=[0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 1.0],
)

vault_unavailable_total = Counter(
    "agent_obs_vault_unavailable_total",
    "Vault unavailability events during guardrail checks",
)


# ---------------------------------------------------------------------------
# Data contracts (matches PC0 canonical definitions)
# ---------------------------------------------------------------------------


@dataclass
class GuardrailVerdict:
    """Result of a single ``GuardrailEngine.check_input()`` call.

    Attributes
    ----------
    verdict:
        ``"clean"`` | ``"flag"`` | ``"block"``.
    score:
        Injection score (0..1).  For pure PII (no injection) this is 0.0.
    masked_text:
        Text after PII masking (identical to input if no PII found).
    redacted_fields:
        List of redacted field paths, e.g.
        ``["user_message.email.5f3a", "tool_output.phone.abc1"]``.
    audit_event_id:
        Unique id of the audit trail record (always generated).
    """

    verdict: str  # "clean" | "flag" | "block"
    score: float  # injection score 0..1
    masked_text: str
    redacted_fields: list[str]
    audit_event_id: str


class GuardrailBlockException(Exception):
    """Raised when a guardrail check returns ``verdict="block"``.

    The SDK raises this from the pre-call hooks in ``llm_call`` / ``tool_call``
    *before* the guarded request is sent, so a blocked prompt or tool input
    never leaves the process.  The originating verdict is carried for auditing
    and debugging; the message deliberately never contains the checked text,
    which may contain unmasked PII.
    """

    def __init__(self, verdict: GuardrailVerdict, *, stage: str = "input") -> None:
        self.verdict = verdict
        self.stage = stage
        super().__init__(
            "Guardrail blocked the request "
            f"(stage={stage}, score={verdict.score:.2f}, "
            f"audit_event_id={verdict.audit_event_id})"
        )


@dataclass
class GuardrailConfig:
    """Engine-level configuration knobs."""

    injection_block_threshold: float = 0.85
    injection_flag_threshold: float = 0.50
    vault_ttl_seconds: int = 86400  # 24 hours


@dataclass
class AuditEvent:
    """Append-only audit record written on every ``check_input`` call."""

    audit_id: str
    timestamp: float
    trace_id: str
    decision: str  # "allow" | "flag" | "block"
    reason: str
    entity_count: int
    injection_score: float
    field: str


# ---------------------------------------------------------------------------
# GuardrailEngine
# ---------------------------------------------------------------------------


class GuardrailEngine:
    """Unified guardrail combining PII masking and injection classification.

    Parameters
    ----------
    pii_detector:
        Pre-configured PII detector instance.
    injection_classifier:
        Pre-configured injection classifier instance.
    vault_client:
        Vault client for storing mask → original mappings.
    config:
        Engine configuration (thresholds, TTL).
    """

    def __init__(
        self,
        pii_detector: PIIDetector,
        injection_classifier: InjectionClassifier,
        vault_client: VaultClient,
        config: GuardrailConfig | None = None,
    ) -> None:
        self.pii_detector = pii_detector
        self.injection_classifier = injection_classifier
        self.vault_client = vault_client
        self.config = config or GuardrailConfig()
        self._audit_log: list[AuditEvent] = []  # in-memory for tests; PC20 → ClickHouse

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    async def check_input(
        self,
        text: str,
        *,
        context: object | None = None,
        field: str = "user_message",
    ) -> GuardrailVerdict:
        """Run PII detection, vault storage, injection classification, and audit.

        The method is technically ``async`` (future-proofing for real Vault
        HTTP calls in PC18) but all current work is CPU-bound and synchronous.

        Parameters
        ----------
        text:
            The raw text to check.
        context:
            A ``SpanContext`` (or ``None`` in tests).  Used to populate the
            audit event's ``trace_id``.
        field:
            Semantic field name, e.g. ``"user_message"`` or ``"tool_output"``.
            Used to build ``redacted_fields`` paths.

        Returns
        -------
        GuardrailVerdict
            The unified verdict with masked text and audit metadata.
        """
        t0 = time.monotonic()
        audit_event_id = f"ae-{uuid.uuid4().hex[:12]}"
        trace_id = getattr(context, "trace_id", "unknown")

        # --- Step 1: PII detection ---
        pii_matches: list[PIIMatch] = self.pii_detector.detect(text)
        masked_text = text
        redacted_fields: list[str] = []

        if pii_matches:
            masked_text, redacted_fields = self._apply_pii_masking(
                text, pii_matches, field
            )

        # --- Step 2: Vault store (fail-closed: masking already applied) ---
        vault_available = True
        for match in pii_matches:
            try:
                await self.vault_client.store(
                    mask=match.mask,
                    original=match.value,
                    ttl_seconds=self.config.vault_ttl_seconds,
                )
            except Exception:
                vault_available = False
                vault_unavailable_total.inc()
                logger.warning(
                    "vault.store failed for mask=%s — recovery will be unavailable",
                    match.mask,
                )

        # --- Step 3: Injection classification on original text ---
        injection_score = self.injection_classifier.classify(text)
        injection_score_distribution.observe(injection_score.score)

        # --- Step 4: Determine verdict ---
        verdict_str, decision, reason = self._resolve_verdict(
            injection_score, pii_matches, vault_available
        )

        # --- Step 5: Audit event (always) ---
        audit_event = AuditEvent(
            audit_id=audit_event_id,
            timestamp=time.time(),
            trace_id=trace_id,
            decision=decision,
            reason=reason,
            entity_count=len(pii_matches),
            injection_score=injection_score.score,
            field=field,
        )
        self._audit_log.append(audit_event)
        logger.debug(
            "guardrail.check decision=%s reason=%s audit=%s",
            decision,
            reason,
            audit_event_id,
        )

        # --- Step 6: Metrics ---
        guardrail_check_total.labels(decision=decision).inc()
        duration = time.monotonic() - t0
        guardrail_check_duration_seconds.observe(duration)

        for match in pii_matches:
            pii_entities_detected_total.labels(entity_type=match.entity_type).inc()

        return GuardrailVerdict(
            verdict=verdict_str,
            score=injection_score.score,
            masked_text=masked_text,
            redacted_fields=redacted_fields,
            audit_event_id=audit_event_id,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_pii_masking(
        self,
        text: str,
        matches: list[PIIMatch],
        field: str,
    ) -> tuple[str, list[str]]:
        """Replace each PII match with its mask and build redacted_fields."""
        # Sort by span_start descending so replacements don't shift indices
        sorted_matches = sorted(matches, key=lambda m: m.span_start, reverse=True)
        masked = text
        redacted: list[str] = []

        for match in sorted_matches:
            masked = masked[: match.span_start] + match.mask + masked[match.span_end :]
            # Build path like "user_message.email.5f3a"
            hex4 = match.mask.split(":")[-1].rstrip("]") if ":" in match.mask else ""
            path = f"{field}.{match.entity_type}.{hex4}"
            redacted.append(path)

        # redacted_fields should be in forward order
        redacted.reverse()
        return masked, redacted

    def _resolve_verdict(
        self,
        injection: InjectionScore,
        pii_matches: list[PIIMatch],
        vault_available: bool,
    ) -> tuple[str, str, str]:
        """Map injection score + PII results to (verdict, decision, reason)."""
        has_pii = len(pii_matches) > 0

        if injection.score >= self.config.injection_block_threshold:
            reason = f"injection_score_{injection.score:.2f}"
            return "block", "block", reason

        if injection.score >= self.config.injection_flag_threshold:
            reason = f"injection_score_{injection.score:.2f}"
            return "flag", "flag", reason

        if has_pii:
            pii_count = len(pii_matches)
            reason = f"pii_masked_{pii_count}_entities"
            return "clean", "allow", reason

        return "clean", "allow", "no_pii_injection"
