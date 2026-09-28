"""Embedding-based evaluator for fast similarity assessment to golden answers."""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import httpx
import yaml
from typing_extensions import override

from agent_obs.eval.base import BaseEvaluator, EvalResult

if TYPE_CHECKING:
    from agent_obs.observability import Span


@dataclass
class GoldenEntry:
    """A golden answer entry for embedding similarity."""
    
    intent: str
    golden_answer: str
    embedding: list[float] | None = None


class GoldenStore:
    """Singleton store for golden answers with precomputed embeddings.
    
    Lazily loads golden_answers.yaml and computes embeddings on first use.
    """
    
    _instance: GoldenStore | None = None
    _embeddings: dict[str, list[float]] = {}
    
    def __new__(cls, *args: Any, **kwargs: Any) -> GoldenStore:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(
        self,
        golden_answers_file: str = "configs/golden_answers.yaml",
        embedding_model: str = "text-embedding-3-small",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if hasattr(self, "_initialized"):
            return
        
        self.golden_answers_file = golden_answers_file
        self.embedding_model = embedding_model
        self._client = client
        self._golden_answers: list[GoldenEntry] = []
        self._initialized = True
    
    @property
    def client(self) -> httpx.AsyncClient:
        """Lazy initialization of HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client
    
    async def _get_embedding(self, text: str) -> list[float]:
        """Get embedding for text via OpenAI API."""
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
                json={
                    "model": self.embedding_model,
                    "input": text,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data["data"][0]["embedding"]
        except Exception as e:
            # Log error and return empty embedding
            print(f"Warning: Failed to get embedding for text: {e}")
            return [0.0] * 1536  # Default embedding size for text-embedding-3-small
    
    def _load_golden_answers(self) -> list[GoldenEntry]:
        """Load golden answers from YAML file."""
        try:
            with open(self.golden_answers_file, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            
            golden_answers = []
            for entry in data.get("golden_answers", []):
                golden_answers.append(GoldenEntry(
                    intent=entry["intent"],
                    golden_answer=entry["golden_answer"],
                    embedding=None,  # Will be computed on demand
                ))
            
            return golden_answers
        except FileNotFoundError:
            print(f"Warning: Golden answers file not found: {self.golden_answers_file}")
            return []
        except Exception as e:
            print(f"Warning: Failed to load golden answers: {e}")
            return []
    
    def _compute_embeddings(self) -> None:
        """Precompute embeddings for all golden answers."""
        if not self._golden_answers:
            self._golden_answers = self._load_golden_answers()
        
        # Create a list of texts that need embeddings
        texts_to_embed = []
        entries_to_embed = []
        
        for entry in self._golden_answers:
            if entry.embedding is None:
                texts_to_embed.append(entry.golden_answer)
                entries_to_embed.append(entry)
        
        # Batch compute embeddings
        if texts_to_embed:
            import asyncio
            embeddings = asyncio.run(self._batch_get_embeddings(texts_to_embed))
            
            # Assign embeddings to entries
            for entry, embedding in zip(entries_to_embed, embeddings):
                entry.embedding = embedding
                self._embeddings[entry.intent] = embedding
    
    async def _batch_get_embeddings(self, texts: list[str]) -> list[list[float]]:
        """Get embeddings for multiple texts in a single API call."""
        try:
            response = await self.client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"},
                json={
                    "model": self.embedding_model,
                    "input": texts,
                },
            )
            response.raise_for_status()
            data = response.json()
            return [item["embedding"] for item in data["data"]]
        except Exception as e:
            print(f"Warning: Failed to get batch embeddings: {e}")
            # Return empty embeddings for all texts
            return [[0.0] * 1536 for _ in texts]
    
    def find_by_intent(self, intent: str) -> GoldenEntry | None:
        """Find golden answer by intent."""
        if not self._golden_answers:
            self._compute_embeddings()
        
        for entry in self._golden_answers:
            if entry.intent == intent and entry.embedding is not None:
                return entry
        
        # Fallback to default intent
        for entry in self._golden_answers:
            if entry.intent == "default" and entry.embedding is not None:
                return entry
        
        return None
    
    def get_embedding(self, text: str) -> list[float]:
        """Get embedding for a given text (for testing/debugging)."""
        loop = __import__("asyncio").get_event_loop()
        return loop.run_until_complete(self._get_embedding(text))


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    if len(a) != len(b) or len(a) == 0:
        return 0.0
    
    dot_product = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    
    if norm_a == 0 or norm_b == 0:
        return 0.0
    
    return dot_product / (norm_a * norm_b)


class EmbeddingEvaluator(BaseEvaluator):
    """Embedding-based evaluator for fast similarity assessment to golden answers.
    
    Computes cosine similarity between agent response and golden answer embeddings.
    Runs synchronously for fast evaluation (~100ms per call).
    """
    
    def __init__(
        self,
        client: httpx.AsyncClient,
        model: str = "text-embedding-3-small",
        golden_store: GoldenStore | None = None,
        sample_rate: float = 1.0,
    ) -> None:
        super().__init__(version="1.0.0")
        self.client = client
        self.model = model
        self.golden_store = golden_store or GoldenStore(client=client)
        self.sample_rate = sample_rate
    
    def _should_skip(self) -> bool:
        """Check if evaluation should be skipped based on sampling rate."""
        import random
        return random.random() > self.sample_rate
    
    def _extract_intent_from_span(self, span: Span) -> str:
        """Extract intent from span attributes."""
        return span.attributes.get("agent.intent", "default")
    
    def _extract_answer_text(self, span: Span) -> str | None:
        """Extract answer text from span."""
        return span.attributes.get("llm.output_text")
    
    def _create_error_result(self, span: Span, eval_id: str, error_type: str) -> EvalResult:
        """Create error evaluation result."""
        return EvalResult(
            trace_id=span.context.trace_id if span else "unknown",
            eval_id=eval_id,
            eval_name=f"embedding_{error_type}",
            eval_version=self.version,
            eval_timestamp=time.time(),
            eval_latency_seconds=0.0,
            scores={},
            flags=[error_type]
        )
    
    def _create_skipped_result(self, span: Span, eval_id: str) -> EvalResult:
        """Create skipped evaluation result."""
        return EvalResult(
            trace_id=span.context.trace_id if span else "unknown",
            eval_id=eval_id,
            eval_name="embedding_skipped",
            eval_version=self.version,
            eval_timestamp=time.time(),
            eval_latency_seconds=0.0,
            scores={},
            flags=["sampling_skipped"]
        )
    
    @override
    def evaluate(self, span: Span, full_trace: list[Span]) -> EvalResult:
        """Evaluate span using embedding similarity to golden answer.
        
        Args:
            span: The span to evaluate
            full_trace: Complete list of spans in the trace (unused for embedding eval)
            
        Returns:
            EvalResult with embedding similarity score or error status
        """
        import random
        
        # Check for empty span
        if not span:
            return self._create_error_result(span, f"error_{int(time.time() * 1000)}", "invalid_span")
        
        # Check sampling rate
        if self._should_skip():
            return self._create_skipped_result(span, f"skip_{int(time.time() * 1000)}")
        
        # Extract answer text
        answer_text = self._extract_answer_text(span)
        if not answer_text:
            return self._create_error_result(span, f"error_{int(time.time() * 1000)}", "no_answer_text")
        
        # Find golden answer
        intent = self._extract_intent_from_span(span)
        golden_entry = self.golden_store.find_by_intent(intent)
        
        if not golden_entry or golden_entry.embedding is None:
            return self._create_error_result(span, f"error_{int(time.time() * 1000)}", "no_golden_answer")
        
        # Compute embeddings
        try:
            # Use asyncio.run for cleaner async execution
            import asyncio
            answer_embedding = asyncio.run(self.golden_store._get_embedding(answer_text))
        except Exception as e:
            print(f"Warning: Failed to compute answer embedding: {e}")
            return self._create_error_result(span, f"error_{int(time.time() * 1000)}", "api_error")
        
        # Compute similarity
        similarity = _cosine_similarity(golden_entry.embedding, answer_embedding)

        # Store embedding on span for downstream drift detection (PC24).
        # The SDK picks this up in _enqueue and routes it to the embedding
        # queue for batched ClickHouse writes.
        span.attributes["response_embedding"] = answer_embedding

        # Create result
        flags = []
        if similarity < 0.7:
            flags.append("low_similarity")

        return EvalResult(
            trace_id=span.context.trace_id,
            eval_id=f"embedding_{int(time.time() * 1000)}",
            eval_name="embedding_similarity",
            eval_version=self.version,
            eval_timestamp=time.time(),
            eval_latency_seconds=0.0,
            scores={"embedding_similarity": similarity},
            judge_model=self.model,
            flags=flags,
            response_embedding=answer_embedding,
        )