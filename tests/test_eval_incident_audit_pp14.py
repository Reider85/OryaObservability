"""Tests for PP14 incident eval audit events."""

import time
from unittest.mock import MagicMock, patch

import pytest

from agent_obs.guardrail.audit import EvalIncidentEvalAuditEvent


class TestEvalIncidentEvalAuditEvent:
    """Test EvalIncidentEvalAuditEvent class."""
    
    def test_for_incident_eval(self):
        """Test EvalIncidentEvalAuditEvent.for_incident_eval."""
        event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id="trace123",
            reason="incident_security",
            reason_label="security",
            mode="incident",
            trigger="security.incident",
            actor_id="test-sampler"
        )
        
        assert event.trace_id == "trace123"
        assert event.action == "eval_sampler.incident_eval"
        assert event.resource["reason"] == "incident_security"
        assert event.resource["reason_label"] == "security"
        assert event.resource["mode"] == "incident"
        assert event.resource["trigger"] == "security.incident"
        assert event.decision == "evaluated"
        assert "incident eval granted" in event.reason
    
    def test_for_incident_eval_injection(self):
        """Test EvalIncidentEvalAuditEvent for prompt injection."""
        event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id="trace456",
            reason="incident_injection",
            reason_label="injection",
            mode="day",
            trigger="error.type=prompt_injection"
        )
        
        assert event.resource["reason"] == "incident_injection"
        assert event.resource["reason_label"] == "injection"
        assert event.resource["mode"] == "day"
        assert event.resource["trigger"] == "error.type=prompt_injection"
    
    def test_to_dict(self):
        """Test EvalIncidentEvalAuditEvent.to_dict."""
        event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id="trace789",
            reason="incident_security",
            reason_label="security",
            mode="night",
            trigger="security.incident"
        )
        
        event_dict = event.to_dict()
        assert event_dict["audit_id"] == event.audit_id
        assert event_dict["trace_id"] == "trace789"
        assert event_dict["action"] == "eval_sampler.incident_eval"
        assert event_dict["resource"]["reason"] == "incident_security"
        assert event_dict["decision"] == "evaluated"
        assert event_dict["reason_category"] == "incident"
    
    def test_to_clickhouse_row(self):
        """Test EvalIncidentEvalAuditEvent.to_clickhouse_row."""
        event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id="trace999",
            reason="incident_security",
            reason_label="security",
            mode="incident",
            trigger="security.incident"
        )
        
        row = event.to_clickhouse_row()
        assert len(row) == 10  # audit_events_hot DDL columns
        assert row[4] == "eval_sampler.incident_eval"  # action
        assert row[5] == "evaluated"  # decision
        assert "incident eval granted" in row[7]  # reason
    
    def test_reason_category(self):
        """Test reason_category property."""
        security_event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id="trace1", reason="incident_security", reason_label="security",
            mode="day", trigger="security.incident"
        )
        assert security_event.reason_category == "incident"
        
        injection_event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id="trace2", reason="incident_injection", reason_label="injection",
            mode="day", trigger="error.type=prompt_injection"
        )
        assert injection_event.reason_category == "incident"
        
        other_event = EvalIncidentEvalAuditEvent.for_incident_eval(
            trace_id="trace3", reason="incident_other", reason_label="other",
            mode="day", trigger="unknown"
        )
        assert other_event.reason_category == "incident"