"""Deterministic content sampling (P22).

Full prompt/response text is only stored for a small, deterministic subset of
traces to cap storage growth (risk R1.5).  The decision is derived from the
``trace_id`` alone, so every span of a trace reaches the same verdict, while
un-sampled traces keep only ``sha256`` hashes and character counts.
"""

from __future__ import annotations

import hashlib
import logging
import os

from agent_obs.observability import Span

logger = logging.getLogger(__name__)

DEFAULT_CONTENT_RATE = 10  # percent
_SUMMARY_CHARS = 200


def content_rate_from_env(env: dict[str, str] | None = None) -> int:
    """Read the content-sampling rate (percent) from the environment.

    ``AGENT_OBS_CONTENT_RATE`` is percent (0-100); an unset or invalid value
    falls back to :data:`DEFAULT_CONTENT_RATE`.
    """
    env = os.environ if env is None else env
    raw = env.get("AGENT_OBS_CONTENT_RATE", str(DEFAULT_CONTENT_RATE))
    try:
        rate = int(raw)
    except ValueError:
        logger.warning(
            "Invalid AGENT_OBS_CONTENT_RATE %r, using default %d",
            raw,
            DEFAULT_CONTENT_RATE,
        )
        return DEFAULT_CONTENT_RATE
    return max(0, min(100, rate))


def should_keep_content(trace_id: str, rate: int | None = None) -> bool:
    """Decide whether a trace keeps full content (deterministic by trace_id).

    All spans sharing *trace_id* agree on the verdict, and roughly *rate*
    percent of traces keep full content.
    """
    if rate is None:
        rate = content_rate_from_env()
    bucket = int(hashlib.sha256(trace_id.encode("utf-8")).hexdigest(), 16) % 100
    return bucket < rate


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def attach_llm_content(
    span: Span,
    *,
    input_text: str | None = None,
    output_text: str | None = None,
    rate: int | None = None,
) -> None:
    """Attach content attributes to an ``llm.call`` span.

    Sampled traces get the full ``llm.input_text`` / ``llm.output_text``;
    un-sampled traces get ``llm.<field>_sha256`` and ``llm.<field>_chars``.
    """
    if input_text is None and output_text is None:
        return
    keep = should_keep_content(span.context.trace_id, rate)
    span.attributes["trace.content_sampled"] = keep
    if input_text is not None:
        if keep:
            span.attributes["llm.input_text"] = input_text
        else:
            span.attributes["llm.input_sha256"] = _sha256(input_text)
            span.attributes["llm.input_chars"] = len(input_text)
    if output_text is not None:
        if keep:
            span.attributes["llm.output_text"] = output_text
        else:
            span.attributes["llm.output_sha256"] = _sha256(output_text)
            span.attributes["llm.output_chars"] = len(output_text)


def attach_tool_content(
    span: Span,
    *,
    input_text: str | None = None,
    rate: int | None = None,
) -> None:
    """Attach content attributes to a ``tool.call`` span.

    The full tool input is never stored — only a short summary (up to 200
    chars), its hash and length.  This guarantees no full-text leak through
    ``tool.input_summary`` even on sampled traces.
    """
    keep = should_keep_content(span.context.trace_id, rate)
    span.attributes["trace.content_sampled"] = keep
    if input_text is None:
        return
    span.attributes["tool.input_sha256"] = _sha256(input_text)
    span.attributes["tool.input_chars"] = len(input_text)
    summary = input_text[:_SUMMARY_CHARS]
    if len(input_text) > _SUMMARY_CHARS:
        summary += "..."
    span.attributes["tool.input_summary"] = summary