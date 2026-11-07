"""Cost-aware model routing for LLM proxy.

This module provides cost-aware model selection logic that can downgrade
expensive models to cheaper alternatives for simple requests.
"""

import json
from dataclasses import dataclass
from typing import Any

from agent_obs.cost.compute_cost import Usage, compute_cost
from agent_obs.cost.price_book import PriceBook, PriceNotFoundError
from agent_obs.proxy import ProxyConfig


@dataclass
class RoutingDecision:
    """Result of cost-aware routing decision."""
    should_downgrade: bool
    new_model: str
    original_model: str
    routing_reason: str
    cost_savings_usd: float = 0.0


class CostAwareRouter:
    """Cost-aware model selection with business logic constraints."""
    
    def __init__(self, config: ProxyConfig, price_book: PriceBook):
        """Initialize cost-aware router.
        
        Args:
            config: Proxy configuration with routing settings
            price_book: Price book for cost estimation
        """
        self.config = config
        self.price_book = price_book
        self.model_policies = self._load_model_policies()
    
    async def select_model(
        self,
        request_body: bytes,
        tenant_id: str,
        agent_id: str,
        user_id: str,
        current_model: str,
    ) -> RoutingDecision:
        """Select optimal model based on cost and business rules.
        
        Args:
            request_body: Request body as bytes
            tenant_id: Tenant identifier for business rules
            agent_id: Agent identifier for business rules
            user_id: User identifier for business rules
            current_model: Currently requested model
            
        Returns:
            RoutingDecision with selection details and cost savings
        """
        # Parse request to extract tokens
        estimated_tokens = self._estimate_tokens(request_body)
        
        # Get available models with pricing
        alternatives = self._get_alternatives(current_model)
        
        if not alternatives:
            return RoutingDecision(
                should_downgrade=False,
                new_model=current_model,
                original_model=current_model,
                routing_reason="no_alternatives",
                cost_savings_usd=0.0,
            )
        
        # Check if request qualifies for downgrade
        if not self._is_simple_request(request_body, current_model):
            return RoutingDecision(
                should_downgrade=False,
                new_model=current_model,
                original_model=current_model,
                routing_reason="complex_request",
                cost_savings_usd=0.0,
            )
        
        # Score models by cost-effectiveness
        scored = await self._score_models(
            alternatives, 
            estimated_tokens, 
            tenant_id, 
            agent_id
        )
        
        # Get best candidate
        best_candidate = scored[0]
        best_model_name = best_candidate["model"]
        
        # Compute cost savings
        original_cost = self._compute_model_cost(current_model, estimated_tokens)
        new_cost = self._compute_model_cost(best_model_name, estimated_tokens)
        cost_savings = original_cost - new_cost
        
        # Apply business rules and constraints
        business_score = await self._apply_business_rules(
            best_model_name, 
            tenant_id, 
            agent_id
        )
        
        # Check if business rules allow the downgrade
        if business_score == 0.0:
            return RoutingDecision(
                should_downgrade=False,
                new_model=current_model,
                original_model=current_model,
                routing_reason="business_rules_blocked",
                cost_savings_usd=0.0,
            )
        
        # Apply cost savings threshold
        threshold = getattr(self.config, 'cost_aware_routing_threshold', 0.8)
        
        # Don't downgrade if alternative has no valid pricing
        if new_cost == float('inf'):
            return RoutingDecision(
                should_downgrade=False,
                new_model=current_model,
                original_model=current_model,
                routing_reason="no_valid_pricing",
                cost_savings_usd=0.0,
            )
        
        # Don't downgrade if original has no valid pricing
        if original_cost == float('inf'):
            return RoutingDecision(
                should_downgrade=False,
                new_model=current_model,
                original_model=current_model,
                routing_reason="no_valid_pricing",
                cost_savings_usd=0.0,
            )
        
        cost_savings_ratio = cost_savings / original_cost if original_cost > 0 else 0
        
        if cost_savings_ratio < threshold:
            return RoutingDecision(
                should_downgrade=False,
                new_model=current_model,
                original_model=current_model,
                routing_reason="cost_threshold_not_met",
                cost_savings_usd=0.0,
            )
        
        return RoutingDecision(
            should_downgrade=True,
            new_model=best_model_name,
            original_model=current_model,
            routing_reason="cost_threshold_met",
            cost_savings_usd=cost_savings,
        )
    
    def should_downgrade(
        self, 
        request_body: bytes, 
        current_model: str
    ) -> tuple[bool, str]:
        """Check if request qualifies for model downgrade.
        
        This is a synchronous version for use where async is not available.
        
        Args:
            request_body: Request body as bytes
            current_model: Currently requested model
            
        Returns:
            Tuple of (should_downgrade, new_model)
        """
        # Parse request to detect simple patterns
        if not self._is_simple_request(request_body, current_model):
            return False, current_model
        
        # Get available alternatives
        alternatives = self._get_alternatives(current_model)
        if not alternatives:
            return False, current_model
        
        # Check if alternatives are significantly cheaper
        original_cost = self._estimate_cost_sync(request_body, current_model)
        alternative_cost = self._estimate_cost_sync(request_body, alternatives[0])
        
        threshold = getattr(self.config, 'cost_aware_routing_threshold', 0.8)
        return alternative_cost < original_cost * threshold, alternatives[0]
    
    def _is_simple_request(self, request_body: bytes, current_model: str) -> bool:
        """Check if request is simple enough for downgrade."""
        try:
            parsed = json.loads(request_body.decode("utf-8"))
        except json.JSONDecodeError:
            return False
        
        # Business rules for simplicity
        if current_model != "gpt-4o":
            return False
        
        messages = parsed.get("messages", [])
        if len(messages) > 3:
            return False
        
        if "tools" in parsed:
            return False
        
        system_prompt = self._extract_system_prompt(messages)
        return not (system_prompt and self._has_reasoning_instructions(system_prompt))
    
    def _get_alternatives(self, model: str) -> list[str]:
        """Get alternative models for downgrade."""
        default_alternatives = {
            "gpt-4o": ["gpt-4o-mini", "gpt-3.5-turbo"],
            "claude-3-opus": ["claude-3-sonnet", "claude-3-haiku"],
            "claude-3-5-sonnet": ["claude-3-5-haiku"],
        }
        
        # Check custom alternatives from config
        custom_alternatives = getattr(self.config, 'downgrade_targets', None)
        if custom_alternatives:
            model_alternatives = custom_alternatives.get(model, default_alternatives.get(model, []))
        else:
            model_alternatives = default_alternatives.get(model, [])
        
        # Filter out current model
        return [m for m in model_alternatives if m != model]
    
    async def _score_models(
        self, 
        alternatives: list[str], 
        estimated_tokens: int, 
        tenant_id: str, 
        agent_id: str
    ) -> list[dict[str, Any]]:
        """Score models by cost-effectiveness with business rules."""
        scored = []
        
        for model in alternatives:
            try:
                # Base cost calculation
                cost = self._compute_model_cost(model, estimated_tokens)
                
                # Business rule adjustments
                business_score = await self._apply_business_rules(model, tenant_id, agent_id)
                
                # Performance adjustments (if available)
                perf_score = await self._get_performance_score(model)
                
                # Combined score: cost-weighted (60%) + business (30%) + performance (10%)
                total_score = (
                    cost * 0.6 + 
                    business_score * 0.3 + 
                    perf_score * 0.1
                )
                
                scored.append({
                    "model": model,
                    "cost": cost,
                    "score": total_score,
                    "metadata": {
                        "business_rules_applied": business_score,
                        "performance_score": perf_score,
                        "original_cost": cost,
                    }
                })
            except PriceNotFoundError:
                # Skip models with invalid pricing
                continue
        
        # Sort by score (lower is better for cost)
        return sorted(scored, key=lambda x: x["score"])
    
    async def _apply_business_rules(
        self, 
        model: str, 
        tenant_id: str, 
        agent_id: str
    ) -> float:
        """Apply business rules and constraints to model selection.
        
        Returns a score from 0.0 to 1.0 where 1.0 means preferred.
        """
        score = 1.0
        
        # Tenant-specific rules
        tenant_rules = getattr(self.config, 'business_rules', None)
        if tenant_rules:
            for rule_type, rule_config in tenant_rules.items():
                if rule_type == "tenant_blacklist":
                    if tenant_id in rule_config.get("models", []):
                        score = 0.0
                        break
                elif rule_type == "tenant_whitelist" and tenant_id not in rule_config.get("models", []):
                        score = 0.0
                        break
        
        # Agent-specific rules
        agent_rules = getattr(self.config, 'agent_rules', None)
        if agent_rules:
            for rule_type, rule_config in agent_rules.items():
                if rule_type == "agent_blacklist" and agent_id in rule_config.get("models", []):
                    score = 0.0
                    break
        
        return score
    
    async def _get_performance_score(self, model: str) -> float:
        """Get performance score for model (0.0 to 1.0).
        
        Higher is better. Uses cached performance data if available.
        """
        # Default: all models have equal performance
        return 0.5
    
    def _compute_model_cost(self, model: str, estimated_tokens: int) -> float:
        """Compute cost for a model given token count."""
        try:
            # Create dummy usage
            usage = Usage(
                input=estimated_tokens,
                output=estimated_tokens // 2,  # Assume 50% output
                cached=0
            )
            result = compute_cost(usage, model, self.price_book)
            return result.cost_usd
        except PriceNotFoundError:
            return float('inf')
    
    def _estimate_cost_sync(self, request_body: bytes, model: str) -> float:
        """Synchronous cost estimation (for simple checks)."""
        try:
            parsed = json.loads(request_body.decode("utf-8"))
            estimated_tokens = self._estimate_tokens(parsed)
            return self._compute_model_cost(model, estimated_tokens)
        except (json.JSONDecodeError, PriceNotFoundError):
            return 0.0
    
    def _estimate_tokens(self, request: Any) -> int:
        """Estimate token count from request.
        
        Args:
            request: Request can be dict, bytes, or parsed object
            
        Returns:
            Estimated token count
        """
        if isinstance(request, bytes):
            parsed = json.loads(request.decode("utf-8"))
        else:
            parsed = request
        
        messages = parsed.get("messages", [])
        
        # Simple token estimation: characters / 4
        total_chars = sum(
            len(str(m.get("content", ""))) 
            for m in messages 
            if m.get("role") == "user"
        )
        
        return max(1, total_chars // 4)  # Ensure at least 1 token
    
    def _extract_system_prompt(self, messages: list[dict]) -> str:
        """Extract system prompt from messages list."""
        for message in messages:
            if message.get("role") == "system":
                return message.get("content", "")
        return ""
    
    def _has_reasoning_instructions(self, system_prompt: str) -> bool:
        """Check if system prompt contains reasoning instructions."""
        reasoning_keywords = [
            "reasoning",
            "think step by step",
            "chain of thought",
            "deliberate",
            "analyze carefully",
        ]
        system_prompt_lower = system_prompt.lower()
        return any(keyword in system_prompt_lower for keyword in reasoning_keywords)
    
    def _load_model_policies(self) -> dict[str, Any]:
        """Load model-specific routing policies."""
        return {}  # Placeholder for future model-specific policies
