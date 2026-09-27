"""Async evaluation pipeline for agent observability.

Provides rule-based, LLM-judge, and embedding evaluators that assess
answer quality and attach results to traces via late annotation.
"""

from agent_obs.eval.base import BaseEvaluator, EvalResult
from agent_obs.eval.rule_based import Rule, RuleBasedEvaluator

__all__ = [
    "BaseEvaluator",
    "EvalResult",
    "Rule",
    "RuleBasedEvaluator",
]