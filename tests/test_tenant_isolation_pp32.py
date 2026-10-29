"""PP32 tenant isolation tests: ClickHouse partitioning + Postgres RLS + cross-tenant audit."""

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_obs.observability import SpanContext, ObservabilitySDK
from agent_obs.storage.hot import HotStore, _validate_tenant_identifier, _tenant_user_statements
from agent_obs.storage.warm import WarmStore, setup_connection
from agent_obs.storage.drill_down import DrillDownRequest, DrillDownService
from agent_obs.guardrail.audit import CrossTenantAccessAuditEvent


class TestSpanContextTenant:
    """Test SpanContext tenant_id field and inheritance."""

    def test_span_context_tenant_field(self):
        """SpanContext has tenant_id field."""
        ctx = SpanContext(
            trace_id="trace-1",
            span_id="span-1",
            tenant_id="tenant-a"
        )
        assert ctx.tenant_id == "tenant-a"
        assert "tenant_id" in ctx.to_dict()
        assert ctx.to_dict()["tenant_id"] == "tenant-a"

    def test_span_context_new_inherits_tenant(self):
        """Child span inherits tenant_id from parent."""
        parent = SpanContext(
            trace_id="trace-1",
            span_id="parent-1",
            tenant_id="tenant-a"
        )
        child = SpanContext.new(
            agent_id="test-agent",
            parent=parent,
            tenant_id="tenant-b"  # Should be ignored, inherits from parent
        )
        assert child.tenant_id == "tenant-a"  # Inherited

    def test_span_context_new_explicit_tenant(self):
        """Root span gets explicit tenant_id."""
        ctx = SpanContext.new(
            agent_id="test-agent",
            tenant_id="tenant-a"
        )
        assert ctx.tenant_id == "tenant-a"

    def test_span_context_new_fallback_to_contextvar(self):
        """New falls back to contextvar if not provided."""
        import contextvars
        _obs_tenant_id = contextvars.ContextVar("_obs_tenant_id", default="")
        
        with patch("agent_obs.observability._obs_tenant_id", _obs_tenant_id):
            ctx = SpanContext.new(agent_id="test-agent")
            assert ctx.tenant_id == ""


class TestHotStoreTenant:
    """Test HotStore tenant isolation features."""

    def test_validate_tenant_identifier_valid(self):
        """Valid tenant identifiers pass validation."""
        _validate_tenant_identifier("tenant_a")
        _validate_tenant_identifier("tenant123")
        _validate_tenant_identifier("t")

    def test_validate_tenant_identifier_invalid(self):
        """Invalid tenant identifiers raise ValueError."""
        with pytest.raises(ValueError, match="Invalid tenant identifier"):
            _validate_tenant_identifier("tenant@a")
        with pytest.raises(ValueError, match="Invalid tenant identifier"):
            _validate_tenant_identifier("tenant-a")
        with pytest.raises(ValueError, match="Invalid tenant identifier"):
            _validate_tenant_identifier("")

    def test_tenant_user_statements_valid(self):
        """Generate valid SQL statements for tenant user creation."""
        statements = _tenant_user_statements("tenant_a", "password123")
        assert len(statements) == 3
        assert "CREATE USER IF NOT EXISTS ch_user_tenant_a" in statements[0]
        assert "CREATE ROW POLICY IF NOT EXISTS tenant_a_select" in statements[1]
        assert "GRANT SELECT ON observability.spans_hot TO ch_user_tenant_a" in statements[2]

    @patch("agent_obs.storage.hot.HotStore._execute_clickhouse")
    def test_create_tenant_user(self, mock_execute):
        """Create tenant user executes SQL statements."""
        store = HotStore()
        store._execute_clickhouse = mock_execute
        
        store.create_tenant_user("tenant_a", "password123")
        
        assert mock_execute.call_count == 3
        calls = mock_execute.call_args_list
        assert "CREATE USER IF NOT EXISTS ch_user_tenant_a" in str(calls[0][0][0])
        assert "CREATE ROW POLICY" in str(calls[1][0][0])
        assert "GRANT SELECT" in str(calls[2][0][0])

    @patch("agent_obs.storage.hot.HotStore._get_client")
    def test_get_spans_with_tenant(self, mock_get_client):
        """get_spans includes tenant filter when provided."""
        mock_client = MagicMock()
        mock_client.execute.return_value = []
        mock_get_client.return_value = mock_client
        
        store = HotStore()
        store.get_spans("trace-1", tenant_id="tenant-a")
        
        mock_client.execute.assert_called_once()
        query = mock_client.execute.call_args[0][0]
        assert "AND tenant_id = %s" in query
        assert mock_client.execute.call_args[0][1] == ["trace-1", "tenant-a"]

    @patch("agent_obs.storage.hot.HotStore._get_client")
    def test_get_spans_without_tenant(self, mock_get_client):
        """get_spans without tenant has no tenant filter in WHERE clause."""
        mock_client = MagicMock()
        mock_client.execute.return_value = []
        mock_get_client.return_value = mock_client
        
        store = HotStore()
        store.get_spans("trace-1")
        
        mock_client.execute.assert_called_once()
        query = mock_client.execute.call_args[0][0]
        assert "WHERE trace_id = %s" in query
        assert "AND tenant_id" not in query  # no tenant filter in WHERE
        assert mock_client.execute.call_args[0][1] == ["trace-1"]

    @patch("agent_obs.storage.hot.HotStore._get_client")
    def test_trace_exists(self, mock_get_client):
        """trace_exists checks trace existence."""
        mock_client = MagicMock()
        mock_client.execute.return_value = [(1,)]  # count = 1
        mock_get_client.return_value = mock_client
        
        store = HotStore()
        result = store.trace_exists("trace-1")
        
        assert result is True
        mock_client.execute.assert_called_once_with(
            "SELECT count() FROM spans_hot WHERE trace_id = %s",
            ["trace-1"]
        )


