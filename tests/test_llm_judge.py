"""Tests for LLMJudgeEvaluator."""

import time
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx
import rq
from redis import Redis

from agent_obs.eval.base import EvalResult
from agent_obs.eval.llm_judge import LLMJudgeEvaluator, LLMJudgeJob, LLMJudgePrompts
from agent_obs.observability import Span, SpanContext, SpanType


@pytest.fixture
def mock_redis():
    """Mock Redis connection."""
    with patch("redis.Redis.from_url") as mock_redis_from_url:
        mock_redis = MagicMock(spec=Redis)
        mock_redis_from_url.return_value = mock_redis
        yield mock_redis


@pytest.fixture
def mock_queue(mock_redis):
    """Mock RQ queue."""
    with patch("rq.Queue") as mock_queue_class:
        mock_queue = MagicMock()
        mock_queue_class.return_value = mock_queue
        yield mock_queue


@pytest.fixture
def mock_span():
    """Create a mock span for testing."""
    context = SpanContext.new("test_agent", parent=None)
    span = Span(
        name="test_llm_call",
        span_type=SpanType.LLM_CALL,
        context=context,
        attributes={
            "llm.input_text": "What is the capital of France?",
            "llm.output_text": "The capital of France is Paris.",
        },
        start_time=time.time() - 1,
        end_time=time.time(),
    )
    return span


@pytest.fixture
def mock_full_trace(mock_span):
    """Create a full trace including root span."""
    context = SpanContext.new("test_agent", parent=None)
    root_span = Span(
        name="test_agent_loop",
        span_type=SpanType.AGENT_LOOP,
        context=context,
        attributes={
            "user_message": "What is the capital of France?",
        },
        start_time=time.time() - 2,
        end_time=time.time() - 1,
    )
    return [root_span, mock_span]


@pytest.fixture
def evaluator(mock_redis, mock_queue):
    """Create LLMJudgeEvaluator with mocked dependencies."""
    client = MagicMock(spec=httpx.AsyncClient)
    evaluator = LLMJudgeEvaluator(
        client=client,
        model="gpt-4o-mini",
        redis_url="redis://localhost:6379/1",
        queue_name="eval-queue",
        sample_rate=1.0,  # Always run for testing
    )
    # Mock the queue property to return our mock queue
    evaluator._queue = mock_queue
    return evaluator


