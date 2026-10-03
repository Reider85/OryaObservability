"""Tests for PC31 Phoenix annotations module.

Covers OTLP span generation, HTTP transport, fail-open behavior, and metrics.
"""

from __future__ import annotations

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from unittest.mock import patch

from agent_obs.metrics import tail_sampler_phoenix_annotations_total
from scripts.sampler.phoenix_annotations import (
    _attr_to_otlp_value,
    _batch_to_otlp,
    _span_to_otlp,
    annotate_rate_change,
)


class TestOtlpConversion:
    """Test OTLP value and span conversion utilities."""

    def test_attr_to_otlp_value_bool(self):
        assert _attr_to_otlp_value(True) == {"boolValue": True}
        assert _attr_to_otlp_value(False) == {"boolValue": False}

    def test_attr_to_otlp_value_number(self):
        assert _attr_to_otlp_value(42) == {"intValue": 42}
        assert _attr_to_otlp_value(3.14) == {"doubleValue": 3.14}

    def test_attr_to_otlp_value_string(self):
        assert _attr_to_otlp_value("test") == {"stringValue": "test"}

    def test_attr_to_otlp_value_list(self):
        result = _attr_to_otlp_value([1, "two", 3.0])
        expected = {
            "arrayValue": {
                "values": [
                    {"intValue": 1},
                    {"stringValue": "two"},
                    {"doubleValue": 3.0},
                ]
            }
        }
        assert result == expected

    def test_span_to_otlp_basic(self):
        span = _span_to_otlp(
            trace_id="abc123",
            span_id="def456",
            name="test.span",
            attributes={"key": "value"},
            start_time=1234567890.0,
            end_time=1234567891.0,
        )
        
        assert span["traceId"] == "abc123"
        assert span["spanId"] == "def456"
        assert span["name"] == "test.span"
        assert span["kind"] == 1  # INTERNAL
        assert span["attributes"] == [{"key": "key", "value": {"stringValue": "value"}}]

    def test_batch_to_otlp_structure(self):
        batch = _batch_to_otlp(
            trace_id="abc123",
            span_id="def456",
            name="test.span",
            attributes={"key": "value"},
            start_time=1234567890.0,
            end_time=1234567891.0,
        )
        
        assert "resourceSpans" in batch
        assert len(batch["resourceSpans"]) == 1
        resource_span = batch["resourceSpans"][0]
        assert resource_span["resource"] == [
            {"key": "service.name", "value": {"stringValue": "sampler-policy"}}
        ]
        assert len(resource_span["scopeSpans"]) == 1
        scope_span = resource_span["scopeSpans"][0]
        assert scope_span["scope"]["name"] == "agent_obs"
        assert len(scope_span["spans"]) == 1


