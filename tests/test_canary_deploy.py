"""Tests for PP29 (T3.6.5) — SLO-gated canary deployments.

Validates the full loop:

- ``scripts/canary_check.py`` exit-code contract (healthy -> 0, burn rate over
  threshold -> 1, undeterminable -> 2, ``--fail-open`` -> 0)
- per-version SLI counts: ``HotStore.get_sli_stats(version=...)`` scopes
  ``spans_hot`` by the ``agent_version`` span attribute
- ``ObservabilitySDK._enqueue`` stamps ``agent_version`` (env wins over the
  span context)
- ``CanaryRollbackAuditEvent`` shape + package export
- ``agent_obs.notify`` Slack/Telegram delivery (best-effort, never raises)
- ``scripts/canary.py`` ramp/rollback executor, including audit + notification
  when the revert command fails
- ``.github/workflows/canary.yml`` shape (deploy -> observe -> gate ->
  ramp/rollback)
- an end-to-end simulation of both flows: healthy -> ramp, unhealthy ->
  rollback + audit + notification
"""

from __future__ import annotations

import importlib.util
import json
import logging
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import yaml

from agent_obs.guardrail import CanaryRollbackAuditEvent
from agent_obs.notify import format_rollback_message, notify, send_slack, send_telegram
from agent_obs.observability import ObservabilitySDK, Span, SpanContext, SpanType
from agent_obs.slo import SLO_CATALOG
from agent_obs.storage.hot import HotStore

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "canary.yml"
CANARY_DASHBOARD = REPO_ROOT / "infra" / "grafana" / "dashboards" / "slo.json"

#: SLI *kinds* (HotStore.get_sli_stats argument) — distinct from catalog names.
SLI_KINDS = ("availability", "latency", "quality", "cost")
#: SLO *names* (catalog keys) — what the gate's thresholds are keyed by.
SLO_NAMES = tuple(SLO_CATALOG)


def _load_script(module_name: str, relpath: str):
    """Load a standalone script the way tests/test_slo_error_budget.py does."""
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _load_script("canary_check_pp29", "scripts/canary_check.py")


@pytest.fixture(scope="module")
def cli():
    return _load_script("canary_cli_pp29", "scripts/canary.py")


class FakeStore:
    """HotStore stand-in: canned (good, total) per (version, sli_kind)."""

    def __init__(self, data: dict[tuple[str, str], tuple[int, int]], fail: bool = False):
        self.data = data
        self.fail = fail
        self.calls: list[dict] = []
        self.audit_events: list = []
        self.closed = False

    def get_sli_stats(self, sli_kind, window, good_threshold=None, version=None):
        self.calls.append(
            {"sli_kind": sli_kind, "window": window, "good_threshold": good_threshold,
             "version": version}
        )
        if self.fail:
            raise RuntimeError("clickhouse down")
        return self.data.get((version, sli_kind), (0, 0))

    def write_audit_event(self, event) -> None:
        self.audit_events.append(event)

    def close(self) -> None:
        self.closed = True


def _healthy(version: str, total: int = 1000) -> dict:
    """Every SLO perfect for this version (SLI 1.0 -> burn rate 0.0)."""
    return {(version, kind): (total, total) for kind in SLI_KINDS}


def _bad_availability(version: str, good: int = 900, total: int = 1000) -> dict:
    data = _healthy(version)
    data[(version, "availability")] = (good, total)
    return data


def _patch_store(monkeypatch, store) -> None:
    monkeypatch.setattr(
        "agent_obs.storage.maintenance.build_hot_store", lambda: store
    )


# ---------------------------------------------------------------------------
# Gate exit codes — the contract CI relies on
# ---------------------------------------------------------------------------