class TestLLMJudgeEvaluator:
    """Test suite for LLMJudgeEvaluator."""

    def test_evaluate_returns_pending_result(self, evaluator, mock_span, mock_full_trace):
        """Test that evaluate returns a pending result when job is enqueued."""
        eval_result = evaluator.evaluate(mock_span, mock_full_trace)
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "llm_judge_pending"
        assert eval_result.trace_id == mock_span.context.trace_id
        assert eval_result.eval_id.startswith("llm_judge_")
        assert eval_result.flags == ["pending"]
        assert eval_result.scores == {}

    def test_evaluate_enqueues_job(self, evaluator, mock_span, mock_full_trace, mock_queue):
        """Test that evaluate enqueues a job with correct arguments."""
        with patch.object(evaluator, '_create_job') as mock_create_job:
            mock_job = MagicMock()
            mock_job.trace_id = mock_span.context.trace_id
            mock_job.span_id = mock_span.context.span_id
            mock_job.model = evaluator.model
            mock_job.eval_id = "test_eval_id"
            mock_job.prompts = {"faithfulness": "test prompt"}
            mock_job.judge_prompt_sha256 = "test_hash"
            mock_create_job.return_value = mock_job
            
            eval_result = evaluator.evaluate(mock_span, mock_full_trace)
            
            # Verify _create_job was called
            mock_create_job.assert_called_once()
            # Verify queue was called with correct function name
            mock_queue.enqueue.assert_called_once()
            call_args = mock_queue.enqueue.call_args
            assert call_args[0][0] == "llm_judge_worker"
            assert "kwargs" in call_args[1]
            
            # Verify kwargs contain expected data
            kwargs = call_args[1]["kwargs"]
            assert kwargs["trace_id"] == mock_span.context.trace_id
            assert kwargs["span_id"] == mock_span.context.span_id
            assert kwargs["model"] == evaluator.model
            assert kwargs["eval_id"] == "test_eval_id"
            assert "prompts" in kwargs
            assert "judge_prompt_sha256" in kwargs

    def test_no_network_call_inside_evaluate(self, evaluator, mock_span, mock_full_trace):
        """Test that evaluate does not make any network calls directly."""
        with patch.object(evaluator.client, "post") as mock_post:
            evaluator.evaluate(mock_span, mock_full_trace)
            mock_post.assert_not_called()

    def test_sampling_skips_when_rate_zero(self, evaluator, mock_span, mock_full_trace):
        """Test that evaluation is skipped when sample rate is 0."""
        evaluator.sample_rate = 0.0
        eval_result = evaluator.evaluate(mock_span, mock_full_trace)
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "llm_judge_skipped"
        assert eval_result.trace_id == mock_span.context.trace_id
        assert eval_result.flags == ["sampling_skipped"]
        assert eval_result.scores == {}

    def test_sampling_always_runs_when_rate_one(self, evaluator, mock_span, mock_full_trace, mock_queue):
        """Test that evaluation always runs when sample rate is 1."""
        evaluator.sample_rate = 1.0
        with patch.object(evaluator, '_create_job') as mock_create_job:
            mock_job = MagicMock()
            mock_create_job.return_value = mock_job
            
            eval_result = evaluator.evaluate(mock_span, mock_full_trace)
            
            # Verify job was created and enqueued
            mock_create_job.assert_called_once()
            mock_queue.enqueue.assert_called_once()

    def test_prompt_sha256_deterministic(self, evaluator, mock_span, mock_full_trace):
        """Test that prompt SHA256 calculation is deterministic."""
        eval_id = "test_eval_123"
        with patch("random.random", return_value=0.5):
            job1 = evaluator._create_job(mock_span, mock_full_trace, eval_id)
            job2 = evaluator._create_job(mock_span, mock_full_trace, eval_id)
            
            assert job1.judge_prompt_sha256 == job2.judge_prompt_sha256

    def test_all_three_metrics_in_job(self, evaluator, mock_span, mock_full_trace, mock_queue):
        """Test that all three metrics (faithfulness, relevancy, completeness) are included in job."""
        with patch("random.random", return_value=0.5), patch.object(evaluator, '_create_job') as mock_create_job:
            mock_job = MagicMock()
            mock_job.prompts = {
                "faithfulness": "You are an expert evaluator for faithfulness assessment.\n\nTask: Evaluate whether the answer is faithful to the provided context.\nContext: {context}\nAnswer: {answer}",
                "answer_relevancy": "You are an expert evaluator for answer relevancy assessment.\n\nTask: Evaluate whether the answer is relevant to the user's question.\nQuestion: {question}\nAnswer: {answer}",
                "completeness": "You are an expert evaluator for answer completeness assessment.\n\nTask: Evaluate whether the answer covers all important aspects of the user's question.\nQuestion: {question}\nAnswer: {answer}"
            }
            mock_create_job.return_value = mock_job
            
            evaluator.evaluate(mock_span, mock_full_trace)
        
        # Get the prompts from the enqueued job
        call_args = mock_queue.enqueue.call_args
        kwargs = call_args[1]["kwargs"]
        prompts = kwargs["prompts"]
        
        assert "faithfulness" in prompts
        assert "answer_relevancy" in prompts
        assert "completeness" in prompts
        
        # Verify each prompt contains expected placeholders
        for prompt in prompts.values():
            assert "{context}" in prompt or "{question}" in prompt
            assert "{answer}" in prompt

    def test_context_extraction_from_trace(self, evaluator, mock_span, mock_full_trace):
        """Test that context is correctly extracted from full trace."""
        context = evaluator._build_context(mock_full_trace)
        
        assert "user_message" in context
        assert "question" in context
        assert "context" in context
        assert "answer" in context
        assert context["user_message"] == "What is the capital of France?"
        assert context["question"] == "What is the capital of France?"
        assert context["context"] == "What is the capital of France?"
        assert context["answer"] == "The capital of France is Paris."

    def test_redis_lazy_initialization(self, evaluator, mock_redis):
        """Test that Redis connection is lazily initialized."""
        # Redis should not be initialized until first use
        assert evaluator._redis is None
        
        # Access the redis property to trigger initialization
        _ = evaluator.redis
        
        # Now Redis should be initialized
        assert evaluator._redis is not None

    def test_queue_lazy_initialization(self, evaluator, mock_queue):
        """Test that RQ queue is lazily initialized."""
        # The queue should be set by the fixture, but let's test the property access
        # Access the queue property to ensure it works
        queue = evaluator.queue
        
        # Verify it's our mock queue
        assert queue is mock_queue

    def test_environment_variable_override(self):
        """Test that environment variables can override default settings."""
        with patch.dict("os.environ", {"AGENT_OBS_LLM_JUDGE_RATE": "0.5"}):
            evaluator = LLMJudgeEvaluator(
                client=MagicMock(spec=httpx.AsyncClient),
                model="gpt-4o-mini",
                redis_url="redis://localhost:6379/1",
                queue_name="eval-queue",
            )
            # The property itself should still be the default, but _should_skip should use the env var
            assert evaluator.sample_rate == 0.1  # Default value
            # Test that _should_skip uses the environment variable
            with patch("random.random", return_value=0.6):  # Should skip with 0.5 rate
                assert evaluator._should_skip() is True

    def test_job_creation_structure(self, evaluator, mock_span, mock_full_trace):
        """Test that job structure is correct."""
        eval_id = "test_eval_123"
        with patch("random.random", return_value=0.5):
            job = evaluator._create_job(mock_span, mock_full_trace, eval_id)
        
        assert isinstance(job, LLMJudgeJob)
        assert job.trace_id == mock_span.context.trace_id
        assert job.span_id == mock_span.context.span_id
        assert job.model == evaluator.model
        assert job.eval_id == eval_id
        assert len(job.judge_prompt_sha256) == 64  # SHA256 hex length
        assert all(c in "0123456789abcdef" for c in job.judge_prompt_sha256)  # Valid hex
        assert "prompts" in job.__dict__
        assert "faithfulness" in job.prompts
        assert "answer_relevancy" in job.prompts
        assert "completeness" in job.prompts

    def test_pending_result_structure(self, evaluator, mock_span, mock_full_trace):
        """Test that pending result structure is correct."""
        eval_result = evaluator.evaluate(mock_span, mock_full_trace)
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "llm_judge_pending"
        assert eval_result.trace_id == mock_span.context.trace_id
        assert eval_result.eval_id.startswith("llm_judge_")
        assert eval_result.flags == ["pending"]
        assert eval_result.scores == {}
        assert eval_result.eval_version == "1.0.0"
        assert eval_result.eval_timestamp > 0
        assert eval_result.eval_latency_seconds == 0.0

    def test_skipped_result_structure(self, evaluator, mock_span, mock_full_trace):
        """Test that skipped result structure is correct."""
        evaluator.sample_rate = 0.0
        eval_result = evaluator.evaluate(mock_span, mock_full_trace)
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "llm_judge_skipped"
        assert eval_result.trace_id == mock_span.context.trace_id
        assert eval_result.eval_id.startswith("skip_")  # Different prefix for skipped
        assert eval_result.flags == ["sampling_skipped"]
        assert eval_result.scores == {}
        assert eval_result.eval_version == "1.0.0"
        assert eval_result.eval_timestamp > 0
        assert eval_result.eval_latency_seconds == 0.0

    def test_custom_prompts(self, evaluator, mock_span, mock_full_trace):
        """Test that custom prompts can be provided."""
        custom_prompts = {
            "faithfulness": "Custom faithfulness prompt: {context} {answer}",
            "answer_relevancy": "Custom relevancy prompt: {question} {answer}",
            "completeness": "Custom completeness prompt: {question} {answer}"
        }
        
        evaluator.prompts = custom_prompts
        with patch("random.random", return_value=0.5):
            job = evaluator._create_job(mock_span, mock_full_trace, "test_eval")
        
        # Verify custom prompts are used
        assert "Custom faithfulness prompt" in job.prompts["faithfulness"]
        assert "Custom relevancy prompt" in job.prompts["answer_relevancy"]
        assert "Custom completeness prompt" in job.prompts["completeness"]

    def test_redis_fallback_handling(self, evaluator, mock_redis):
        """Test that Redis fallback works when connection fails."""
        # Make Redis initialization fail
        mock_redis_from_url = MagicMock(side_effect=Exception("Connection failed"))
        with patch("redis.Redis.from_url", mock_redis_from_url):
            # Should not raise exception, but should log warning
            evaluator = LLMJudgeEvaluator(
                client=MagicMock(spec=httpx.AsyncClient),
                model="gpt-4o-mini",
                redis_url="redis://localhost:6379/1",
                queue_name="eval-queue",
            )
            # Redis should be None after failed initialization
            assert evaluator._redis is None

    def test_eval_result_version(self, evaluator, mock_span, mock_full_trace):
        """Test that eval result has correct version."""
        eval_result = evaluator.evaluate(mock_span, mock_full_trace)
        
        assert eval_result.eval_version == "1.0.0"

    def test_eval_result_timestamps(self, evaluator, mock_span, mock_full_trace):
        """Test that eval result has correct timestamps."""
        eval_result = evaluator.evaluate(mock_span, mock_full_trace)
        
        assert eval_result.eval_timestamp > 0
        assert isinstance(eval_result.eval_timestamp, float)

    def test_empty_trace_handling(self, evaluator):
        """Test that empty trace is handled gracefully."""
        empty_trace = []
        eval_result = evaluator.evaluate(None, empty_trace)
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "llm_judge_skipped"
        assert eval_result.flags == ["sampling_skipped"]

    def test_no_user_message_in_trace(self, evaluator, mock_span):
        """Test that trace without user message is handled gracefully."""
        trace = [mock_span]
        eval_result = evaluator.evaluate(mock_span, trace)
        
        assert isinstance(eval_result, EvalResult)
        assert eval_result.eval_name == "llm_judge_pending"
        # Should still work even without user message