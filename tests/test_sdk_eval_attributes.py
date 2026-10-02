"""Tests for the eval attribute integration in the SDK hot path (PC09/PC12).

Covers:

* rule-based evaluator → ``span.attributes["eval.rule_based"]`` on enqueue;
* LLM judge → ``eval.pending`` / ``eval.llm_judge.status`` on the root span;
* opt-in switches (constructor instances + ``AGENT_OBS_EVAL_*_ENABLED`` env);
* fail-open behaviour when an evaluator raises;
* the YAML rules loader for ``configs/eval_rules.yaml``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent_obs.eval.base import EvalResult
from agent_obs.eval.rule_based import (
    Rule,
    RuleBasedEvaluator,
    load_eval_rules,
    load_rule_based_evaluator,
)
from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sdk(**kwargs) -> ObservabilitySDK:
    return ObservabilitySDK(exporters=[], **kwargs)


def _make_llm_span(**attributes) -> Span:
    attrs = {"llm.model": "gpt-4o", **attributes}
    return Span(
        name="llm.call:gpt-4o",
        span_type=SpanType.LLM_CALL,
        context=SpanContext.new(agent_id="test-agent"),
        attributes=attrs,
    )


def _rule_result(passed: int = 3, failed: int = 1, flags=None) -> EvalResult:
    return EvalResult(
        trace_id="t",
        eval_id="rule_1",
        eval_name="rule_based",
        eval_version="1.0.0",
        eval_timestamp=0.0,
        eval_latency_seconds=0.001,
        scores={"rules_passed": passed, "rules_failed": failed},
        flags=["pii_leak"] if flags is None else list(flags),
    )


def _judge_result(eval_name: str) -> EvalResult:
    return EvalResult(
        trace_id="t",
        eval_id="llm_judge_1",
        eval_name=eval_name,
        eval_version="1.0.0",
        eval_timestamp=0.0,
        eval_latency_seconds=0.0,
        scores={},
        flags=["pending"] if eval_name == "llm_judge_pending" else ["sampling_skipped"],
    )


@pytest.fixture(autouse=True)
def _clean_eval_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT_OBS_EVAL_RULES_ENABLED", raising=False)
    monkeypatch.delenv("AGENT_OBS_LLM_JUDGE_ENABLED", raising=False)


# ---------------------------------------------------------------------------
# Rule-based eval → eval.rule_based attribute
# ---------------------------------------------------------------------------


class TestRuleBasedAttributes:
    async def test_default_no_eval_attributes(self):
        sdk = _build_sdk()
        span = _make_llm_span()
        await sdk._enqueue(span)
        assert "eval.rule_based" not in span.attributes

    async def test_supplied_evaluator_sets_eval_rule_based(self):
        evaluator = MagicMock()
        evaluator.evaluate.return_value = _rule_result(
            passed=4, failed=0, flags=[]
        )
        sdk = _build_sdk(rule_based_evaluator=evaluator)

        span = _make_llm_span()
        await sdk._enqueue(span)

        evaluator.evaluate.assert_called_once_with(span, [])
        assert span.attributes["eval.rule_based"] == {
            "rules_passed": 4,
            "rules_failed": 0,
            "flags": [],
        }

    async def test_eval_rule_based_shape_matches_otlp_mapping(self):
        """The dict must carry the keys enrich_eval_attributes reads."""
        from agent_obs.exporters.otlp_mapping import enrich_eval_attributes

        evaluator = MagicMock()
        evaluator.evaluate.return_value = _rule_result()
        sdk = _build_sdk(rule_based_evaluator=evaluator)

        span = _make_llm_span()
        await sdk._enqueue(span)

        enriched = enrich_eval_attributes(
            [{"key": "llm.model", "value": {"stringValue": "gpt-4o"}}], span
        )
        keys = {a["key"] for a in enriched}
        assert "eval.rule_based_passed" in keys
        assert "eval.rule_based_failed" in keys
        assert "eval.rule_based_flags" in keys

    async def test_evaluator_error_is_fail_open(self):
        evaluator = MagicMock()
        evaluator.evaluate.side_effect = RuntimeError("eval boom")
        sdk = _build_sdk(rule_based_evaluator=evaluator)

        span = _make_llm_span()
        await sdk._enqueue(span)

        assert "eval.rule_based" not in span.attributes
        # The span must still have been exported (fire-and-forget preserved).
        assert any(s is span for s in sdk.last_spans)

    async def test_env_disables_supplied_evaluator(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_EVAL_RULES_ENABLED", "false")
        evaluator = MagicMock()
        sdk = _build_sdk(rule_based_evaluator=evaluator)
        assert sdk._rule_based_evaluator is None

    async def test_config_disables_supplied_evaluator(self):
        evaluator = MagicMock()
        sdk = _build_sdk(
            rule_based_evaluator=evaluator, config={"eval_rules_enabled": False}
        )
        assert sdk._rule_based_evaluator is None

    def test_env_opt_in_loads_yaml_rules(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_EVAL_RULES_ENABLED", "true")
        sdk = _build_sdk()
        assert sdk._rule_based_evaluator is not None
        assert isinstance(sdk._rule_based_evaluator, RuleBasedEvaluator)
        assert len(sdk._rule_based_evaluator.rules) == 4

    def test_no_auto_load_by_default(self):
        sdk = _build_sdk()
        assert sdk._rule_based_evaluator is None


# ---------------------------------------------------------------------------
# LLM judge → eval.pending attribute
# ---------------------------------------------------------------------------


class TestLLMJudgePendingAttributes:
    async def test_pending_result_sets_eval_pending(self):
        judge = MagicMock()
        judge.evaluate.return_value = _judge_result("llm_judge_pending")
        sdk = _build_sdk(llm_judge=judge)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            return "ok"

        result = await run()
        assert result == "ok"

        root = sdk.last_spans[-1]
        assert root.span_type == SpanType.AGENT_LOOP
        assert root.attributes["eval.pending"] is True
        assert root.attributes["eval.llm_judge.status"] == "pending"

    async def test_skipped_result_sets_status_only(self):
        judge = MagicMock()
        judge.evaluate.return_value = _judge_result("llm_judge_skipped")
        sdk = _build_sdk(llm_judge=judge)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            return "ok"

        await run()

        root = sdk.last_spans[-1]
        assert "eval.pending" not in root.attributes
        assert root.attributes["eval.llm_judge.status"] == "skipped"

    async def test_full_trace_passed_to_judge_includes_root_and_children(self):
        judge = MagicMock()
        judge.evaluate.return_value = _judge_result("llm_judge_pending")
        sdk = _build_sdk(llm_judge=judge)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            # Child must share the root's trace_id, as sdk.llm_call does.
            child_ctx = SpanContext.new(agent_id="test-agent", parent=_obs_ctx)
            child = Span(
                name="llm.call:gpt-4o",
                span_type=SpanType.LLM_CALL,
                context=child_ctx,
                attributes={"llm.model": "gpt-4o"},
            )
            await sdk._enqueue(child)
            return child

        child = await run()
        root = sdk.last_spans[-1]

        judge.evaluate.assert_called_once()
        called_span, called_trace = judge.evaluate.call_args[0]
        assert called_span is root
        assert child in called_trace
        assert root in called_trace

    async def test_judge_error_is_fail_open(self):
        judge = MagicMock()
        judge.evaluate.side_effect = RuntimeError("redis down")
        sdk = _build_sdk(llm_judge=judge)

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            return "ok"

        result = await run()
        assert result == "ok"

        root = sdk.last_spans[-1]
        assert "eval.pending" not in root.attributes
        assert root.attributes["status"] == "ok"

    async def test_no_judge_no_eval_attributes(self):
        sdk = _build_sdk()

        @sdk.agent_observed("test-agent")
        async def run(_obs_ctx=None):
            return "ok"

        await run()
        root = sdk.last_spans[-1]
        assert "eval.pending" not in root.attributes
        assert "eval.llm_judge.status" not in root.attributes

    def test_env_disables_supplied_judge(self, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_LLM_JUDGE_ENABLED", "false")
        judge = MagicMock()
        sdk = _build_sdk(llm_judge=judge)
        assert sdk._llm_judge is None


# ---------------------------------------------------------------------------
# YAML rules loader
# ---------------------------------------------------------------------------


class TestEvalRulesLoader:
    def test_load_repo_config_returns_four_rules(self):
        rules = load_eval_rules()
        assert len(rules) == 4
        assert [r.name for r in rules] == [
            "no_pii_in_output",
            "json_output_valid",
            "no_blacklisted_words",
            "max_response_chars",
        ]
        assert all(isinstance(r, Rule) for r in rules)

    def test_load_missing_file_returns_empty(self, tmp_path):
        missing = tmp_path / "nope.yaml"
        assert load_eval_rules(str(missing)) == []

    def test_load_malformed_file_returns_empty(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text("rules: not-a-list", encoding="utf-8")
        assert load_eval_rules(str(bad)) == []

    def test_loader_skips_entries_missing_keys(self, tmp_path):
        partial = tmp_path / "partial.yaml"
        partial.write_text(
            "rules:\n"
            "  - name: ok_rule\n"
            "    field: llm.output_text\n"
            "    rule_type: max_length\n"
            "    params: {max_chars: 10}\n"
            "  - name: broken\n",
            encoding="utf-8",
        )
        rules = load_eval_rules(str(partial))
        assert [r.name for r in rules] == ["ok_rule"]

    def test_load_rule_based_evaluator_builds_evaluator(self):
        evaluator = load_rule_based_evaluator()
        assert evaluator is not None
        assert len(evaluator.rules) == 4

    def test_load_rule_based_evaluator_none_on_missing(self, tmp_path):
        assert load_rule_based_evaluator(str(tmp_path / "nope.yaml")) is None

    def test_loaded_rules_evaluate_a_span(self):
        """End-to-end: yaml rules run against a span with PII in output."""
        evaluator = load_rule_based_evaluator()
        span = _make_llm_span()
        span.attributes["llm.output_text"] = "contact me at ivan@example.com"

        result = evaluator.evaluate(span, [])

        assert result.scores["rules_failed"] >= 1
        assert "pii_leak" in result.flags
