"""Tests for otlp_mapping (P23): span attributes -> Langfuse UI labels / OTel usage fields."""

from agent_obs.exporters.otlp_mapping import (
    LANGFUSE_LABELS,
    USAGE_CONVENTION_MAP,
    enrich_otlp_attributes,
    enrich_semconv_attributes,
    span_to_langfuse_labels,
)
from agent_obs.observability import Span, SpanContext, SpanType, _now


def _make_span(
    span_type: SpanType = SpanType.LLM_CALL,
    attributes: dict | None = None,
    agent_id: str = "test-agent",
    agent_version: str = "1.0.0",
    user_id: str = "",
    session_id: str = "",
) -> Span:
    ctx = SpanContext.new(
        agent_id=agent_id,
        agent_version=agent_version,
        user_id=user_id,
        session_id=session_id,
    )
    return Span(
        name=f"llm.call:gpt-4o",
        span_type=span_type,
        context=ctx,
        start_time=_now(),
        end_time=_now(),
        attributes=attributes or {},
    )


class TestSpanToLangfuseLabels:
    def test_agent_id_from_context(self) -> None:
        span = _make_span(agent_id="my-agent")
        labels = span_to_langfuse_labels(span)
        assert labels["agent_id"] == "my-agent"

    def test_agent_version_from_context(self) -> None:
        span = _make_span(agent_version="2.3.4")
        labels = span_to_langfuse_labels(span)
        assert labels["agent.version"] == "2.3.4"

    def test_session_id_from_context(self) -> None:
        span = _make_span(session_id="sess-abc")
        labels = span_to_langfuse_labels(span)
        assert labels["session_id"] == "sess-abc"

    def test_user_id_from_context(self) -> None:
        span = _make_span(user_id="user-42")
        labels = span_to_langfuse_labels(span)
        assert labels["user_id"] == "user-42"

    def test_model_from_span_attributes(self) -> None:
        span = _make_span(attributes={"llm.model": "gpt-4o"})
        labels = span_to_langfuse_labels(span)
        assert labels["model"] == "gpt-4o"

    def test_provider_from_span_attributes(self) -> None:
        span = _make_span(attributes={"llm.provider": "openai"})
        labels = span_to_langfuse_labels(span)
        assert labels["provider"] == "openai"

    def test_status_from_span_attributes(self) -> None:
        span = _make_span(attributes={"status": "error"})
        labels = span_to_langfuse_labels(span)
        assert labels["status"] == "error"

    def test_cost_from_span_attributes(self) -> None:
        span = _make_span(attributes={"cost.usd": 0.0123})
        labels = span_to_langfuse_labels(span)
        assert labels["cost.usd"] == "0.0123"

    def test_empty_context_omits_labels(self) -> None:
        ctx = SpanContext.new(agent_id="", agent_version="", user_id="", session_id="")
        span = Span(
            name="llm.call:x",
            span_type=SpanType.LLM_CALL,
            context=ctx,
            start_time=_now(),
            end_time=_now(),
            attributes={},
        )
        labels = span_to_langfuse_labels(span)
        # Context labels absent when empty
        assert "agent_id" not in labels
        assert "agent.version" not in labels
        assert "session_id" not in labels
        assert "user_id" not in labels

    def test_none_attribute_is_omitted(self) -> None:
        span = _make_span(attributes={"llm.model": None})
        labels = span_to_langfuse_labels(span)
        assert "model" not in labels

    def test_all_mapped_attributes_present(self) -> None:
        span = _make_span(
            attributes={
                "llm.model": "gpt-4o",
                "llm.provider": "openai",
                "status": "ok",
                "cost.usd": 0.05,
            }
        )
        labels = span_to_langfuse_labels(span)
        expected = {"agent_id", "agent.version", "model", "provider", "status", "cost.usd"}
        assert expected.issubset(labels.keys())


class TestEnrichOtlpAttributes:
    @staticmethod
    def _kv(key: str, value) -> dict:
        return {"key": key, "value": {"stringValue": str(value)}}

    @staticmethod
    def _int_kv(key: str, value: int) -> dict:
        return {"key": key, "value": {"intValue": value}}

    def test_input_tokens_enriched(self) -> None:
        attrs = [self._int_kv("tokens.input", 100)]
        result = enrich_otlp_attributes(attrs)
        keys = {a["key"] for a in result}
        assert "gen_ai.usage.input_tokens" in keys
        assert "tokens.input" in keys

    def test_output_tokens_enriched(self) -> None:
        attrs = [self._int_kv("tokens.output", 50)]
        result = enrich_otlp_attributes(attrs)
        keys = {a["key"] for a in result}
        assert "gen_ai.usage.output_tokens" in keys

    def test_cached_tokens_enriched(self) -> None:
        attrs = [self._int_kv("tokens.cached", 25)]
        result = enrich_otlp_attributes(attrs)
        keys = {a["key"] for a in result}
        assert "gen_ai.usage.cached_input_tokens" in keys

    def test_all_three_usage_fields_enriched(self) -> None:
        attrs = [
            self._int_kv("tokens.input", 100),
            self._int_kv("tokens.output", 50),
            self._int_kv("tokens.cached", 10),
        ]
        result = enrich_otlp_attributes(attrs)
        keys = {a["key"] for a in result}
        assert "gen_ai.usage.input_tokens" in keys
        assert "gen_ai.usage.output_tokens" in keys
        assert "gen_ai.usage.cached_input_tokens" in keys

    def test_original_attributes_preserved(self) -> None:
        attrs = [self._int_kv("tokens.input", 100)]
        result = enrich_otlp_attributes(attrs)
        keys = {a["key"] for a in result}
        assert "tokens.input" in keys

    def test_no_usage_attrs_returns_unchanged(self) -> None:
        attrs = [self._kv("llm.model", "gpt-4o")]
        result = enrich_otlp_attributes(attrs)
        assert result == attrs

    def test_empty_input_returns_empty(self) -> None:
        result = enrich_otlp_attributes([])
        assert result == []

    def test_already_enriched_not_duplicated(self) -> None:
        attrs = [
            self._int_kv("tokens.input", 100),
            {"key": "gen_ai.usage.input_tokens", "value": {"intValue": 100}},
        ]
        result = enrich_otlp_attributes(attrs)
        gen_ai_keys = [a for a in result if a["key"] == "gen_ai.usage.input_tokens"]
        assert len(gen_ai_keys) == 1

    def test_value_copy_preserved(self) -> None:
        attrs = [self._int_kv("tokens.input", 42)]
        result = enrich_otlp_attributes(attrs)
        gen_ai = next(a for a in result if a["key"] == "gen_ai.usage.input_tokens")
        assert gen_ai["value"]["intValue"] == 42

    def test_mixed_usage_and_non_usage(self) -> None:
        attrs = [
            self._kv("llm.model", "gpt-4o"),
            self._int_kv("tokens.input", 100),
            self._kv("status", "ok"),
        ]
        result = enrich_otlp_attributes(attrs)
        keys = {a["key"] for a in result}
        assert "llm.model" in keys
        assert "status" in keys
        assert "gen_ai.usage.input_tokens" in keys


