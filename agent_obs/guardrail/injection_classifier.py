"""Prompt injection classifier based on DeBERTa-v3.

PC06 — [T2.1.3] Local classifier for prompt-injection attacks using
deepset/deberta-v3-base-prompt-injection from HuggingFace Hub.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment configuration
# ---------------------------------------------------------------------------
_INJECTION_THRESHOLD_LOW_ENV = "AGENT_OBS_INJECTION_THRESHOLD_LOW"
_INJECTION_THRESHOLD_HIGH_ENV = "AGENT_OBS_INJECTION_THRESHOLD_HIGH"
_INJECTION_MODEL_ENV = "AGENT_OBS_INJECTION_MODEL"

_DEFAULT_THRESHOLD_LOW = 0.5
_DEFAULT_THRESHOLD_HIGH = 0.85
_DEFAULT_MODEL = "deepset/deberta-v3-base-prompt-injection"

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InjectionScore:
    """Result of prompt-injection classification.

    Attributes:
        score: Raw model output in [0, 1].  Higher = more likely injection.
        label: One of ``"benign"``, ``"suspicious"``, ``"injection"``.
        confidence: Model confidence (softmax probability of the chosen label).
    """

    score: float
    label: str  # "benign" | "suspicious" | "injection"
    confidence: float

    def __post_init__(self) -> None:
        if not (0.0 <= self.score <= 1.0):
            raise ValueError(f"score must be in [0, 1], got {self.score}")
        if self.label not in ("benign", "suspicious", "injection"):
            raise ValueError(f"invalid label: {self.label!r}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {self.confidence}")


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------


class InjectionClassifier:
    """DeBERTa-v3 prompt-injection classifier with lazy model loading.

    The model is loaded on first call to :meth:`classify` (cold-start may be
    slow).  Subsequent calls reuse the cached model (singleton pattern).

    Thresholds are configurable via environment variables or constructor args:

    - ``AGENT_OBS_INJECTION_THRESHOLD_LOW`` (default 0.5) — below = benign,
      at-or-above = suspicious.
    - ``AGENT_OBS_INJECTION_THRESHOLD_HIGH`` (default 0.85) — at-or-above =
      injection.
    - ``AGENT_OBS_INJECTION_MODEL`` (default ``deepset/deberta-v3-base-prompt-injection``).

    **Fail-open**: if the model is unavailable (no weights, import error,
    network issue) the classifier returns ``InjectionScore(score=0.0,
    label="benign", confidence=0.0)`` so that production is never blocked by
    a missing ML component.
    """

    _instance: Optional["InjectionClassifier"] = None
    _model: object | None = None  # transformers pipeline or None on failure
    _model_loaded: bool = False

    def __init__(
        self,
        threshold_low: float | None = None,
        threshold_high: float | None = None,
        model_name: str | None = None,
    ) -> None:
        self._threshold_low = (
            threshold_low
            if threshold_low is not None
            else self._env_float(_INJECTION_THRESHOLD_LOW_ENV, _DEFAULT_THRESHOLD_LOW)
        )
        self._threshold_high = (
            threshold_high
            if threshold_high is not None
            else self._env_float(_INJECTION_THRESHOLD_HIGH_ENV, _DEFAULT_THRESHOLD_HIGH)
        )
        self._model_name = (
            model_name
            if model_name is not None
            else os.environ.get(_INJECTION_MODEL_ENV, _DEFAULT_MODEL)
        )

    @classmethod
    def get_instance(cls) -> "InjectionClassifier":
        """Return the singleton instance (lazy-created with defaults)."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Reset singleton (for testing)."""
        cls._instance = None
        cls._model = None
        cls._model_loaded = False

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def classify(self, text: str) -> InjectionScore:
        """Classify *text* for prompt-injection risk.

        Returns an :class:`InjectionScore` with ``score`` in [0, 1] and a
        human-readable ``label``.  On any failure the classifier **fails open**
        (returns benign with score 0.0).
        """
        if not text or not text.strip():
            return InjectionScore(score=0.0, label="benign", confidence=1.0)

        try:
            pipeline = self._load_model()
            if pipeline is None:
                return self._fail_open()
            return self._run_inference(pipeline, text)
        except Exception:
            logger.warning(
                "InjectionClassifier failed on input (%d chars), failing open",
                len(text),
                exc_info=True,
            )
            return self._fail_open()

    # ------------------------------------------------------------------
    # Model loading (lazy singleton)
    # ------------------------------------------------------------------

    def _load_model(self):
        """Load the HuggingFace pipeline (lazy, cached in class var)."""
        if self._model_loaded:
            return self._model

        self._model_loaded = True
        t0 = time.monotonic()

        try:
            from transformers import pipeline as hf_pipeline  # type: ignore[import-untyped]

            model = hf_pipeline(
                "text-classification",
                model=self._model_name,
                top_k=None,  # return all labels with scores
            )
            elapsed = time.monotonic() - t0
            logger.info(
                "InjectionClassifier loaded model %s in %.1fs", self._model_name, elapsed
            )
            if elapsed > 5.0:
                logger.warning(
                    "InjectionClassifier cold-start took %.1fs — first request "
                    "will be slow; consider pre-loading the model at startup",
                    elapsed,
                )
            InjectionClassifier._model = model
            return model

        except Exception:
            logger.warning(
                "Failed to load injection model %s — classifier will fail open",
                self._model_name,
                exc_info=True,
            )
            InjectionClassifier._model = None
            return None

    # ------------------------------------------------------------------
    # Inference
    # ------------------------------------------------------------------

    def _run_inference(self, pipeline, text: str) -> InjectionScore:
        """Run the model pipeline on *text* and map to InjectionScore."""
        # deepset/deberta-v3-base-prompt-injection outputs LABEL_0 (benign)
        # and LABEL_1 (injection).  We normalise to score ∈ [0, 1] where
        # higher = more injection-like.
        results = pipeline(text)

        # pipeline returns list[list[dict]] when top_k=None
        if isinstance(results, list) and results and isinstance(results[0], list):
            labels_list = results[0]
        elif isinstance(results, list) and results and isinstance(results[0], dict):
            labels_list = results
        else:
            return self._fail_open()

        score_map: dict[str, float] = {}
        for item in labels_list:
            label_name = item.get("label", "")
            score_val = float(item.get("score", 0.0))
            score_map[label_name.upper()] = score_val

        # Determine injection score: prefer LABEL_1, fallback to "INJECTION"
        injection_score = score_map.get(
            "LABEL_1", score_map.get("INJECTION", 0.0)
        )
        benign_score = score_map.get(
            "LABEL_0", score_map.get("BENIGN", 0.0)
        )

        # Normalise so that injection_score is in [0, 1]
        total = injection_score + benign_score
        if total > 0:
            injection_score = injection_score / total

        confidence = max(injection_score, benign_score)

        # Map to label via thresholds
        if injection_score >= self._threshold_high:
            label = "injection"
        elif injection_score >= self._threshold_low:
            label = "suspicious"
        else:
            label = "benign"

        return InjectionScore(
            score=round(injection_score, 6),
            label=label,
            confidence=round(confidence, 6),
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _fail_open() -> InjectionScore:
        """Return a safe default when the model is unavailable."""
        return InjectionScore(score=0.0, label="benign", confidence=0.0)

    @staticmethod
    def _env_float(key: str, default: float) -> float:
        raw = os.environ.get(key)
        if raw is None:
            return default
        try:
            return float(raw)
        except ValueError:
            logger.warning("Invalid float for %s=%r, using default %s", key, raw, default)
            return default
