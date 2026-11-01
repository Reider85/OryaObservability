#!/usr/bin/env python3
"""
Demonstration script for shared proxy mode (PP44).

This script demonstrates the shared proxy functionality with mock OryaSecurity service.
It shows how security scanning and observability enrichment work together in Mode B.
"""

import asyncio
import json
import logging
import time
from typing import Dict, Any

from prometheus_client import Counter
from agent_obs.observability import ObservabilitySDK, Span, SpanContext
from agent_obs.proxy import ProxyConfig
from agent_obs.proxy.security_client import SecurityClient, SecurityScanResult
from agent_obs.proxy.shared_pipeline import SharedPipeline
from agent_obs.proxy.decision_router import DecisionRouter
from agent_obs.proxy.audit_trail import SharedAuditTrail
from agent_obs.proxy.server import LLMProxyServer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MockOryaSecurityService:
    """Mock OryaSecurity service for demonstration."""

    def __init__(self):
        self.scan_count = 0
        self.block_count = 0
        self.flag_count = 0
        self.clean_count = 0

    async def scan(self, request_body: str, headers: Dict[str, str], **kwargs) -> Dict[str, Any]:
        """Mock security scan implementation."""
        self.scan_count += 1
        
        # Parse request body
        try:
            data = json.loads(request_body)
            messages = data.get("messages", [])
            text = " ".join([msg.get("content", "") for msg in messages])
        except:
            text = request_body

        # Mock security analysis
        if "credit card" in text.lower() or "password" in text.lower():
            # Block payment card information
            verdict = "block"
            score = 0.95
            redacted_fields = ["payment_info"]
            logger.info(f"BLOCKED: Payment card detected in request {self.scan_count}")
            self.block_count += 1
            
        elif "attack" in text.lower() or "hack" in text.lower():
            # Flag suspicious content
            verdict = "flag"
            score = 0.7
            redacted_fields = []
            logger.info(f"FLAGGED: Suspicious content in request {self.scan_count}")
            self.flag_count += 1
            
        else:
            # Clean request
            verdict = "clean"
            score = 0.1
            redacted_fields = []
            logger.info(f"CLEAN: Request {self.scan_count} passed security check")
            self.clean_count += 1

        return {
            "verdict": verdict,
            "score": score,
            "redacted_fields": redacted_fields,
            "masked_request_body": request_body,  # In real implementation, this would be masked
            "security_metadata": {
                "threat_detected": verdict != "clean",
                "scan_id": f"scan_{self.scan_count}",
                "analysis_timestamp": time.time(),
            }
        }


async def demonstrate_security_client():
    """Demonstrate SecurityClient functionality."""
    logger.info("=== Demonstrating SecurityClient ===")
    
    # Create mock security service
    mock_service = MockOryaSecurityService()
    
    # Create security client
    security_client = SecurityClient(
        base_url="http://mock-orya-security:8000",
        timeout=0.05,
        fail_open=True,
    )
    
    # Mock HTTP client
    security_client.client.post = AsyncMock()
    mock_response = MagicMock()
    mock_response.json = AsyncMock()
    mock_response.json.side_effect = mock_service.scan
    mock_response.raise_for_status = MagicMock()
    security_client.client.post.return_value = mock_response

    # Test different scenarios
    test_cases = [
        ("Clean request", {"messages": [{"content": "Hello, how are you?"}]}),
        ("Flagged request", {"messages": [{"content": "I need to hack the system"}]}),
        ("Blocked request", {"messages": [{"content": "My credit card is 4111111111111111"}]}),
    ]

    for case_name, request_body in test_cases:
        logger.info(f"\nTesting: {case_name}")
        
        try:
            result = await security_client.scan(
                request_body=json.dumps(request_body).encode(),
                headers={"Content-Type": "application/json"},
                trace_id=f"trace-{case_name.lower().replace(' ', '-')}",
            )
            
            logger.info(f"  Verdict: {result.verdict}")
            logger.info(f"  Score: {result.score:.2f}")
            logger.info(f"  Redacted fields: {result.redacted_fields}")
            logger.info(f"  Audit ID: {result.audit_event_id}")
            
        except Exception as e:
            logger.error(f"  Error: {e}")