class TestWarmStoreTenant:
    """Test WarmStore tenant isolation features."""

    @patch("agent_obs.storage.warm.setup_connection")
    async def test_get_trace_with_tenant(self, mock_setup):
        """get_trace sets GUC and includes tenant filter."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        # Make pool.acquire() return an async context manager
        mock_acquire = AsyncMock()
        mock_acquire.__aenter__.return_value = mock_conn
        mock_acquire.__aexit__.return_value = None
        mock_pool.acquire.return_value = mock_acquire
        
        # Mock the lazy import of asyncpg
        with patch.dict('sys.modules', {'asyncpg': MagicMock()}):
            # Make create_pool return our mock_pool directly
            with patch('asyncpg.create_pool', return_value=mock_pool):
                store = WarmStore()
                await store.get_trace("trace-1", tenant_id="tenant-a")
                
                mock_setup.assert_called_once_with(mock_conn, "tenant-a")
                mock_conn.fetchrow.assert_called_once()
                query = mock_conn.fetchrow.call_args[0][0]
                assert "AND tenant_id = $2" in query

    @patch("agent_obs.storage.warm.setup_connection")
    async def test_get_trace_without_tenant(self, mock_setup):
        """get_trace without tenant doesn't set GUC or add filter."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        # Make pool.acquire() return an async context manager
        mock_acquire = AsyncMock()
        mock_acquire.__aenter__.return_value = mock_conn
        mock_acquire.__aexit__.return_value = None
        mock_pool.acquire.return_value = mock_acquire
        
        # Mock the lazy import of asyncpg
        with patch.dict('sys.modules', {'asyncpg': MagicMock()}):
            # Make create_pool return our mock_pool directly
            with patch('asyncpg.create_pool', return_value=mock_pool):
                store = WarmStore()
                await store.get_trace("trace-1")
                
                mock_setup.assert_not_called()
                mock_conn.fetchrow.assert_called_once()
                query = mock_conn.fetchrow.call_args[0][0]
                assert "tenant_id" not in query

    @patch("agent_obs.storage.warm.setup_connection")
    async def test_trace_exists(self, mock_setup):
        """trace_exists checks trace existence with unscoped GUC."""
        mock_pool = AsyncMock()
        mock_conn = AsyncMock()
        # Make pool.acquire() return an async context manager
        mock_acquire = AsyncMock()
        mock_acquire.__aenter__.return_value = mock_conn
        mock_acquire.__aexit__.return_value = None
        mock_pool.acquire.return_value = mock_acquire
        
        # Mock the lazy import of asyncpg
        with patch.dict('sys.modules', {'asyncpg': MagicMock()}):
            # Make create_pool return our mock_pool directly
            with patch('asyncpg.create_pool', return_value=mock_pool):
                store = WarmStore()
                result = await store.trace_exists("trace-1")
                
                assert result is True
                mock_setup.assert_called_once_with(mock_conn, "")
                mock_conn.fetchrow.assert_called_once_with(
                    "SELECT 1 FROM traces_warm WHERE trace_id = $1 LIMIT 1",
                    "trace-1"
                )


