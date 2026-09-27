"""LLM-as-judge evaluator for async evaluation pipeline."""

from __future__ import annotations

import hashlib
import json
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import rq
from redis import Redis

from agent_obs.eval.base import BaseEvaluator, EvalResult

if TYPE_CHECKING:
    from agent_obs.observability import Span


@dataclass
class LLMJudgeJob:
    """Job payload for LLM judge worker."""
    trace_id: str
    span_id: str
    prompts: dict[str, str]
    model: str
    eval_id: str
    judge_prompt_sha256: str


@dataclass
class LLMJudgePrompts:
    """Default prompts for LLM judge evaluation."""
    
    faithfulness: str = """You are an expert evaluator for faithfulness assessment.
    
Task: Evaluate whether the answer is faithful to the provided context.
Context: {context}
Answer: {answer}

Instructions:
- Check if the answer contains information that is not present in the context
- Check if the answer contradicts the context
- Score from 0 to 1, where 1 means fully faithful (no hallucinations), 0 means completely unfaithful

Return JSON format:
{{
    "score": float,
    "reasoning": string,
    "flags": []
}}"""

    answer_relevancy: str = """You are an expert evaluator for answer relevancy assessment.
    
Task: Evaluate whether the answer is relevant to the user's question.
Question: {question}
Answer: {answer}

Instructions:
- Check if the answer directly addresses the user's question
- Check if the answer contains relevant information to solve the user's problem
- Score from 0 to 1, where 1 means highly relevant, 0 means completely irrelevant

Return JSON format:
{{
    "score": float,
    "reasoning": string,
    "flags": []
}}"""

    completeness: str = """You are an expert evaluator for answer completeness assessment.
    
Task: Evaluate whether the answer covers all important aspects of the user's question.
Question: {question}
Answer: {answer}

Instructions:
- Check if the answer addresses all key parts of the question
- Check if the answer provides sufficient depth and detail
- Score from 0 to 1, where 1 means completely comprehensive, 0 means incomplete

Return JSON format:
{{
    "score": float,
    "reasoning": string,
    "flags": []
}}"""