class TestGateExitCodes:
    def test_healthy_returns_zero(self, gate, capsys):
        thresholds = gate.default_thresholds(["availability", "latency_p95"])
        result = gate.CanaryResult(
            new_rates={"availability": 0.4, "latency_p95": 1.0},
            old_rates={"availability": 0.1},
        )
        assert gate.evaluate_gate(result, thresholds) == 0
        assert "healthy" in capsys.readouterr().out

    def test_breach_returns_one(self, gate, capsys):
        thresholds = gate.default_thresholds(["availability"])
        result = gate.CanaryResult(new_rates={"availability": 20.0})
        assert gate.evaluate_gate(result, thresholds) == 1
        err = capsys.readouterr().err
        assert "UNHEALTHY" in err and "availability" in err

    def test_exact_threshold_is_not_a_breach(self, gate):
        # PP26 semantics: strict ">", burning exactly at the threshold is ok.
        thresholds = {"availability": 14.4}
        result = gate.CanaryResult(new_rates={"availability": 14.4})
        assert gate.evaluate_gate(result, thresholds) == 0

    def test_error_fails_closed(self, gate, capsys):
        result = gate.CanaryResult(error="burn-rate query failed: down")
        assert gate.evaluate_gate(result, {"availability": 14.4}) == 2
        assert "burn-rate query failed" in capsys.readouterr().err

    def test_error_fail_open_allows(self, gate):
        result = gate.CanaryResult(error="burn-rate query failed: down")
        assert gate.evaluate_gate(result, {"availability": 14.4}, fail_open=True) == 0

    def test_empty_rates_fail_closed(self, gate):
        assert gate.evaluate_gate(gate.CanaryResult(new_rates={}), {}) == 2
        assert (
            gate.evaluate_gate(gate.CanaryResult(new_rates={}), {}, fail_open=True) == 0
        )

    def test_skipped_slos_do_not_block(self, gate, capsys):
        result = gate.CanaryResult(
            new_rates={"availability": 0.0}, skipped={"quality": "no_data"}
        )
        thresholds = gate.default_thresholds(list(SLO_NAMES))
        assert gate.evaluate_gate(result, thresholds) == 0
        assert "skipped: quality" in capsys.readouterr().out

    def test_default_thresholds_come_from_catalog(self, gate):
        thresholds = gate.default_thresholds(list(SLO_NAMES))
        assert thresholds["availability"] == 14.4
        assert thresholds["latency_p95"] == 14.4
        assert thresholds["quality"] == 6.0
        assert thresholds["cost_p99"] == 6.0


# ---------------------------------------------------------------------------
# fetch_burn_rates — per-version counts from storage
# ---------------------------------------------------------------------------

class TestFetchBurnRates:
    def test_rates_computed_per_version(self, gate):
        data = _bad_availability("1.1.0")
        data.update(_healthy("1.0.0"))
        store = FakeStore(data)
        result = gate.fetch_burn_rates(store, "1.1.0", "1.0.0", "30m", ["availability"])

        assert result.error is None
        # SLI 0.9 against target 0.999 -> (1-0.9)/(1-0.999) = 100
        assert result.new_rates["availability"] == pytest.approx(100.0)
        assert result.old_rates["availability"] == 0.0
        assert result.new_totals["availability"] == 1000

    def test_window_and_version_reach_the_store(self, gate):
        data = _healthy("1.1.0")
        store = FakeStore(data)
        gate.fetch_burn_rates(store, "1.1.0", "1.0.0", "45m", ["availability"])

        assert all(call["window"] == timedelta(minutes=45) for call in store.calls)
        assert {call["version"] for call in store.calls} == {"1.1.0", "1.0.0"}
        assert all(
            call["sli_kind"] == "availability" for call in store.calls
        )

    def test_no_telemetry_on_new_version_is_an_error(self, gate):
        store = FakeStore(_healthy("1.0.0"))
        result = gate.fetch_burn_rates(store, "1.1.0", "1.0.0", "30m", ["availability"])
        assert result.error is not None
        assert "no telemetry" in result.error
        assert "1.1.0" in result.error

    def test_store_failure_is_an_error(self, gate):
        store = FakeStore({}, fail=True)
        result = gate.fetch_burn_rates(store, "1.1.0", "1.0.0")
        assert result.error is not None
        assert "burn-rate query failed" in result.error

    def test_slo_without_evals_is_skipped_not_failed(self, gate):
        data = _healthy("1.1.0")
        del data[("1.1.0", "quality")]
        store = FakeStore(data)
        result = gate.fetch_burn_rates(store, "1.1.0", "1.0.0", "30m")
        assert result.error is None
        assert result.skipped == {"quality": "no_data"}
        assert "quality" not in result.new_rates


# ---------------------------------------------------------------------------
# canary_check.main — CLI wiring
# ---------------------------------------------------------------------------

