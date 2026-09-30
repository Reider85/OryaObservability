"""Tests for the compliance catalog (PC33) — record every PII masking event.

Covers:

* ``parse_redacted_field`` for plain / SQL / JSON redacted-field formats;
* ``ComplianceCatalog`` in-memory aggregation, timer flush, failure handling;
* ``NullCatalogWriter`` / ``PostgresCatalogWriter`` behaviour;
* ``build_compliance_catalog`` env-config switching;
* ``GuardrailEngine.check_input`` integration (record on masking, tool_name
  plumbing via ``SpanContext``, no double-count, ``catalog=None`` no-op).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_obs.compliance.catalog import (
    ENV_COMPLIANCE_PG,
    ENV_COMPLIANCE_PG_DSN,
    ComplianceCatalog,
    ComplianceRow,
    NullCatalogWriter,
    PostgresCatalogWriter,
    build_compliance_catalog,
    compliance_catalog_buffer_size,
    compliance_catalog_flush_errors_total,
    compliance_catalog_writes_total,
    parse_redacted_field,
)
from agent_obs.guardrail.engine import GuardrailConfig, GuardrailEngine
from agent_obs.guardrail.injection_classifier import InjectionClassifier, InjectionScore
from agent_obs.guardrail.pii_detector import PIIDetector
from agent_obs.guardrail.vault_client import VaultClient
from agent_obs.observability import SpanContext

EMAIL_TEXT = "my email is ivan@example.com"
PHONE_TEXT = "call me at +79001234567 now"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class RecordingWriter:
    """In-memory CatalogWriter capturing every upsert batch."""

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[list[ComplianceRow]] = []
        self.fail = fail
        self.closed = False

    async def upsert(self, rows: list[ComplianceRow]) -> None:
        if self.fail:
            raise RuntimeError("postgres down")
        self.calls.append(list(rows))

    async def close(self) -> None:
        self.closed = True

    @property
    def all_rows(self) -> list[ComplianceRow]:
        return [r for call in self.calls for r in call]


def _metric_value(counter_or_gauge) -> float:
    return counter_or_gauge._value.get()


def _build_engine_with_catalog(catalog: ComplianceCatalog | None) -> GuardrailEngine:
    classifier = MagicMock(spec=InjectionClassifier)
    classifier.classify.return_value = InjectionScore(
        score=0.0, label="benign", confidence=0.99
    )
    return GuardrailEngine(
        pii_detector=PIIDetector(enabled_detectors={"email", "phone"}),
        injection_classifier=classifier,
        vault_client=VaultClient(),
        config=GuardrailConfig(),
        catalog=catalog,
    )


# ---------------------------------------------------------------------------
# parse_redacted_field
# ---------------------------------------------------------------------------


class TestParseRedactedField:
    def test_plain_format(self):
        assert parse_redacted_field("user_message.email.5f3a") == (
            "user_message",
            "email",
        )

    def test_plain_format_phone(self):
        assert parse_redacted_field("tool_output.phone.abc1") == (
            "tool_output",
            "phone",
        )

    def test_sql_rows_format(self):
        assert parse_redacted_field("tool.rows[0].email.abc1") == (
            "tool.rows[0]",
            "email",
        )

    def test_json_path_format(self):
        assert parse_redacted_field("tool.user.email.abc1") == (
            "tool.user",
            "email",
        )

    def test_passport_type(self):
        assert parse_redacted_field("user_message.passport.12ab") == (
            "user_message",
            "passport",
        )

    def test_unknown_shape_fallback(self):
        field, pii_type = parse_redacted_field("mystery.foo.bar")
        assert field == "mystery"
        assert pii_type == "foo"

    def test_single_segment_fallback(self):
        assert parse_redacted_field("onlyfield") == ("onlyfield", "")

    def test_empty_string(self):
        assert parse_redacted_field("") == ("", "")


# ---------------------------------------------------------------------------
# ComplianceCatalog — buffer aggregation
# ---------------------------------------------------------------------------


class TestCatalogBuffer:
    def test_five_maskings_aggregate_to_two_keys(self):
        """3 email + 2 phone for one agent+tool → 2 buffer keys, freq 3 and 2."""
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)

        for _ in range(3):
            catalog.record(
                agent_id="agent-1", tool="crm", field="user_message", pii_type="email"
            )
        for _ in range(2):
            catalog.record(
                agent_id="agent-1", tool="crm", field="user_message", pii_type="phone"
            )

        assert catalog.buffer_size == 2
        # Keys are (agent_id, tool, field, pii_type)
        assert catalog._buffer[("agent-1", "crm", "user_message", "email")] == 3
        assert catalog._buffer[("agent-1", "crm", "user_message", "phone")] == 2

    def test_different_agents_are_separate_keys(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)

        catalog.record(agent_id="a1", tool="t", field="f", pii_type="email")
        catalog.record(agent_id="a2", tool="t", field="f", pii_type="email")

        assert catalog.buffer_size == 2

    def test_writes_metric_increments(self):
        before = _metric_value(compliance_catalog_writes_total)
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)

        catalog.record(agent_id="a", tool="t", field="f", pii_type="email")
        catalog.record(agent_id="a", tool="t", field="f", pii_type="email")

        assert _metric_value(compliance_catalog_writes_total) == before + 2

    def test_buffer_size_metric(self):
        before = _metric_value(compliance_catalog_buffer_size)
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)

        catalog.record(agent_id="a", tool="t", field="f", pii_type="email")
        # Gauge reflects global state; our catalog set it to 1.
        assert _metric_value(compliance_catalog_buffer_size) == 1
        assert before >= 0  # sanity

    def test_record_is_sync_and_nonblocking(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        # Must not raise outside a running loop; no worker started.
        catalog.record(agent_id="a", tool="t", field="f", pii_type="email")
        assert catalog._task is None
        assert catalog.buffer_size == 1


# ---------------------------------------------------------------------------
# ComplianceCatalog — flush
# ---------------------------------------------------------------------------


class TestCatalogFlush:
    async def test_manual_flush_sends_aggregated_rows(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)

        for _ in range(3):
            catalog.record(
                agent_id="agent-1", tool="crm", field="user_message", pii_type="email"
            )
        for _ in range(2):
            catalog.record(
                agent_id="agent-1", tool="crm", field="user_message", pii_type="phone"
            )

        await catalog.flush()

        assert len(writer.calls) == 1
        rows = writer.calls[0]
        assert len(rows) == 2
        by_type = {r.pii_type: r for r in rows}
        assert by_type["email"].frequency == 3
        assert by_type["phone"].frequency == 2
        assert by_type["email"].agent_id == "agent-1"
        assert by_type["email"].tool == "crm"
        assert by_type["email"].field == "user_message"
        # Buffer drained
        assert catalog.buffer_size == 0

    async def test_flush_empty_buffer_is_noop(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        await catalog.flush()
        assert writer.calls == []

    async def test_timer_based_flush(self):
        """Background worker flushes on the configured interval."""
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=0.05)

        catalog.record(agent_id="a", tool="t", field="f", pii_type="email")
        catalog.record(agent_id="a", tool="t", field="f", pii_type="email")

        # Wait for at least one worker tick
        for _ in range(40):
            await asyncio.sleep(0.05)
            if writer.calls:
                break

        assert writer.calls, "background flush did not run"
        rows = writer.calls[0]
        assert len(rows) == 1
        assert rows[0].frequency == 2
        assert catalog.buffer_size == 0

        await catalog.close()
        assert writer.closed is True

    async def test_flush_failure_preserves_buffer_and_counts_errors(self):
        """Postgres down → buffer grows, errors_total++, data not lost."""
        errors_before = _metric_value(compliance_catalog_flush_errors_total)
        writer = RecordingWriter(fail=True)
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)

        for _ in range(5):
            catalog.record(
                agent_id="agent-1", tool="crm", field="user_message", pii_type="email"
            )
        for _ in range(2):
            catalog.record(
                agent_id="agent-1", tool="crm", field="user_message", pii_type="phone"
            )

        await catalog.flush()

        # Buffer restored with original frequencies
        assert catalog.buffer_size == 2
        assert catalog._buffer[("agent-1", "crm", "user_message", "email")] == 5
        assert catalog._buffer[("agent-1", "crm", "user_message", "phone")] == 2
        assert _metric_value(compliance_catalog_flush_errors_total) == errors_before + 1

        # Retry succeeds once Postgres is back
        writer.fail = False
        await catalog.flush()
        assert catalog.buffer_size == 0
        rows = writer.calls[0]
        by_type = {r.pii_type: r for r in rows}
        assert by_type["email"].frequency == 5
        assert by_type["phone"].frequency == 2

    async def test_close_flushes_remaining_buffer(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        catalog.record(agent_id="a", tool="t", field="f", pii_type="email")
        await catalog.close()
        assert writer.closed is True
        assert catalog.buffer_size == 0
        assert len(writer.all_rows) == 1
        assert writer.all_rows[0].frequency == 1


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


class TestNullCatalogWriter:
    async def test_upsert_and_close_are_noop(self):
        writer = NullCatalogWriter()
        await writer.upsert(
            [ComplianceRow("a", "t", "f", "email", 1)]
        )  # must not raise
        await writer.close()


class TestPostgresCatalogWriter:
    def test_dsn_from_env(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_COMPLIANCE_PG_DSN, "postgresql://u:p@h:5432/db")
        writer = PostgresCatalogWriter()
        assert writer._dsn == "postgresql://u:p@h:5432/db"

    def test_dsn_default_when_env_unset(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv(ENV_COMPLIANCE_PG_DSN, raising=False)
        writer = PostgresCatalogWriter()
        assert "localhost:5433" in writer._dsn

    def test_explicit_dsn_wins(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_COMPLIANCE_PG_DSN, "postgresql://env@h/db")
        writer = PostgresCatalogWriter(dsn="postgresql://explicit@h/db")
        assert writer._dsn == "postgresql://explicit@h/db"

    async def test_upsert_empty_rows_is_noop(self):
        writer = PostgresCatalogWriter(dsn="postgresql://x@h/db")
        # No pool created, no asyncpg import needed
        await writer.upsert([])

    async def test_upsert_without_asyncpg_raises_runtime_error(self):
        writer = PostgresCatalogWriter(dsn="postgresql://x@h/db")
        with patch.dict("sys.modules", {"asyncpg": None}):
            with patch(
                "builtins.__import__",
                side_effect=ImportError("no asyncpg"),
            ):
                # Force the lazy import inside _get_pool to fail
                with pytest.raises((RuntimeError, ImportError)):
                    await writer.upsert(
                        [ComplianceRow("a", "t", "f", "email", 1)]
                    )

    async def test_upsert_executes_batch_upsert_sql(self):
        fake_pool = MagicMock()
        fake_conn = AsyncMock()
        fake_pool.acquire.return_value.__aenter__ = AsyncMock(
            return_value=fake_conn
        )
        fake_pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)

        writer = PostgresCatalogWriter(dsn="postgresql://x@h/db")
        writer._pool = fake_pool

        rows = [
            ComplianceRow("a1", "crm", "user_message", "email", 3),
            ComplianceRow("a1", "crm", "user_message", "phone", 2),
        ]
        await writer.upsert(rows)

        fake_conn.executemany.assert_awaited_once()
        sql, values = fake_conn.executemany.call_args.args
        assert "ON CONFLICT (agent_id, tool, field, pii_type)" in sql
        assert "frequency = compliance_catalog.frequency + EXCLUDED.frequency" in sql
        assert values == [
            ("a1", "crm", "user_message", "email", 3),
            ("a1", "crm", "user_message", "phone", 2),
        ]

    async def test_close_releases_pool(self):
        fake_pool = MagicMock()
        fake_pool.close = AsyncMock()
        writer = PostgresCatalogWriter(dsn="postgresql://x@h/db")
        writer._pool = fake_pool
        await writer.close()
        fake_pool.close.assert_awaited_once()
        assert writer._pool is None

    def test_redact_dsn(self):
        from agent_obs.compliance.catalog import _redact_dsn

        assert (
            _redact_dsn("postgresql://warm:secret@localhost:5433/warm_store")
            == "postgresql://warm:***@localhost:5433/warm_store"
        )
        assert _redact_dsn("no-at-sign") == "no-at-sign"


# ---------------------------------------------------------------------------
# build_compliance_catalog — env config switching
# ---------------------------------------------------------------------------


class TestBuildCatalog:
    def test_off_selects_null_writer(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_COMPLIANCE_PG, "off")
        catalog = build_compliance_catalog()
        assert isinstance(catalog._writer, NullCatalogWriter)

    def test_false_selects_null_writer(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_COMPLIANCE_PG, "false")
        catalog = build_compliance_catalog()
        assert isinstance(catalog._writer, NullCatalogWriter)

    def test_on_selects_postgres_writer(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_COMPLIANCE_PG, "on")
        monkeypatch.setenv(ENV_COMPLIANCE_PG_DSN, "postgresql://u:p@h:5432/db")
        catalog = build_compliance_catalog()
        assert isinstance(catalog._writer, PostgresCatalogWriter)
        assert catalog._writer._dsn == "postgresql://u:p@h:5432/db"

    def test_auto_without_dsn_selects_null(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_COMPLIANCE_PG, "auto")
        monkeypatch.delenv(ENV_COMPLIANCE_PG_DSN, raising=False)
        catalog = build_compliance_catalog()
        assert isinstance(catalog._writer, NullCatalogWriter)

    def test_auto_with_dsn_and_asyncpg_selects_postgres(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(ENV_COMPLIANCE_PG, "auto")
        monkeypatch.setenv(ENV_COMPLIANCE_PG_DSN, "postgresql://u:p@h:5432/db")
        fake_asyncpg = MagicMock()
        with patch.dict("sys.modules", {"asyncpg": fake_asyncpg}):
            catalog = build_compliance_catalog()
        assert isinstance(catalog._writer, PostgresCatalogWriter)

    def test_auto_without_asyncpg_selects_null(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setenv(ENV_COMPLIANCE_PG, "auto")
        monkeypatch.setenv(ENV_COMPLIANCE_PG_DSN, "postgresql://u:p@h:5432/db")
        with patch.dict("sys.modules", {"asyncpg": None}):
            # Make import fail even if a real asyncpg is installed
            import builtins

            real_import = builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "asyncpg":
                    raise ImportError("blocked for test")
                return real_import(name, *args, **kwargs)

            with patch("builtins.__import__", side_effect=fake_import):
                catalog = build_compliance_catalog()
        assert isinstance(catalog._writer, NullCatalogWriter)

    def test_flush_interval_propagates(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv(ENV_COMPLIANCE_PG, "off")
        catalog = build_compliance_catalog(flush_interval=12.5)
        assert catalog._flush_interval == 12.5


# ---------------------------------------------------------------------------
# GuardrailEngine integration (PC33)
# ---------------------------------------------------------------------------


class TestEngineCatalogIntegration:
    async def test_record_called_on_masking(self):
        """check_input with PII → catalog.record with parsed field/pii_type."""
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        ctx = SimpleNamespace(
            trace_id="t1", agent_id="agent-1", tool_name="crm"
        )
        verdict = await engine.check_input(EMAIL_TEXT, context=ctx)

        assert verdict.verdict == "clean"
        assert len(verdict.redacted_fields) == 1
        # redacted path: "user_message.email.XXXX"
        assert verdict.redacted_fields[0].startswith("user_message.email.")

        assert catalog.buffer_size == 1
        key = ("agent-1", "crm", "user_message", "email")
        assert catalog._buffer[key] == 1

    async def test_tool_name_defaults_to_empty(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        ctx = SimpleNamespace(trace_id="t1", agent_id="agent-1")
        await engine.check_input(EMAIL_TEXT, context=ctx)

        assert catalog._buffer[("agent-1", "", "user_message", "email")] == 1

    async def test_tool_name_flows_from_span_context(self):
        """tool_name set on SpanContext reaches the catalog record."""
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        ctx = SpanContext.new(agent_id="agent-9", tool_name="search_tool")
        await engine.check_input(EMAIL_TEXT, context=ctx)

        assert catalog._buffer[("agent-9", "search_tool", "user_message", "email")] == 1

    async def test_five_maskings_three_email_two_phone(self):
        """DoD scenario: 5 maskings → freq=3 email, freq=2 phone."""
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        ctx = SimpleNamespace(trace_id="t1", agent_id="agent-1", tool_name="crm")
        for _ in range(3):
            await engine.check_input(EMAIL_TEXT, context=ctx)
        for _ in range(2):
            await engine.check_input(PHONE_TEXT, context=ctx)

        assert catalog._buffer[("agent-1", "crm", "user_message", "email")] == 3
        assert catalog._buffer[("agent-1", "crm", "user_message", "phone")] == 2

        await catalog.flush()
        rows = writer.calls[0]
        by_type = {r.pii_type: r for r in rows}
        assert by_type["email"].frequency == 3
        assert by_type["phone"].frequency == 2

    async def test_no_pii_no_record(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        ctx = SimpleNamespace(trace_id="t1", agent_id="agent-1", tool_name="crm")
        verdict = await engine.check_input("nothing interesting here", context=ctx)

        assert verdict.verdict == "clean"
        assert verdict.redacted_fields == []
        assert catalog.buffer_size == 0

    async def test_catalog_none_is_noop(self):
        engine = _build_engine_with_catalog(None)
        ctx = SimpleNamespace(trace_id="t1", agent_id="agent-1", tool_name="crm")
        verdict = await engine.check_input(EMAIL_TEXT, context=ctx)
        assert verdict.verdict == "clean"
        assert len(verdict.redacted_fields) == 1

    async def test_context_none_agent_id_defaults(self):
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        verdict = await engine.check_input(EMAIL_TEXT, context=None)
        assert verdict.verdict == "clean"
        assert catalog._buffer[("", "", "user_message", "email")] == 1

    async def test_cached_verdict_does_not_double_count(self):
        """Verdict caching (span.guardrail_verdicts) happens before check_input,
        so a second check on cached text would re-record — but the SDK cache
        path never re-enters check_input. Here we verify record is tied to
        check_input invocations only."""
        writer = RecordingWriter()
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        ctx = SimpleNamespace(trace_id="t1", agent_id="agent-1", tool_name="crm")
        await engine.check_input(EMAIL_TEXT, context=ctx)
        await engine.check_input(EMAIL_TEXT, context=ctx)

        # Two separate check_input calls → two records (correct: two maskings)
        assert catalog._buffer[("agent-1", "crm", "user_message", "email")] == 2

    async def test_record_does_not_block_check_input(self):
        """A failing writer must not break the guardrail hot path."""
        writer = RecordingWriter(fail=True)
        catalog = ComplianceCatalog(writer, flush_interval=3600.0)
        engine = _build_engine_with_catalog(catalog)

        ctx = SimpleNamespace(trace_id="t1", agent_id="agent-1", tool_name="crm")
        verdict = await engine.check_input(EMAIL_TEXT, context=ctx)

        # Masking still succeeded despite writer being down
        assert verdict.verdict == "clean"
        assert "ivan@example.com" not in verdict.masked_text
        assert catalog.buffer_size == 1
