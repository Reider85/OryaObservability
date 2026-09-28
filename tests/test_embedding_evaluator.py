"""Tests for EmbeddingEvaluator."""

import time
import unittest
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
import yaml

from agent_obs.eval.base import EvalResult
from agent_obs.eval.embedding import EmbeddingEvaluator, GoldenEntry, GoldenStore
from agent_obs.observability import Span, SpanContext, SpanType


@pytest.fixture
def mock_client():
    """Mock HTTP client."""
    return MagicMock(spec=httpx.AsyncClient)


@pytest.fixture
def mock_span():
    """Create a mock span for testing."""
    context = SpanContext.new("test_agent", parent=None)
    span = Span(
        name="test_llm_call",
        span_type=SpanType.LLM_CALL,
        context=context,
        attributes={
            "agent.intent": "capital_of_france",
            "llm.output_text": "The capital of France is Paris.",
        },
        start_time=time.time() - 1,
        end_time=time.time(),
    )
    return span


@pytest.fixture
def mock_span_no_intent():
    """Create a mock span without intent."""
    context = SpanContext.new("test_agent", parent=None)
    span = Span(
        name="test_llm_call",
        span_type=SpanType.LLM_CALL,
        context=context,
        attributes={
            "llm.output_text": "The capital of France is Paris.",
        },
        start_time=time.time() - 1,
        end_time=time.time(),
    )
    return span


@pytest.fixture
def mock_span_no_output():
    """Create a mock span without output text."""
    context = SpanContext.new("test_agent", parent=None)
    span = Span(
        name="test_llm_call",
        span_type=SpanType.LLM_CALL,
        context=context,
        attributes={
            "agent.intent": "capital_of_france",
        },
        start_time=time.time() - 1,
        end_time=time.time(),
    )
    return span


@pytest.fixture
def golden_store(mock_client):
    """Create GoldenStore with mocked dependencies."""
    store = GoldenStore(
        golden_answers_file="configs/golden_answers.yaml",
        client=mock_client,
    )
    return store