class TestGateMain:
    @staticmethod
    def _run(gate, monkeypatch, capsys, data, argv_extra=None, env=None):
        store = FakeStore(data)
        _patch_store(monkeypatch, store)
        monkeypatch.delenv("AGENT_OBS_CANARY_FAIL_OPEN", raising=False)
        monkeypatch.delenv("AGENT_OBS_CANARY_BURN_THRESHOLD", raising=False)
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        argv = ["--new-version", "1.1.0", "--old-version", "1.0.0"] + (argv_extra or [])
        rc = gate.main(argv)
        return rc, store, capsys.readouterr()

    def test_healthy_canary_exits_zero(self, gate, monkeypatch, capsys):
        data = _healthy("1.1.0")
        data.update(_healthy("1.0.0"))
        rc, store, out = self._run(gate, monkeypatch, capsys, data)
        assert rc == 0
        assert "healthy" in out.out
        assert store.closed  # the store is always released

    def test_burning_canary_exits_one(self, gate, monkeypatch, capsys):
        data = _bad_availability("1.1.0")
        rc, _store, out = self._run(gate, monkeypatch, capsys, data)
        assert rc == 1
        assert "UNHEALTHY" in out.err

    def test_no_telemetry_exits_two(self, gate, monkeypatch, capsys):
        rc, _store, out = self._run(gate, monkeypatch, capsys, {})
        assert rc == 2
        assert "no telemetry" in out.err

    def test_fail_open_flag_allows_no_telemetry(self, gate, monkeypatch, capsys):
        rc, _store, _out = self._run(
            gate, monkeypatch, capsys, {}, argv_extra=["--fail-open"]
        )
        assert rc == 0

    def test_fail_open_env_allows_no_telemetry(self, gate, monkeypatch, capsys):
        rc, _store, _out = self._run(
            gate, monkeypatch, capsys, {}, env={"AGENT_OBS_CANARY_FAIL_OPEN": "1"}
        )
        assert rc == 0

    def test_threshold_flag_overrides_catalog(self, gate, monkeypatch, capsys):
        # burn rate 100 > 5 -> rolled back; with --threshold 1000 -> healthy.
        data = _bad_availability("1.1.0")
        rc, _s, _o = self._run(gate, monkeypatch, capsys, data,
                               argv_extra=["--threshold", "5"])
        assert rc == 1
        rc, _s, _o = self._run(gate, monkeypatch, capsys, data,
                               argv_extra=["--threshold", "1000"])
        assert rc == 0

    def test_env_threshold_overrides_catalog(self, gate, monkeypatch, capsys):
        data = _bad_availability("1.1.0")
        rc, _s, _o = self._run(
            gate, monkeypatch, capsys, data,
            env={"AGENT_OBS_CANARY_BURN_THRESHOLD": "1000"},
        )
        assert rc == 0

    def test_invalid_env_threshold_exits_two(self, gate, monkeypatch, capsys):
        rc, _s, out = self._run(
            gate, monkeypatch, capsys, _healthy("1.1.0"),
            env={"AGENT_OBS_CANARY_BURN_THRESHOLD": "not-a-number"},
        )
        assert rc == 2
        assert "AGENT_OBS_CANARY_BURN_THRESHOLD" in out.err

    def test_slo_subset_is_honoured(self, gate, monkeypatch, capsys):
        # Only availability is evaluated: the burning cost SLO is out of scope.
        data = _healthy("1.1.0")
        data[("1.1.0", "cost")] = (0, 1000)
        rc, store, _out = self._run(
            gate, monkeypatch, capsys, data, argv_extra=["--slos", "availability"]
        )
        assert rc == 0
        assert {c["sli_kind"] for c in store.calls} == {"availability"}

    def test_unknown_slo_is_rejected(self, gate, monkeypatch, capsys):
        _patch_store(monkeypatch, FakeStore({}))
        with pytest.raises(SystemExit):
            gate.main(
                ["--new-version", "1", "--old-version", "0", "--slos", "nope"]
            )

    def test_json_output(self, gate, monkeypatch, capsys):
        data = _bad_availability("1.1.0")
        rc, _store, out = self._run(gate, monkeypatch, capsys, data,
                                    argv_extra=["--json"])
        assert rc == 1
        payload = json.loads(out.out.split("canary gate:")[0])
        assert payload["new_version"] == "1.1.0"
        assert payload["old_version"] == "1.0.0"
        assert payload["window"] == "30m"
        assert payload["new_rates"]["availability"] == pytest.approx(100.0)
        assert payload["thresholds"]["availability"] == 14.4


# ---------------------------------------------------------------------------
# HotStore.get_sli_stats — the version filter (per-version pipeline)
# ---------------------------------------------------------------------------

