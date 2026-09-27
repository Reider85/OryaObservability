"""Base evaluation contracts and abstract base class for evaluators."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent_obs.observability import Span


@dataclass
class EvalResult:
    """Evaluation result contract from PC0.
    
    Represents the output of an evaluation performed on a span within a trace.
    All fields are required except judge_model and judge_prompt_sha256.
    """
    
    trace_id: str
    eval_id: str
    eval_name: str
    eval_version: str
    eval_timestamp: float
    eval_latency_seconds: float
    scores: dict[str, Any]
    judge_model: str = ""
    judge_prompt_sha256: str = ""
    reasoning: str = ""
    flags: list[str] = field(default_factory=list)


class BaseEvaluator(ABC):
    """Abstract base class for all evaluators.
    
    Evaluators assess span quality and return EvalResult objects.
    The evaluate method is synchronous but may contain async logic internally.
    """
    
    def __init__(self, version: str = "1.0.0") -> None:
        self.version = version
    
    @abstractmethod
    def evaluate(self, span: Span, full_trace: list[Span]) -> EvalResult:
        """Evaluate a span in the context of the full trace.
        
        Args:
            span: The span to evaluate
            full_trace: Complete list of spans in the trace
            
        Returns:
            EvalResult with evaluation metrics and metadata
        """
        raise NotImplementedError