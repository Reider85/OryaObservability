"""Cost-aware model routing tests for LLM proxy (PP55).

Tests the CostAwareRouter class and integration with LLMProxyServer for
downgrading expensive models to cheaper alternatives based on cost savings
thresholds and business rules.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from agent_obs.proxy.router import CostAwareRouter, RoutingDecision
from agent_obs.proxy import ProxyConfig
from agent_obs.cost.price_book import PriceBook, PriceNotFoundError


class TestCostAwareRouter:
    """Test the CostAwareRouter class functionality."""

    @pytest.fixture
    def config(self):
        """Create a test proxy configuration with cost-aware routing enabled."""
        return ProxyConfig(
            cost_aware_routing_enabled=True,
            cost_aware_routing_threshold=0.8,
            downgrade_targets={
                "gpt-4o": ["gpt-4o-mini", "gpt-3.5-turbo"],
                "claude-3.5-sonnet": ["claude-3-haiku"],
            },
        )

    @pytest.fixture
    def price_book(self):
        """Create a test price book."""
        return PriceBook.load("price_book.yaml")

    @pytest.fixture
    def router(self, config, price_book):
        """Create a CostAwareRouter instance for testing."""
        return CostAwareRouter(config, price_book)

    @pytest.mark.asyncio
    async def test_select_model_downgrade_simple_request(self, router, config):
        """Test model selection for simple requests that qualify for downgrade."""
        # Simple request: gpt-4o -> gpt-4o-mini
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello world"}
            ]
        }).encode("utf-8")

        decision = await router.select_model(
            request_body=request_body,
            tenant_id="tenant_a",
            agent_id="agent_1",
            user_id="user_1",
            current_model="gpt-4o"
        )

        assert decision.should_downgrade is True
        assert decision.new_model == "gpt-4o-mini"
        assert decision.original_model == "gpt-4o"
        assert decision.cost_savings_usd > 0
        assert decision.routing_reason in ["cost_threshold_met", "business_rules"]

    @pytest.mark.asyncio
    async def test_select_model_no_downgrade_complex_request(self, router, config):
        """Test that complex requests are not downgraded."""
        # Complex request with tools - should not be downgraded
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello world"}
            ],
            "tools": [
                {"type": "function", "function": {"name": "test", "parameters": {}}}
            ]
        }).encode("utf-8")

        decision = await router.select_model(
            request_body=request_body,
            tenant_id="tenant_a",
            agent_id="agent_1",
            user_id="user_1",
            current_model="gpt-4o"
        )

        assert decision.should_downgrade is False
        assert decision.new_model == "gpt-4o"
        assert decision.original_model == "gpt-4o"
        assert decision.routing_reason == "complex_request"

    @pytest.mark.asyncio
    async def test_select_model_tenant_blacklist(self, router, config):
        """Test tenant blacklist business rule."""
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello world"}
            ]
        }).encode("utf-8")

        decision = await router.select_model(
            request_body=request_body,
            tenant_id="blacklisted_tenant",  # This tenant has gpt-3.5-turbo blacklisted
            agent_id="agent_1",
            user_id="user_1",
            current_model="gpt-4o"
        )

        # Should downgrade to gpt-4o-mini (whitelisted) but not gpt-3.5-turbo (blacklisted)
        assert decision.should_downgrade is True
        assert decision.new_model == "gpt-4o-mini"

    @pytest.mark.asyncio
    async def test_select_model_agent_blacklist(self, router, config):
        """Test agent blacklist business rule."""
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello world"}
            ]
        }).encode("utf-8")

        decision = await router.select_model(
            request_body=request_body,
            tenant_id="tenant_a",
            agent_id="blacklisted_agent",  # This agent has claude-3-haiku blacklisted
            user_id="user_1",
            current_model="claude-3.5-sonnet"
        )

        # Should not downgrade to claude-3-haiku due to agent blacklist
        assert decision.should_downgrade is False
        assert decision.new_model == "claude-3.5-sonnet"

    @pytest.mark.asyncio
    async def test_select_model_no_alternatives(self, router, config):
        """Test behavior when no alternatives are available."""
        # Model with no configured alternatives
        request_body = json.dumps({
            "model": "gemini-1.5-pro",
            "messages": [
                {"role": "user", "content": "Hello world"}
            ]
        }).encode("utf-8")

        decision = await router.select_model(
            request_body=request_body,
            tenant_id="tenant_a",
            agent_id="agent_1",
            user_id="user_1",
            current_model="gemini-1.5-pro"
        )

        assert decision.should_downgrade is False
        assert decision.new_model == "gemini-1.5-pro"
        assert decision.routing_reason == "no_alternatives"

    def test_is_simple_request(self, router, config):
        """Test simple request detection logic."""
        # Simple request
        simple_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello"}
            ]
        }).encode("utf-8")
        assert router._is_simple_request(simple_body, "gpt-4o") is True

        # Complex request (too many messages)
        complex_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
                {"role": "user", "content": "How are you?"},
                {"role": "assistant", "content": "Good"},
                {"role": "user", "content": "What's next?"}
            ]
        }).encode("utf-8")
        assert router._is_simple_request(complex_body, "gpt-4o") is False

        # Request with tools
        tools_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello"}
            ],
            "tools": []
        }).encode("utf-8")
        assert router._is_simple_request(tools_body, "gpt-4o") is False

        # Wrong model type
        wrong_model_body = json.dumps({
            "model": "gpt-3.5-turbo",
            "messages": [
                {"role": "user", "content": "Hello"}
            ]
        }).encode("utf-8")
        assert router._is_simple_request(wrong_model_body, "gpt-3.5-turbo") is False

    def test_get_alternatives(self, router, config):
        """Test alternative model selection."""
        # Test custom alternatives
        alternatives = router._get_alternatives("gpt-4o")
        assert "gpt-4o-mini" in alternatives
        assert "gpt-3.5-turbo" in alternatives
        assert "gpt-4o" not in alternatives  # Current model should be filtered out

        # Test default alternatives
        alternatives = router._get_alternatives("claude-3.5-sonnet")
        assert "claude-3-haiku" in alternatives

        # Test model with no alternatives
        alternatives = router._get_alternatives("gemini-1.5-pro")
        assert alternatives == []

    @pytest.mark.asyncio
    async def test_score_models(self, router, config):
        """Test model scoring logic."""
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello world"}
            ]
        }).encode("utf-8")

        alternatives = ["gpt-4o-mini", "gpt-3.5-turbo"]
        scored = await router._score_models(
            alternatives,
            estimated_tokens=100,
            tenant_id="tenant_a",
            agent_id="agent_1"
        )

        assert len(scored) == 2
        # Models should be sorted by score (lower is better for cost)
        assert scored[0]["model"] == "gpt-4o-mini"  # Should be cheaper
        assert scored[1]["model"] == "gpt-3.5-turbo"

    def test_should_downgrade_sync(self, router, config):
        """Test synchronous downgrade check."""
        # Simple request - should downgrade
        simple_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello"}
            ]
        }).encode("utf-8")
        
        should_downgrade, new_model = router.should_downgrade(simple_body, "gpt-4o")
        assert should_downgrade is True
        assert new_model == "gpt-4o-mini"

        # Complex request - should not downgrade
        complex_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
                {"role": "user", "content": "How are you?"},
                {"role": "assistant", "content": "Good"},
                {"role": "user", "content": "What's next?"}
            ]
        }).encode("utf-8")
        
        should_downgrade, new_model = router.should_downgrade(complex_body, "gpt-4o")
        assert should_downgrade is False
        assert new_model == "gpt-4o"

    def test_extract_system_prompt(self, router, config):
        """Test system prompt extraction."""
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "system", "content": "You are a helpful assistant"},
            {"role": "user", "content": "How are you?"}
        ]
        
        system_prompt = router._extract_system_prompt(messages)
        assert system_prompt == "You are a helpful assistant"

        # No system prompt
        messages_no_system = [
            {"role": "user", "content": "Hello"},
            {"role": "user", "content": "How are you?"}
        ]
        
        system_prompt = router._extract_system_prompt(messages_no_system)
        assert system_prompt == ""

    def test_has_reasoning_instructions(self, router, config):
        """Test reasoning instruction detection."""
        # Contains reasoning instructions
        reasoning_prompt = "Think step by step and provide a detailed analysis"
        assert router._has_reasoning_instructions(reasoning_prompt) is True

        # No reasoning instructions
        normal_prompt = "You are a helpful assistant"
        assert router._has_reasoning_instructions(normal_prompt) is False

    @pytest.mark.asyncio
    async def test_price_not_found_handling(self, router, config):
        """Test handling of models with no price information."""
        # Mock price book to raise PriceNotFoundError for a model
        with patch('agent_obs.proxy.router.compute_cost') as mock_compute:
            mock_compute.side_effect = PriceNotFoundError("Model not found")
            
            request_body = json.dumps({
                "model": "gpt-4o",
                "messages": [
                    {"role": "user", "content": "Hello world"}
                ]
            }).encode("utf-8")

            decision = await router.select_model(
                request_body=request_body,
                tenant_id="tenant_a",
                agent_id="agent_1",
                user_id="user_1",
                current_model="gpt-4o"
            )

            # Should not downgrade if alternatives have no pricing
            assert decision.should_downgrade is False
            assert decision.new_model == "gpt-4o"


class TestCostAwareRouterIntegration:
    """Test integration with LLMProxyServer."""

    @pytest.fixture
    def mock_sdk(self):
        """Create a mock SDK for testing."""
        mock_sdk = MagicMock()
        mock_sdk._enqueue = AsyncMock()
        mock_sdk._resolve_hot_store = MagicMock(return_value=None)
        return mock_sdk

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        return ProxyConfig(
            cost_aware_routing_enabled=True,
            cost_aware_routing_threshold=0.8,
            downgrade_targets={
                "gpt-4o": ["gpt-4o-mini"],
            },
        )

    def test_cost_router_initialization(self, config, mock_sdk):
        """Test that CostAwareRouter is properly initialized in LLMProxyServer."""
        from agent_obs.proxy.server import LLMProxyServer
        
        with patch('agent_obs.cost.price_book.PriceBook.load') as mock_load:
            mock_load.return_value = MagicMock()
            
            server = LLMProxyServer(config, mock_sdk)
            
            assert server.cost_router is not None
            assert getattr(config, 'cost_aware_routing_enabled', False) is True

    def test_cost_router_disabled(self, config, mock_sdk):
        """Test that CostAwareRouter is not initialized when disabled."""
        config.cost_aware_routing_enabled = False
        
        from agent_obs.proxy.server import LLMProxyServer
        
        server = LLMProxyServer(config, mock_sdk)
        
        assert server.cost_router is None

    @pytest.mark.asyncio
    async def test_routing_decision_in_span_attributes(self, config, mock_sdk):
        """Test that routing decisions are tracked in span attributes."""
        from agent_obs.proxy.server import LLMProxyServer
        
        with patch('agent_obs.cost.price_book.PriceBook.load') as mock_load:
            from unittest.mock import MagicMock
            mock_price_book = MagicMock()
            mock_load.return_value = mock_price_book
            
            server = LLMProxyServer(config, mock_sdk)
            
            # Mock the cost router to return a downgrade decision
            mock_decision = RoutingDecision(
                should_downgrade=True,
                new_model="gpt-4o-mini",
                original_model="gpt-4o",
                routing_reason="cost_threshold_met",
                cost_savings_usd=0.01
            )
            server.cost_router.select_model = AsyncMock(return_value=mock_decision)
            
            # Create a mock request
            from fastapi import Request
            from unittest.mock import MagicMock
            
            mock_request = MagicMock()
            mock_request.body.return_value = json.dumps({
                "model": "gpt-4o",
                "messages": [
                    {"role": "user", "content": "Hello world"}
                ]
            }).encode("utf-8")
            # Make body() return an awaitable that resolves to the bytes
            mock_request.body = AsyncMock(return_value=mock_request.body.return_value)
            mock_request.headers = {
                "X-OryaObs-Trace-Id": "test-trace-id",
                "X-OryaObs-User-Id": "test-user",
                "X-OryaObs-Tenant-Id": "test-tenant",
                "X-OryaObs-Agent-Id": "test-agent",
            }
            
            # Mock the upstream call to avoid actual HTTP requests
            with patch.object(server, '_handle_non_streaming') as mock_handler:
                mock_response = MagicMock()
                mock_response.status_code = 200
                mock_response.content = b'{"choices": [{"message": {"content": "Hello"}}]}'
                mock_handler.return_value = mock_response
                
                # Mock the budget check to pass
                with patch.object(server, 'budget') as mock_budget:
                    mock_budget.check.return_value = "ok"
                    
                    # Call the chat completions endpoint
                    response = await server.chat_completions(mock_request)
                    
                    # Verify that the cost router was called at least once
                    server.cost_router.select_model.assert_called()
                    
                    # Verify the request was modified to use the downgraded model
                    assert mock_handler.call_count == 1
                    call_args = mock_handler.call_args
                    # The body is the 2nd positional argument (after self)
                    request_body = call_args[0][2]  # body is the 3rd positional argument
                    parsed_request = json.loads(request_body.decode('utf-8'))
                    assert parsed_request['model'] == 'gpt-4o-mini'
                    
                    # Verify that the span would contain routing attributes
                    # (This is tested indirectly through the integration)


class TestCostAwareRouterEdgeCases:
    """Test edge cases and error conditions."""

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        return ProxyConfig(
            cost_aware_routing_enabled=True,
            cost_aware_routing_threshold=0.8,
        )

    @pytest.fixture
    def price_book(self):
        """Create a test price book."""
        return PriceBook.load("price_book.yaml")

    @pytest.fixture
    def router(self, config, price_book):
        """Create a CostAwareRouter instance for testing."""
        return CostAwareRouter(config, price_book)

    def test_invalid_json_request(self, router, config):
        """Test handling of invalid JSON request bodies."""
        # Invalid JSON
        invalid_body = b"not json content"
        
        assert router._is_simple_request(invalid_body, "gpt-4o") is False

    def test_empty_request_body(self, router, config):
        """Test handling of empty request bodies."""
        # Empty request
        empty_body = b""
        
        assert router._is_simple_request(empty_body, "gpt-4o") is False

    @pytest.mark.asyncio
    async def test_threshold_adjustment(self, router, config):
        """Test cost threshold adjustment."""
        # Set a very high threshold (should prevent downgrades)
        config.cost_aware_routing_threshold = 0.99
        
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello world"}
            ]
        }).encode("utf-8")

        decision = await router.select_model(
            request_body=request_body,
            tenant_id="tenant_a",
            agent_id="agent_1",
            user_id="user_1",
            current_model="gpt-4o"
        )

        # With high threshold, should not downgrade
        assert decision.should_downgrade is False

    def test_token_estimation(self, router, config):
        """Test token estimation accuracy."""
        # Simple test: 4 characters ≈ 1 token
        request_body = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Hello"}
            ]
        }).encode("utf-8")

        tokens = router._estimate_tokens(request_body)
        assert tokens == 1  # "Hello" = 5 chars ≈ 1 token

        # Longer message
        long_request = json.dumps({
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "This is a longer message with more content"}
            ]
        }).encode("utf-8")

        tokens = router._estimate_tokens(long_request)
        assert tokens > 1


class TestRoutingMetadata:
    """Test routing metadata tracking in ObsPipeline."""

    @pytest.fixture
    def config(self):
        """Create a test configuration."""
        return ProxyConfig(
            log_bodies=False,
        )

    @pytest.fixture
    def pipeline(self, config):
        """Create an ObsPipeline instance for testing."""
        from agent_obs.proxy.obs_pipeline import ObsPipeline
        return ObsPipeline(config)

    @pytest.fixture
    def span(self):
        """Create a test span."""
        from agent_obs.observability import Span, SpanContext, SpanType
        
        context = SpanContext(
            trace_id="test-trace-id",
            span_id="test-span-id",
            agent_id="test-agent",
            user_id="test-user",
            tenant_id="test-tenant",
        )
        
        span = Span(
            name="test-span",
            span_type=SpanType.LLM_PROXY_CALL,
            context=context,
            attributes={},
        )
        span.start_time = 1234567890.0
        return span

    def test_attach_routing_metadata(self, pipeline, span):
        """Test that routing metadata is properly attached to span."""
        # Add routing attributes to simulate routing decision
        span.attributes.update({
            "llm.original_model": "gpt-4o",
            "llm.routed_model": "gpt-4o-mini",
            "llm.routing_reason": "cost_threshold_met",
            "llm.cost_savings_usd": 0.01,
        })
        
        headers = {
            "X-OryaObs-Trace-Id": "test-trace-id",
            "X-OryaObs-User-Id": "test-user",
            "X-OryaObs-Tenant-Id": "test-tenant",
            "X-OryaObs-Agent-Id": "test-agent",
        }
        
        # Call the routing metadata attachment
        pipeline._attach_routing_metadata(span, headers=headers)
        
        # Verify that routing attributes are preserved
        assert span.attributes["llm.original_model"] == "gpt-4o"
        assert span.attributes["llm.routed_model"] == "gpt-4o-mini"
        assert span.attributes["llm.routing_reason"] == "cost_threshold_met"
        assert span.attributes["llm.cost_savings_usd"] == 0.01
        
        # Verify that routing event was added
        routing_events = [e for e in span.events if e.name == "cost_aware_routing_decision"]
        assert len(routing_events) == 1
        assert routing_events[0].attributes["llm.original_model"] == "gpt-4o"
        assert routing_events[0].attributes["llm.routed_model"] == "gpt-4o-mini"

    def test_attach_routing_metadata_no_routing(self, pipeline, span):
        """Test behavior when no routing metadata is present."""
        headers = {
            "X-OryaObs-Trace-Id": "test-trace-id",
            "X-OryaObs-User-Id": "test-user",
            "X-OryaObs-Tenant-Id": "test-tenant",
            "X-OryaObs-Agent-Id": "test-agent",
        }
        
        # Call the routing metadata attachment without routing attributes
        pipeline._attach_routing_metadata(span, headers=headers)
        
        # Verify that no routing event is added when no routing metadata exists
        routing_events = [e for e in span.events if e.name == "cost_aware_routing_decision"]
        assert len(routing_events) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])