class TestSliStatsVersionFilter:
    @staticmethod
    def _store(fake_execute):
        store = HotStore.__new__(HotStore)
        store._execute_clickhouse = fake_execute  # type: ignore[method-assign]
        return store

    @staticmethod
    def _capturing(return_value):
        captured: dict = {}

        def fake_execute(query, params=None):
            captured["query"] = query
            captured["params"] = params
            return return_value

        return captured, fake_execute

    def test_availability_scoped_by_version(self):
        captured, fake = self._capturing([(1000, 900)])
        store = self._store(fake)
        good, total = store.get_sli_stats(
            "availability", timedelta(minutes=30), version="1.1.0"
        )
        assert (good, total) == (900, 1000)
        assert "JSONExtractString(attributes, 'agent_version')" in captured["query"]
        assert captured["params"]["version"] == "1.1.0"

    def test_without_version_query_is_unchanged(self):
        captured, fake = self._capturing([(1000, 999)])
        store = self._store(fake)
        store.get_sli_stats("availability", timedelta(days=30))
        assert "agent_version" not in captured["query"]
        assert "version" not in captured["params"]
        assert "cutoff" in captured["params"]

    def test_latency_scoped_by_version(self):
        captured, fake = self._capturing([(100, 95)])
        store = self._store(fake)
        store.get_sli_stats(
            "latency", timedelta(minutes=30), good_threshold=3.0, version="1.1.0"
        )
        assert "JSONExtractString(attributes, 'agent_version')" in captured["query"]
        assert captured["params"]["version"] == "1.1.0"
        assert captured["params"]["threshold"] == 3.0

    def test_cost_scoped_by_version_inside_trace_grouping(self):
        captured, fake = self._capturing([(50, 50)])
        store = self._store(fake)
        store.get_sli_stats(
            "cost", timedelta(minutes=30), good_threshold=0.05, version="1.1.0"
        )
        assert "JSONExtractString(attributes, 'agent_version')" in captured["query"]
        assert "GROUP BY trace_id" in captured["query"]
        assert captured["params"]["version"] == "1.1.0"

    def test_quality_scoped_by_version_through_trace_ids(self):
        captured, fake = self._capturing([(100, 90)])
        store = self._store(fake)
        store.get_sli_stats(
            "quality", timedelta(minutes=30), good_threshold=0.7, version="1.1.0"
        )
        assert "trace_id IN" in captured["query"]
        assert "JSONExtractString(attributes, 'agent_version')" in captured["query"]
        assert captured["params"]["version"] == "1.1.0"

    def test_quality_without_version_has_no_trace_subquery(self):
        captured, fake = self._capturing([(100, 90)])
        store = self._store(fake)
        store.get_sli_stats("quality", timedelta(days=30), good_threshold=0.7)
        assert "trace_id IN" not in captured["query"]
        assert "version" not in captured["params"]


# ---------------------------------------------------------------------------
# SDK stamps the deploy version onto every span
# ---------------------------------------------------------------------------