class TestAnnotateRateChange:
    """Test Phoenix annotation function."""

    @pytest.mark.asyncio
    async def test_success_annotation(self):
        """Test successful annotation send."""
        mock_client = AsyncMock()
        mock_client.post.return_value.raise_for_status = MagicMock()
        
        event = {
            "audit_id": "test-123",
            "timestamp": 1234567890.0,
            "prev_rate": 0.10,
            "new_rate": 0.05,
            "prev_reason": "default",
            "new_reason": "cpu_high",
            "system_cpu_ratio": 0.92,
            "agent_error_rate_5m": 0.01,
            "agent_id": "test-agent",
            "triggering_agent_id": "trigger-agent",
            "reason": "test reason",
        }
        
        with patch('scripts.sampler.phoenix_annotations.tail_sampler_phoenix_annotations_total') as mock_metrics:
            mock_counter = MagicMock()
            mock_metrics.labels.return_value = mock_counter
            
            result = await annotate_rate_change(
                event, client=mock_client, otlp_url="http://test:4318"
            )
            
            assert result is True
            mock_client.post.assert_called_once()
            call_args = mock_client.post.call_args
            assert call_args[1]["json"] is not None
            assert call_args[1]["headers"]["Content-Type"] == "application/json"
            
            # Check metrics
            mock_metrics.labels.assert_called_once_with(outcome="success")
            mock_counter.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_failure_annotation(self):
        """Test annotation failure handling."""
        mock_client = AsyncMock()
        mock_client.post.side_effect = Exception("Network error")
        
        event = {
            "audit_id": "test-123",
            "timestamp": 1234567890.0,
            "prev_rate": 0.10,
            "new_rate": 0.05,
            "prev_reason": "default",
            "new_reason": "cpu_high",
            "system_cpu_ratio": 0.92,
            "agent_error_rate_5m": 0.01,
            "agent_id": "test-agent",
            "triggering_agent_id": "trigger-agent",
            "reason": "test reason",
        }
        
        with patch('scripts.sampler.phoenix_annotations.tail_sampler_phoenix_annotations_total') as mock_metrics:
            mock_counter = MagicMock()
            mock_metrics.labels.return_value = mock_counter
            
            result = await annotate_rate_change(
                event, client=mock_client, otlp_url="http://test:4318"
            )
            
            assert result is False
            # Check metrics
            mock_metrics.labels.assert_called_once_with(outcome="failed")
            mock_counter.inc.assert_called_once()

    @pytest.mark.asyncio
    async def test_default_client(self):
        """Test annotation with default client creation."""
        event = {
            "audit_id": "test-123",
            "timestamp": 1234567890.0,
            "prev_rate": 0.10,
            "new_rate": 0.05,
            "prev_reason": "default",
            "new_reason": "cpu_high",
            "system_cpu_ratio": 0.92,
            "agent_error_rate_5m": 0.01,
            "agent_id": "test-agent",
            "triggering_agent_id": "trigger-agent",
            "reason": "test reason",
        }
        
        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client
            
            result = await annotate_rate_change(
                event, otlp_url="http://test:4318"
            )
            
            assert result is True
            mock_client_class.assert_called_once()
            mock_client.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_otlp_payload_structure(self):
        """Test that OTLP payload has correct structure for sampler rate change."""
        mock_client = AsyncMock()
        mock_client.post.return_value.raise_for_status = MagicMock()
        
        event = {
            "audit_id": "test-123",
            "timestamp": 1234567890.0,
            "prev_rate": 0.10,
            "new_rate": 0.05,
            "prev_reason": "default",
            "new_reason": "cpu_high",
            "system_cpu_ratio": 0.92,
            "agent_error_rate_5m": 0.01,
            "agent_id": "test-agent",
            "triggering_agent_id": "trigger-agent",
            "reason": "cpu_high (system_cpu_ratio=0.92 > 0.80)",
        }
        
        await annotate_rate_change(event, client=mock_client)
        
        # Get the OTLP payload from the call
        call_args = mock_client.post.call_args
        payload = call_args[1]["json"]
        
        # Check structure
        assert "resourceSpans" in payload
        assert len(payload["resourceSpans"]) == 1
        
        resource_span = payload["resourceSpans"][0]
        assert resource_span["resource"] == [
            {"key": "service.name", "value": {"stringValue": "sampler-policy"}}
        ]
        
        scope_span = resource_span["scopeSpans"][0]
        assert scope_span["scope"]["name"] == "agent_obs"
        assert len(scope_span["spans"]) == 1
        
        span = scope_span["spans"][0]
        assert span["name"] == "sampler.rate_change"
        assert span["kind"] == 1  # INTERNAL
        
        # Check sampler attributes
        attrs = {attr["key"]: attr["value"] for attr in span["attributes"]}
        assert attrs["sampler.prev_rate"]["doubleValue"] == 0.10
        assert attrs["sampler.new_rate"]["doubleValue"] == 0.05
        assert attrs["sampler.prev_reason"]["stringValue"] == "default"
        assert attrs["sampler.new_reason"]["stringValue"] == "cpu_high"
        assert attrs["sampler.system_cpu_ratio"]["doubleValue"] == 0.92
        assert attrs["sampler.agent_error_rate_5m"]["doubleValue"] == 0.01
        assert attrs["sampler.agent_id"]["stringValue"] == "test-agent"
        assert attrs["sampler.triggering_agent_id"]["stringValue"] == "trigger-agent"
        assert attrs["annotation.type"]["stringValue"] == "sampler_rate_change"