class TestEnrichSemconvAttributes:
    @staticmethod
    def _kv(key: str, value) -> dict:
        return {"key": key, "value": {"stringValue": str(value)}}

    def _enrich(
        self,
        span_type: SpanType,
        attributes: dict,
        existing: list[dict] | None = None,
    ) -> list[dict]:
        span = _make_span(span_type=span_type, attributes=attributes)
        return enrich_semconv_attributes(existing or [], span)

    def test_llm_call_becomes_generation_operation(self) -> None:
        attrs = self._enrich(SpanType.LLM_CALL, {"llm.provider": "openai"})
        op = next(
            a for a in attrs if a["key"] == "gen_ai.operation.name"
        )
        assert op["value"] == {"stringValue": "chat"}

    def test_llm_model_aliased_to_gen_ai_request_model(self) -> None:
        attrs = self._enrich(SpanType.LLM_CALL, {"llm.model": "gpt-4o"})
        model = next(a for a in attrs if a["key"] == "gen_ai.request.model")
        assert model["value"] == {"stringValue": "gpt-4o"}

    def test_cost_aliased_to_gen_ai_usage_cost(self) -> None:
        attrs = self._enrich(SpanType.LLM_CALL, {"cost.usd": 0.0123})
        cost = next(a for a in attrs if a["key"] == "gen_ai.usage.cost")
        assert cost["value"] == {"doubleValue": 0.0123}

    def test_tool_call_maps_to_gen_ai_tool_name(self) -> None:
        attrs = self._enrich(SpanType.TOOL_CALL, {"tool.name": "search"})
        tool = next(a for a in attrs if a["key"] == "gen_ai.tool.name")
        assert tool["value"] == {"stringValue": "search"}

    def test_agent_loop_maps_to_openinference_kind(self) -> None:
        attrs = self._enrich(SpanType.AGENT_LOOP, {})
        kind = next(a for a in attrs if a["key"] == "openinference.span.kind")
        assert kind["value"] == {"stringValue": "AGENT"}

    def test_existing_semconv_attributes_not_duplicated(self) -> None:
        existing = [
            {"key": "gen_ai.operation.name", "value": {"stringValue": "chat"}},
            {"key": "gen_ai.request.model", "value": {"stringValue": "gpt-4o"}},
        ]
        attrs = self._enrich(
            SpanType.LLM_CALL, {"llm.model": "gpt-4o"}, existing
        )
        gen_ai_op = [a for a in attrs if a["key"] == "gen_ai.operation.name"]
        assert len(gen_ai_op) == 1
        gen_ai_model = [a for a in attrs if a["key"] == "gen_ai.request.model"]
        assert len(gen_ai_model) == 1

    def test_original_attributes_preserved(self) -> None:
        attrs = self._enrich(
            SpanType.TOOL_CALL, {"tool.name": "search"}, [self._kv("x", "y")]
        )
        keys = {a["key"] for a in attrs}
        assert "x" in keys
        assert "gen_ai.tool.name" in keys

    def test_missing_alias_source_skips_convention(self) -> None:
        attrs = self._enrich(SpanType.LLM_CALL, {"llm.provider": "openai"})
        keys = {a["key"] for a in attrs}
        assert "gen_ai.request.model" not in keys
        assert "gen_ai.usage.cost" not in keys


class TestMappingConstants:
    def test_langfuse_labels_cover_context_fields(self) -> None:
        assert "agent_id" in LANGFUSE_LABELS
        assert "agent.version" in LANGFUSE_LABELS
        assert "session_id" in LANGFUSE_LABELS
        assert "user_id" in LANGFUSE_LABELS

    def test_langfuse_labels_cover_span_fields(self) -> None:
        assert "llm.model" in LANGFUSE_LABELS
        assert "llm.provider" in LANGFUSE_LABELS
        assert "status" in LANGFUSE_LABELS
        assert "cost.usd" in LANGFUSE_LABELS

    def test_usage_convention_map_targets_gen_ai(self) -> None:
        for convention in USAGE_CONVENTION_MAP.values():
            assert convention.startswith("gen_ai.usage.")

    def test_usage_convention_map_covers_all_token_attrs(self) -> None:
        assert "tokens.input" in USAGE_CONVENTION_MAP
        assert "tokens.output" in USAGE_CONVENTION_MAP
        assert "tokens.cached" in USAGE_CONVENTION_MAP
