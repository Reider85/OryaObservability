"""
Tests for PP21 DrillDown API: tiered storage field restoration.

Tests the DrillDownService and FastAPI endpoint for recovering trace fields
from hot/warm/cold tiers with RBAC + MFA, Redis cache, and rate limiting.
"""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from agent_obs.storage.drill_down import DrillDownRequest, DrillDownResult, DrillDownService
from agent_obs.storage.hot import HotStore
from agent_obs.storage.warm import WarmStore
from agent_obs.storage.cold import ColdStore
from agent_obs.trace_viewer.ui import app, get_drill_down_service


class MockHotStore(HotStore):
    """Mock HotStore for testing drill-down functionality."""
    
    def __init__(self):
        self.spans = {}
    
    def is_available(self):
        return True
    
    def get_spans(self, trace_id):
        if trace_id not in self.spans:
            raise Exception("not found")
        return self.spans[trace_id]


class MockWarmStore(WarmStore):
    """Mock WarmStore for testing drill-down functionality."""
    
    def __init__(self):
        self.traces = {}
    
    async def get_trace(self, trace_id):
        return self.traces.get(trace_id)
    
    async def connect(self):
        return None


class MockColdStore(ColdStore):
    """Mock ColdStore for testing drill-down functionality."""
    
    def __init__(self):
        self.parquet_data = {}
    
    def is_available(self):
        return True
    
    def list_keys(self, prefix=""):
        return list(self.parquet_data.keys())
    
    def read_bytes(self, key, bucket=None):
        if key not in self.parquet_data:
            raise Exception("not found")
        return self.parquet_data[key]


@pytest.fixture
def mock_hot_store():
    return MockHotStore()


@pytest.fixture
def mock_warm_store():
    return MockWarmStore()


@pytest.fixture
def mock_cold_store():
    return MockColdStore()


@pytest.fixture
def mock_redis():
    with patch('redis.asyncio.from_url') as mock:
        redis_client = MagicMock()
        mock.return_value = redis_client
        yield redis_client


@pytest.fixture
def drill_down_service(mock_hot_store, mock_warm_store, mock_cold_store, mock_redis):
    return DrillDownService(
        hot_store=mock_hot_store,
        warm_store=mock_warm_store,
        cold_store=mock_cold_store,
        redis_client=mock_redis,
        rate_limit=10,
        cache_ttl=600
    )


@pytest.fixture
def client():
    """TestClient with mocked stores"""
    with patch('agent_obs.trace_viewer.ui.get_drill_down_service') as mock_service:
        # Create a mock service instance
        mock_service_instance = MagicMock()
        mock_service.return_value = mock_service_instance
        
        client = TestClient(app)
        yield client


