"""Mapping of agent_obs span attributes to Langfuse UI labels and OTLP fields.

Langfuse can render spans natively if attributes follow the OTel semantic
conventions for generative AI (``gen_ai.usage.*``).  This module centralises
that mapping so both the exporter and the UI documentation share one source
of truth (P23).
"""

from __future__ import annotations

from typing import Any

from agent_obs.observability import Span

# Internal attribute name -> Langfuse UI label shown to the operator.
LANGFUSE_LABELS: dict[str, str] = {
    "agent_id": "agent_id",
    "agent.version": "agent.version",
    "llm.model": "model",
    "llm.provider": "provider",
    "status": "status",
    "cost.usd": "cost.usd",
    "session_id": "session_id",
    "user_id": "user_id",
}

# Internal attribute name -> OTel gen_ai semantic-convention attribute name.
USAGE_CONVENTION_MAP: dict[str, str] = {
    "tokens.input": "gen_ai.usage.input_tokens",
    "tokens.output": "gen_ai.usage.output_tokens",
    "tokens.cached": "gen_ai.usage.cached_input_tokens",
}


def span_to_langfuse_labels(span: Span) -> dict[str, str]:
    """Extract the label subset Langfuse UI can display for a span.

    The keys are the Langfuse-facing label names; values are taken from the
    span context (agent/session/user) and span attributes (model/status/cost).
    """
    labels: dict[str, str] = {}
    ctx = span.context
    for attribute, label in LANGFUSE_LABELS.items():
        if attribute == "agent_id" and ctx.agent_id:
            labels[label] = ctx.agent_id
        elif attribute == "agent.version" and ctx.agent_version:
            labels[label] = ctx.agent_version
        elif attribute == "session_id" and ctx.session_id:
            labels[label] = ctx.session_id
        elif attribute == "user_id" and ctx.user_id:
            labels[label] = ctx.user_id
        elif attribute != "agent_id" and attribute in span.attributes:
            value = span.attributes[attribute]
            if value is not None:
                labels[label] = str(value)
    return labels


def enrich_otlp_attributes(attributes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add OTel ``gen_ai.usage.*`` attributes for Langfuse native token UI.

    Input is the KV-array of OTLP attributes already built from a span.
    Usage attributes are appended (not replaced) so Langfuse can render
    tokens without losing the original ``tokens.*`` attributes.
    """
    existing: dict[str, Any] = {}
    for attr in attributes:
        existing[attr["key"]] = attr["value"]

    enriched = list(attributes)
    added: set[str] = set()
    for internal, convention in USAGE_CONVENTION_MAP.items():
        if internal not in existing or convention in existing:
            continue
        enriched.append({"key": convention, "value": existing[internal]})
        added.add(convention)
    return enriched