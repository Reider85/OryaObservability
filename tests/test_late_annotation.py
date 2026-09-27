"""Tests for late annotation (annotate, annotate_with_fallback, get_eval_results)."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from agent_obs.eval.base import EvalResult
from agent_obs.eval.late_annotation import (
    annotate,
    annotate_with_fallback,
    get_eval_results,
)


@pytest.fixture
def eval_result():
    """Create a test EvalResult."""
    return EvalResult(
        trace_id="trace_abc123",
        eval_id="eval_001",
        eval_name="llm_judge",
        eval_version="1.0.0",
        eval_timestamp=time.time(),
        eval_latency_seconds=5.2,
        scores={"faithfulness": 0.92, "answer_relevancy": 0.88},
        judge_model="gpt-4o-mini",
        judge_prompt_sha256="abc123def456",
        reasoning="Good faithfulness, high relevancy",
        flags=[],
    )


@pytest.fixture
def mock_hot_store():
    """Mock HotStore client."""
    store = MagicMock()
    store.write_eval_result = MagicMock()
    store.get_eval_results = MagicMock(return_value=[])
    return store


@pytest.fixture
def mock_redis():
    """Mock Redis client."""
    client = MagicMock()
    client.setex = MagicMock()
    client.get = MagicMock(return_value=None)
    client.keys = MagicMock(return_value=[])
    return client


class TestAnnotate:
    """Test annotate() function."""

    @pytest.mark.asyncio
    async def test_annotate_writes_to_clickhouse(self, eval_result, mock_hot_store):
        """Test that annotate writes EvalResult to ClickHouse."""
        await annotate(eval_result, mock_hot_store)

        mock_hot_store.write_eval_result.assert_called_once_with(eval_result)

    @pytest.mark.asyncio
    async def test_annotate_increments_success_metric(self, eval_result, mock_hot_store):
        """Test that annotate increments success metric."""
        with patch("agent_obs.metrics.eval_annotation_total") as mock_metric:
            mock_metric.labels.return_value = MagicMock()
            await annotate(eval_result, mock_hot_store)
            mock_metric.labels.assert_called_with(status="success")
            mock_metric.labels.return_value.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_annotate_handles_clickhouse_error(self, eval_result, mock_hot_store):
        """Test that annotate handles ClickHouse errors."""
        mock_hot_store.write_eval_result.side_effect = Exception("Connection failed")

        with patch("agent_obs.metrics.eval_annotation_total") as mock_metric:
            mock_metric.labels.return_value = MagicMock()
            with pytest.raises(Exception, match="Connection failed"):
                await annotate(eval_result, mock_hot_store)
            mock_metric.labels.assert_called_with(status="failed")

    @pytest.mark.asyncio
    async def test_annotate_passes_correct_data(self, mock_hot_store):
        """Test that annotate passes the correct EvalResult fields."""
        result = EvalResult(
            trace_id="trace_xyz",
            eval_id="eval_999",
            eval_name="rule_based",
            eval_version="2.0.0",
            eval_timestamp=1234567890.0,
            eval_latency_seconds=0.5,
            scores={"rules_passed": 4, "rules_failed": 0},
            judge_model="",
            judge_prompt_sha256="",
            reasoning="All rules passed",
            flags=[],
        )

        await annotate(result, mock_hot_store)

        call_args = mock_hot_store.write_eval_result.call_args[0][0]
        assert call_args.trace_id == "trace_xyz"
        assert call_args.eval_id == "eval_999"
        assert call_args.eval_name == "rule_based"


class TestAnnotateWithFallback:
    """Test annotate_with_fallback() function."""

    @pytest.mark.asyncio
    async def test_fallback_uses_clickhouse_when_available(
        self, eval_result, mock_hot_store, mock_redis
    ):
        """Test that fallback uses ClickHouse when available."""
        await annotate_with_fallback(eval_result, mock_hot_store, mock_redis)

        mock_hot_store.write_eval_result.assert_called_once()
        mock_redis.setex.assert_not_called()

    @pytest.mark.asyncio
    async def test_fallback_uses_redis_when_clickhouse_down(
        self, eval_result, mock_redis
    ):
        """Test that fallback uses Redis when ClickHouse is unavailable."""
        mock_hot_store = MagicMock()
        mock_hot_store.write_eval_result.side_effect = Exception("CH down")

        await annotate_with_fallback(eval_result, mock_hot_store, mock_redis)

        mock_redis.setex.assert_called_once()
        call_args = mock_redis.setex.call_args
        key = call_args[0][0]
        ttl = call_args[0][1]
        data = json.loads(call_args[0][2])

        assert key == f"eval_fallback:{eval_result.trace_id}:{eval_result.eval_id}"
        assert ttl == 3600  # 1 hour
        assert data["trace_id"] == eval_result.trace_id
        assert data["eval_id"] == eval_result.eval_id
        assert data["scores"] == eval_result.scores

    @pytest.mark.asyncio
    async def test_fallback_increments_redis_metric(
        self, eval_result, mock_redis
    ):
        """Test that fallback increments Redis fallback metric."""
        mock_hot_store = MagicMock()
        mock_hot_store.write_eval_result.side_effect = Exception("CH down")

        with patch("agent_obs.metrics.eval_annotation_redis_fallback_total") as mock_fallback:
            with patch("agent_obs.metrics.eval_annotation_total") as mock_total:
                mock_total.labels.return_value = MagicMock()
                await annotate_with_fallback(eval_result, mock_hot_store, mock_redis)
                mock_fallback.inc.assert_called_once()
                mock_total.labels.assert_called_with(status="redis_fallback")

    @pytest.mark.asyncio
    async def test_fallback_no_exception_when_both_unavailable(self, eval_result):
        """Test that no exception is raised when both stores are unavailable."""
        # No hot_store, no redis
        await annotate_with_fallback(eval_result, None, None)
        # Should not raise

    @pytest.mark.asyncio
    async def test_fallback_increments_failed_metric_when_both_down(self, eval_result):
        """Test that failed metric is incremented when both stores are down."""
        with patch("agent_obs.metrics.eval_annotation_total") as mock_metric:
            mock_metric.labels.return_value = MagicMock()
            await annotate_with_fallback(eval_result, None, None)
            mock_metric.labels.assert_called_with(status="failed")

    @pytest.mark.asyncio
    async def test_fallback_redis_also_fails(self, eval_result, mock_redis):
        """Test graceful handling when Redis also fails."""
        mock_hot_store = MagicMock()
        mock_hot_store.write_eval_result.side_effect = Exception("CH down")
        mock_redis.setex.side_effect = Exception("Redis down")

        with patch("agent_obs.metrics.eval_annotation_total") as mock_metric:
            mock_metric.labels.return_value = MagicMock()
            # Should not raise
            await annotate_with_fallback(eval_result, mock_hot_store, mock_redis)
            mock_metric.labels.assert_called_with(status="failed")


class TestGetEvalResults:
    """Test get_eval_results() function."""

    @pytest.mark.asyncio
    async def test_get_results_from_clickhouse(self, mock_hot_store):
        """Test retrieval from ClickHouse."""
        expected = [
            EvalResult(
                trace_id="trace_123",
                eval_id="eval_1",
                eval_name="llm_judge",
                eval_version="1.0.0",
                eval_timestamp=time.time(),
                eval_latency_seconds=5.0,
                scores={"faithfulness": 0.9},
            )
        ]
        mock_hot_store.get_eval_results.return_value = expected

        results = await get_eval_results("trace_123", mock_hot_store)

        assert len(results) == 1
        assert results[0].trace_id == "trace_123"
        mock_hot_store.get_eval_results.assert_called_once_with("trace_123")

    @pytest.mark.asyncio
    async def test_get_results_falls_back_to_redis(self, mock_redis):
        """Test fallback to Redis when ClickHouse is unavailable."""
        redis_data = {
            "trace_id": "trace_456",
            "eval_id": "eval_2",
            "eval_name": "llm_judge",
            "eval_version": "1.0.0",
            "eval_timestamp": time.time(),
            "eval_latency_seconds": 3.0,
            "scores": {"relevancy": 0.85},
            "judge_model": "",
            "judge_prompt_sha256": "",
            "reasoning": "",
            "flags": [],
        }
        mock_redis.keys.return_value = [b"eval_fallback:trace_456:eval_2"]
        mock_redis.get.return_value = json.dumps(redis_data)

        results = await get_eval_results("trace_456", None, mock_redis)

        assert len(results) == 1
        assert results[0].trace_id == "trace_456"
        assert results[0].scores == {"relevancy": 0.85}

    @pytest.mark.asyncio
    async def test_get_results_returns_empty_when_nothing_found(self, mock_hot_store):
        """Test empty list when no results found anywhere."""
        mock_hot_store.get_eval_results.return_value = []

        results = await get_eval_results("trace_nonexistent", mock_hot_store, None)

        assert results == []

    @pytest.mark.asyncio
    async def test_get_results_prefers_clickhouse_over_redis(
        self, mock_hot_store, mock_redis
    ):
        """Test that ClickHouse results take precedence over Redis."""
        ch_result = [
            EvalResult(
                trace_id="trace_789",
                eval_id="eval_3",
                eval_name="llm_judge",
                eval_version="1.0.0",
                eval_timestamp=time.time(),
                eval_latency_seconds=4.0,
                scores={"completeness": 0.95},
            )
        ]
        mock_hot_store.get_eval_results.return_value = ch_result

        results = await get_eval_results("trace_789", mock_hot_store, mock_redis)

        assert len(results) == 1
        assert results[0].scores == {"completeness": 0.95}
        mock_redis.keys.assert_not_called()
