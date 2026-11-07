"""Prompt template versioning for system prompts (PP53).
 
This module provides functionality to extract, hash, and store system prompts
from LLM requests, enabling A/B testing and traceability of prompt versions.
"""
 
from __future__ import annotations
 
import hashlib
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
 
from agent_obs.storage.hot import HotStore
from agent_obs.observability import Span
 
if TYPE_CHECKING:
    from agent_obs.proxy import ObsPipeline
 
logger = logging.getLogger(__name__)
 
# Environment variable for content sampling rate
AGENT_OBS_CONTENT_RATE = "AGENT_OBS_CONTENT_RATE"
DEFAULT_CONTENT_RATE = 10  # percent


def extract_first_system_prompt(request_body: bytes) -> str | None:
    """Extract the first system prompt from a request body.
    
    Args:
        request_body: Raw request body bytes
        
    Returns:
        System prompt text or None if no system prompt found
    """
    try:
        import json
        req = json.loads(request_body.decode("utf-8", errors="replace"))
        messages = req.get("messages", [])
        
        for message in messages:
            if isinstance(message, dict) and message.get("role") == "system":
                content = message.get("content")
                if isinstance(content, str):
                    return content
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    
    return None


def extract_version_label(prompt_text: str) -> str | None:
    """Extract version label from system prompt.
    
    Looks for VERSION: x.y.z pattern at the beginning of the prompt.
    
    Args:
        prompt_text: System prompt text
        
    Returns:
        Version label (e.g., "1.2.3") or None if not found
    """
    # Look for VERSION: x.y.z pattern at the beginning of the prompt
    match = re.match(r'^\s*VERSION:\s*(\d+\.\d+\.\d+)', prompt_text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Look for # Version x.y.y or similar comments
    match = re.match(r'^\s*#?\s*Version\s+(\d+\.\d+\.\d+)', prompt_text, re.IGNORECASE)
    if match:
        return match.group(1)
    
    return None


def compute_prompt_sha256(prompt_text: str) -> str:
    """Compute SHA256 hash of a system prompt.
    
    Args:
        prompt_text: System prompt text
        
    Returns:
        SHA256 hash string
    """
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


class PromptTemplateManager:
    """Manages system prompt template versioning and storage.
    
    Handles extraction, hashing, and storage of system prompts with version tracking.
    """
    
    def __init__(self, hot_store: HotStore, content_rate: int | None = None):
        self.hot_store = hot_store
        self.content_rate = content_rate or int(
            DEFAULT_CONTENT_RATE if not hasattr(self, '_content_rate') else self._content_rate
        )
        
    async def store_prompt_template(
        self,
        trace_id: str,
        prompt_text: str,
        prompt_sha256: str,
        version_label: str | None = None,
    ) -> bool:
        """Store a prompt template in ClickHouse if trace is sampled.
        
        Args:
            trace_id: Trace ID for sampling decision
            prompt_text: System prompt text
            prompt_sha256: SHA256 hash of the prompt
            version_label: Optional version label extracted from prompt
            
        Returns:
            True if stored, False if not sampled or storage failed
        """
        # Check if trace is sampled for content storage
        from agent_obs.content_sampling import should_keep_content
        if not should_keep_content(trace_id, self.content_rate):
            return False
            
        try:
            # Prepare record for insertion
            now = datetime.now(UTC)
            # Insert into ClickHouse using positional parameters
            await self.hot_store.execute(
                "INSERT INTO prompt_templates (sha256, version_label, first_seen, last_seen, sample_text) "
                "VALUES (%s, %s, %s, %s, %s)",
                [prompt_sha256, version_label or "", now, now, prompt_text[:1000]]
            )
            
            logger.debug("Stored prompt template for trace %s", trace_id)
            return True
            
        except Exception as e:
            logger.warning("Failed to store prompt template for trace %s: %s", trace_id, e)
            return False
    
    async def get_prompt_templates(
        self,
        sha256: str | None = None,
        version_label: str | None = None,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get prompt templates from ClickHouse.
        
        Args:
            sha256: Filter by specific SHA256 hash
            version_label: Filter by version label
            limit: Maximum number of records to return
            
        Returns:
            List of prompt template records
        """
        where_clauses = []
        params = []
        
        if sha256:
            where_clauses.append("sha256 = %s")
            params.append(sha256)
            
        if version_label:
            where_clauses.append("version_label = %s")
            params.append(version_label)
            
        where_clause = " AND ".join(where_clauses) if where_clauses else "1=1"
        
        query = f"""
        SELECT sha256, version_label, first_seen, last_seen, sample_text
        FROM prompt_templates
        WHERE {where_clause}
        ORDER BY first_seen DESC
        LIMIT {limit}
        """
        
        try:
            return await self.hot_store.execute(query, params)
        except Exception as e:
            logger.warning("Failed to get prompt templates: %s", e)
            return []
    
    async def get_traces_with_prompt(
        self,
        prompt_sha256: str,
        limit: int = 100
    ) -> list[dict[str, Any]]:
        """Get traces that used a specific prompt template.
        
        Args:
            prompt_sha256: SHA256 hash of the prompt template
            limit: Maximum number of traces to return
            
        Returns:
            List of trace records that used this prompt
        """
        query = f"""
        SELECT DISTINCT trace_id, span_id, agent_id, tenant_id, start_time
        FROM spans_hot
        WHERE attributes['llm.system_prompt_sha256'] = %s
        ORDER BY start_time DESC
        LIMIT {limit}
        """
        
        try:
            return await self.hot_store.execute(query, [prompt_sha256])
        except Exception as e:
            logger.warning("Failed to get traces with prompt: %s", e)
            return []


class PromptTemplateEnricher:
    """Enrich spans with system prompt versioning information."""
    
    def __init__(self, prompt_manager: PromptTemplateManager):
        self.prompt_manager = prompt_manager
    
    async def enrich_span_with_prompt(
        self,
        span: Span,
        request_body: bytes,
        trace_id: str,
    ) -> Span:
        """Enrich a span with system prompt information.
        
        Args:
            span: Span to enrich
            request_body: Raw request body bytes
            trace_id: Trace ID for sampling decision
            
        Returns:
            Enriched span
        """
        # Extract system prompt
        prompt_text = extract_first_system_prompt(request_body)
        if not prompt_text:
            return span
            
        # Compute SHA256 hash
        prompt_sha256 = compute_prompt_sha256(prompt_text)
        
        # Extract version label
        version_label = extract_version_label(prompt_text)
        
        # Store in span attributes
        span.attributes["llm.system_prompt_sha256"] = prompt_sha256
        if version_label:
            span.attributes["llm.system_prompt_version"] = version_label
        
        # Store in ClickHouse if sampled
        await self.prompt_manager.store_prompt_template(
            trace_id=trace_id,
            prompt_text=prompt_text,
            prompt_sha256=prompt_sha256,
            version_label=version_label,
        )
        
        return span


def create_prompt_enricher(hot_store: HotStore) -> PromptTemplateEnricher:
    """Create a prompt template enricher with dependencies.
    
    Args:
        hot_store: HotStore instance for ClickHouse operations
        
    Returns:
        Configured PromptTemplateEnricher instance
    """
    content_rate = int(os.environ.get(AGENT_OBS_CONTENT_RATE, str(DEFAULT_CONTENT_RATE)))
    prompt_manager = PromptTemplateManager(hot_store, content_rate)
    return PromptTemplateEnricher(prompt_manager)


# Import os at module level for environment variable access
import os