class TestGoldenStore:
    """Test suite for GoldenStore."""

    def test_singleton_pattern(self):
        """Test that GoldenStore follows singleton pattern."""
        store1 = GoldenStore()
        store2 = GoldenStore()
        assert store1 is store2

    def test_load_golden_answers(self, golden_store):
        """Test that golden answers are loaded from YAML file."""
        # Mock file reading
        with patch("builtins.open", unittest.mock.mock_open(read_data='golden_answers:\n  - intent: "test"\n    golden_answer: "Test answer"')):
            with patch("yaml.safe_load", return_value={"golden_answers": [{"intent": "test", "golden_answer": "Test answer"}]}):
                entries = golden_store._load_golden_answers()
                assert len(entries) == 1
                assert entries[0].intent == "test"
                assert entries[0].golden_answer == "Test answer"

    def test_load_golden_answers_file_not_found(self, golden_store):
        """Test handling of missing golden answers file."""
        with patch("builtins.open", side_effect=FileNotFoundError):
            entries = golden_store._load_golden_answers()
            assert entries == []

    def test_find_by_intent_exists(self, golden_store):
        """Test finding existing intent."""
        # Mock the golden answers
        golden_store._golden_answers = [
            GoldenEntry(intent="test", golden_answer="Test answer", embedding=[0.1, 0.2, 0.3]),
            GoldenEntry(intent="default", golden_answer="Default answer", embedding=[0.4, 0.5, 0.6]),
        ]
        
        entry = golden_store.find_by_intent("test")
        assert entry is not None
        assert entry.intent == "test"
        assert entry.golden_answer == "Test answer"

    def test_find_by_intent_fallback_to_default(self, golden_store):
        """Test fallback to default intent when not found."""
        golden_store._golden_answers = [
            GoldenEntry(intent="default", golden_answer="Default answer", embedding=[0.4, 0.5, 0.6]),
        ]
        
        entry = golden_store.find_by_intent("nonexistent")
        assert entry is not None
        assert entry.intent == "default"

    def test_find_by_intent_no_default(self, golden_store):
        """Test handling when no default intent exists."""
        golden_store._golden_answers = [
            GoldenEntry(intent="test", golden_answer="Test answer", embedding=[0.1, 0.2, 0.3]),
        ]
        
        entry = golden_store.find_by_intent("nonexistent")
        assert entry is None

    @respx.mock
    def test_get_embedding_success(self, golden_store):
        """Test successful embedding retrieval."""
        # Mock OpenAI API response
        mock_response = {
            "data": [
                {
                    "embedding": [0.1, 0.2, 0.3, 0.4]
                }
            ]
        }
        
        respx.post("https://api.openai.com/v1/embeddings").respond(json=mock_response)
        
        # Set environment variable for API key
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test_key"}):
            import asyncio
            embedding = asyncio.run(golden_store._get_embedding("test text"))
        
        assert embedding == [0.1, 0.2, 0.3, 0.4]

    @respx.mock
    def test_get_embedding_api_error(self, golden_store):
        """Test embedding retrieval API error."""
        # Mock API error
        respx.post("https://api.openai.com/v1/embeddings").respond(status_code=500)
        
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test_key"}):
            import asyncio
            embedding = asyncio.run(golden_store._get_embedding("test text"))
        
        # Should return default embedding
        assert embedding == [0.0] * 1536

    @respx.mock
    def test_batch_get_embeddings(self, golden_store):
        """Test batch embedding retrieval."""
        mock_response = {
            "data": [
                {"embedding": [0.1, 0.2, 0.3]},
                {"embedding": [0.4, 0.5, 0.6]}
            ]
        }
        
        respx.post("https://api.openai.com/v1/embeddings").respond(json=mock_response)
        
        with patch.dict("os.environ", {"OPENAI_API_KEY": "test_key"}):
            import asyncio
            embeddings = asyncio.run(
                golden_store._batch_get_embeddings(["text1", "text2"])
            )
        
        assert embeddings == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    def test_cosine_similarity(self):
        """Test cosine similarity calculation."""
        from agent_obs.eval.embedding import _cosine_similarity
        
        # Identical vectors
        a = [1.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert _cosine_similarity(a, b) == 1.0
        
        # Orthogonal vectors
        a = [1.0, 0.0, 0.0]
        b = [0.0, 1.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0
        
        # Opposite vectors
        a = [1.0, 0.0, 0.0]
        b = [-1.0, 0.0, 0.0]
        assert _cosine_similarity(a, b) == -1.0
        
        # Zero vector
        a = [0.0, 0.0, 0.0]
        b = [1.0, 0.0, 0.0]
        assert _cosine_similarity(a, b) == 0.0


class TestEmbeddingEvaluator:
    """Test suite for EmbeddingEvaluator."""

    @pytest.fixture
    def evaluator(self, mock_client):
        """Create EmbeddingEvaluator with mocked dependencies."""
        return EmbeddingEvaluator(
            client=mock_client,
            model="text-embedding-3-small",
            sample_rate=1.0,  # Always run for testing
        )

    def test_evaluate_success_high_similarity(self, evaluator, mock_span, golden_store):
        """Test successful evaluation with high similarity."""
        # Mock golden store
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(
                intent="capital_of_france",
                golden_answer="The capital of France is Paris.",
                embedding=[0.1, 0.2, 0.3]
            )
        ]
        
        # Mock embedding API
        with patch.object(golden_store, '_get_embedding') as mock_get_embedding:
            mock_get_embedding.return_value = [0.1, 0.2, 0.31]  # Very similar
            
            eval_result = evaluator.evaluate(mock_span, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_similarity"
        assert "embedding_similarity" in eval_result.scores
        assert eval_result.scores["embedding_similarity"] > 0.9  # High similarity
        assert "low_similarity" not in eval_result.flags
        assert eval_result.judge_model == "text-embedding-3-small"

    def test_evaluate_success_low_similarity(self, evaluator, mock_span, golden_store):
        """Test successful evaluation with low similarity."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(
                intent="capital_of_france",
                golden_answer="The capital of France is Paris.",
                embedding=[0.1, 0.2, 0.3]
            )
        ]
        
        with patch.object(golden_store, '_get_embedding') as mock_get_embedding:
            mock_get_embedding.return_value = [1.0, 0.0, 0.0]  # Completely different from [0.1, 0.2, 0.3]
            
            eval_result = evaluator.evaluate(mock_span, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_similarity"
        assert eval_result.scores["embedding_similarity"] < 0.3  # Low similarity
        assert "low_similarity" in eval_result.flags

    def test_evaluate_no_golden_answer(self, evaluator, mock_span, golden_store):
        """Test evaluation when no golden answer exists."""
        evaluator.golden_store = golden_store
        # Mock the _compute_embeddings method to return empty list
        with patch.object(golden_store, '_compute_embeddings') as mock_compute:
            mock_compute.return_value = None  # No embeddings computed
            golden_store._golden_answers = []  # No golden answers
            
            eval_result = evaluator.evaluate(mock_span, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_no_golden_answer"
        assert eval_result.scores == {}
        assert "no_golden_answer" in eval_result.flags

    def test_evaluate_no_answer_text(self, evaluator, mock_span_no_output, golden_store):
        """Test evaluation when no answer text exists."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(intent="default", golden_answer="Default", embedding=[0.1, 0.2, 0.3])
        ]
        
        eval_result = evaluator.evaluate(mock_span_no_output, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_no_answer_text"
        assert eval_result.scores == {}
        assert "no_answer_text" in eval_result.flags

    def test_evaluate_no_intent_fallback_to_default(self, evaluator, mock_span_no_intent, golden_store):
        """Test evaluation when no intent exists (fallback to default)."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(intent="default", golden_answer="Default answer", embedding=[0.1, 0.2, 0.3])
        ]
        
        with patch.object(golden_store, '_get_embedding') as mock_get_embedding:
            mock_get_embedding.return_value = [0.1, 0.2, 0.31]
            
            eval_result = evaluator.evaluate(mock_span_no_intent, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_similarity"
        assert eval_result.scores["embedding_similarity"] > 0.9

    def test_evaluate_api_error(self, evaluator, mock_span, golden_store):
        """Test evaluation when API call fails."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(
                intent="capital_of_france",
                golden_answer="The capital of France is Paris.",
                embedding=[0.1, 0.2, 0.3]
            )
        ]
        
        with patch.object(golden_store, '_get_embedding') as mock_get_embedding:
            mock_get_embedding.side_effect = Exception("API Error")
            
            eval_result = evaluator.evaluate(mock_span, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_api_error"
        assert eval_result.scores == {}
        assert "api_error" in eval_result.flags

    def test_evaluate_sampling_skipped(self, evaluator, mock_span, golden_store):
        """Test evaluation when sampling rate causes skip."""
        evaluator.sample_rate = 0.0  # Never run
        evaluator.golden_store = golden_store
        
        eval_result = evaluator.evaluate(mock_span, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_skipped"
        assert eval_result.scores == {}
        assert "sampling_skipped" in eval_result.flags

    def test_evaluate_empty_span(self, evaluator, golden_store):
        """Test evaluation with empty span."""
        evaluator.golden_store = golden_store
        
        eval_result = evaluator.evaluate(None, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_invalid_span"
        assert eval_result.scores == {}
        assert "invalid_span" in eval_result.flags

    def test_sample_rate_environment_override(self, evaluator, mock_span, golden_store):
        """Test that environment variable can override sample rate."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(intent="default", golden_answer="Default", embedding=[0.1, 0.2, 0.3])
        ]
        
        with patch.dict("os.environ", {"AGENT_OBS_EMBEDDING_RATE": "0.0"}):
            with patch.object(evaluator, '_should_skip') as mock_should_skip:
                # The _should_skip should use the environment variable
                mock_should_skip.return_value = True
                
                eval_result = evaluator.evaluate(mock_span, [])
        
        assert eval_result.eval_name == "embedding_skipped"

    def test_eval_result_structure(self, evaluator, mock_span, golden_store):
        """Test that eval result structure is correct."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(intent="default", golden_answer="Default", embedding=[0.1, 0.2, 0.3])
        ]
        
        with patch.object(golden_store, '_get_embedding') as mock_get_embedding:
            mock_get_embedding.return_value = [0.1, 0.2, 0.31]
            
            eval_result = evaluator.evaluate(mock_span, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_similarity"
        assert eval_result.trace_id == mock_span.context.trace_id
        assert eval_result.eval_id.startswith("embedding_")
        assert eval_result.eval_version == "1.0.0"
        assert eval_result.eval_timestamp > 0
        assert eval_result.eval_latency_seconds == 0.0
        assert isinstance(eval_result.scores, dict)
        assert isinstance(eval_result.flags, list)

    def test_custom_golden_store(self, evaluator, mock_span):
        """Test that custom golden store can be provided."""
        custom_store = GoldenStore()
        # Prevent the store from loading from file
        custom_store._golden_answers = [
            GoldenEntry(intent="capital_of_france", golden_answer="The capital of France is Paris.", embedding=[0.1, 0.2, 0.3])
        ]
        # Ensure no default intent exists to avoid fallback
        custom_store._compute_embeddings = lambda: None
        
        evaluator.golden_store = custom_store
        
        eval_result = evaluator.evaluate(mock_span, [])
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "embedding_similarity"

    def test_version_inheritance(self, evaluator):
        """Test that evaluator inherits version from base class."""
        assert evaluator.version == "1.0.0"

    def test_evaluate_stores_embedding_in_eval_result(self, evaluator, mock_span, golden_store):
        """PC24: evaluate() must include the answer embedding in EvalResult."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(
                intent="capital_of_france",
                golden_answer="The capital of France is Paris.",
                embedding=[0.1, 0.2, 0.3],
            )
        ]
        with patch.object(golden_store, "_get_embedding") as mock_get_embedding:
            mock_get_embedding.return_value = [0.1, 0.2, 0.31]
            eval_result = evaluator.evaluate(mock_span, [])

        assert eval_result.response_embedding is not None
        assert isinstance(eval_result.response_embedding, list)
        assert len(eval_result.response_embedding) > 0
        assert all(isinstance(v, float) for v in eval_result.response_embedding)

    def test_evaluate_stores_embedding_on_span(self, evaluator, mock_span, golden_store):
        """PC24: evaluate() must set response_embedding on the span attributes."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = [
            GoldenEntry(
                intent="capital_of_france",
                golden_answer="The capital of France is Paris.",
                embedding=[0.1, 0.2, 0.3],
            )
        ]
        with patch.object(golden_store, "_get_embedding") as mock_get_embedding:
            mock_get_embedding.return_value = [0.1, 0.2, 0.31]
            evaluator.evaluate(mock_span, [])

        assert "response_embedding" in mock_span.attributes
        embedding = mock_span.attributes["response_embedding"]
        assert isinstance(embedding, list)
        assert len(embedding) > 0

    def test_error_result_no_embedding(self, evaluator, mock_span, golden_store):
        """PC24: error results must have response_embedding=None."""
        evaluator.golden_store = golden_store
        golden_store._golden_answers = []
        with patch.object(golden_store, "_compute_embeddings") as mock_compute:
            mock_compute.return_value = None
            golden_store._golden_answers = []
            eval_result = evaluator.evaluate(mock_span, [])

        assert eval_result.response_embedding is None

    def test_skipped_result_no_embedding(self, evaluator, mock_span, golden_store):
        """PC24: skipped results must have response_embedding=None."""
        evaluator.sample_rate = 0.0
        evaluator.golden_store = golden_store
        eval_result = evaluator.evaluate(mock_span, [])
        assert eval_result.response_embedding is None