class TestVersionStamping:
    @pytest.fixture
    def sdk(self):
        return ObservabilitySDK(exporters=[])

    @staticmethod
    def _span(agent_version: str = "") -> Span:
        return Span(
            name="loop",
            span_type=SpanType.AGENT_LOOP,
            context=SpanContext.new(agent_id="agent", agent_version=agent_version),
            start_time=1000.0,
        )

    async def test_env_wins_over_span_context(self, sdk, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_DEPLOY_VERSION", "9.9.9")
        span = self._span("1.2.3")
        await sdk._enqueue(span)
        assert span.attributes["agent_version"] == "9.9.9"

    async def test_context_version_is_the_fallback(self, sdk, monkeypatch):
        monkeypatch.delenv("AGENT_OBS_DEPLOY_VERSION", raising=False)
        span = self._span("1.2.3")
        await sdk._enqueue(span)
        assert span.attributes["agent_version"] == "1.2.3"

    async def test_no_version_no_attribute(self, sdk, monkeypatch):
        monkeypatch.delenv("AGENT_OBS_DEPLOY_VERSION", raising=False)
        span = self._span("")
        await sdk._enqueue(span)
        assert "agent_version" not in span.attributes

    async def test_explicit_attribute_is_kept(self, sdk, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_DEPLOY_VERSION", "9.9.9")
        span = self._span("1.2.3")
        span.attributes["agent_version"] = "pinned-by-app"
        await sdk._enqueue(span)
        assert span.attributes["agent_version"] == "pinned-by-app"


# ---------------------------------------------------------------------------
# CanaryRollbackAuditEvent
# ---------------------------------------------------------------------------

class TestCanaryRollbackAuditEvent:
    def _event(self, **overrides):
        kwargs = {
            "new_version": "1.1.0",
            "old_version": "1.0.0",
            "slo_name": "availability",
            "burn_rates": {"availability": 100.0, "latency_p95": 0.0},
            "threshold": 14.4,
            "window": "30m",
        }
        kwargs.update(overrides)
        return CanaryRollbackAuditEvent.for_rollback(**kwargs)

    def test_factory_shape(self):
        event = self._event()
        assert event.action == "canary_rollback"
        assert event.decision == "rolled_back"
        assert event.audit_id.startswith("cy-")
        assert event.trace_id == ""
        assert event.actor["type"] == "system"
        assert event.user_agent == "agent-obs/canary"

    def test_resource_is_self_contained(self):
        event = self._event()
        assert event.resource["type"] == "canary_deploy"
        assert event.resource["new_version"] == "1.1.0"
        assert event.resource["old_version"] == "1.0.0"
        assert event.resource["slo"] == "availability"
        assert event.resource["window"] == "30m"
        assert event.resource["threshold"] == 14.4
        assert event.resource["burn_rates"]["availability"] == 100.0
        assert event.resource["breached_slos"] == ["availability"]

    def test_reason_embeds_versions_and_threshold(self):
        event = self._event()
        assert "1.1.0" in event.reason
        assert "1.0.0" in event.reason
        assert "14.4" in event.reason
        assert "30m" in event.reason

    def test_error_is_recorded_when_rollback_failed(self):
        event = self._event(decision="rollback_failed", error="exited 3")
        assert event.decision == "rollback_failed"
        assert event.resource["error"] == "exited 3"
        assert "exited 3" in event.reason

    def test_clickhouse_row_matches_ddl(self):
        event = self._event()
        row = event.to_clickhouse_row()
        assert len(row) == 10
        audit_id, ts, trace_id, actor, action, decision, resource, reason, _ip, ua = row
        assert audit_id == event.audit_id
        assert ts.tzinfo is not None  # UTC datetime
        assert trace_id == ""
        assert json.loads(actor)["type"] == "system"
        assert action == "canary_rollback"
        assert decision == "rolled_back"
        assert json.loads(resource)["new_version"] == "1.1.0"
        assert reason == event.reason
        assert ua == "agent-obs/canary"

    def test_serializes_to_dict(self):
        payload = self._event().to_dict()
        assert payload["action"] == "canary_rollback"
        json.dumps(payload)  # must be JSON-serializable


# ---------------------------------------------------------------------------
# Notifications — best-effort push
# ---------------------------------------------------------------------------

class TestNotify:
    @staticmethod
    def _client(handler):
        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_slack_posts_text_to_webhook(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        client = self._client(handler)
        ok = send_slack(
            "rollback!", client=client, env={"SLACK_WEBHOOK_URL": "https://hooks.example/T/B"}
        )
        assert ok is True
        assert seen["url"] == "https://hooks.example/T/B"
        assert seen["body"] == {"text": "rollback!"}

    def test_slack_unconfigured_skips_without_request(self):
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
            raise AssertionError("must not be called when unconfigured")

        client = self._client(handler)
        assert send_slack("hi", client=client, env={}) is False

    def test_slack_failure_is_swallowed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("boom", request=request)

        client = self._client(handler)
        assert (
            send_slack("hi", client=client, env={"SLACK_WEBHOOK_URL": "https://x"}) is False
        )

    def test_telegram_skipped_without_both_settings(self):
        client = self._client(lambda request: httpx.Response(200))
        assert send_telegram("hi", client=client, env={}) is False
        assert (
            send_telegram(
                "hi", client=client, env={"AGENT_OBS_TELEGRAM_BOT_TOKEN": "t"}
            )
            is False
        )

    def test_telegram_posts_when_configured(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})

        client = self._client(handler)
        ok = send_telegram(
            "hello",
            client=client,
            env={
                "AGENT_OBS_TELEGRAM_BOT_TOKEN": "123:abc",
                "AGENT_OBS_TELEGRAM_CHAT_ID": "-10042",
            },
        )
        assert ok is True
        assert seen["url"].endswith("/bot123:abc/sendMessage")
        assert seen["body"] == {"chat_id": "-10042", "text": "hello"}

    def test_notify_fans_out_and_reports(self):
        posted = []

        def handler(request: httpx.Request) -> httpx.Response:
            posted.append(str(request.url))
            return httpx.Response(200, json={"ok": True})

        client = self._client(handler)
        result = notify(
            "rolled back",
            client=client,
            env={
                "SLACK_WEBHOOK_URL": "https://hooks.example/T/B",
                "AGENT_OBS_TELEGRAM_BOT_TOKEN": "123:abc",
                "AGENT_OBS_TELEGRAM_CHAT_ID": "-10042",
            },
        )
        assert result == {"slack": True, "telegram": True}
        assert len(posted) == 2

    def test_rollback_message_is_readable(self):
        message = format_rollback_message(
            new_version="1.1.0",
            old_version="1.0.0",
            burn_rates={"availability": 100.0},
            threshold=14.4,
            window="30m",
            error="exited 3",
        )
        assert "1.1.0" in message and "1.0.0" in message
        assert "availability=100.00" in message
        assert "14.4" in message and "30m" in message
        assert "exited 3" in message


# ---------------------------------------------------------------------------
# scripts/canary.py — ramp / rollback executor
# ---------------------------------------------------------------------------

class TestCanaryExecutor:
    def test_parse_burn_rates(self, cli):
        rates = cli.parse_burn_rates("availability=20.1, latency_p95=3.2")
        assert rates == {"availability": 20.1, "latency_p95": 3.2}
        assert cli.parse_burn_rates("") == {}
        with pytest.raises(ValueError):
            cli.parse_burn_rates("availability=abc")

    def test_run_command_contract(self, cli):
        assert cli.run_command(None) == (False, "no command configured")
        assert cli.run_command("") == (False, "no command configured")
        assert cli.run_command("exit 0") == (True, None)
        ok, error = cli.run_command("exit 3")
        assert ok is False and "exited 3" in error

    def test_ramp_success_and_failure(self, cli, capsys):
        assert cli.main(["ramp", "--new-version", "1", "--old-version", "0",
                         "--cmd", "exit 0"]) == 0
        assert cli.main(["ramp", "--new-version", "1", "--old-version", "0",
                         "--cmd", "exit 3"]) == 1
        assert "FAILED" in capsys.readouterr().err

    def test_ramp_without_command_fails(self, cli, monkeypatch, capsys):
        monkeypatch.delenv("CANARY_RAMP_CMD", raising=False)
        assert cli.main(["ramp", "--new-version", "1", "--old-version", "0"]) == 1
        assert "no command configured" in capsys.readouterr().err

    def test_ramp_dry_run_is_safe(self, cli, monkeypatch, capsys):
        monkeypatch.delenv("CANARY_RAMP_CMD", raising=False)
        assert (
            cli.main(["ramp", "--new-version", "1", "--old-version", "0",
                      "--dry-run"]) == 0
        )
        assert "dry-run" in capsys.readouterr().out

    def _patch_side_effects(self, cli, monkeypatch):
        events: list = []
        messages: list = []
        monkeypatch.setattr(cli, "_write_audit", lambda e: events.append(e) or True)
        monkeypatch.setattr(
            cli,
            "notify",
            lambda msg, **kw: (messages.append(msg),
                               {"slack": True, "telegram": False})[1],
        )
        return events, messages

    def test_rollback_dry_run_records_simulated(self, cli, monkeypatch, capsys):
        events, messages = self._patch_side_effects(cli, monkeypatch)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--dry-run", "--burn-rates", "availability=20.0",
        ])
        assert rc == 0
        assert len(events) == 1
        assert events[0].decision == "rollback_simulated"
        assert events[0].action == "canary_rollback"
        assert events[0].resource["burn_rates"] == {"availability": 20.0}
        assert events[0].resource["threshold"] == 14.4
        assert events[0].resource["slo"] == "availability"
        assert messages and "1.1.0" in messages[0]
        assert "dry-run" in capsys.readouterr().out

    def test_rollback_success_writes_audit_and_notifies(self, cli, monkeypatch):
        events, messages = self._patch_side_effects(cli, monkeypatch)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--cmd", "exit 0", "--burn-rates", "availability=20.0",
        ])
        assert rc == 0
        assert events[0].decision == "rolled_back"
        assert messages and "CANARY ROLLBACK" in messages[0]

    def test_failed_revert_still_audits_and_notifies(self, cli, monkeypatch, capsys):
        events, messages = self._patch_side_effects(cli, monkeypatch)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--cmd", "exit 3", "--burn-rates", "availability=20.0",
        ])
        assert rc == 1
        assert events[0].decision == "rollback_failed"
        assert "exited 3" in events[0].resource["error"]
        assert messages, "notification must fire even when the revert failed"
        assert "exited 3" in messages[0]
        assert "FAILED" in capsys.readouterr().err

    def test_missing_rollback_command_fails_loudly(self, cli, monkeypatch):
        events, messages = self._patch_side_effects(cli, monkeypatch)
        monkeypatch.delenv("CANARY_ROLLBACK_CMD", raising=False)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--burn-rates", "availability=20.0",
        ])
        assert rc == 1
        assert events[0].decision == "rollback_failed"
        assert "no command configured" in events[0].resource["error"]
        assert messages

    def test_audit_failure_fails_the_run_but_notifies(self, cli, monkeypatch):
        events, messages = self._patch_side_effects(cli, monkeypatch)
        monkeypatch.setattr(cli, "_write_audit", lambda e: events.append(e) or False)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--cmd", "exit 0", "--burn-rates", "availability=20.0",
        ])
        assert rc == 1
        assert events, "the event is still built"
        assert messages, "notification must still be attempted"

    def test_audit_failure_is_tolerated_in_dry_run(self, cli, monkeypatch):
        events, _messages = self._patch_side_effects(cli, monkeypatch)
        monkeypatch.setattr(cli, "_write_audit", lambda e: events.append(e) or False)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--dry-run", "--burn-rates", "availability=20.0",
        ])
        assert rc == 0

    def test_rollback_requeries_storage_when_no_rates_given(
        self, cli, monkeypatch
    ):
        # No --burn-rates: the executor asks ClickHouse itself (best-effort).
        store = FakeStore(_bad_availability("1.1.0"))
        _patch_store(monkeypatch, store)
        events, _messages = self._patch_side_effects(cli, monkeypatch)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--cmd", "exit 0",
        ])
        assert rc == 0
        assert store.calls, "burn rates were re-queried"
        assert events[0].resource["slo"] == "availability"
        assert events[0].resource["burn_rates"]["availability"] == pytest.approx(100.0)

    def test_failed_storage_requery_still_rolls_back(self, cli, monkeypatch):
        store = FakeStore({}, fail=True)
        _patch_store(monkeypatch, store)
        events, messages = self._patch_side_effects(cli, monkeypatch)
        rc = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--cmd", "exit 0",
        ])
        assert rc == 0
        assert events[0].decision == "rolled_back"
        assert messages

    def test_unknown_slo_does_not_break_rollback(self, cli, monkeypatch, caplog):
        # A typo'd --slos must degrade to the full catalog, not raise mid-rollback.
        events, _messages = self._patch_side_effects(cli, monkeypatch)
        with caplog.at_level(logging.WARNING, logger="canary"):
            rc = cli.main([
                "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
                "--dry-run", "--slos", "nope", "--burn-rates", "availability=20.0",
            ])
        assert rc == 0
        assert events[0].resource["slo"] == "availability"
        assert any("unknown SLO" in record.message for record in caplog.records)


