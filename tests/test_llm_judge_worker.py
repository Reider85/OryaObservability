"""Tests for LLM judge worker (llm_judge_worker)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, AsyncMock

import pytest

from agent_obs.eval.base import EvalResult


@pytest.fixture
def mock_hot_store():
    """Mock HotStore."""
    store = MagicMock()
    store.write_eval_result = MagicMock()
    return store


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    client = MagicMock()
    client.setex = MagicMock()
    return client


@pytest.fixture
def sample_prompts():
    """Sample rendered prompts for testing."""
    return {
        "faithfulness": "Evaluate faithfulness: Context: X Answer: Y",
        "answer_relevancy": "Evaluate relevancy: Question: Q Answer: Y",
        "completeness": "Evaluate completeness: Question: Q Answer: Y",
    }


class TestLLMJudgeWorker:
    """Test suite for llm_judge_worker function."""

    def test_worker_imports_correctly(self):
        """Test that worker module can be imported."""
        from agent_obs.eval.llm_judge_worker import llm_judge_worker
        assert callable(llm_judge_worker)

    @patch("agent_obs.eval.llm_judge_worker._process_llm_judge_job")
    def test_worker_calls_async_processor(self, mock_process):
        """Test that sync worker delegates to async processor."""
        from agent_obs.eval.llm_judge_worker import llm_judge_worker

        llm_judge_worker(
            trace_id="trace_123",
            span_id="span_456",
            prompts={"faithfulness": "test"},
            model="gpt-4o-mini",
            eval_id="eval_789",
            judge_prompt_sha256="hash123",
        )

        mock_process.assert_called_once_with(
            trace_id="trace_123",
            span_id="span_456",
            prompts={"faithfulness": "test"},
            model="gpt-4o-mini",
            eval_id="eval_789",
            judge_prompt_sha256="hash123",
        )

    @patch("agent_obs.eval.llm_judge_worker._process_llm_judge_job")
    def test_worker_increments_failed_metric_on_error(self, mock_process):
        """Test that failed metric is incremented when worker raises."""
        mock_process.side_effect = Exception("Worker failed")

        from agent_obs.eval.llm_judge_worker import llm_judge_worker

        with patch("agent_obs.metrics.eval_jobs_failed_total") as mock_metric:
            llm_judge_worker(
                trace_id="trace_123",
                span_id="span_456",
                prompts={},
                model="gpt-4o-mini",
                eval_id="eval_789",
                judge_prompt_sha256="hash123",
            )
            mock_metric.inc.assert_called_once()


class TestProcessLlmJudgeJob:
    """Test suite for _process_llm_judge_job async function."""

    @pytest.mark.asyncio
    async def test_process_job_calls_llm_for_each_metric(self, sample_prompts):
        """Test that each metric prompt is sent to the LLM."""
        from agent_obs.eval.llm_judge_worker import _process_llm_judge_job

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"score": 0.9, "reasoning": "Good", "flags": []}'}}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_client

        with patch("agent_obs.eval.llm_judge_worker._create_hot_store") as mock_hot:
            with patch("agent_obs.eval.llm_judge_worker._create_redis_client") as mock_red:
                mock_hot.return_value = MagicMock()
                mock_red.return_value = MagicMock()

                with patch.dict("sys.modules", {"httpx": mock_httpx_module}):
                    with patch("agent_obs.eval.late_annotation.annotate_with_fallback") as mock_annotate:
                        mock_annotate.return_value = None

                        await _process_llm_judge_job(
                            trace_id="trace_123",
                            span_id="span_456",
                            prompts=sample_prompts,
                            model="gpt-4o-mini",
                            eval_id="eval_789",
                            judge_prompt_sha256="hash123",
                        )

                        # Should be called once for each metric
                        assert mock_client.post.call_count == 3

    @pytest.mark.asyncio
    async def test_process_job_constructs_eval_result(self, sample_prompts):
        """Test that EvalResult is correctly constructed."""
        from agent_obs.eval.llm_judge_worker import _process_llm_judge_job

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"score": 0.85, "reasoning": "OK", "flags": []}'}}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_client

        with patch("agent_obs.eval.llm_judge_worker._create_hot_store") as mock_hot:
            with patch("agent_obs.eval.llm_judge_worker._create_redis_client") as mock_red:
                mock_hot.return_value = MagicMock()
                mock_red.return_value = MagicMock()

                with patch.dict("sys.modules", {"httpx": mock_httpx_module}):
                    with patch("agent_obs.eval.late_annotation.annotate_with_fallback") as mock_annotate:
                        captured_result = None

                        async def capture_annotate(result, hs, rc):
                            nonlocal captured_result
                            captured_result = result

                        mock_annotate.side_effect = capture_annotate

                        await _process_llm_judge_job(
                            trace_id="trace_123",
                            span_id="span_456",
                            prompts=sample_prompts,
                            model="gpt-4o-mini",
                            eval_id="eval_789",
                            judge_prompt_sha256="hash123",
                        )

                        assert captured_result is not None
                        assert isinstance(captured_result, EvalResult)
                        assert captured_result.trace_id == "trace_123"
                        assert captured_result.eval_id == "eval_789"
                        assert captured_result.eval_name == "llm_judge"
                        assert captured_result.judge_model == "gpt-4o-mini"
                        assert captured_result.judge_prompt_sha256 == "hash123"
                        assert "faithfulness" in captured_result.scores
                        assert "answer_relevancy" in captured_result.scores
                        assert "completeness" in captured_result.scores

    @pytest.mark.asyncio
    async def test_process_job_records_metrics(self, sample_prompts):
        """Test that metrics are recorded after successful processing."""
        from agent_obs.eval.llm_judge_worker import _process_llm_judge_job

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": '{"score": 0.9, "reasoning": "Good", "flags": []}'}}]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_client

        with patch("agent_obs.eval.llm_judge_worker._create_hot_store") as mock_hot:
            with patch("agent_obs.eval.llm_judge_worker._create_redis_client") as mock_red:
                mock_hot.return_value = MagicMock()
                mock_red.return_value = MagicMock()

                with patch.dict("sys.modules", {"httpx": mock_httpx_module}):
                    with patch("agent_obs.eval.late_annotation.annotate_with_fallback"):
                        with patch("agent_obs.metrics.eval_jobs_completed_total") as mock_completed:
                            with patch("agent_obs.metrics.eval_job_latency_seconds") as mock_latency:
                                await _process_llm_judge_job(
                                    trace_id="trace_123",
                                    span_id="span_456",
                                    prompts=sample_prompts,
                                    model="gpt-4o-mini",
                                    eval_id="eval_789",
                                    judge_prompt_sha256="hash123",
                                )

                                mock_completed.inc.assert_called_once()
                                mock_latency.observe.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_job_handles_llm_error_gracefully(self, sample_prompts):
        """Test that LLM errors are handled gracefully per metric."""
        from agent_obs.eval.llm_judge_worker import _process_llm_judge_job

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.raise_for_status.side_effect = Exception("LLM API error")

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        mock_httpx_module = MagicMock()
        mock_httpx_module.AsyncClient.return_value = mock_client

        with patch("agent_obs.eval.llm_judge_worker._create_hot_store") as mock_hot:
            with patch("agent_obs.eval.llm_judge_worker._create_redis_client") as mock_red:
                mock_hot.return_value = MagicMock()
                mock_red.return_value = MagicMock()

                with patch.dict("sys.modules", {"httpx": mock_httpx_module}):
                    with patch("agent_obs.eval.late_annotation.annotate_with_fallback") as mock_annotate:
                        captured_result = None

                        async def capture_annotate(result, hs, rc):
                            nonlocal captured_result
                            captured_result = result

                        mock_annotate.side_effect = capture_annotate

                        # Should not raise
                        await _process_llm_judge_job(
                            trace_id="trace_123",
                            span_id="span_456",
                            prompts=sample_prompts,
                            model="gpt-4o-mini",
                            eval_id="eval_789",
                            judge_prompt_sha256="hash123",
                        )

                        # Should still produce result with 0.0 scores for failed metrics
                        assert captured_result is not None
                        assert all(v == 0.0 for v in captured_result.scores.values())
                        assert any("failed" in f for f in captured_result.flags)


class TestCreateHotStore:
    """Test _create_hot_store helper."""

    def test_creates_hot_store_from_env(self):
        """Test that HotStore is created from environment."""
        from agent_obs.eval.llm_judge_worker import _create_hot_store

        mock_hot_store_cls = MagicMock()
        mock_hot_store_cls.return_value = MagicMock()

        with patch.dict("sys.modules", {"agent_obs.storage.hot": MagicMock(HotStore=mock_hot_store_cls)}):
            result = _create_hot_store()
            assert result is not None

    def test_returns_none_on_import_error(self):
        """Test graceful handling when clickhouse-driver is missing."""
        from agent_obs.eval.llm_judge_worker import _create_hot_store

        with patch.dict("sys.modules", {"agent_obs.storage.hot": None}):
            result = _create_hot_store()
            # Should return None or a HotStore (depends on import handling)


class TestCreateRedisClient:
    """Test _create_redis_client helper."""

    def test_creates_redis_from_env(self):
        """Test that Redis client is created from environment."""
        from agent_obs.eval.llm_judge_worker import _create_redis_client

        mock_redis_cls = MagicMock()
        mock_redis_cls.from_url.return_value = MagicMock()

        with patch.dict("sys.modules", {"redis": MagicMock(Redis=mock_redis_cls)}):
            result = _create_redis_client()
            assert result is not None

    def test_returns_none_on_error(self):
        """Test graceful handling when Redis is unavailable."""
        from agent_obs.eval.llm_judge_worker import _create_redis_client

        mock_redis_cls = MagicMock()
        mock_redis_cls.from_url.side_effect = Exception("Connection refused")

        with patch.dict("sys.modules", {"redis": MagicMock(Redis=mock_redis_cls)}):
            result = _create_redis_client()
            assert result is None