class LLMJudgeEvaluator(BaseEvaluator):
    """LLM-as-judge evaluator for async evaluation pipeline.
    
    Evaluates answer quality using LLM models with RAGAS-style prompts.
    Does NOT make network calls directly - enqueues jobs for async processing.
    """
    
    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str = "gpt-4o-mini",
        prompts: dict[str, str] | None = None,
        redis_url: str = "redis://localhost:6379/1",
        queue_name: str = "eval-queue",
        sample_rate: float = 0.1,
    ) -> None:
        super().__init__(version="1.0.0")
        self.client = client
        self.model = model
        self.prompts = prompts or LLMJudgePrompts().__dict__
        self.redis_url = redis_url
        self.queue_name = queue_name
        self.sample_rate = sample_rate
        
        # Lazy initialization of Redis connection
        self._redis: Redis | None = None
        
    @property
    def redis(self) -> Redis:
        """Lazy initialization of Redis connection."""
        if self._redis is None:
            self._redis = Redis.from_url(self.redis_url)
        return self._redis
    
    @property
    def queue(self) -> rq.Queue:
        """Get the RQ queue for evaluation jobs."""
        return rq.Queue(name=self.queue_name, connection=self.redis)
    
    def _build_context(self, full_trace: list[Span]) -> dict[str, str]:
        """Extract context from full trace."""
        context_parts = []
        user_message = ""
        answer_text = ""
        
        # Extract user message from root span
        for span in full_trace:
            if span.span_type.name == "AGENT_LOOP" and span.attributes.get("user_message"):
                user_message = span.attributes["user_message"]
                break
        
        # Extract context and answer from LLM calls
        for span in full_trace:
            if span.span_type.name == "LLM_CALL":
                if span.attributes.get("llm.input_text"):
                    context_parts.append(span.attributes["llm.input_text"])
                if span.attributes.get("llm.output_text"):
                    answer_text = span.attributes["llm.output_text"]
        
        return {
            "user_message": user_message,
            "question": user_message,
            "context": "\n\n".join(context_parts),
            "answer": answer_text,
        }
    
    def _render_prompt(self, prompt_template: str, context: dict[str, str]) -> str:
        """Render prompt template with context."""
        return prompt_template.format(**context)
    
    def _calculate_prompt_sha256(self, prompt: str) -> str:
        """Calculate SHA256 hash of rendered prompt for audit."""
        return hashlib.sha256(prompt.encode()).hexdigest()
    
    def _should_skip(self) -> bool:
        """Check if evaluation should be skipped based on sampling rate."""
        import os
        sample_rate = float(os.environ.get("AGENT_OBS_LLM_JUDGE_RATE", self.sample_rate))
        return random.random() > sample_rate
    
    def _create_job(
        self, 
        span: Span, 
        full_trace: list[Span],
        eval_id: str
    ) -> LLMJudgeJob:
        """Create evaluation job for LLM judge worker."""
        context = self._build_context(full_trace)
        
        # Prepare prompts data for worker
        prompts_data = {}
        for metric_name, prompt_template in self.prompts.items():
            rendered_prompt = self._render_prompt(prompt_template, context)
            prompts_data[metric_name] = rendered_prompt
        
        # Calculate prompt hashes for audit
        prompt_hashes = {
            metric_name: self._calculate_prompt_sha256(rendered_prompt)
            for metric_name, rendered_prompt in prompts_data.items()
        }
        
        return LLMJudgeJob(
            trace_id=span.context.trace_id,
            span_id=span.context.span_id,
            prompts=prompts_data,
            model=self.model,
            eval_id=eval_id,
            judge_prompt_sha256=prompt_hashes["faithfulness"]  # Use faithfulness as primary
        )
    
    def _create_pending_result(self, span: Span, eval_id: str) -> EvalResult:
        """Create pending evaluation result."""
        return EvalResult(
            trace_id=span.context.trace_id if span else "unknown",
            eval_id=eval_id,
            eval_name="llm_judge_pending",
            eval_version=self.version,
            eval_timestamp=time.time(),
            eval_latency_seconds=0.0,
            scores={},
            flags=["pending"]
        )
    
    def _create_skipped_result(self, span: Span, eval_id: str) -> EvalResult:
        """Create skipped evaluation result due to sampling."""
        return EvalResult(
            trace_id=span.context.trace_id if span else "unknown",
            eval_id=eval_id,
            eval_name="llm_judge_skipped",
            eval_version=self.version,
            eval_timestamp=time.time(),
            eval_latency_seconds=0.0,
            scores={},
            flags=["sampling_skipped"]
        )
    
    def evaluate(self, span: Span, full_trace: list[Span]) -> EvalResult:
        """Evaluate span using LLM judge - enqueues job for async processing.
        
        Args:
            span: The span to evaluate
            full_trace: Complete list of spans in the trace
            
        Returns:
            EvalResult with pending or skipped status
        """
        import os
        
        # Check for empty trace or None span
        if not span or not full_trace:
            return self._create_skipped_result(span, f"skip_{int(time.time() * 1000)}")
        
        # Override sample rate from environment if set
        sample_rate = float(os.environ.get("AGENT_OBS_LLM_JUDGE_RATE", self.sample_rate))
        
        # Check if we should skip this evaluation
        if self._should_skip():
            return self._create_skipped_result(span, f"skip_{int(time.time() * 1000)}")
        
        # Create evaluation ID
        eval_id = f"llm_judge_{int(time.time() * 1000)}"
        
        # Create and enqueue job
        try:
            job = self._create_job(span, full_trace, eval_id)
            self.queue.enqueue(
                "llm_judge_worker",
                kwargs={
                    "trace_id": job.trace_id,
                    "span_id": job.span_id,
                    "prompts": job.prompts,
                    "model": job.model,
                    "eval_id": job.eval_id,
                    "judge_prompt_sha256": job.judge_prompt_sha256,
                }
            )
        except Exception as e:
            # If Redis is unavailable, return pending result anyway
            # The late annotation worker will handle retry logic
            return self._create_pending_result(span, eval_id)
        
        # Return pending result
        return self._create_pending_result(span, eval_id)