# ---------------------------------------------------------------------------
# .github/workflows/canary.yml
# ---------------------------------------------------------------------------

class TestCanaryWorkflow:
    @pytest.fixture
    def workflow(self):
        return yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    @pytest.fixture
    def steps(self, workflow):
        return workflow["jobs"]["canary"]["steps"]

    def test_dispatch_inputs(self, workflow):
        inputs = workflow[True]["workflow_dispatch"]["inputs"]  # YAML parses `on` as True
        assert inputs["new_version"]["required"] is True
        assert inputs["old_version"]["required"] is True
        assert inputs["observation_minutes"]["default"] == "30"

    def test_step_order_deploy_observe_gate_ramp_rollback(self, steps):
        names = [s.get("name") for s in steps]
        deploy = names.index("Deploy to canary (5% traffic)")
        observe = names.index("Observe SLO health")
        gate = names.index("SLO gate")
        ramp = names.index("Ramp to 100%")
        rollback = names.index("Rollback, audit and notify on-call")
        assert deploy < observe < gate < ramp < rollback

    def test_gate_invokes_canary_check_with_both_versions(self, steps):
        gate_step = next(s for s in steps if s.get("name") == "SLO gate")
        run = gate_step["run"]
        assert "scripts/canary_check.py" in run
        assert "--new-version" in run and "--old-version" in run
        assert "--window" in run

    def test_observation_uses_the_input(self, steps):
        observe = next(s for s in steps if s.get("name") == "Observe SLO health")
        assert "OBSERVATION_MINUTES" in observe["run"]

    def test_ramp_only_on_success_rollback_on_failure(self, steps):
        ramp = next(s for s in steps if s.get("name") == "Ramp to 100%")
        rollback = next(
            s for s in steps if s.get("name") == "Rollback, audit and notify on-call"
        )
        assert ramp["if"] == "success()"
        assert "failure()" in rollback["if"]
        assert "steps.deployed.outcome" in rollback["if"]
        assert "scripts/canary.py rollback" in rollback["run"]

    def test_pluggable_commands_and_secrets_are_wired(self, workflow):
        env = workflow["jobs"]["canary"]["env"]
        for var in ("CANARY_DEPLOY_CMD", "CANARY_RAMP_CMD", "CANARY_ROLLBACK_CMD"):
            assert var in env
        for secret in (
            "CLICKHOUSE_HOST",
            "CLICKHOUSE_PASSWORD",
            "SLACK_WEBHOOK_URL",
            "AGENT_OBS_TELEGRAM_BOT_TOKEN",
        ):
            assert secret in env, f"{secret} must reach the rollback steps"
        assert env["NEW_VERSION"] == "${{ inputs.new_version }}"

    def test_error_budget_gate_runs_before_the_deploy(self, steps):
        names = [s.get("name") for s in steps]
        assert names.index("Error budget gate (PP27)") < names.index(
            "Deploy to canary (5% traffic)"
        )

    def test_one_canary_at_a_time(self, workflow):
        assert workflow["concurrency"]["cancel-in-progress"] is False