class TestDrillDownService:
    """Test DrillDownService functionality."""
    
    def test_hot_tier_recover_all_fields(self, drill_down_service, mock_hot_store):
        """Test recovering all fields from hot tier."""
        trace_id = "test-trace-123"
        mock_hot_store.spans[trace_id] = [
            {
                "attributes": {
                    "llm.input_text": "Hello world",
                    "llm.output_text": "Hi there",
                    "custom.field": "value"
                }
            }
        ]
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=[],
            user="testuser",
            mfa_used=False
        )
        
        result = asyncio.run(drill_down_service.drill_down(request))
        
        assert result.storage_tier == "hot"
        assert "llm.input_text" in result.fields
        assert "llm.output_text" in result.fields
        assert "custom.field" in result.fields
        assert result.mfa_used == False
    
    def test_hot_tier_recover_specific_fields(self, drill_down_service, mock_hot_store):
        """Test recovering specific fields from hot tier."""
        trace_id = "test-trace-123"
        mock_hot_store.spans[trace_id] = [
            {
                "attributes": {
                    "custom.field": "value",
                    "another.field": "data"
                }
            }
        ]
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=["custom.field", "another.field"],
            user="testuser",
            mfa_used=False
        )
        
        result = asyncio.run(drill_down_service.drill_down(request))
        
        assert result.storage_tier == "hot"
        assert "custom.field" in result.fields
        assert "another.field" in result.fields
        assert "llm.input_text" not in result.fields
    
    def test_warm_tier_recover(self, drill_down_service, mock_warm_store):
        """Test recovering fields from warm tier."""
        trace_id = "test-trace-123"
        
        # Hot store should NOT have this trace, so it falls through to warm
        mock_warm_store.traces[trace_id] = {
            "trace_id": trace_id,
            "tenant_id": "test-tenant",
            "cost_usd_total": 0.05,
            "span_count": 10,
            "eval_avg": {"faithfulness": 0.8, "relevancy": 0.9}
        }
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=["cost_usd_total", "span_count"],
            user="testuser",
            mfa_used=False
        )
        
        result = asyncio.run(drill_down_service.drill_down(request))
        
        assert result.storage_tier == "warm"
        assert result.fields["cost_usd_total"] == 0.05
        assert result.fields["span_count"] == 10
        assert "llm.input_text" not in result.fields  # Not in warm tier
    
    def test_cold_tier_recover_with_trace_id(self, drill_down_service, mock_cold_store):
        """Test recovering fields from cold tier with trace_id column."""
        trace_id = "test-trace-123"
        
        # Override the cold tier recovery to return our mock data
        original_cold_recover = drill_down_service._cold_tier_recover
        
        async def mock_cold_recover(trace_id, fields, tenant_id=None):
            return {
                "fields": {"cost_usd_total": 0.05},
                "metadata": {}
            }
        
        drill_down_service._cold_tier_recover = mock_cold_recover
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=["cost_usd_total"],
            user="testuser",
            mfa_used=False
        )
        
        result = asyncio.run(drill_down_service.drill_down(request))
        
        assert result.storage_tier == "cold"
        assert "cost_usd_total" in result.fields
        assert result.metadata.get("cold_note") is None
    
    def test_cold_tier_recover_aggregates(self, drill_down_service, mock_cold_store):
        """Test recovering aggregates from cold tier when no trace_id column."""
        trace_id = "test-trace-123"
        
        # Override the cold tier recovery to return aggregates
        original_cold_recover = drill_down_service._cold_tier_recover
        
        async def mock_cold_recover(trace_id, fields, tenant_id=None):
            return {
                "fields": {"tenant_id": "test-tenant", "cost_usd_sum": 0.5, "traces_count": 10},
                "metadata": {"cold_note": "Returning partition aggregates (no trace_id column)"}
            }
        
        drill_down_service._cold_tier_recover = mock_cold_recover
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=[],
            user="testuser",
            mfa_used=False
        )
        
        result = asyncio.run(drill_down_service.drill_down(request))
        
        assert result.storage_tier == "cold"
        assert result.metadata.get("cold_note") is not None
        assert "tenant_id" in result.fields
        assert "cost_usd_sum" in result.fields
    
    def test_pii_fields_require_mfa(self, drill_down_service, mock_hot_store):
        """Test that PII fields require MFA."""
        trace_id = "test-trace-123"
        mock_hot_store.spans[trace_id] = [
            {"attributes": {"llm.input_text": "Hello world"}}
        ]
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=["llm.input_text"],
            user="testuser",
            mfa_used=False  # MFA not used for PII field
        )
        
        with pytest.raises(PermissionError, match="MFA required for PII fields"):
            asyncio.run(drill_down_service.drill_down(request))
    
    def test_rate_limit_enforcement(self, drill_down_service, mock_redis):
        """Test rate limit enforcement."""
        # Mock Redis to return 11 for the rate limit check
        mock_redis.incr = MagicMock(return_value=11)
        mock_redis.expire = MagicMock()
        
        request = DrillDownRequest(
            trace_id="test-trace-123",
            fields=[],
            user="testuser",
            mfa_used=False
        )
        
        with pytest.raises(ValueError, match="Rate limit exceeded"):
            asyncio.run(drill_down_service.drill_down(request))
    
    def test_cache_hit(self, drill_down_service, mock_redis, mock_hot_store):
        """Test Redis cache hit."""
        trace_id = "test-trace-123"
        mock_hot_store.spans[trace_id] = [
            {"attributes": {"custom.field": "Hello world"}}
        ]
        
        # Mock Redis to return cached data
        cached_data = {
            "fields": {"custom.field": "cached_value"},
            "storage_tier": "hot",
            "query_latency_ms": 10.5,
            "mfa_used": False,
            "cache_hit": False,
            "metadata": {}
        }
        mock_redis.get = MagicMock(return_value=json.dumps(cached_data))
        mock_redis.setex = MagicMock()
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=["custom.field"],
            user="testuser",
            mfa_used=False
        )
        
        result = asyncio.run(drill_down_service.drill_down(request))
        
        assert result.cache_hit == True
        assert result.fields["custom.field"] == "cached_value"
        # The cached latency should be preserved, but actual time will be different
        assert result.query_latency_ms > 0
    
    def test_trace_not_found(self, drill_down_service, mock_hot_store, mock_warm_store, mock_cold_store):
        """Test handling of trace not found in any tier."""
        # Ensure no store has this trace
        assert "nonexistent-trace" not in mock_hot_store.spans
        assert "nonexistent-trace" not in mock_warm_store.traces
        assert "nonexistent-trace" not in mock_cold_store.parquet_data
        
        request = DrillDownRequest(
            trace_id="nonexistent-trace",
            fields=[],
            user="testuser",
            mfa_used=False
        )
        
        with pytest.raises(ValueError, match="Trace not found in any tier"):
            asyncio.run(drill_down_service.drill_down(request))
    
    def test_latency_measurement(self, drill_down_service, mock_hot_store):
        """Test that latency is measured and returned."""
        trace_id = "test-trace-123"
        mock_hot_store.spans[trace_id] = [
            {"attributes": {"llm.input_text": "Hello world"}}
        ]
        
        request = DrillDownRequest(
            trace_id=trace_id,
            fields=[],
            user="testuser",
            mfa_used=False
        )
        
        result = asyncio.run(drill_down_service.drill_down(request))
        
        assert result.query_latency_ms > 0
        assert result.query_latency_ms < 1000  # Should be fast with mocks


