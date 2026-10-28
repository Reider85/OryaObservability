"""Core telemetry objects: SpanContext, Span and (later) ObservabilitySDK.

Span Context carries the identity and lineage of a single trace.

ID generation strategy: ULID-like ids are produced from the standard library
only (no external dependency). Each id is 26 Crockford base32 characters:
10 chars encode millisecond timestamp (48 bits) and 16 chars encode 80 random
bits. The time prefix keeps ids lexicographically sortable by creation time.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import contextvars
import functools
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any, AsyncIterator, Callable, NamedTuple, Optional

from agent_obs.cost.compute_cost import Usage, compute_cost
from agent_obs.cost.price_book import PriceBook, PriceNotFoundError

if TYPE_CHECKING:
    from agent_obs.guardrail.engine import GuardrailEngine, GuardrailVerdict
    from agent_obs.span_aggregator import SpanAggregator

logger = logging.getLogger(__name__)

# PP11 metrics imports
from agent_obs.metrics import (
    cost_per_request_usd,
    cost_total_usd,
    spans_total,
    spans_aggregated_total,
    spans_micro_input_total,
    bytes_saved_total,
    aggregation_ratio,
)

# Global references to PP11 metrics for use in _record_aggregation_metrics
_PP11_SPANS_AGGREGATED_TOTAL = spans_aggregated_total
_PP11_SPANS_MICRO_INPUT_TOTAL = spans_micro_input_total
_PP11_BYTES_SAVED_TOTAL = bytes_saved_total
_PP11_AGGREGATION_RATIO = aggregation_ratio

def get_pp11_metrics():
    """Get PP11 metrics for testing."""
    return {
        'spans_aggregated_total': spans_aggregated_total,
        'spans_micro_input_total': spans_micro_input_total,
        'bytes_saved_total': bytes_saved_total,
        'aggregation_ratio': aggregation_ratio,
    }

_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_TRACE_ID_CHARS = 26

# Text span attributes subject to guardrail masking in ``_enqueue`` (PC08), as
# ``(attribute, field_name, stage)``.  ``field_name`` is the semantic name used
# to build ``pii.redacted_fields`` paths ("user_message.email.5f3a"), ``stage``
# labels ``guardrail_block_total``.
_GUARDRAIL_TEXT_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("llm.input_text", "user_message", "input"),
    ("llm.output_text", "llm_output_text", "output"),
    ("tool.input_summary", "tool_input_summary", "tool"),
    ("tool.output_text", "tool_output", "output"),
)

# Attribute written instead of the text when the guardrail itself errors:
# masking is fail-closed, so a failed check must never let plaintext through.
_REDACTED_ON_GUARDRAIL_FAILURE = "[REDACTED:guardrail_unavailable]"

# Environment flag that turns the guardrail on/off when an engine is supplied.
_GUARDRAIL_ENABLED_ENV = "AGENT_OBS_GUARDRAIL_ENABLED"

# PP29 (T3.6.5): the deploy version stamped onto every exported span as the
# ``agent_version`` attribute. `spans_hot` has no version column, so the canary
# gate scopes SLI counts by JSONExtractString(attributes, 'agent_version').
# When set, the deployment env is authoritative over the version passed into
# the span context — CI stamps exactly what it deployed.
_DEPLOY_VERSION_ENV = "AGENT_OBS_DEPLOY_VERSION"


class _GuardrailCheck(NamedTuple):
    """A guardrail check cached on a span by a pre-call hook.

    ``text`` is the exact string that was checked, so a cached verdict is only
    reused when the attribute still holds that same string.  ``tool_call``
    truncates its input to a 200-char summary, which makes the attribute differ
    from the checked text and correctly forces a fresh check.
    """

    text: str
    verdict: "GuardrailVerdict"


def _encode_crockford(value: int, length: int) -> str:
    """Encode an integer as a fixed-length Crockford base32 string."""
    chars = [""] * length
    for i in range(length - 1, -1, -1):
        chars[i] = _CROCKFORD_ALPHABET[value & 0x1F]
        value >>= 5
    return "".join(chars)


def _new_ulid() -> str:
    """Generate a ULID-like sortable id (26 chars, timestamp + random)."""
    ts_ms = int(time.time() * 1000)
    random_bits = secrets.randbits(80)
    return _encode_crockford(ts_ms, 10) + _encode_crockford(random_bits, 16)


@dataclass
class SpanContext:
    """Immutable-ish context object identifying a span within a trace.

    Matches the canonical contract from ARCHITECT.md field-for-field.
    """

    trace_id: str
    span_id: str
    parent_span_id: Optional[str] = None
    agent_id: str = ""
    agent_version: str = ""
    user_id: str = ""
    session_id: str = ""
    tool_name: Optional[str] = None

    @classmethod
    def new(
        cls,
        agent_id: str,
        agent_version: str = "",
        parent: Optional["SpanContext"] = None,
        user_id: str = "",
        session_id: str = "",
        tool_name: Optional[str] = None,
    ) -> "SpanContext":
        """Create a new span context.

        A root context (no parent) gets a fresh trace_id; a child context
        inherits trace_id from its parent and records parent_span_id.
        """
        if parent is not None:
            trace_id = parent.trace_id
            parent_span_id = parent.span_id
        else:
            trace_id = _new_ulid()
            parent_span_id = None
        return cls(
            trace_id=trace_id,
            span_id=_new_ulid(),
            parent_span_id=parent_span_id,
            agent_id=agent_id,
            agent_version=agent_version,
            user_id=user_id,
            session_id=session_id,
            tool_name=tool_name,
        )

    def to_dict(self) -> dict:
        """Serialize to a plain dict (attribute names match the contract)."""
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "agent_id": self.agent_id,
            "agent_version": self.agent_version,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
        }


def _now() -> float:
    """Current time as POSIX timestamp (seconds)."""
    return time.time()


def _to_iso8601(ts: Optional[float]) -> Optional[str]:
    """Convert a POSIX timestamp to ISO 8601 string (UTC)."""
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _parse_iso8601(iso_str: str) -> float:
    """Parse ISO8601 string to POSIX timestamp (PP09 from_dict)."""
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    return datetime.fromisoformat(iso_str).timestamp()


class SpanType(Enum):
    """Allowed span types on MVP (level 2 types are placeholders, must not be used).
    PROD-ready: AGENT_REASONING added for span-aggregation (PP09).
    """

    AGENT_LOOP = "agent.loop"
    LLM_CALL = "llm.call"
    TOOL_CALL = "tool.call"
    AGENT_REASONING = "agent.reasoning"
    AGENT_REASONING_COMPRESSED = "agent.reasoning.compressed"
    # Level 2 placeholders — not implemented in MVP
    # TOOL_INPUT = "tool.input"
    # TOOL_OUTPUT = "tool.output"
    # LLM_INPUT = "llm.input"
    # LLM_OUTPUT = "llm.output"
    # EVAL_RESULT = "eval.result"
    # AUDIT_EVENT = "audit.event"


class InvalidSpanTypeError(ValueError):
    """Raised when an invalid span_type string is provided."""

    pass


@dataclass
class Event:
    """A timestamped event within a span."""

    name: str
    timestamp: str  # ISO 8601
    attributes: dict = field(default_factory=dict)


@dataclass
class Span:
    """Core telemetry object representing a unit of work within a trace.

    Matches the canonical contract from ARCHITECT.md §7.1.
    """

    name: str
    span_type: SpanType
    context: SpanContext
    attributes: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    start_time: float = 0.0
    end_time: Optional[float] = None
    # Verdicts already produced by the pre-call guardrail hooks, keyed by text
    # attribute name.  Deliberately kept out of ``attributes``/``events`` so it
    # never reaches an exporter — it only lets ``_enqueue`` reuse a check on the
    # same text instead of running detection twice (PC08).
    guardrail_verdicts: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.span_type, str):
            try:
                self.span_type = SpanType(self.span_type)
            except ValueError:
                raise InvalidSpanTypeError(
                    f"Invalid span_type: {self.span_type!r}. "
                    f"Allowed: {[st.value for st in SpanType]}"
                )

    @property
    def duration_ms(self) -> float:
        """Duration of the span in milliseconds."""
        if self.end_time is None:
            return 0.0
        return (self.end_time - self.start_time) * 1000.0

    def set_attribute(self, key: str, value: object) -> None:
        """Set a key-value attribute on the span."""
        self.attributes[key] = value

    def add_event(self, name: str, attrs: Optional[dict] = None) -> None:
        """Append a timestamped event to the span."""
        self.events.append(
            Event(
                name=name,
                timestamp=_to_iso8601(_now()) or "",
                attributes=attrs or {},
            )
        )

    def to_dict(self) -> dict:
        """Serialize to the §7.1 ARCHITECT.md schema."""
        # Deduplicate events by (span_id, event_name, event_ts) for idempotency on retry (PP09)
        deduped_events = self._deduped_events()
        return {
            "trace_id": self.context.trace_id,
            "span_id": self.context.span_id,
            "parent_span_id": self.context.parent_span_id,
            "name": self.name,
            "span_type": self.span_type.value,
            "start_time": _to_iso8601(self.start_time),
            "end_time": _to_iso8601(self.end_time),
            "status": self.attributes.get("status", "unset"),
            "attributes": dict(self.attributes),
            "events": [
                {
                    "name": e.name,
                    "timestamp": e.timestamp,
                    "attributes": dict(e.attributes),
                }
                for e in deduped_events
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Span":
        """Deserialize from dict (reverse of to_dict, PP09 round-trip)."""
        ctx = SpanContext(
            trace_id=data["trace_id"],
            span_id=data["span_id"],
            parent_span_id=data["parent_span_id"],
            agent_id=data.get("agent_id", ""),
            agent_version=data.get("agent_version", ""),
            user_id=data.get("user_id", ""),
            session_id=data.get("session_id", ""),
            tool_name=data.get("tool_name", None),
        )
        span = cls(
            name=data["name"],
            span_type=data["span_type"],
            context=ctx,
            start_time=_parse_iso8601(data["start_time"]),
            end_time=_parse_iso8601(data["end_time"]) if data["end_time"] else None,
            attributes=dict(data["attributes"]),
        )
        # Tolerate both {name, timestamp, attributes} (OTLP) and {name, ts, attrs} (PP0 internal)
        for event_data in data["events"]:
            timestamp = event_data.get("timestamp", event_data.get("ts"))
            attrs = event_data.get("attributes", event_data.get("attrs", {}))
            span.add_event(event_data["name"], attrs)
            # Set correct timestamp after add_event generates current time
            for event in span.events:
                if event.name == event_data["name"] and not event.timestamp.startswith("20"):
                    event.timestamp = timestamp
        return span

    def _deduped_events(self) -> list[Event]:
        """Return events deduplicated by (event_name, event_timestamp) within this span (PP09)."""
        seen = set()
        deduped = []
        for event in self.events:
            key = (event.name, event.timestamp)
            if key not in seen:
                seen.add(key)
                deduped.append(event)
        return deduped


class NullSpan:
    """Zero-overhead no-op span for disabled observability mode."""

    __slots__ = ()

    def set_attribute(self, key: str, value: object) -> None:
        """No-op: does nothing."""
        pass

    def add_event(self, name: str, attrs: Optional[dict] = None) -> None:
        """No-op: does nothing."""
        pass


_obs_user_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_obs_user_id", default=""
)
_obs_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_obs_session_id", default=""
)
_active_span_context: contextvars.ContextVar[Optional[SpanContext]] = (
    contextvars.ContextVar("_active_span_context", default=None)
)


class ObservabilitySDK:
    """SDK entry point for agent observability.

    Instrumentation happens via the ``agent_observed`` decorator, which wraps an
    async agent ``run()`` method into an ``agent.loop`` root span. Completed
    spans are enqueued (a temporary in-memory list on MVP; replaced by the ring
    buffer + export worker in P07-P08).

    Passing a ``guardrail`` engine turns on the CRITICAL security layer (PC08):
    every text attribute of a span is PII-masked before the span reaches the
    ring buffer, and ``llm_call`` / ``tool_call`` refuse to run when the check
    returns ``block``.  The guardrail stays off when no engine is supplied, so
    dev environments with synthetic data are unaffected; a supplied engine can
    still be switched off with ``AGENT_OBS_GUARDRAIL_ENABLED=false`` or
    ``config["guardrail_enabled"] = False``.
    """

    def __init__(self, exporters: list, enabled: bool = None, config: dict | None = None, ring_buffer_maxsize: int = 100_000, metrics_port: int | None = None, guardrail: "GuardrailEngine | None" = None, rule_based_evaluator: Any | None = None, llm_judge: Any | None = None, audit_store: "HotStore | None" = None):
        self._exporters = exporters

        # Priority: explicit enabled parameter > config > environment variable
        if config is not None and "enabled" in config:
            self.enabled = config["enabled"]
        else:
            # Use explicit enabled parameter if provided, otherwise check environment
            if enabled is not None:
                self.enabled = enabled
            else:
                env_enabled = os.environ.get("AGENT_OBS_ENABLED", "true").strip().lower()
                self.enabled = env_enabled in ("true", "1", "yes", "on")

        self.last_spans: list[Span] = []
        self._ring_buffer: asyncio.Queue[Span] = asyncio.Queue(
            maxsize=ring_buffer_maxsize
        )
        self._shutdown: asyncio.Event = asyncio.Event()
        self._last_drop_warning: float = 0.0
        self._worker_task: asyncio.Task | None = None
        self._metrics_http_server = None
        self._trace_agg: dict[str, dict] = {}
        self._max_trace_agg = 100_000

        # PP09: span aggregation configuration
        self._aggregation_enabled = self._load_aggregation_enabled(config)
        self._span_aggregator: "SpanAggregator | None" = None
        if self._aggregation_enabled:
            # Import SpanAggregator at runtime to avoid circular import
            from agent_obs.span_aggregator import SpanAggregator
            self._span_aggregator = SpanAggregator(
                max_count=self._load_aggregation_max_count(config),
                max_window_seconds=self._load_aggregation_window(config),
            )
            logger.info("Span aggregation enabled (PP09)")
        self._aggregation_src_ctx: Optional[SpanContext] = None
        self._aggregation_src_tenant_id: Optional[str] = None
        
        # PP11: span aggregation audit configuration
        self._aggregation_audit_enabled = self._load_aggregation_audit_enabled(config)
        self._audit_store: "HotStore | None" = audit_store
        if self._aggregation_audit_enabled:
            # Lazily initialize audit store on first write if not provided
            if self._audit_store is None:
                try:
                    from agent_obs.storage.hot import HotStore
                    self._audit_store = HotStore()
                    logger.info("Span aggregation audit enabled (PP11), HotStore will be initialized on first write")
                except ImportError:
                    logger.warning("ClickHouse driver not available, span aggregation audit disabled")
                    self._aggregation_audit_enabled = False
                except Exception as e:
                    logger.warning("Failed to initialize HotStore for span aggregation audit: %s", str(e))
                    self._aggregation_audit_enabled = False
        if self._aggregation_audit_enabled:
            logger.info("Span aggregation audit enabled (PP11)")
        
        # Track pending audit tasks for graceful shutdown
        self._audit_tasks: set[asyncio.Task] = set()
        self.cost_threshold = self._load_cost_threshold(config)
        self._guardrail: "GuardrailEngine | None" = None
        # PC24: dedicated ring buffer for response embeddings → ClickHouse.
        # Separate from the span ring buffer so embedding writes never block
        # the main export path.
        self._embedding_queue: asyncio.Queue[
            tuple[str, str, list[float]]
        ] = asyncio.Queue(maxsize=50_000)
        self._embedding_worker_task: asyncio.Task | None = None
        self._embedding_shutdown: asyncio.Event = asyncio.Event()
        # PC29: CPU monitoring and sliding-window error rate tracking.
        self._cpu_monitor_task: asyncio.Task | None = None
        self._cpu_shutdown: asyncio.Event = asyncio.Event()
        self._error_rate_windows: dict[
            str, collections.deque[tuple[float, bool]]
        ] = {}  # agent_id -> deque of (timestamp, is_error)
        if guardrail is not None and self._load_guardrail_enabled(config):
            self._guardrail = guardrail
            logger.info(
                "Guardrail active: PII masking before enqueue, "
                "pre-call injection checks on llm_call/tool_call"
            )
        # PC09/PC12: optional evaluators.  Supplied instances are active
        # unless explicitly disabled by env/config (guardrail pattern); with
        # no instance supplied the SDK stays eval-free unless
        # AGENT_OBS_EVAL_RULES_ENABLED opts into loading configs/eval_rules.yaml.
        self._rule_based_evaluator = self._setup_rule_based_evaluator(
            rule_based_evaluator, config
        )
        self._llm_judge = self._setup_llm_judge(llm_judge, config)
        if self.enabled:
            if self._exporters:
                self._worker_task = asyncio.create_task(self._export_worker())
                atexit.register(self._atexit_handler)
            # PC24: start the embedding storage worker alongside the main
            # export worker.  It drains the embedding queue every 5 seconds
            # and batch-inserts into ClickHouse.  Only started when a running
            # event loop is available (same guard as the export worker).
            try:
                asyncio.get_running_loop()
                self._embedding_worker_task = asyncio.create_task(
                    self._embedding_worker()
                )
                self._cpu_monitor_task = asyncio.create_task(
                    self._cpu_monitor_task_fn()
                )
            except RuntimeError:
                # No running loop — SDK constructed outside async context.
                # The worker will not start; callers using the SDK outside
                # async must use ``async with sdk:`` to start it.
                pass
            if metrics_port is not None:
                self._start_metrics_server(metrics_port)

    def _start_metrics_server(self, port: int) -> None:
        """Start a Prometheus HTTP server on the given port to serve /metrics."""
        from prometheus_client import start_http_server

        self._metrics_http_server = start_http_server(port)
        logger.info("Prometheus metrics server started on port %d", port)

    @staticmethod
    def _load_cost_threshold(config: dict | None) -> float:
        """Read the tail-sampler cost threshold (P20).

        Priority: ``config["cost_threshold"]`` > ``TAIL_SAMPLER_COST_THRESHOLD``
        env var > default ``0.05``.
        """
        if config is not None and "cost_threshold" in config:
            try:
                return float(config["cost_threshold"])
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid config cost_threshold %r, using default",
                    config["cost_threshold"],
                )
        env_threshold = os.environ.get("TAIL_SAMPLER_COST_THRESHOLD", "0.05")
        try:
            return float(env_threshold)
        except (TypeError, ValueError):
            logger.warning(
                "Invalid TAIL_SAMPLER_COST_THRESHOLD, using default 0.05"
            )
            return 0.05

    @staticmethod
    def _load_aggregation_enabled(config: dict | None) -> bool:
        """Read span aggregation enabled flag (PP09).

        Priority: ``config["aggregation_enabled"]`` > ``AGENT_OBS_AGGREGATION_ENABLED``
        env var > default ``true``.
        """
        if config is not None and "aggregation_enabled" in config:
            return bool(config["aggregation_enabled"])
        env_enabled = os.environ.get("AGENT_OBS_AGGREGATION_ENABLED", "true").strip().lower()
        return env_enabled in ("true", "1", "yes", "on")

    def _load_aggregation_audit_enabled(self, config: dict | None) -> bool:
        """Read span aggregation audit enabled flag (PP11).

        Priority: ``config["aggregation_audit_enabled"]`` > ``AGENT_OBS_AGGREGATION_AUDIT``
        env var > default ``false``.
        """
        if config is not None and "aggregation_audit_enabled" in config:
            return bool(config["aggregation_audit_enabled"])
        env_enabled = os.environ.get("AGENT_OBS_AGGREGATION_AUDIT", "false").strip().lower()
        return env_enabled in ("true", "1", "yes", "on")

    @staticmethod
    def _load_aggregation_max_count(config: dict | None) -> int:
        """Read span aggregation max count (PP09).

        Priority: ``config["aggregation"]["max_count"]`` > ``AGENT_OBS_AGGREGATION_MAX_COUNT``
        env var > default ``50``.
        """
        if config is not None and "aggregation" in config and "max_count" in config["aggregation"]:
            try:
                return int(config["aggregation"]["max_count"])
            except (TypeError, ValueError):
                logger.warning("Invalid aggregation max_count, using default 50")
        env_max_count = os.environ.get("AGENT_OBS_AGGREGATION_MAX_COUNT", "50")
        try:
            return int(env_max_count)
        except (TypeError, ValueError):
            logger.warning("Invalid AGENT_OBS_AGGREGATION_MAX_COUNT, using default 50")
            return 50

    @staticmethod
    def _load_aggregation_window(config: dict | None) -> float:
        """Read span aggregation max window in seconds (PP09).

        Priority: ``config["aggregation"]["window_seconds"]`` > ``AGENT_OBS_AGGREGATION_WINDOW_SECONDS``
        env var > default ``1.0``.
        """
        if config is not None and "aggregation" in config and "window_seconds" in config["aggregation"]:
            try:
                return float(config["aggregation"]["window_seconds"])
            except (TypeError, ValueError):
                logger.warning("Invalid aggregation window_seconds, using default 1.0")
        env_window = os.environ.get("AGENT_OBS_AGGREGATION_WINDOW_SECONDS", "1.0")
        try:
            return float(env_window)
        except (TypeError, ValueError):
            logger.warning("Invalid AGENT_OBS_AGGREGATION_WINDOW_SECONDS, using default 1.0")
            return 1.0
        if config is not None and "cost_threshold" in config:
            try:
                return float(config["cost_threshold"])
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid config cost_threshold %r, using default",
                    config["cost_threshold"],
                )
        raw = os.environ.get("TAIL_SAMPLER_COST_THRESHOLD", "0.05")
        try:
            return float(raw)
        except ValueError:
            return 0.05

    @staticmethod
    def _load_guardrail_enabled(config: dict | None) -> bool:
        """Read the guardrail on/off switch (PC08).

        Only consulted when a guardrail engine is actually supplied — without an
        engine there is nothing to enable.  Priority: ``config["guardrail_enabled"]``
        > ``AGENT_OBS_GUARDRAIL_ENABLED`` env var > default ``true``.
        """
        if config is not None and "guardrail_enabled" in config:
            return bool(config["guardrail_enabled"])
        raw = os.environ.get(_GUARDRAIL_ENABLED_ENV, "true")
        return str(raw).strip().lower() in ("true", "1", "yes", "on")

    @staticmethod
    def _eval_flag(
        config: dict | None, config_key: str, env_name: str, default: str
    ) -> bool:
        """Read an eval on/off switch (PC09/PC12).

        Priority: ``config[config_key]`` > ``env_name`` env var > *default*.
        """
        if config is not None and config_key in config:
            return bool(config[config_key])
        raw = os.environ.get(env_name, default)
        return str(raw).strip().lower() in ("true", "1", "yes", "on")

    @classmethod
    def _setup_rule_based_evaluator(
        cls, evaluator: Any | None, config: dict | None
    ) -> Any | None:
        """Resolve the rule-based evaluator (PC09).

        A supplied instance is active unless ``AGENT_OBS_EVAL_RULES_ENABLED``
        disables it (guardrail pattern).  Without a supplied instance the
        SDK stays eval-free unless the env flag opts in — then rules are
        loaded from ``configs/eval_rules.yaml``.  A broken config degrades
        to "no evaluator", never to a crash.
        """
        if evaluator is not None:
            enabled = cls._eval_flag(
                config, "eval_rules_enabled", "AGENT_OBS_EVAL_RULES_ENABLED", "true"
            )
            return evaluator if enabled else None

        enabled = cls._eval_flag(
            config, "eval_rules_enabled", "AGENT_OBS_EVAL_RULES_ENABLED", "false"
        )
        if not enabled:
            return None
        try:
            from agent_obs.eval.rule_based import load_rule_based_evaluator

            loaded = load_rule_based_evaluator()
        except Exception:
            logger.warning(
                "Failed to load eval rules from configs/eval_rules.yaml; "
                "rule-based eval disabled",
                exc_info=True,
            )
            return None
        if loaded is None:
            logger.warning(
                "AGENT_OBS_EVAL_RULES_ENABLED set but no usable rules in "
                "configs/eval_rules.yaml; rule-based eval disabled"
            )
        else:
            logger.info("Rule-based eval active (configs/eval_rules.yaml)")
        return loaded

    @classmethod
    def _setup_llm_judge(cls, judge: Any | None, config: dict | None) -> Any | None:
        """Resolve the LLM judge (PC10/PC12).

        Unlike rule-based, there is no auto-load path: a judge needs a
        client/queue configuration, so it only runs when supplied.  The
        env flag can still disable a supplied instance.
        """
        if judge is None:
            return None
        enabled = cls._eval_flag(
            config, "llm_judge_enabled", "AGENT_OBS_LLM_JUDGE_ENABLED", "true"
        )
        return judge if enabled else None

    def _build_context(
        self,
        agent_id: str,
        version: str = "",
        user_id: str = "",
        session_id: str = "",
    ) -> SpanContext:
        """Create a root SpanContext for an agent loop.

        user_id/session_id fall back to values stored in the current
        contextvars (``_obs_user_id`` / ``_obs_session_id``).
        """
        return SpanContext.new(
            agent_id=agent_id,
            agent_version=version,
            user_id=user_id or _obs_user_id.get(),
            session_id=session_id or _obs_session_id.get(),
        )

    async def _enqueue(self, span: Span) -> None:
        """Mask the span, then enqueue it into the ring buffer for async export.

        Masking happens first so no plaintext PII can reach a container log or a
        backend (anti-pattern §8.3).  Enqueuing is fire-and-forget — never blocks
        the caller.  If the buffer is full, the span is dropped and counted.

        PP09: if span_type is AGENT_REASONING and aggregation is enabled, route
        through SpanAggregator. Aggregated spans are converted to Span and continue
        normally.
        """
        await self._apply_guardrail(span)
        self._apply_rule_based_eval(span)

        # PC24: extract response embedding before the span enters the ring
        # buffer.  Embeddings go to ClickHouse through a separate queue so
        # they never block the main Langfuse/OTLP export path.
        embedding = span.attributes.pop("response_embedding", None)
        if embedding is not None:
            try:
                self._embedding_queue.put_nowait(
                    (
                        span.context.trace_id,
                        span.context.span_id,
                        span.context.agent_id,
                        span.attributes.get("tenant_id", ""),
                        embedding,
                    )
                )
            except asyncio.QueueFull:
                from agent_obs.metrics import embeddings_storage_failed_total

                embeddings_storage_failed_total.inc()
                logger.warning(
                    "Embedding queue full, dropping embedding for span %s",
                    span.context.span_id,
                )

        # PP09: handle span aggregation
        if self._aggregation_enabled and self._span_aggregator is not None:
            if span.span_type == SpanType.AGENT_REASONING:
                # Check if this is not already an aggregated span
                if "aggregation.children_count" not in span.attributes:
                    # Micro-span: buffer it
                    if self._aggregation_src_ctx is None:
                        # First micro-span: track its context for aggregate
                        self._aggregation_src_ctx = span.context
                        # Store tenant_id separately
                        self._aggregation_src_tenant_id = span.attributes.get("tenant_id", "")
                        
                    # Record micro-span input (PP11)
                    spans_micro_input_total.labels(agent_id=span.context.agent_id, tenant_id=span.attributes.get("tenant_id", "")).inc()
                    aggregate = await self._span_aggregator.add(span)
                    if aggregate is not None:
                        # Buffer full: convert to aggregated span and continue
                        span = self._aggregate_to_span(aggregate)
                        # Record aggregation metrics (PP11)
                        self._record_aggregation_metrics(aggregate, span)
                        # Reset aggregation source context after flush
                        self._aggregation_src_ctx = None
                        self._aggregation_src_tenant_id = None
                        # Reset aggregator buffer to continue collecting new spans
                        self._span_aggregator._buffer = []
                        self._span_aggregator._first_span_time = None
                # If it's already an aggregate or no aggregate produced, continue
            elif span.span_type == SpanType.AGENT_LOOP:
                # Root span: flush any remaining micro-spans
                await self._flush_aggregation()

        # PP29: stamp the deploy version before the span enters the buffer.
        # spans_hot has no version column — the canary gate scopes SLI counts by
        # this attribute (JSONExtractString(attributes, 'agent_version')).
        # AGENT_OBS_DEPLOY_VERSION (set by CI on the canary deployment) wins over
        # the version in the span context; an explicitly set attribute is kept.
        _version = os.environ.get(_DEPLOY_VERSION_ENV, "").strip() or span.context.agent_version
        if _version:
            span.attributes.setdefault("agent_version", _version)

        try:
            self._ring_buffer.put_nowait(span)
            self.last_spans.append(span)
            self._accumulate_trace_aggregates(span)
            
            spans_total.labels(agent_id=span.context.agent_id).inc()

            is_error = span.attributes.get("status") == "error"
            self._update_error_rate(span.context.agent_id, is_error)

            if span.span_type == SpanType.LLM_CALL and "cost.usd" in span.attributes:
                agent_id = span.context.agent_id
                model = span.attributes.get("llm.model", "unknown")
                cost_usd = span.attributes["cost.usd"]
                cost_per_request_usd.labels(agent_id=agent_id, model=model).set(
                    cost_usd
                )
                cost_total_usd.labels(agent_id=agent_id, model=model).inc(cost_usd)
        except asyncio.QueueFull:
            from agent_obs.metrics import dropped_spans_total

            dropped_spans_total.labels(agent_id=span.context.agent_id).inc()
            now = _now()
            if now - self._last_drop_warning >= 5.0:
                self._last_drop_warning = now
                logger.warning(
                    "Ring buffer full, dropping span %s (trace=%s)",
                    span.context.span_id,
                    span.context.trace_id,
                )

    def _aggregate_to_span(self, aggregate: SpanAggregate) -> Span:
        """Convert SpanAggregate to Span (PP09 wiring)."""
        if self._aggregation_src_ctx is None:
            # Fallback: use current span's context (should not happen with proper flush)
            src_ctx = SpanContext.new(agent_id="unknown")
        else:
            src_ctx = self._aggregation_src_ctx

        # Build new context for aggregate
        ctx = SpanContext(
            trace_id=src_ctx.trace_id,
            span_id=_new_ulid(),
            parent_span_id=aggregate.parent_span_id,
            agent_id=src_ctx.agent_id,
            agent_version=src_ctx.agent_version,
            user_id=src_ctx.user_id,
            session_id=src_ctx.session_id,
            tool_name=src_ctx.tool_name,
        )
        
        # Preserve tenant_id from the original micro-spans
        # Use the stored tenant_id if available
        tenant_id = self._aggregation_src_tenant_id if self._aggregation_src_tenant_id else ""
        # Fallback: get tenant_id from the first micro-span in the aggregate
        if not tenant_id and aggregate.events:
            first_event_attrs = aggregate.events[0].get('attrs', {})
            tenant_id = first_event_attrs.get('tenant_id', '')
        

        # Build events with sorted timestamps and ISO format
        events = []
        for event_data in sorted(aggregate.events, key=lambda e: e["ts"]):
            events.append(
                Event(
                    name=event_data["name"],
                    timestamp=_to_iso8601(event_data["ts"]),
                    attributes=dict(event_data["attrs"]),
                )
            )

        return Span(
            name=f"agent.reasoning:aggregated:{aggregate.children_count}",
            span_type=SpanType.AGENT_REASONING,
            context=ctx,
            start_time=aggregate.events[0]["ts"] if aggregate.events else _now(),
            end_time=_now(),
            attributes={
                "status": "ok",
                "aggregation.children_count": aggregate.children_count,
                "aggregation.bytes_saved": aggregate.bytes_saved,
                "aggregation.source": "span_aggregator",
                "tenant_id": tenant_id,
            },
            events=events,
        )

    async def _flush_aggregation(self) -> None:
        """Flush any buffered micro-spans to aggregate (PP09)."""
        if self._span_aggregator is None:
            return
        aggregate = await self._span_aggregator.flush()
        if aggregate is not None:
            span = self._aggregate_to_span(aggregate)
            # Record aggregation metrics (PP11)
            self._record_aggregation_metrics(aggregate, span)
            # Enqueue the aggregated span (same path as regular spans)
            try:
                self._ring_buffer.put_nowait(span)
                self.last_spans.append(span)
                self._accumulate_trace_aggregates(span)
                
                spans_total.labels(agent_id=span.context.agent_id).inc()
            except asyncio.QueueFull:
                from agent_obs.metrics import dropped_spans_total

                dropped_spans_total.labels(agent_id=span.context.agent_id).inc()
                logger.warning(
                    "Ring buffer full, dropping aggregated span %s (trace=%s)",
                    span.context.span_id,
                    span.context.trace_id,
                )
        # Reset aggregation source context
        self._aggregation_src_ctx = None
        self._aggregation_src_tenant_id = None

    def _record_aggregation_metrics(self, aggregate, span):
        """Record aggregation metrics and audit event (PP11).
        
        Args:
            aggregate: SpanAggregate with children_count, bytes_saved
            span: resulting aggregated span (for agent_id, tenant_id)
        """
        
        agent_id = span.context.agent_id
        # Use the tenant_id from the aggregated span attributes
        tenant_id = span.attributes.get("tenant_id", "")
        
        span_type = "agent.reasoning"

        # Use get_pp11_metrics to ensure we're using the same metric instances
        metrics = get_pp11_metrics()
        aggregated_metric = metrics['spans_aggregated_total'].labels(agent_id=agent_id, tenant_id=tenant_id, span_type=span_type)
        micro_metric = metrics['spans_micro_input_total'].labels(agent_id=agent_id, tenant_id=tenant_id)
        bytes_saved_metric = metrics['bytes_saved_total'].labels(agent_id=agent_id, tenant_id=tenant_id)
        
        # Increment counters
        aggregated_metric.inc()
        bytes_saved_metric.inc(aggregate.bytes_saved)

        # Update ratio gauge
        aggregated_value = aggregated_metric._value.get()
        micro_value = micro_metric._value.get()
        if micro_value > 0:
            metrics['aggregation_ratio'].labels(agent_id=agent_id).set(aggregated_value / micro_value)

        # Log audit event (never raises)
        try:
            from agent_obs.guardrail.audit import AggregationFlushAuditEvent
            
            event = AggregationFlushAuditEvent.for_aggregation_flush(
                children_count=aggregate.children_count,
                bytes_saved=aggregate.bytes_saved,
                span_type=span_type,
                agent_id=agent_id,
                trace_id=span.context.trace_id,
            )
            
            # Log structured audit record (always)
            logger.info(
                "Aggregation flushed: audit_id=%s children_count=%d bytes_saved=%d agent_id=%s trace_id=%s",
                event.audit_id,
                aggregate.children_count,
                aggregate.bytes_saved,
                agent_id,
                span.context.trace_id,
            )
            
            # Write audit event to HotStore if enabled (PP11)
            if self._aggregation_audit_enabled and self._audit_store is not None:
                try:
                    # Schedule audit write in background thread (never blocks)
                    task = asyncio.create_task(
                        asyncio.to_thread(self._write_aggregation_audit, event)
                    )
                    self._audit_tasks.add(task)
                    task.add_done_callback(self._audit_tasks.discard)
                except Exception as e:
                    logger.error("Failed to schedule aggregation audit write: %s", str(e))
            
        except Exception as e:
            logger.error("Failed to create aggregation audit event: %s", str(e))
            # Never raises - continue with metrics

    def _write_aggregation_audit(self, event: "AggregationFlushAuditEvent") -> None:
        """Write aggregation audit event to ClickHouse (PP11).
        
        Runs in a background thread to never block the async loop.
        """
        try:
            self._audit_store.write_audit_event(event)
            logger.debug(
                "Aggregation audit written: audit_id=%s action=%s children_count=%d",
                event.audit_id,
                event.action,
                event.resource.get("children_count", 0),
            )
        except Exception as e:
            logger.error("Failed to write aggregation audit event: %s", str(e))
            # Never raises - audit is best-effort

    async def _wait_for_audit_tasks(self, timeout: float = 5.0) -> None:
        """Wait for pending audit tasks to complete during shutdown."""
        if self._audit_tasks:
            logger.info("Waiting for %d pending audit tasks to complete...", len(self._audit_tasks))
            await asyncio.wait(self._audit_tasks, timeout=timeout)

    @asynccontextmanager
    async def reasoning(
        self,
        ctx: SpanContext,
        *,
        name: str = "reasoning",
        attributes: dict | None = None,
    ) -> AsyncIterator[Span]:
        """Create a child ``agent.reasoning`` span for a reasoning step (PP09).

        The span inherits ``trace_id`` from *ctx* and gets its own ``span_id``
        with ``parent_span_id`` set to the active span.  On exit the span is
        enqueued via the aggregator (if enabled) or directly.
        """
        if not self.enabled:
            # Zero-overhead mode: yield a no-op NullSpan
            yield NullSpan()
            return

        parent_ctx = _active_span_context.get()
        child_ctx = SpanContext.new(
            agent_id=ctx.agent_id,
            agent_version=ctx.agent_version,
            parent=parent_ctx if parent_ctx is not None else ctx,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
        )
        span = Span(
            name=f"agent.reasoning:{name}",
            span_type=SpanType.AGENT_REASONING,
            context=child_ctx,
            attributes=attributes or {},
        )
        span.start_time = _now()
        token = _active_span_context.set(child_ctx)
        try:
            yield span
        finally:
            span.end_time = _now()
            _active_span_context.reset(token)
            await self._enqueue(span)

    async def _apply_guardrail(self, span: Span) -> None:
        """Mask every text attribute of a span before it is exported (PC08).

        This is the *last* barrier, not the primary one: the hooks in
        :meth:`llm_call` / :meth:`tool_call` already refuse to issue a guarded
        request when the check returns ``block``.  This pass exists for text that
        reached a span without passing through those hooks — a tool output set
        directly by the caller, or a span built by hand.

        Verdicts already computed by a pre-call hook for the same attribute are
        reused rather than re-detected, so the hot path runs one check per text.
        Masking is fail-closed: if the guardrail raises, the text is replaced by
        a redaction placeholder instead of being exported verbatim.
        """
        guardrail = self._guardrail
        if guardrail is None:
            return

        from agent_obs.metrics import guardrail_check_in_enqueue_total

        redacted_fields: list[str] = []
        for attribute, field_name, stage in _GUARDRAIL_TEXT_FIELDS:
            text = span.attributes.get(attribute)
            if not isinstance(text, str) or not text:
                continue

            cached = span.guardrail_verdicts.get(attribute)
            reused = cached is not None and cached.text == text
            if reused:
                verdict = cached.verdict
            else:
                try:
                    verdict = await guardrail.check_input(
                        text, context=span.context, field=field_name
                    )
                except Exception:
                    logger.warning(
                        "Guardrail check failed on attribute %s of span %s; "
                        "text redacted",
                        attribute,
                        span.context.span_id,
                        exc_info=True,
                    )
                    span.attributes[attribute] = _REDACTED_ON_GUARDRAIL_FAILURE
                    continue

            span.attributes[attribute] = verdict.masked_text
            redacted_fields.extend(verdict.redacted_fields)
            span.add_event(
                "guardrail.check",
                {
                    "audit_event_id": verdict.audit_event_id,
                    "field": field_name,
                    "verdict": verdict.verdict,
                    "score": verdict.score,
                    "ts": _now(),
                },
            )
            if verdict.verdict == "block":
                # The span is still recorded for post-incident debugging.  A
                # reused verdict was already counted by the hook that raised.
                span.attributes["guardrail.block"] = True
                if not reused:
                    from agent_obs.metrics import guardrail_block_total

                    guardrail_block_total.labels(stage=stage).inc()
            guardrail_check_in_enqueue_total.inc()

        if redacted_fields:
            span.attributes["pii.redacted_fields"] = redacted_fields
            span.attributes["pii.total_entities"] = len(redacted_fields)
            entity_types = {f.split(".")[1] for f in redacted_fields}
            span.attributes["pii.entity_types"] = sorted(entity_types)

    def _apply_rule_based_eval(self, span: Span) -> None:
        """Run the rule-based evaluator on a span before export (PC09).

        Runs after guardrail masking, so ``no_pii_in_output`` only flags PII
        the guardrail missed — exactly the critical signal PC09 wants.  The
        result is stored as a plain dict (``rules_passed`` / ``rules_failed``
        / ``flags``), the shape ``otlp_mapping.enrich_eval_attributes``
        consumes.

        Fail-open: an evaluator error is logged and the span is exported
        without the attribute — observability never blocks business logic.
        """
        evaluator = self._rule_based_evaluator
        if evaluator is None:
            return
        try:
            result = evaluator.evaluate(span, [])
            span.attributes["eval.rule_based"] = {
                "rules_passed": int(result.scores.get("rules_passed", 0)),
                "rules_failed": int(result.scores.get("rules_failed", 0)),
                "flags": list(result.flags),
            }
        except Exception:
            logger.warning(
                "Rule-based eval failed for span %s; exporting without "
                "eval.rule_based",
                span.context.span_id,
                exc_info=True,
            )

    async def _guard_call(
        self,
        span: Span,
        text: str | None,
        *,
        attribute: str,
        field_name: str,
        stage: str,
    ) -> None:
        """Check a call input before the guarded request is issued (PC08).

        Runs inside ``llm_call`` / ``tool_call`` before the body is entered, so
        a ``block`` verdict prevents the LLM or tool request from ever leaving
        the process.  The verdict is cached on the span so that :meth:`_enqueue`
        can apply its masking without detecting the same text twice.
        """
        if self._guardrail is None or not text:
            return

        verdict = await self._guardrail.check_input(
            text, context=span.context, field=field_name
        )
        span.guardrail_verdicts[attribute] = _GuardrailCheck(text, verdict)
        if verdict.verdict != "block":
            return

        from agent_obs.guardrail.engine import GuardrailBlockException
        from agent_obs.metrics import guardrail_block_total

        span.attributes["status"] = "blocked"
        span.attributes["guardrail.block"] = True
        guardrail_block_total.labels(stage=stage).inc()
        logger.warning(
            "Guardrail blocked %s call for trace %s (score=%.2f, audit=%s)",
            field_name,
            span.context.trace_id,
            verdict.score,
            verdict.audit_event_id,
        )
        raise GuardrailBlockException(verdict, stage=stage)

    def _accumulate_trace_aggregates(self, span: Span) -> None:
        """Aggregate per-trace data onto the root span (P20/P24).

        Children (``llm.call`` / ``tool.call``) are enqueued when their context
        managers exit — before the ``agent.loop`` root, which is enqueued in
        the decorator's ``finally``.  So when the root arrives we can stamp it
        with ``steps.count``, ``cost.usd_sum``, ``cost.over_budget`` and the
        error flag for the tail sampler and the Langfuse saved queries.
        """
        trace_id = span.context.trace_id
        agg = self._trace_agg.get(trace_id)
        if agg is None:
            if len(self._trace_agg) >= self._max_trace_agg:
                # Memory guard: stop aggregating, never grow unboundedly.
                return
            agg = {"cost_usd": 0.0, "steps": 0, "has_error": False}
            self._trace_agg[trace_id] = agg

        agg["steps"] += 1
        if span.attributes.get("status") == "error":
            agg["has_error"] = True
        if span.span_type == SpanType.LLM_CALL:
            cost = span.attributes.get("cost.usd")
            if isinstance(cost, (int, float)):
                agg["cost_usd"] += cost

        if span.span_type != SpanType.AGENT_LOOP:
            return

        span.attributes["steps.count"] = agg["steps"]
        span.attributes["cost.usd_sum"] = round(agg["cost_usd"], 8)
        span.attributes["cost.over_budget"] = (
            "true" if agg["cost_usd"] > self.cost_threshold else "false"
        )
        # MVP stub: the security layer arrives in Level 2 (E2.1), but the
        # tail sampler already knows how to check the field.
        span.attributes["security.incident"] = "false"
        if agg["has_error"]:
            span.attributes["status"] = "error"
        self._trace_agg.pop(trace_id, None)

    async def _export_worker(self) -> None:
        """Background worker that drains the ring buffer and exports spans.

        Batches spans with a 50ms drain window and max batch size of 512.
        Fan-outs to all registered exporters via asyncio.gather.

        When ``_shutdown`` is signalled the worker finishes the current batch
        then continues draining any remaining spans until the queue is empty
        before exiting.
        """
        while True:
            batch: list[Span] = []
            try:
                first = await asyncio.wait_for(
                    self._ring_buffer.get(), timeout=0.5
                )
                batch.append(first)
            except asyncio.TimeoutError:
                if self._shutdown.is_set() and self._ring_buffer.empty():
                    break
                continue

            deadline = _now() + 0.05  # 50ms drain window
            while _now() < deadline and len(batch) < 512:
                try:
                    batch.append(self._ring_buffer.get_nowait())
                except asyncio.QueueEmpty:
                    break

            try:
                await asyncio.gather(
                    *[exp.export(batch) for exp in self._exporters],
                    return_exceptions=True,
                )
            except Exception:
                logger.exception("Export worker encountered an unexpected error")

            if self._shutdown.is_set() and self._ring_buffer.empty():
                break

    # --- PC24: embedding storage worker ------------------------------------

    async def _embedding_worker(self) -> None:
        """Background worker that drains the embedding queue and batch-inserts
        into ClickHouse ``span_embeddings``.

        Batches are flushed every 5 seconds or when the queue reaches 256
        entries, whichever comes first.  This keeps the main export path
        completely unblocked.
        """
        batch: list[tuple[str, str, str, str, list[float]]] = []
        deadline: float = 0.0

        while True:
            try:
                first = await asyncio.wait_for(
                    self._embedding_queue.get(), timeout=0.5
                )
                batch.append(first)
                deadline = _now() + 5.0  # 5-second flush window
            except asyncio.TimeoutError:
                if self._embedding_shutdown.is_set() and self._embedding_queue.empty():
                    break
                # If we have a batch and the deadline expired, flush it.
                if batch and _now() >= deadline:
                    await self._flush_embedding_batch(batch)
                    batch = []
                    deadline = 0.0
                continue

            # Drain up to 256 entries within the 5-second window.
            while _now() < deadline and len(batch) < 256:
                try:
                    batch.append(self._embedding_queue.get_nowait())
                except asyncio.QueueEmpty:
                    break

            await self._flush_embedding_batch(batch)
            batch = []
            deadline = 0.0

            if self._embedding_shutdown.is_set() and self._embedding_queue.empty():
                break

    # --- PC29: CPU monitor and error rate tracking -------------------------

    async def _cpu_monitor_task_fn(self) -> None:
        """Background task that samples system CPU every 10 seconds.

        Writes to the ``agent_obs_system_cpu_ratio`` Prometheus gauge so that
        the OTel Collector (or Prometheus) can pick it up for adaptive
        tail-sampling decisions (PC30).
        """
        from agent_obs.metrics import system_cpu_ratio

        try:
            import psutil
        except ImportError:
            logger.warning(
                "psutil not installed; system_cpu_ratio metric will not be updated"
            )
            return

        # Prime psutil (first call always returns 0.0)
        psutil.cpu_percent(interval=None)

        while True:
            try:
                await asyncio.wait_for(self._cpu_shutdown.wait(), timeout=10.0)
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass
            try:
                ratio = psutil.cpu_percent(interval=None) / 100.0
                system_cpu_ratio.set(ratio)
            except Exception:
                logger.debug("Failed to read CPU percent", exc_info=True)

    def _update_error_rate(self, agent_id: str, is_error: bool) -> None:
        """Record a span event and recompute the 5-minute error rate gauge.

        Called from ``_enqueue`` for every span.  Old entries (>5 min) are
        evicted before recomputation so the gauge always reflects a sliding
        window.  Each agent_id has its own independent window.
        """
        now = _now()
        window = self._error_rate_windows.setdefault(agent_id, collections.deque())
        window.append((now, is_error))

        cutoff = now - 300.0  # 5 minutes
        while window and window[0][0] < cutoff:
            window.popleft()

        total = len(window)
        if total == 0:
            return
        errors = sum(1 for _, err in window if err)
        from agent_obs.metrics import agent_error_rate_5m

        agent_error_rate_5m.labels(agent_id=agent_id).set(errors / total)

    async def _flush_embedding_batch(
        self, batch: list[tuple[str, str, str, str, list[float]]]
    ) -> None:
        """Persist a batch of (trace_id, span_id, agent_id, tenant_id, embedding) tuples.

        Writes to ClickHouse span_embeddings first, then falls back to Redis if
        ClickHouse is unavailable (PC24). Only successful ClickHouse writes
        increment embeddings_stored_total; failures increment embeddings_storage_failed_total.
        Redis fallback increments embeddings_redis_fallback_total.
        """
        from agent_obs.metrics import (
            embeddings_batch_size,
            embeddings_stored_total,
            embeddings_storage_failed_total,
            embeddings_redis_fallback_total,
        )

        if not batch:
            return

        embeddings_batch_size.observe(len(batch))
        
        try:
            # Try ClickHouse first
            await self._embedding_clickhouse_write(batch)
            embeddings_stored_total.inc(len(batch))
        except Exception as e:
            embeddings_storage_failed_total.inc(len(batch))
            logger.warning("ClickHouse write failed, falling back to Redis: %s", e)
            try:
                await self._embedding_redis_fallback(batch)
                embeddings_redis_fallback_total.inc(len(batch))
            except Exception:
                logger.warning(
                    "Redis fallback for %d embeddings also failed; data lost",
                    len(batch),
                    exc_info=True,
                )

    async def _embedding_clickhouse_write(
        self, batch: list[tuple[str, str, str, str, list[float]]]
    ) -> None:
        """Write embeddings to ClickHouse span_embeddings table.
        
        In sharded setup, writes to Distributed table which routes to appropriate shard.
        Uses asyncio.to_thread to avoid blocking the event loop with sync clickhouse_driver.
        """
        from agent_obs.storage.hot import HotStore
        
        hot_store = HotStore()
        client = hot_store._get_client()
        
        rows = []
        for trace_id, span_id, agent_id, tenant_id, embedding in batch:
            rows.append((
                trace_id,
                span_id,
                agent_id,
                tenant_id,
                "text-embedding-3-small",  # default model
                len(embedding),
                embedding,
            ))
        
        # Use to_thread to avoid blocking the event loop with sync client.execute
        await asyncio.to_thread(
            client.execute,
            """
            INSERT INTO span_embeddings
            (trace_id, span_id, agent_id, tenant_id, model, dims, embedding)
            VALUES
            """,
            rows,
        )

    async def _embedding_redis_fallback(
        self, batch: list[tuple[str, str, str, str, list[float]]]
    ) -> None:
        """Store embeddings in Redis with TTL 1 hour when ClickHouse is down (PC24).
        
        Stores as JSON with key format: embedding:{trace_id}:{span_id}
        Used only as degraded fallback when ClickHouse is unavailable.
        """
        try:
            import json

            import redis.asyncio as aioredis

            redis_url = os.environ.get("AGENT_OBS_REDIS_URL", "redis://localhost:6379")
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
            pipe = redis_client.pipeline()
            for trace_id, span_id, agent_id, tenant_id, embedding in batch:
                key = f"embedding:{trace_id}:{span_id}"
                pipe.set(key, json.dumps(embedding), ex=3600)  # TTL 1 hour
            await pipe.execute()
            await redis_client.aclose()
            logger.info(
                "Stored %d embeddings in Redis fallback", len(batch)
            )
        except Exception:
            logger.warning(
                "Redis fallback for %d embeddings also failed; data lost",
                len(batch),
                exc_info=True,
            )

    async def shutdown(self) -> None:
        """Gracefully shut down: drain buffer, flush exporters, cancel worker.

        The entire procedure is bounded by a 5-second deadline.  If the deadline
        is exceeded the worker is cancelled and a warning is logged with the
        count of undelivered spans.
        """
        # PP09: flush any remaining buffered micro-spans before shutdown
        await self._flush_aggregation()

        self._shutdown.set()

        # PC24: drain the embedding worker first so pending embeddings are
        # flushed to ClickHouse before we exit.
        self._embedding_shutdown.set()
        if self._embedding_worker_task is not None:
            try:
                await asyncio.wait_for(self._embedding_worker_task, timeout=5.0)
            except asyncio.TimeoutError:
                remaining = self._embedding_queue.qsize()
                logger.warning(
                    "Embedding worker shutdown timed out after 5s, "
                    "cancelling (%d embeddings undelivered)",
                    remaining,
                )
                self._embedding_worker_task.cancel()
                try:
                    await self._embedding_worker_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            self._embedding_worker_task = None

        # PC29: stop the CPU monitor task.
        self._cpu_shutdown.set()
        if self._cpu_monitor_task is not None:
            try:
                await asyncio.wait_for(self._cpu_monitor_task, timeout=5.0)
            except asyncio.TimeoutError:
                self._cpu_monitor_task.cancel()
                try:
                    await self._cpu_monitor_task
                except asyncio.CancelledError:
                    pass
            except asyncio.CancelledError:
                pass
            self._cpu_monitor_task = None

        # PP11: wait for pending audit tasks to complete
        await self._wait_for_audit_tasks(timeout=5.0)

        if self._worker_task is None:
            return

        try:
            await asyncio.wait_for(self._worker_task, timeout=5.0)
        except asyncio.TimeoutError:
            remaining = self._ring_buffer.qsize()
            logger.warning(
                "Shutdown timed out after 5s, cancelling worker (%d spans undelivered)",
                remaining,
            )
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        except asyncio.CancelledError:
            pass

        self._worker_task = None

        # Flush all exporters (best-effort within remaining time, max 3s)
        for exporter in self._exporters:
            try:
                await asyncio.wait_for(exporter.flush(), timeout=3.0)
            except (asyncio.TimeoutError, Exception):
                logger.warning("Exporter %s flush failed or timed out", type(exporter).__name__)

    async def __aenter__(self) -> "ObservabilitySDK":
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.shutdown()

    def agent_observed(self, agent_id: str, version: str = ""):
        """Decorate an async agent ``run()`` method into an ``agent.loop`` root span.

        On success the span gets ``status=ok``; on exception the span gets
        ``status=error`` and ``error.type``, and the exception is re-raised.
        The wrapped function receives the trace context as the ``_obs_ctx`` kwarg.
        """

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            if not self.enabled:
                # Zero-overhead mode: return a decorator that returns the function unchanged
                @functools.wraps(fn)
                def wrapper(*args: Any, **kwargs: Any) -> Any:
                    return fn(*args, **kwargs)
                return wrapper
            
            @functools.wraps(fn)
            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                ctx = self._build_context(agent_id=agent_id, version=version)
                span = Span(
                    name=f"agent.loop:{agent_id}",
                    span_type=SpanType.AGENT_LOOP,
                    context=ctx,
                )
                span.start_time = _now()
                token = _active_span_context.set(ctx)
                try:
                    result = await fn(*args, _obs_ctx=ctx, **kwargs)
                    span.attributes["status"] = "ok"
                    return result
                except Exception as e:
                    span.attributes["status"] = "error"
                    span.attributes["error.type"] = type(e).__name__
                    raise
                finally:
                    span.end_time = _now()
                    _active_span_context.reset(token)
                    await self._apply_llm_judge_pending(span)
                    await self._enqueue(span)

            return wrapper

        return decorator

    async def _apply_llm_judge_pending(self, span: Span) -> None:
        """Queue an LLM-judge job and stamp the root span (PC10/PC12).

        The judge only runs when one was supplied to the SDK.  ``evaluate``
        enqueues the job in RQ (no network call to the judge model) and
        returns a pending placeholder — the real result arrives via late
        annotation keyed by trace_id, so the closed span itself is never
        modified afterwards.  Until then the span carries
        ``eval.pending=True`` and ``eval.llm_judge.status`` so the
        trace-viewer can render a PENDING badge (PC13).

        Fail-open: any judge/queue error is logged; the span is exported
        without eval attributes.
        """
        judge = self._llm_judge
        if judge is None:
            return
        try:
            # Children are already enqueued (and in last_spans) at this
            # point; the root span itself is not yet, so add it explicitly
            # to give the judge the full trace context.
            trace_id = span.context.trace_id
            full_trace = [
                s for s in self.last_spans if s.context.trace_id == trace_id
            ]
            full_trace.append(span)
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None, judge.evaluate, span, full_trace
            )
            if getattr(result, "eval_name", "") == "llm_judge_pending":
                span.attributes["eval.pending"] = True
                span.attributes["eval.llm_judge.status"] = "pending"
            elif getattr(result, "eval_name", "") == "llm_judge_skipped":
                span.attributes["eval.llm_judge.status"] = "skipped"
        except Exception:
            logger.warning(
                "LLM judge enqueue failed for trace %s; exporting without "
                "eval.pending",
                span.context.trace_id,
                exc_info=True,
            )

    def _atexit_handler(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            # No running loop — we can create one for sync scripts
            asyncio.run(self.shutdown())
        else:
            logger.warning(
                "Event loop is running at exit; async shutdown not possible. "
                "Use 'async with sdk:' or call sdk.shutdown() explicitly."
            )

    def _atexit_handler(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is None or loop.is_closed():
            # No running loop — we can create one for sync scripts
            asyncio.run(self.shutdown())
        else:
            logger.warning(
                "Event loop is running at exit; async shutdown not possible. "
                "Use 'async with sdk:' or call sdk.shutdown() explicitly."
            )

    @asynccontextmanager
    async def llm_call(
        self,
        ctx: SpanContext,
        *,
        model: str,
        provider: str,
        price_book: PriceBook | None = None,
        usage: Usage | None = None,
        input_text: str | None = None,
        output_text: str | None = None,
    ) -> AsyncIterator[Span]:
        """Create a child ``llm.call`` span for an LLM invocation.

        The span inherits ``trace_id`` from *ctx* and gets its own ``span_id``
        with ``parent_span_id`` set to the active span.  On exit the span is
        enqueued regardless of exceptions.

        When both *usage* and *price_book* are provided, token and cost
        attributes are auto-filled on exit (SDK sugar, P15):
        ``tokens.input``, ``tokens.output``, ``tokens.cached``,
        ``cost.usd`` and ``cost.price_book_version``.

        When *input_text* and/or *output_text* are provided, deterministic
        content sampling (P22) decides whether the full text is stored or
        replaced by ``sha256`` + character counts.

        When the guardrail is active, *input_text* is checked before the LLM is
        called: a ``block`` verdict raises ``GuardrailBlockException`` so no
        request is sent, and the span is still recorded with ``status=blocked``
        (PC08).
        """
        if not self.enabled:
            # Zero-overhead mode: yield a no-op NullSpan
            yield NullSpan()
            return

        parent_ctx = _active_span_context.get()
        child_ctx = SpanContext.new(
            agent_id=ctx.agent_id,
            agent_version=ctx.agent_version,
            parent=parent_ctx if parent_ctx is not None else ctx,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
        )
        span = Span(
            name=f"llm.call:{model}",
            span_type=SpanType.LLM_CALL,
            context=child_ctx,
            attributes={"llm.model": model, "llm.provider": provider},
        )
        span.start_time = _now()
        token = _active_span_context.set(child_ctx)
        try:
            await self._guard_call(
                span,
                input_text,
                attribute="llm.input_text",
                field_name="user_message",
                stage="input",
            )
            yield span
        finally:
            span.end_time = _now()
            _active_span_context.reset(token)
            self._attach_cost_attributes(span, model, usage, price_book)
            self._attach_llm_content(span, input_text, output_text)
            await self._enqueue(span)

    @staticmethod
    def _attach_llm_content(
        span: Span,
        input_text: str | None,
        output_text: str | None,
    ) -> None:
        """Content-sample an llm.call span (P22)."""
        if input_text is None and output_text is None:
            return
        from agent_obs.content_sampling import attach_llm_content

        attach_llm_content(
            span, input_text=input_text, output_text=output_text
        )

    @staticmethod
    def _attach_cost_attributes(
        span: Span,
        model: str,
        usage: Usage | None,
        price_book: PriceBook | None,
    ) -> None:
        """Attach token and cost attributes to a span (P15).

        If *usage* is provided, token counts are always recorded.  Cost
        attributes additionally require *price_book*: a missing model raises
        ``PriceNotFoundError`` which is swallowed with a warning so that
        observability never blocks the agent.
        """
        if usage is None:
            return
        span.attributes["tokens.input"] = usage.input
        span.attributes["tokens.output"] = usage.output
        span.attributes["tokens.cached"] = usage.cached
        if price_book is None:
            return
        try:
            result = compute_cost(usage, model, price_book)
        except PriceNotFoundError:
            logger.warning(
                "No price for model %r in price book %s; cost attributes skipped",
                model,
                price_book.version,
            )
            return
        span.attributes["cost.usd"] = round(result.cost_usd, 8)
        span.attributes["cost.price_book_version"] = result.price_book_version

    @asynccontextmanager
    async def tool_call(self, ctx: SpanContext, *, tool_name: str, input_text: str | None = None) -> AsyncIterator[Span]:
        """Create a child ``tool.call`` span for an instrumented tool.

        The span inherits ``trace_id`` from *ctx* and gets its own ``span_id``
        with ``parent_span_id`` set to the active span.  On exit the span is
        enqueued regardless of exceptions.

        When *input_text* is provided, only a summary (≤200 chars), its hash
        and length are stored — the full tool input never reaches the backend
        (P22, risk R1.5).

        When the guardrail is active, *input_text* is checked before the tool
        runs: a ``block`` verdict raises ``GuardrailBlockException`` so the tool
        is never invoked, and the span is still recorded with ``status=blocked``
        (PC08).
        """
        if not self.enabled:
            # Zero-overhead mode: yield a no-op NullSpan
            yield NullSpan()
            return
            
        parent_ctx = _active_span_context.get()
        child_ctx = SpanContext.new(
            agent_id=ctx.agent_id,
            agent_version=ctx.agent_version,
            parent=parent_ctx if parent_ctx is not None else ctx,
            user_id=ctx.user_id,
            session_id=ctx.session_id,
            tool_name=tool_name,
        )
        span = Span(
            name=f"tool.call:{tool_name}",
            span_type=SpanType.TOOL_CALL,
            context=child_ctx,
            attributes={"tool.name": tool_name},
        )
        span.start_time = _now()
        token = _active_span_context.set(child_ctx)
        try:
            await self._guard_call(
                span,
                input_text,
                attribute="tool.input_summary",
                field_name="tool_input_summary",
                stage="tool",
            )
            yield span
        finally:
            span.end_time = _now()
            _active_span_context.reset(token)
            if input_text is not None:
                from agent_obs.content_sampling import attach_tool_content

                attach_tool_content(span, input_text=input_text)
            await self._enqueue(span)