# ---------------------------------------------------------------------------
# Dashboard: canary rollbacks are visible next to deploy freezes
# ---------------------------------------------------------------------------

class TestDashboardAnnotations:
    @pytest.fixture
    def annotations(self):
        dashboard = json.loads(CANARY_DASHBOARD.read_text(encoding="utf-8"))
        return {a["name"]: a for a in dashboard["annotations"]["list"]}

    def test_canary_annotation_exists(self, annotations):
        assert "Canary rollbacks" in annotations

    def test_annotation_queries_audit_trail(self, annotations):
        sql = annotations["Canary rollbacks"]["target"]["rawSql"]
        assert "audit_events_hot" in sql
        assert "action = 'canary_rollback'" in sql
        assert "new_version" in sql and "old_version" in sql


# ---------------------------------------------------------------------------
# End-to-end simulation: healthy -> ramp, unhealthy -> rollback
# ---------------------------------------------------------------------------

class TestCanaryEndToEnd:
    def test_healthy_canary_is_ramped(self, gate, cli, monkeypatch, capsys):
        data = _healthy("1.1.0")
        data.update(_healthy("1.0.0"))
        store = FakeStore(data)
        _patch_store(monkeypatch, store)
        monkeypatch.delenv("AGENT_OBS_CANARY_FAIL_OPEN", raising=False)

        rc_gate = gate.main(["--new-version", "1.1.0", "--old-version", "1.0.0"])
        assert rc_gate == 0, "healthy burn rates must ramp"

        rc_ramp = cli.main([
            "ramp", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--cmd", "exit 0",
        ])
        assert rc_ramp == 0
        assert not store.audit_events, "a healthy canary never rolls back"
        capsys.readouterr()

    def test_unhealthy_canary_rolls_back_with_audit_and_notification(
        self, gate, cli, monkeypatch, capsys
    ):
        data = _bad_availability("1.1.0")
        data.update(_healthy("1.0.0"))
        store = FakeStore(data)
        _patch_store(monkeypatch, store)
        monkeypatch.delenv("AGENT_OBS_CANARY_FAIL_OPEN", raising=False)

        rc_gate = gate.main(["--new-version", "1.1.0", "--old-version", "1.0.0"])
        assert rc_gate == 1, "burn rate over threshold must block the ramp"

        messages: list = []
        monkeypatch.setattr(
            cli,
            "notify",
            lambda msg, **kw: (messages.append(msg),
                               {"slack": True, "telegram": False})[1],
        )

        rc_rollback = cli.main([
            "rollback", "--new-version", "1.1.0", "--old-version", "1.0.0",
            "--cmd", "exit 0",
        ])
        assert rc_rollback == 0

        assert len(store.audit_events) == 1
        event = store.audit_events[0]
        assert event.action == "canary_rollback"
        assert event.decision == "rolled_back"
        assert event.resource["new_version"] == "1.1.0"
        assert event.resource["old_version"] == "1.0.0"
        assert event.resource["burn_rates"]["availability"] == pytest.approx(100.0)
        assert event.resource["breached_slos"] == ["availability"]

        assert messages, "on-call must be notified"
        assert "1.1.0" in messages[0] and "1.0.0" in messages[0]
        capsys.readouterr()