class TestDrillDownServiceTenant:
    """Test DrillDownService tenant isolation and audit."""

    def test_drill_down_request_tenant_field(self):
        """DrillDownRequest has tenant_id field."""
        request = DrillDownRequest(
            trace_id="trace-1",
            user="alice",
            tenant_id="tenant-a"
        )
        assert request.tenant_id == "tenant-a"

    @patch("agent_obs.storage.drill_down.DrillDownService._hot_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._warm_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._cold_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._trace_exists_audit")
    @patch("agent_obs.storage.drill_down.DrillDownService._cache_get")
    @patch("agent_obs.storage.drill_down.DrillDownService._rate_limit_check")
    async def test_drill_down_tenant_match(
        self, 
        mock_rate, 
        mock_cache, 
        mock_exists, 
        mock_cold, 
        mock_warm, 
        mock_hot
    ):
        """Drill-down succeeds when tenant matches."""
        mock_rate.return_value = True
        mock_cache.return_value = None
        mock_hot.return_value = ({"field": "value"}, "tenant-a")  # fields, tenant
        mock_warm.return_value = ({"field": "value"}, "tenant-a")
        mock_cold.return_value = {"fields": {}, "metadata": {}}
        
        service = DrillDownService(
            hot_store=MagicMock(),
            warm_store=MagicMock(),
            cold_store=MagicMock(),
            redis_client=None
        )
        
        request = DrillDownRequest(
            trace_id="trace-1",
            user="alice",
            tenant_id="tenant-a"
        )
        
        result = await service.drill_down(request)
        assert result.storage_tier == "hot"  # hot tier succeeded
        assert result.fields["field"] == "value"

    @patch("agent_obs.storage.drill_down.DrillDownService._hot_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._warm_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._cold_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._trace_exists_audit")
    @patch("agent_obs.storage.drill_down.DrillDownService._cache_get")
    @patch("agent_obs.storage.drill_down.DrillDownService._rate_limit_check")
    async def test_drill_down_tenant_mismatch(
        self, 
        mock_rate, 
        mock_cache, 
        mock_exists, 
        mock_cold, 
        mock_warm, 
        mock_hot
    ):
        """Drill-down denies when tenant mismatch and writes audit event."""
        mock_rate.return_value = True
        mock_cache.return_value = None
        mock_hot.return_value = ({"field": "value"}, "tenant-b")  # mismatch!
        mock_warm.return_value = ({"field": "value"}, "tenant-b")
        mock_cold.return_value = {"fields": {}, "metadata": {}}
        mock_exists.return_value = False  # trace doesn't exist
        
        mock_hot_store = MagicMock()
        mock_hot_store.write_audit_event = MagicMock()
        
        service = DrillDownService(
            hot_store=mock_hot_store,
            warm_store=MagicMock(),
            cold_store=MagicMock(),
            redis_client=None
        )
        
        request = DrillDownRequest(
            trace_id="trace-1",
            user="alice",
            tenant_id="tenant-a"
        )
        
        with pytest.raises(PermissionError, match="Cross-tenant access denied"):
            await service.drill_down(request)
        
        # Audit event written
        mock_hot_store.write_audit_event.assert_called_once()
        audit_event = mock_hot_store.write_audit_event.call_args[0][0]
        assert isinstance(audit_event, CrossTenantAccessAuditEvent)
        assert audit_event.resource["caller_tenant"] == "tenant-a"
        assert audit_event.resource["resource_tenant"] == "tenant-b"
        assert audit_event.decision == "deny"

    @patch("agent_obs.storage.drill_down.DrillDownService._hot_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._warm_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._cold_tier_recover")
    @patch("agent_obs.storage.drill_down.DrillDownService._trace_exists_audit")
    @patch("agent_obs.storage.drill_down.DrillDownService._cache_get")
    @patch("agent_obs.storage.drill_down.DrillDownService._rate_limit_check")
    async def test_drill_down_cross_tenant_attempt(
        self, 
        mock_rate, 
        mock_cache, 
        mock_exists, 
        mock_cold, 
        mock_warm, 
        mock_hot
    ):
        """Cross-tenant attempt audit when all tiers fail but trace exists."""
        mock_rate.return_value = True
        mock_cache.return_value = None
        mock_hot.side_effect = ValueError("Trace not found")
        mock_warm.side_effect = ValueError("Trace not found")
        mock_cold.side_effect = ValueError("Trace not found")
        mock_exists.return_value = True  # trace exists in audit probe
        
        mock_hot_store = MagicMock()
        mock_hot_store.write_audit_event = MagicMock()
        
        service = DrillDownService(
            hot_store=mock_hot_store,
            warm_store=MagicMock(),
            cold_store=MagicMock(),
            redis_client=None
        )
        
        request = DrillDownRequest(
            trace_id="trace-1",
            user="alice",
            tenant_id="tenant-a"
        )
        
        with pytest.raises(PermissionError, match="Cross-tenant access denied"):
            await service.drill_down(request)
        
        # Audit event written for attempted access
        mock_hot_store.write_audit_event.assert_called_once()
        audit_event = mock_hot_store.write_audit_event.call_args[0][0]
        assert isinstance(audit_event, CrossTenantAccessAuditEvent)
        assert audit_event.resource["caller_tenant"] == "tenant-a"
        assert audit_event.resource["resource_tenant"] == "<unknown>"


