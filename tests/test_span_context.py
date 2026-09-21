"""Tests for the SpanContext dataclass (T1.1.1)."""

import pytest

from agent_obs.observability import SpanContext


def test_new_creates_root_context():
    ctx = SpanContext.new(agent_id="pilot-agent", agent_version="0.1.0")
    assert ctx.agent_id == "pilot-agent"
    assert ctx.agent_version == "0.1.0"
    assert ctx.parent_span_id is None
    assert ctx.user_id == ""
    assert ctx.session_id == ""


def test_ids_are_unique():
    ctxs = [SpanContext.new(agent_id="a") for _ in range(1000)]
    assert len({c.trace_id for c in ctxs}) == 1000
    assert len({c.span_id for c in ctxs}) == 1000


def test_ids_are_ulid_like_and_sortable():
    first = SpanContext.new(agent_id="a")
    second = SpanContext.new(agent_id="a")
    assert len(first.trace_id) == 26
    assert len(first.span_id) == 26
    assert first.trace_id.isupper()
    assert (
        second.trace_id[:10] >= first.trace_id[:10]
    ), "time-prefix of ULID ids must be non-decreasing"


def test_parent_span_id_propagated():
    parent = SpanContext.new(agent_id="a")
    child = SpanContext.new(agent_id="b", parent=parent)
    assert child.parent_span_id == parent.span_id
    assert child.trace_id == parent.trace_id
    assert child.span_id != parent.span_id


def test_fields_filled_from_factory():
    parent = SpanContext.new(agent_id="a")
    ctx = SpanContext.new(
        agent_id="b",
        agent_version="2.0",
        parent=parent,
        user_id="u-1",
        session_id="s-1",
    )
    assert ctx.agent_id == "b"
    assert ctx.agent_version == "2.0"
    assert ctx.user_id == "u-1"
    assert ctx.session_id == "s-1"


def test_to_dict_serialization():
    ctx = SpanContext.new(agent_id="pilot-agent", agent_version="0.1.0")
    data = ctx.to_dict()
    assert data["trace_id"] == ctx.trace_id
    assert data["span_id"] == ctx.span_id
    assert data["parent_span_id"] is None
    assert data["agent_id"] == "pilot-agent"
    assert data["agent_version"] == "0.1.0"
    assert data["user_id"] == ""
    assert data["session_id"] == ""
    assert set(data.keys()) == {
        "trace_id",
        "span_id",
        "parent_span_id",
        "agent_id",
        "agent_version",
        "user_id",
        "session_id",
    }


@pytest.mark.asyncio
async def test_module_importable():
    import agent_obs.observability  # noqa: F401

    assert callable(SpanContext.new)