class TestDrillDownAPI:
    """Test FastAPI DrillDown endpoint."""
    
    def setup_method(self):
        """Set up test environment."""
        # Set required environment variables
        os.environ["AGENT_OBS_RBAC_ROLES"] = "testuser:developer"
        
        # Mock the service
        self.mock_service = MagicMock()
        self.mock_service.drill_down = AsyncMock()
        
        with patch('agent_obs.trace_viewer.ui.get_drill_down_service') as mock_get:
            mock_get.return_value = self.mock_service
            self.client = TestClient(app)
    
    def teardown_method(self):
        """Clean up test environment."""
        # Clean up environment variables
        if "AGENT_OBS_RBAC_ROLES" in os.environ:
            del os.environ["AGENT_OBS_RBAC_ROLES"]
    
    def test_drill_down_success(self):
        """Test successful drill-down request."""
        # Mock service response
        self.mock_service.drill_down.return_value = DrillDownResult(
            fields={"llm.input_text": "Hello world"},
            storage_tier="hot",
            query_latency_ms=25.5,
            mfa_used=False,
            cache_hit=False
        )
        
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down",
            headers={"X-OryaObs-User": "testuser"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["storage_tier"] == "hot"
        assert data["fields"]["llm.input_text"] == "Hello world"
        assert data["query_latency_ms"] == 25.5
        assert data["mfa_used"] == False
    
    def test_drill_down_missing_user_header(self):
        """Test missing user header."""
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down"
        )
        
        assert response.status_code == 403
        assert "X-OryaObs-User header required" in response.json()["detail"]
    
    def test_drill_down_missing_trace_read_permission(self):
        """Test missing trace:read permission."""
        os.environ["AGENT_OBS_RBAC_ROLES"] = "testuser:auditor"  # auditor doesn't have trace:read
        
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down",
            headers={"X-OryaObs-User": "testuser"}
        )
        
        assert response.status_code == 403
        assert "trace:read permission required" in response.json()["detail"]
    
    def test_drill_down_pii_without_mfa(self):
        """Test PII fields without MFA."""
        self.mock_service.drill_down.return_value = DrillDownResult(
            fields={"llm.input_text": "Hello world"},
            storage_tier="hot",
            query_latency_ms=25.5,
            mfa_used=False
        )
        
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down?fields=llm.input_text",
            headers={"X-OryaObs-User": "testuser"}
        )
        
        assert response.status_code == 403
        assert "pii:recover permission required" in response.json()["detail"]
    
    def test_drill_down_pii_with_invalid_mfa(self):
        """Test PII fields with invalid MFA token."""
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down?fields=llm.input_text",
            headers={
                "X-OryaObs-User": "testuser",
                "X-OryaObs-MFA": "invalid_token"
            }
        )
        
        assert response.status_code == 403
        assert "MFA: Invalid token provided" in response.json()["detail"]
    
    def test_drill_down_pii_with_valid_mfa(self):
        """Test PII fields with valid MFA token."""
        self.mock_service.drill_down.return_value = DrillDownResult(
            fields={"llm.input_text": "Hello world"},
            storage_tier="hot",
            query_latency_ms=25.5,
            mfa_used=True
        )
        
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down?fields=llm.input_text",
            headers={
                "X-OryaObs-User": "testuser",
                "X-OryaObs-MFA": "valid_token"
            }
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["mfa_used"] == True
    
    def test_drill_down_fields_parameter(self):
        """Test comma-separated fields parameter."""
        self.mock_service.drill_down.return_value = DrillDownResult(
            fields={"llm.input_text": "Hello", "llm.output_text": "World"},
            storage_tier="hot",
            query_latency_ms=25.5,
            mfa_used=False
        )
        
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down?fields=llm.input_text,llm.output_text",
            headers={"X-OryaObs-User": "testuser"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "llm.input_text" in data["fields"]
        assert "llm.output_text" in data["fields"]
    
    def test_drill_down_trace_not_found(self):
        """Test trace not found error."""
        self.mock_service.drill_down.side_effect = ValueError("Trace not found in any tier")
        
        response = self.client.get(
            "/api/v1/traces/nonexistent-trace/drill-down",
            headers={"X-OryaObs-User": "testuser"}
        )
        
        assert response.status_code == 404
        assert "not found" in response.json()["detail"]
    
    def test_drill_down_rate_limited(self):
        """Test rate limit exceeded error."""
        self.mock_service.drill_down.side_effect = ValueError("Rate limit exceeded")
        
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down",
            headers={"X-OryaObs-User": "testuser"}
        )
        
        assert response.status_code == 429
        assert "Rate limit exceeded" in response.json()["detail"]
    
    def test_drill_down_server_error(self):
        """Test generic server error."""
        self.mock_service.drill_down.side_effect = Exception("Internal error")
        
        response = self.client.get(
            "/api/v1/traces/test-trace-123/drill-down",
            headers={"X-OryaObs-User": "testuser"}
        )
        
        assert response.status_code == 500
        assert "Internal server error" in response.json()["detail"]


if __name__ == "__main__":
    pytest.main([__file__])