class TestCrossTenantAccessAuditEvent:
    """Test CrossTenantAccessAuditEvent class."""

    def test_for_attempt_factory(self):
        """Create audit event with correct fields."""
        event = CrossTenantAccessAuditEvent.for_attempt(
            trace_id="trace-1",
            user="alice",
            caller_tenant="tenant-a",
            resource_tenant="tenant-b",
            tier="hot"
        )
        
        assert event.action == "tenant.cross_tenant_access"
        assert event.decision == "deny"
        assert event.reason == "cross_tenant_denied"
        assert event.resource["type"] == "trace"
        assert event.resource["trace_id"] == "trace-1"
        assert event.resource["caller_tenant"] == "tenant-a"
        assert event.resource["resource_tenant"] == "tenant-b"
        assert event.resource["tier"] == "hot"
        assert event.resource["severity"] == "warning"

    def test_to_clickhouse_row(self):
        """Convert to ClickHouse row with 10 columns."""
        event = CrossTenantAccessAuditEvent(
            audit_id="cta-1234567890ab",
            timestamp=1234567890.0,
            trace_id="trace-1",
            actor={"type": "user", "id": "alice"},
            resource={
                "type": "trace",
                "trace_id": "trace-1",
                "caller_tenant": "tenant-a",
                "resource_tenant": "tenant-b",
                "tier": "hot",
                "severity": "warning"
            },
            reason="cross_tenant_denied",
            ip_address="192.168.1.1",
            user_agent="test"
        )
        
        row = event.to_clickhouse_row()
        assert len(row) == 10
        assert row[0] == "cta-1234567890ab"  # audit_id
        assert row[4] == "tenant.cross_tenant_access"  # action
        assert row[5] == "deny"  # decision
        assert row[7] == "cross_tenant_denied"  # reason


class TestSdkTenantStamping:
    """Test SDK tenant_id stamping in _enqueue."""

    @patch("agent_obs.observability.ObservabilitySDK._apply_guardrail")
    @patch("agent_obs.observability.ObservabilitySDK._apply_rule_based_eval")
    async def test_enqueue_stamps_tenant(self, mock_eval, mock_guardrail):
        """_enqueue stamps tenant_id from context into span attributes."""
        from agent_obs.observability import Span, ObservabilitySDK
        
        sdk = ObservabilitySDK(exporters=[])
        sdk._ring_buffer = MagicMock()
        sdk._ring_buffer.put_nowait = MagicMock()
        
        span = Span(
            name="test-span",
            span_type="agent.loop",
            context=SpanContext(
                trace_id="trace-1",
                span_id="span-1",
                tenant_id="tenant-a"
            ),
            attributes={}
        )
        
        await sdk._enqueue(span)
        
        # Verify tenant_id was stamped
        assert "tenant_id" in span.attributes
        assert span.attributes["tenant_id"] == "tenant-a"


# Integration test (skip if no Postgres available)
@pytest.mark.skipif(
    not os.environ.get("WARM_PG_DSN", "").startswith("postgresql://"),
    reason="Postgres not available"
)
class TestRlsIntegration:
    """Live Postgres RLS integration tests."""

    @pytest.mark.asyncio
    async def test_warm_user_rls_isolation(self):
        """Warm user sees only own tenant rows under RLS."""
        # This test requires a running Postgres instance with the DDL applied
        # and test data in different tenants
        pass  # Implementation depends on test data setup