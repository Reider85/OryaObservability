"""
PP24 tests: drill-down UI для cold-полей в trace-viewer.

Покрывает:
- AuditEvent.to_clickhouse_row (фикс несовместимости с HotStore.write_audit_event)
- GET /api/v1/traces/{id}/drill-down/capabilities (RBAC + rate-limit для UI)
- GET /api/v1/traces/{id}/drill-down/history (sidebar, последние 10)
- Cold-страница трейса: [COLD]-бейджи, кнопка Drill down, маскировка PII
- DrillDownService.get_rate_remaining (read-only Redis-счётчик)
"""

import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import agent_obs.trace_viewer.ui as ui_module
from agent_obs.storage.drill_down import DrillDownResult, DrillDownService
from agent_obs.trace_viewer.ui import (
    AuditEvent,
    app,
    generate_cold_trace_html,
)


def _find_hot_store_dep():
    """Найти оригинальную Depends-функцию get_hot_store в FastAPI-роутах.

    tests/test_trace_viewer.py подменяет ui_module.get_hot_store (monkey-patch
    атрибута модуля), но FastAPI-роуты держат ссылку на оригинальную функцию —
    переопределять dependency нужно именно по ней.
    """
    for route in app.routes:
        dependant = getattr(route, "dependant", None)
        if dependant is None:
            continue
        for dep in dependant.dependencies:
            if getattr(dep.call, "__name__", "") == "get_hot_store":
                return dep.call
    return ui_module.get_hot_store


_REAL_GET_HOT_STORE = _find_hot_store_dep()


@pytest.fixture(autouse=True)
def _isolate_ui_module_state():
    """Изолировать глобальные side-effect'ы других сьютов от ui_module.

    tests/test_trace_viewer.py на import мутирует sys.modules['agent_obs.storage.hot']
    и подменяет ui_module.get_hot_store (monkey-patch функции). Восстанавливаем
    оба, чтобы PP24-тесты были детерминированными в любом порядке сборки.
    """
    saved_hot = sys.modules.get("agent_obs.storage.hot")
    saved_get_hot_store = ui_module.get_hot_store
    ui_module.get_hot_store = _REAL_GET_HOT_STORE
    app.dependency_overrides.clear()
    yield
    app.dependency_overrides.clear()
    ui_module.get_hot_store = saved_get_hot_store
    if saved_hot is not None:
        sys.modules["agent_obs.storage.hot"] = saved_hot
    else:
        sys.modules.pop("agent_obs.storage.hot", None)


@pytest.fixture
def client():
    """TestClient с очисткой dependency_overrides после теста."""
    with TestClient(app) as c:
        yield c