async def demonstrate_shared_pipeline():
    """Demonstrate SharedPipeline functionality."""
    logger.info("\n=== Demonstrating SharedPipeline ===")
    
    # Create mock components
    mock_service = MockOryaSecurityService()
    mock_sdk = MagicMock()
    mock_sdk._create_span = MagicMock()
    
    # Create mock obs pipeline
    mock_obs_pipeline = MagicMock()
    mock_span = MagicMock(spec=Span)
    mock_span.attributes = {}
    mock_obs_pipeline.enrich_span = AsyncMock(return_value=mock_span)
    
    # Create security client
    security_client = SecurityClient("http://mock-orya-security:8000")
    security_client.client.post = AsyncMock()
    security_client.client.post.return_value = MagicMock()
    security_client.client.post.return_value.json.side_effect = mock_service.scan
    security_client.client.post.return_value.raise_for_status.return_value = None

    # Create shared pipeline
    shared_pipeline = SharedPipeline(
        security_client=security_client,
        obs_pipeline=mock_obs_pipeline,
        sdk=mock_sdk,
    )

    # Test different scenarios
    test_cases = [
        ("Clean request", {"messages": [{"content": "Hello, tell me about AI"}]}),
        ("Flagged request", {"messages": [{"content": "I need to attack the server"}]}),
        ("Blocked request", {"messages": [{"content": "My password is secret123"}]}),
    ]

    for case_name, request_body in test_cases:
        logger.info(f"\nTesting: {case_name}")
        
        try:
            result = await shared_pipeline.process(
                request_body=json.dumps(request_body).encode(),
                headers={"Content-Type": "application/json"},
                trace_id=f"trace-{case_name.lower().replace(' ', '-')}",
                request_id=f"req-{case_name.lower().replace(' ', '-')}",
            )
            
            logger.info(f"  Decision: {result.decision}")
            logger.info(f"  Security verdict: {result.security_result.verdict}")
            logger.info(f"  Security score: {result.security_result.score:.2f}")
            logger.info(f"  Processing time: {result.latency_seconds:.3f}s")
            
        except Exception as e:
            logger.error(f"  Error: {e}")


async def demonstrate_decision_router():
    """Demonstrate DecisionRouter functionality."""
    logger.info("\n=== Demonstrating DecisionRouter ===")
    
    # Create audit trail
    audit_trail = SharedAuditTrail()
    
    # Create decision router
    decision_router = DecisionRouter(audit_logger=audit_trail)
    
    # Test scenarios
    test_scenarios = [
        ("clean", 0.1, False),
        ("flag", 0.7, False),
        ("block", 0.9, False),
        ("clean", 0.1, True),  # With policy block
    ]
    
    for verdict, score, has_policy in test_scenarios:
        logger.info(f"\nTesting: {verdict} verdict, policy={has_policy}")
        
        # Create mock security result
        security_result = SecurityScanResult(
            verdict=verdict,
            score=score,
            redacted_fields=[],
            masked_request_body=b'{"messages": []}',
            audit_event_id=f"audit-{verdict}",
            security_metadata={"threat_detected": verdict != "clean"}
        )
        
        # Create mock span
        mock_span = MagicMock(spec=Span)
        mock_span.attributes = {}
        
        # Mock policy engine for one scenario
        if has_policy:
            decision_router.policy_engine = MagicMock()
            decision_router.policy_engine.evaluate = AsyncMock(return_value=MagicMock(
                allowed=False,
                rule="free_tier_cant_use_gpt4o",
                message="Free tier cannot use GPT-4o"
            ))
        else:
            decision_router.policy_engine = None
        
        try:
            decision = await decision_router.decide(
                security_result=security_result,
                obs_span=mock_span,
                request_id=f"req-{verdict}",
                trace_id=f"trace-{verdict}",
            )
            
            logger.info(f"  Action: {decision.action}")
            logger.info(f"  Status code: {decision.status_code}")
            logger.info(f"  Reason: {decision.reason}")
            logger.info(f"  Policy rule: {decision.policy_rule}")
            
        except Exception as e:
            logger.error(f"  Error: {e}")


async def demonstrate_audit_trail():
    """Demonstrate SharedAuditTrail functionality."""
    logger.info("\n=== Demonstrating SharedAuditTrail ===")
    
    # Create audit trail
    audit_trail = SharedAuditTrail()
    
    # Write multiple events
    events = [
        ("clean", 0.1, "req-1", "trace-1"),
        ("flag", 0.7, "req-2", "trace-1"),
        ("block", 0.9, "req-3", "trace-2"),
        ("clean", 0.2, "req-4", "trace-1"),
    ]
    
    for verdict, score, request_id, trace_id in events:
        await audit_trail.write(
            request_id=request_id,
            trace_id=trace_id,
            security_audit_id=f"sec-{request_id}",
            obs_span_id=f"span-{request_id}",
            decision="forward" if verdict == "clean" else ("flag" if verdict == "flag" else "block"),
            timestamp=time.time(),
            security_verdict=verdict,
            security_score=score,
            redacted_fields_count=0 if verdict == "clean" else 1,
        )
        time.sleep(0.1)  # Add small delay for different timestamps
    
    # Query by trace_id
    trace_events = await audit_trail.get_by_trace("trace-1")
    logger.info(f"\nEvents for trace-1: {len(trace_events)}")
    for event in trace_events:
        logger.info(f"  {event.timestamp}: {event.decision} (score: {event.security_score:.2f})")
    
    # Get security summary
    summary = await audit_trail.get_security_summary()
    logger.info(f"\nSecurity summary:")
    logger.info(f"  Total events: {summary['total_events']}")
    logger.info(f"  Clean: {summary['clean_count']}")
    logger.info(f"  Flagged: {summary['flag_count']}")
    logger.info(f"  Blocked: {summary['block_count']}")
    logger.info(f"  Block rate: {summary['block_rate']:.2f}")


