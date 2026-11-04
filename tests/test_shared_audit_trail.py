"""Tests for SharedAuditTrail (PP46)."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, UTC

from agent_obs.proxy.audit_trail import SharedAuditTrail, SharedAuditEvent
from agent_obs.storage.hot import HotStore


@pytest.fixture
def mock_hot_store():
    """Mock HotStore for testing."""
    store = AsyncMock(spec=HotStore)
    
    # Mock ClickHouse events data
    mock_events = [
        (
            "audit_1",
            datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC),
            "trace_1",
            '{"user_id": "user1", "agent_id": "agent1", "tenant_id": "tenant1"}',
            "security.clean",
            "forward",
            '{"security_score": 0.95, "redacted_fields_count": 0, "security_audit_id": "sec_1", "obs_span_id": "obs_1"}',
            "Security verdict: clean, Decision: forward",
            "192.168.1.1",
            "Mozilla/5.0"
        ),
        (
            "audit_2",
            datetime(2024, 1, 1, 12, 1, 0, tzinfo=UTC),
            "trace_1",
            '{"user_id": "user2", "agent_id": "agent2", "tenant_id": "tenant1"}',
            "security.flag",
            "flag",
            '{"security_score": 0.3, "redacted_fields_count": 2, "security_audit_id": "sec_2", "obs_span_id": "obs_2"}',
            "Security verdict: flag, Decision: flag",
            "192.168.1.2",
            "curl/7.68.0"
        )
    ]
    
    store.get_audit_events.return_value = mock_events
    store.write_audit_event = AsyncMock()
    
    return store


@pytest.fixture
def audit_trail(mock_hot_store):
    """SharedAuditTrail instance with mocked HotStore."""
    return SharedAuditTrail(hot_store=mock_hot_store)


@pytest.fixture
def audit_trail_with_mock_storage():
    """SharedAuditTrail instance with mock storage fallback."""
    return SharedAuditTrail(hot_store=None)


@pytest.mark.asyncio
async def test_shared_audit_trail_write_with_hot_store(audit_trail, mock_hot_store):
    """Test writing audit event with real HotStore."""
    result = await audit_trail.write(
        request_id="req_1",
        trace_id="trace_1",
        security_audit_id="sec_1",
        obs_span_id="obs_1",
        decision="forward",
        timestamp=1640995200.0,
        security_verdict="clean",
        security_score=0.95,
        redacted_fields_count=0,
        policy_rule=None,
        metadata={"user_id": "user1", "agent_id": "agent1", "tenant_id": "tenant1"},
        ip_address="192.168.1.1",
        user_agent="Mozilla/5.0"
    )
    
    # Verify HotStore was called
    mock_hot_store.write_audit_event.assert_called_once()
    
    # Verify result (should be SHA256 hash)
    assert len(result) == 64  # SHA256 hash length
    assert result.startswith("7f8483")  # First few characters of the hash


@pytest.mark.asyncio
async def test_shared_audit_trail_get_by_trace(audit_trail, mock_hot_store):
    """Test retrieving events by trace ID."""
    events = await audit_trail.get_by_trace("trace_1", limit=10)
    
    # Verify HotStore was called
    mock_hot_store.get_audit_events.assert_called_once_with(trace_id="trace_1", limit=10)
    
    # Verify results
    assert len(events) == 2
    
    # Debug: print the actual event data
    for event in events:
        print(f"Event: {event}")
        print(f"IP: {event.ip_address}")
        print(f"User Agent: {event.user_agent}")
    
    # Check first event (should be the later one due to sorting)
    event1 = events[0]
    assert event1.audit_id == "audit_2"
    assert event1.trace_id == "trace_1"
    assert event1.security_verdict == "flag"
    assert event1.decision == "flag"
    assert event1.security_score == 0.3
    assert event1.redacted_fields_count == 2
    assert event1.security_audit_id == "sec_2"
    assert event1.obs_span_id == "obs_2"
    assert event1.ip_address == "192.168.1.2"
    assert event1.user_agent == "curl/7.68.0"


@pytest.mark.asyncio
async def test_shared_audit_trail_get_by_decision(audit_trail, mock_hot_store):
    """Test retrieving events by decision type."""
    events = await audit_trail.get_by_decision("forward")
    
    # Verify HotStore was called
    mock_hot_store.get_audit_events.assert_called_once_with(limit=1000)
    
    # Verify results
    assert len(events) == 1
    assert events[0].decision == "forward"


@pytest.mark.asyncio
async def test_shared_audit_trail_get_security_summary(audit_trail, mock_hot_store):
    """Test getting security summary statistics."""
    summary = await audit_trail.get_security_summary()
    
    # Verify HotStore was called
    mock_hot_store.get_audit_events.assert_called_once_with(trace_id=None, limit=1000)
    
    # Verify summary
    assert summary["total_events"] == 2
    assert summary["block_count"] == 0
    assert summary["flag_count"] == 1
    assert summary["forward_count"] == 1
    assert summary["block_rate"] == 0.0
    assert summary["flag_rate"] == 0.5
    assert summary["avg_security_score"] == 0.625
    assert summary["total_redacted_fields"] == 2


@pytest.mark.asyncio
async def test_shared_audit_trail_get_audit_events_for_ui(audit_trail, mock_hot_store):
    """Test getting audit events for UI display."""
    events = await audit_trail.get_audit_events_for_ui(
        tenant_id="tenant1",
        security_verdict="clean",
        limit=10
    )
    
    # Verify HotStore was called
    mock_hot_store.get_audit_events.assert_called_once_with(limit=1000)
    
    # Verify results
    assert len(events) == 1
    assert events[0]["tenant_id"] == "tenant1"
    assert events[0]["security_verdict"] == "clean"
    assert events[0]["trace_id"] == "trace_1"


@pytest.mark.asyncio
async def test_shared_audit_trail_get_audit_summary_for_ui(audit_trail, mock_hot_store):
    """Test getting audit summary for UI."""
    summary = await audit_trail.get_audit_summary_for_ui()
    
    # Verify HotStore was called
    mock_hot_store.get_audit_events.assert_called_once_with(limit=10000)
    
    # Verify summary
    assert summary["total_events"] == 2
    assert summary["unique_tenants"] == 1
    assert summary["unique_users"] == 2
    assert summary["unique_agents"] == 2


@pytest.mark.asyncio
async def test_shared_audit_trail_fallback_to_mock(audit_trail):
    """Test fallback to mock storage when HotStore is not available."""
    # Create audit trail without HotStore
    audit_trail_mock = SharedAuditTrail()
    
    # Add some mock data
    audit_trail_mock._mock_storage = {
        "mock_1": SharedAuditEvent(
            audit_id="mock_1",
            trace_id="mock_trace_1",
            request_id="mock_req_1",
            security_audit_id="mock_sec_1",
            obs_span_id="mock_obs_1",
            decision="forward",
            timestamp=1640995200.0,
            security_verdict="clean",
            security_score=0.95,
            redacted_fields_count=0,
            policy_rule=None,
            metadata={"user_id": "user1", "agent_id": "agent1", "tenant_id": "tenant1"},
            ip_address="192.168.1.1",
            user_agent="Mozilla/5.0"
        )
    }
    
    # Test retrieval
    events = await audit_trail_mock.get_by_trace("mock_trace_1")
    assert len(events) == 1
    assert events[0].audit_id == "mock_1"


@pytest.mark.asyncio
async def test_shared_audit_trail_error_handling(audit_trail, mock_hot_store):
    """Test error handling when HotStore operations fail."""
    # Make HotStore raise an exception
    mock_hot_store.get_audit_events.side_effect = Exception("Database error")
    
    # Test that the method doesn't crash and returns empty list
    events = await audit_trail.get_by_trace("trace_1")
    assert len(events) == 0
    
    # Test summary error handling
    summary = await audit_trail.get_security_summary()
    assert summary == {}


@pytest.mark.asyncio
async def test_shared_audit_trail_cleanup_old_events(audit_trail, mock_hot_store):
    """Test cleanup of old audit events."""
    # Mock HotStore doesn't have cleanup, so this should return 0
    result = await audit_trail.cleanup_old_events(max_age_hours=168)
    assert result == 0


@pytest.mark.asyncio
async def test_shared_audit_trail_export_to_csv(audit_trail_with_mock_storage):
    """Test export to CSV functionality."""
    # First write some events to populate the mock storage
    await audit_trail_with_mock_storage.write(
        request_id="req_1",
        trace_id="trace_1",
        security_audit_id="sec_1",
        obs_span_id="obs_1",
        decision="forward",
        timestamp=1704067200.0,
        security_verdict="clean",
        security_score=0.95,
        redacted_fields_count=0,
        policy_rule=None,
        metadata={"user_id": "user1", "agent_id": "agent1", "tenant_id": "tenant1"},
        ip_address="192.168.1.1",
        user_agent="Mozilla/5.0"
    )
    
    await audit_trail_with_mock_storage.write(
        request_id="req_2",
        trace_id="trace_1",
        security_audit_id="sec_2",
        obs_span_id="obs_2",
        decision="flag",
        timestamp=1704067260.0,
        security_verdict="flag",
        security_score=0.3,
        redacted_fields_count=2,
        policy_rule=None,
        metadata={"user_id": "user2", "agent_id": "agent2", "tenant_id": "tenant1"},
        ip_address="192.168.1.2",
        user_agent="curl/7.68.0"
    )
    
    # Mock HotStore doesn't have real export, so this should return count
    result = await audit_trail_with_mock_storage.export_to_csv(1704067000.0, 1704068000.0, "/tmp/test.csv")
    assert result == 2  # We have 2 mock events


def test_shared_audit_event_dataclass():
    """Test SharedAuditEvent dataclass structure."""
    event = SharedAuditEvent(
        audit_id="test_audit",
        trace_id="test_trace",
        request_id="test_request",
        security_audit_id="test_security",
        obs_span_id="test_obs",
        decision="forward",
        timestamp=1640995200.0,
        security_verdict="clean",
        security_score=0.95,
        redacted_fields_count=0,
        policy_rule=None,
        metadata={"user_id": "test_user"},
        ip_address="192.168.1.1",
        user_agent="Mozilla/5.0"
    )
    
    assert event.audit_id == "test_audit"
    assert event.trace_id == "test_trace"
    assert event.decision == "forward"
    assert event.security_verdict == "clean"
    assert event.security_score == 0.95


@pytest.mark.asyncio
async def test_shared_audit_trail_circuit_breaker_integration(audit_trail, mock_hot_store):
    """Test circuit breaker integration for failover."""
    # Simulate circuit breaker open
    audit_trail.circuit_breaker.allow_request = MagicMock(return_value=False)
    
    # Write should be skipped
    result = await audit_trail.write(
        request_id="req_1",
        trace_id="trace_1",
        security_audit_id="sec_1",
        obs_span_id="obs_1",
        decision="forward",
        timestamp=1640995200.0,
        security_verdict="clean",
        security_score=0.95,
        redacted_fields_count=0,
        metadata={},
        ip_address="192.168.1.1",
        user_agent="Mozilla/5.0"
    )
    
    # Should return skipped ID
    assert result.startswith("skipped_")
    
    # HotStore should not have been called
    mock_hot_store.write_audit_event.assert_not_called()


@pytest.mark.asyncio
async def test_shared_audit_trail_batch_write_optimization(audit_trail, mock_hot_store):
    """Test that batch operations are optimized."""
    # Multiple writes should use the same HotStore instance
    await audit_trail.write(
        request_id="req_1", trace_id="trace_1", security_audit_id="sec_1", obs_span_id="obs_1",
        decision="forward", timestamp=1640995200.0, security_verdict="clean", security_score=0.95,
        redacted_fields_count=0, metadata={}, ip_address="192.168.1.1", user_agent="Mozilla/5.0"
    )
    
    await audit_trail.write(
        request_id="req_2", trace_id="trace_2", security_audit_id="sec_2", obs_span_id="obs_2",
        decision="flag", timestamp=1640995260.0, security_verdict="flag", security_score=0.3,
        redacted_fields_count=2, metadata={}, ip_address="192.168.1.2", user_agent="curl/7.68.0"
    )
    
    # Verify HotStore was called twice (once per write)
    assert mock_hot_store.write_audit_event.call_count == 2


@pytest.mark.asyncio
async def test_shared_audit_trail_time_filtering(audit_trail, mock_hot_store):
    """Test time-based filtering of audit events."""
    # Test with time range that should include both events
    start_time = 1704110300.0  # Before first event (Jan 1, 2024 11:58:00 UTC)
    end_time = 1704110500.0   # After second event (Jan 1, 2024 12:01:40 UTC)
    
    events = await audit_trail.get_audit_events_for_ui(
        start_time=start_time,
        end_time=end_time
    )
    
    # Debug: print what we got
    print(f"Events returned: {len(events)}")
    for event in events:
        print(f"Event: {event}")
    
    # Should return both events
    assert len(events) == 2
    
    # Test with time range that should exclude both events
    start_time = 1704110600.0  # After both events
    
    events = await audit_trail.get_audit_events_for_ui(
        start_time=start_time,
        end_time=1704110700.0  # End time after start time
    )
    
    # Should return no events
    assert len(events) == 0