class FakeRedis:
    """Minimal sync-подобный Redis для get_rate_remaining / _rate_limit_check."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def incr(self, key):
        self.store[key] = int(self.store.get(key, 0)) + 1
        return self.store[key]

    def expire(self, key, ttl):
        return True

    def setex(self, key, ttl, value):
        self.store[key] = value
        return True


def _mock_drill_service(rate_remaining=7, rate_limit=10):
    service = MagicMock()
    service.rate_limit = rate_limit
    service.get_rate_remaining = AsyncMock(return_value=rate_remaining)
    return service


class TestAuditEventClickHouseRow:
    """PP24: ui.AuditEvent теперь пишется в audit_events_hot без AttributeError."""

    def test_to_clickhouse_row_shape(self):
        event = AuditEvent(
            timestamp=1705326600.0,
            trace_id="t1",
            node_id="",
            user="alice",
            decision="allow",
            reason="success",
            action="drill_down",
            requested_fields=["llm.input_text"],
            mfa_used=True,
        )
        row = event.to_clickhouse_row()
        assert len(row) == 10
        assert row[0].startswith("dd-")
        assert row[2] == "t1"
        assert row[4] == "drill_down"
        assert row[5] == "allow"
        assert "alice" in row[3]

    def test_audit_id_stable_between_calls(self):
        event = AuditEvent(
            timestamp=1.0, trace_id="t", node_id="", user="u",
            decision="allow", reason="r", action="drill_down",
            requested_fields=[], mfa_used=False,
        )
        row1 = event.to_clickhouse_row()
        row2 = event.to_clickhouse_row()
        assert row1[0] == row2[0]

    def test_write_audit_event_via_hot_store_contract(self):
        """HotStore.write_audit_event вызывает event.to_clickhouse_row() — контракт соблюдён."""
        # Берём класс через ui_module: tests/test_trace_viewer.py подменяет
        # sys.modules['agent_obs.storage.hot'] на MagicMock, но ui.py импортировал
        # реальный HotStore до этого и хранит ссылку.
        HotStore = ui_module.HotStore
        if not isinstance(HotStore, type):
            pytest.skip("agent_obs.storage.hot замокан другим сьютом")
        event = AuditEvent(
            timestamp=1.0, trace_id="t", node_id="", user="u",
            decision="deny", reason="rate_limited", action="drill_down",
            requested_fields=["input"], mfa_used=False,
        )
        client_mock = MagicMock()
        hot_store = MagicMock()
        hot_store._get_client.return_value = client_mock
        # Используем реальный код HotStore.write_audit_event
        HotStore.write_audit_event(hot_store, event)
        client_mock.execute.assert_called_once()


class TestDrillDownCapabilities:
    """PP24: capabilities endpoint — can_read / can_pii_recover / rate_remaining."""

    def test_missing_user_header_403(self, client):
        app.dependency_overrides[ui_module.get_drill_down_service] = lambda: _mock_drill_service()
        resp = client.get("/api/v1/traces/t1/drill-down/capabilities")
        assert resp.status_code == 403
        assert "X-OryaObs-User" in resp.json()["detail"]

    def test_unknown_user_no_roles(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "")
        app.dependency_overrides[ui_module.get_drill_down_service] = lambda: _mock_drill_service()
        resp = client.get(
            "/api/v1/traces/t1/drill-down/capabilities",
            headers={"X-OryaObs-User": "nobody"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["can_read"] is False
        assert data["can_pii_recover"] is False

    def test_admin_has_pii_recover(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        app.dependency_overrides[ui_module.get_drill_down_service] = lambda: _mock_drill_service(
            rate_remaining=3, rate_limit=10
        )
        resp = client.get(
            "/api/v1/traces/t1/drill-down/capabilities",
            headers={"X-OryaObs-User": "alice"},
        )
        data = resp.json()
        assert data["can_read"] is True
        assert data["can_pii_recover"] is True
        assert data["rate_limit"] == 10
        assert data["rate_remaining"] == 3

    def test_developer_no_pii_recover(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "bob:developer")
        app.dependency_overrides[ui_module.get_drill_down_service] = lambda: _mock_drill_service()
        resp = client.get(
            "/api/v1/traces/t1/drill-down/capabilities",
            headers={"X-OryaObs-User": "bob"},
        )
        data = resp.json()
        assert data["can_read"] is True
        assert data["can_pii_recover"] is False

    def test_rate_remaining_none_when_redis_down(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        app.dependency_overrides[ui_module.get_drill_down_service] = lambda: _mock_drill_service(
            rate_remaining=None
        )
        resp = client.get(
            "/api/v1/traces/t1/drill-down/capabilities",
            headers={"X-OryaObs-User": "alice"},
        )
        assert resp.json()["rate_remaining"] is None


class TestDrillDownHistory:
    """PP24: история drill-down (audit_events_hot), последние 10 для user."""

    def _hot_store_with_events(self, events):
        hot_store = MagicMock()
        hot_store.get_audit_events.return_value = events
        return hot_store

    def test_missing_user_403(self, client):
        resp = client.get("/api/v1/traces/t1/drill-down/history")
        assert resp.status_code == 403

    def test_rbac_denied(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "")
        resp = client.get(
            "/api/v1/traces/t1/drill-down/history",
            headers={"X-OryaObs-User": "nobody"},
        )
        assert resp.status_code == 403

    def test_history_filtered_by_user(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        events = [
            {
                "audit_id": "a1", "timestamp": "2026-10-28T10:00:00",
                "trace_id": "t1", "actor": {"type": "viewer_user", "id": "alice"},
                "action": "drill_down", "decision": "allow", "reason": "success",
                "resource": {"node_id": "", "requested_fields": ["llm.input_text"], "mfa_used": True},
                "ip_address": "unknown", "user_agent": "trace-viewer",
            },
            {
                "audit_id": "a2", "timestamp": "2026-10-28T10:01:00",
                "trace_id": "t1", "actor": {"type": "viewer_user", "id": "bob"},
                "action": "drill_down", "decision": "deny", "reason": "rate_limited",
                "resource": {"node_id": "", "requested_fields": ["input"], "mfa_used": False},
                "ip_address": "unknown", "user_agent": "trace-viewer",
            },
        ]
        app.dependency_overrides[_REAL_GET_HOT_STORE] = lambda: self._hot_store_with_events(events)
        resp = client.get(
            "/api/v1/traces/t1/drill-down/history",
            headers={"X-OryaObs-User": "alice"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"] == "alice"
        assert len(data["items"]) == 1
        assert data["items"][0]["decision"] == "allow"
        assert data["items"][0]["requested_fields"] == ["llm.input_text"]
        assert data["items"][0]["mfa_used"] is True

    def test_history_limited_to_10(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        events = [
            {
                "audit_id": f"a{i}", "timestamp": "2026-10-28T10:00:00",
                "trace_id": "t1", "actor": {"type": "viewer_user", "id": "alice"},
                "action": "drill_down", "decision": "allow", "reason": "success",
                "resource": {"requested_fields": [f"field_{i}"], "mfa_used": False},
            }
            for i in range(25)
        ]
        app.dependency_overrides[_REAL_GET_HOT_STORE] = lambda: self._hot_store_with_events(events)
        resp = client.get(
            "/api/v1/traces/t1/drill-down/history",
            headers={"X-OryaObs-User": "alice"},
        )
        assert len(resp.json()["items"]) == 10

    def test_history_empty_when_store_unavailable(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        hot_store = MagicMock(spec=[])  # нет get_audit_events
        app.dependency_overrides[_REAL_GET_HOT_STORE] = lambda: hot_store
        resp = client.get(
            "/api/v1/traces/t1/drill-down/history",
            headers={"X-OryaObs-User": "alice"},
        )
        assert resp.status_code == 200
        assert resp.json()["items"] == []


class TestColdTracePage:
    """PP24: /trace/{id} для трейса, мигрированного в warm/cold — cold-страница."""

    COLD_FIELDS = {
        "trace_id": "cold1",
        "status": "ok",
        "span_count": 5,
        "input_chars": 120,
        "llm.input_text": "user@example.com content",
        "output_chars": 80,
    }

    def _cold_service(self, tier="cold"):
        service = MagicMock()
        service.drill_down = AsyncMock(return_value=DrillDownResult(
            fields=dict(self.COLD_FIELDS),
            storage_tier=tier,
            query_latency_ms=12.0,
            mfa_used=False,
        ))
        service.rate_limit = 10
        service.get_rate_remaining = AsyncMock(return_value=9)
        return service

    def _hot_miss_store(self):
        hot_store = MagicMock()
        hot_store.get_spans.side_effect = Exception("Trace not found")
        return hot_store

    def test_cold_trace_renders_cold_badges(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        app.dependency_overrides[_REAL_GET_HOT_STORE] = self._hot_miss_store
        with patch.object(ui_module, "get_drill_down_service", return_value=self._cold_service("cold")):
            resp = client.get("/trace/cold1")
        assert resp.status_code == 200
        html = resp.text
        assert "[COLD]" in html
        assert "drill-down-btn" in html
        assert "cold-field-row" in html
        assert "user-modal" in html
        assert "#mfa-input" in html or 'id="mfa-input"' in html
        assert "drill-down-history-list" in html

    def test_pii_fields_masked_on_cold_page(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        app.dependency_overrides[_REAL_GET_HOT_STORE] = self._hot_miss_store
        with patch.object(ui_module, "get_drill_down_service", return_value=self._cold_service("cold")):
            resp = client.get("/trace/cold1")
        html = resp.text
        # PII-значение не должно попасть на страницу без MFA
        assert "user@example.com content" not in html
        assert "MASKED" in html

    def test_non_pii_fields_shown(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        app.dependency_overrides[_REAL_GET_HOT_STORE] = self._hot_miss_store
        with patch.object(ui_module, "get_drill_down_service", return_value=self._cold_service("warm")):
            resp = client.get("/trace/cold1")
        html = resp.text
        assert "input_chars" in html
        assert "120" in html

    def test_404_when_not_in_any_tier(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        app.dependency_overrides[_REAL_GET_HOT_STORE] = self._hot_miss_store
        service = MagicMock()
        service.drill_down = AsyncMock(side_effect=ValueError("Trace not found in any tier"))
        service.rate_limit = 10
        with patch.object(ui_module, "get_drill_down_service", return_value=service):
            resp = client.get("/trace/missing")
        assert resp.status_code == 404

    def test_hot_trace_unchanged(self, client, monkeypatch):
        """Горячий трейс рендерится как раньше, cold-fallback не трогается."""
        monkeypatch.setenv("AGENT_OBS_RBAC_ROLES", "alice:admin")
        hot_store = MagicMock()
        hot_store.get_spans.return_value = [{
            "trace_id": "hot1", "span_id": "s1", "parent_span_id": "",
            "name": "agent.loop", "span_type": "agent.loop", "status": "ok",
            "attributes": {}, "events": [],
        }]
        app.dependency_overrides[_REAL_GET_HOT_STORE] = lambda: hot_store
        with patch.object(ui_module, "get_drill_down_service") as mock_service:
            resp = client.get("/trace/hot1")
        assert resp.status_code == 200
        assert "hot1" in resp.text
        mock_service.assert_not_called()


class TestGenerateColdTraceHtml:
    """Прямой тест генератора cold-страницы."""

    def test_modal_and_sidebar_present(self):
        html = generate_cold_trace_html(
            "t1", "cold", {"status": "ok", "input": "secret"}, "2026-10-28T00:00:00"
        )
        assert 'id="drill-down-modal"' in html
        assert "Recovering from cold storage" in html
        assert "&lt; 1 min" in html
        assert 'id="drill-down-history-list"' in html

    def test_pii_masked_non_pii_shown(self):
        html = generate_cold_trace_html(
            "t1", "cold", {"llm.output_text": "top secret", "span_count": 3}, ""
        )
        assert "top secret" not in html
        assert "span_count" in html

    def test_drill_down_button_disabled_state_attrs(self):
        html = generate_cold_trace_html("t1", "cold", {"status": "ok"}, "")
        assert 'data-pii="0"' in html
        assert "openDrillDownModal" in html


class TestDrillDownServiceRateRemaining:
    """PP24: get_rate_remaining — read-only, без инкремента."""

    def _service(self, redis_client, rate_limit=10):
        return DrillDownService(
            hot_store=MagicMock(),
            warm_store=MagicMock(),
            cold_store=MagicMock(),
            redis_client=redis_client,
            rate_limit=rate_limit,
            cache_ttl=600,
        )

    async def test_full_quota_initially(self):
        service = self._service(FakeRedis())
        assert await service.get_rate_remaining("alice") == 10

    async def test_remaining_decrements_with_usage(self):
        fake = FakeRedis()
        service = self._service(fake, rate_limit=10)
        for _ in range(3):
            assert await service._rate_limit_check("alice") is True
        assert await service.get_rate_remaining("alice") == 7

    async def test_zero_after_limit(self):
        fake = FakeRedis()
        service = self._service(fake, rate_limit=2)
        await service._rate_limit_check("alice")
        await service._rate_limit_check("alice")
        assert await service.get_rate_remaining("alice") == 0

    async def test_no_redis_returns_none(self):
        service = self._service(None)
        assert await service.get_rate_remaining("alice") is None

    async def test_read_only_does_not_consume_quota(self):
        fake = FakeRedis()
        service = self._service(fake, rate_limit=10)
        await service.get_rate_remaining("alice")
        await service.get_rate_remaining("alice")
        assert await service.get_rate_remaining("alice") == 10