async def demonstrate_proxy_server():
    """Demonstrate LLMProxyServer with shared mode."""
    logger.info("\n=== Demonstrating LLMProxyServer (Shared Mode) ===")
    
    # Create shared proxy configuration
    config = ProxyConfig(
        mode="on",
        enabled=True,
        shared_proxy_enabled=True,
        shared_proxy_security_url="http://mock-orya-security:8000",
        shared_proxy_security_timeout=0.05,
        shared_proxy_security_fail_open=True,
    )
    
    # Create mock SDK
    mock_sdk = MagicMock(spec=ObservabilitySDK)
    mock_sdk._audit_trail = MagicMock()
    mock_sdk._create_span = MagicMock()
    
    # Create proxy server
    with patch('agent_obs.proxy.server.httpx.AsyncClient'):
        proxy_server = LLMProxyServer(config=config, sdk=mock_sdk)
        
        logger.info(f"Proxy server initialized: {proxy_server.shared_enabled}")
        logger.info(f"Security client: {proxy_server.security_client is not None}")
        logger.info(f"Shared pipeline: {proxy_server.shared_pipeline is not None}")
        logger.info(f"Decision router: {proxy_server.decision_router is not None}")
        
        # Test server stop
        await proxy_server.stop()
        logger.info("Proxy server stopped")


async def run_performance_test():
    """Run performance test for shared proxy."""
    logger.info("\n=== Running Performance Test ===")
    
    # Create mock components
    mock_service = MockOryaSecurityService()
    mock_sdk = MagicMock()
    mock_sdk._create_span = MagicMock()
    
    mock_obs_pipeline = MagicMock()
    mock_span = MagicMock(spec=Span)
    mock_span.attributes = {}
    mock_obs_pipeline.enrich_span = AsyncMock(return_value=mock_span)
    
    # Create security client
    security_client = SecurityClient("http://mock-orya-security:8000")
    security_client.client.post = AsyncMock()
    security_client.client.post.return_value = MagicMock()
    security_client.client.post.return_value.json.side_effect = mock_service.scan
    security_client.client.post.return_value.raise_for_status.return_value = None

    # Create shared pipeline
    shared_pipeline = SharedPipeline(
        security_client=security_client,
        obs_pipeline=mock_obs_pipeline,
        sdk=mock_sdk,
    )

    # Test performance
    num_requests = 100
    test_start = time.time()
    
    for i in range(num_requests):
        request_body = json.dumps({"messages": [{"content": f"Test request {i}"}]}).encode()
        start = time.time()
        
        try:
            await shared_pipeline.process(
                request_body=request_body,
                headers={"Content-Type": "application/json"},
                trace_id=f"trace-{i}",
                request_id=f"req-{i}",
            )
            
            latency = time.time() - start
            if i % 10 == 0:
                logger.info(f"Request {i}: {latency:.3f}s")
                
        except Exception as e:
            logger.error(f"Request {i} failed: {e}")
    
    total_time = time.time() - test_start
    avg_latency = total_time / num_requests
    
    logger.info(f"\nPerformance results:")
    logger.info(f"  Total requests: {num_requests}")
    logger.info(f"  Total time: {total_time:.2f}s")
    logger.info(f"  Average latency: {avg_latency:.3f}s")
    logger.info(f"  Requests per second: {num_requests / total_time:.2f}")


async def main():
    """Main demonstration function."""
    logger.info("Starting Shared Proxy Mode Demonstration (PP44)")
    logger.info("=" * 60)
    
    try:
        # Run demonstrations
        await demonstrate_security_client()
        await demonstrate_shared_pipeline()
        await demonstrate_decision_router()
        await demonstrate_audit_trail()
        await demonstrate_proxy_server()
        await run_performance_test()
        
        logger.info("\n" + "=" * 60)
        logger.info("Demonstration completed successfully!")
        
    except Exception as e:
        logger.error(f"Demonstration failed: {e}")
        raise


# Helper classes for mocking
class AsyncMock:
    def __init__(self, return_value=None):
        self.return_value = return_value
        self.side_effect = None
        
    def __call__(self, *args, **kwargs):
        if self.side_effect:
            if asyncio.iscoroutinefunction(self.side_effect):
                return self.side_effect(*args, **kwargs)
            else:
                return self.side_effect(*args, **kwargs)
        else:
            if asyncio.iscoroutinefunction(self.return_value):
                return self.return_value
            else:
                return self.return_value
                
    def __await__(self):
        return self().__await__()


class MagicMock:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)
            
    def __call__(self, *args, **kwargs):
        return MagicMock()
        
    def __await__(self):
        return iter([])


if __name__ == "__main__":
    asyncio.run(main())