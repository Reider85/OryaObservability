"""Mapping of agent_obs span attributes to Langfuse UI labels and OTLP fields.

Langfuse can render spans natively if attributes follow the OTel semantic
conventions for generative AI (``gen_ai.usage.*``).  This module centralises
that mapping so both the exporter and the UI documentation share one source
of truth (P23).
"""

from __future__ import annotations

from typing import Any

from agent_obs.observability import Span, SpanType

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

# Per-span-type OTel GenAI attributes that let Langfuse classify the
# observation and render it natively (tree/type, usage, cost).
# Keys are the source span attribute; ``None`` means a constant value.
SEMCONV_TYPE_MAP: dict[SpanType, dict[str, str | None]] = {
    SpanType.AGENT_LOOP: {"openinference.span.kind": None},
    SpanType.LLM_CALL: {"gen_ai.operation.name": None},
    SpanType.TOOL_CALL: {"gen_ai.tool.name": "tool.name"},
}

# Constant values for convention attributes whose source is ``None``.
SEMCONV_CONSTANTS: dict[str, str] = {
    "openinference.span.kind": "AGENT",
}

# Extra value-aliasing so Langfuse picks up model/cost from our attributes.
ALIAS_CONVENTION_MAP: dict[str, str] = {
    "llm.model": "gen_ai.request.model",
    "cost.usd": "gen_ai.usage.cost",
}

_LLM_GEN_AI_OPERATION = "chat"

# Attribute carrying the span event names for the UI metadata panel.
# Langfuse v3's legacy OTLP path does not persist OTLP span events natively,
# so events are mirrored under this attribute (P23).
EVENTS_ATTRIBUTE = "agent_obs.events"


def _otlp_value(value: Any) -> dict[str, Any]:
    """Convert a Python value to an OTLP KeyValue value."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": value}
    if isinstance(value, float):
        return {"doubleValue": value}
    return {"stringValue": str(value)}


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


def enrich_semconv_attributes(
    attributes: list[dict[str, Any]], span: Span
) -> list[dict[str, Any]]:
    """Add OTel GenAI semantic-convention attributes for Langfuse v3.

    Langfuse v3 classifies observations from ``gen_ai.operation.name`` /
    ``gen_ai.tool.name`` / ``openinference.span.kind`` and only renders native
    usage/cost/model columns for generation observations.  This enrichment
    aliases our span attributes so LLM calls become native ``GENERATION``
    observations, tool calls ``TOOL``, and the agent loop ``AGENT`` (P23).

    Called after :func:`enrich_otlp_attributes`; keys already present are
    never duplicated.
    """
    existing = {a["key"] for a in attributes}
    enriched = list(attributes)

    def _add(key: str, value: Any) -> None:
        if key not in existing:
            enriched.append({"key": key, "value": _otlp_value(value)})
            existing.add(key)

    span_attrs = span.attributes or {}
    for convention, source in SEMCONV_TYPE_MAP.get(span.span_type, {}).items():
        if source is not None:
            value = span_attrs.get(source)
            if value is None:
                continue
        elif convention in SEMCONV_CONSTANTS:
            value = SEMCONV_CONSTANTS[convention]
        else:
            value = _LLM_GEN_AI_OPERATION
        _add(convention, value)

    if span.span_type == SpanType.LLM_CALL:
        for source, convention in ALIAS_CONVENTION_MAP.items():
            value = span_attrs.get(source)
            if value is not None:
                _add(convention, value)

    return enriched